"""The brain and a collector, both real, talking over 127.0.0.1 — and a Facebook that is a script.

What is real: the brain's ``BrainApp`` (its ingest server on a local port, a ``fb.sqlite`` in a
temporary directory, the matcher, delivery, the bot's router) and the collector's runtime as
``runner.build`` wires it (store, scheduler, gates, ladder, control, reader, parser, detectors,
uplink over a real socket). What is fake: Telegram (ddiver-core's ``FakeBotClient`` records what
would have been sent), the home network and the power source (``gate_fakes``), and Facebook —
``Site``, a driver that answers each allow-listed URL with a page built from ``reader_support``.

One clock drives both sides: the collector's ``FakeClock``, which the brain reads through a
``datetime`` adapter. Nothing runs concurrently: a test moves time and calls one step at a time
(``World.turn`` is one turn of the read loop, ``World.uplink.cycle`` one round of the uplink), so
every outcome is deterministic.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import socket
import sqlite3
import zoneinfo
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ddiver_core.bot.router import Action, Incoming
from ddiver_core.storage import Role
from ddiver_core_testing import FakeBotClient, SentMessage
from ddiver_fb.runtime import BrainApp
from fbcollector import paths
from fbcollector.alerts import AlertResult
from fbcollector.clock import FakeClock
from fbcollector.detectors import FEED_URL, JOINS_URL
from fbcollector.driver.launch import ChromeVersion, LaunchInfo, SessionMode
from fbcollector.gates import KV_HOME_PIN, KV_LOGIN_AT
from fbcollector.model import CollectorState, Step
from fbcollector.rhythm import KV_SEED
from fbcollector.runner import GATE_ORDER, KV_CMDLINE, Ports, Runtime, build
from fbcollector.scheduler import ActionKind, Grant, Hold
from fbcollector.status import note_state
from fbcollector.store import Store
from fbcollector.uplink import Uplink
from tests.brain.helpers import FakeTelethon, settings_for
from tests.collector.gate_fakes import HOME_PIN, FakeLookup, FakeProbes
from tests.collector.pacing_support import ZONE, make_settings
from tests.collector.reader_support import (
    FakeDriver,
    feed_body,
    feed_document,
    group_body,
    group_document,
    joins_body,
    joins_document,
    nav,
    story,
)
from tests.collector.test_runner import SEED, daytime

TOKEN = "c" * 40
OWNER, MOD = 111, 222
G1, G2 = "400000000000001", "400000000000002"
CMDLINE = "chrome --no-proxy-server about:blank"
DAY = 86400.0
BUSY_MS = 100
"""The brain's SQLite busy timeout in these tests: a held lock fails a request in 0.1 s, not 5."""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def brain_clock(clock: FakeClock) -> Callable[[], dt.datetime]:
    """The brain's clock is the collector's: one timeline for both sides."""
    return lambda: dt.datetime.fromtimestamp(clock.time(), dt.UTC)


class RecordingAlerts:
    """The collector's owner-only alert bot, recorded (nothing reaches Telegram)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def alert(
        self, key: str, text: str, *, critical: bool = False, dedup: float | None = None
    ) -> AlertResult:
        self.sent.append((key, text))
        return AlertResult.SENT

    def sent_keys(self) -> list[str]:
        return [key for key, _ in self.sent]


class Site(FakeDriver):
    """Facebook as the driver would see it: each allow-listed URL answered from ``posts``.

    Every group has one old post (three days before the test starts), so no page is ever an
    empty shell: a page without stories fails the detectors' positive signature.
    """

    def __init__(self, clock: FakeClock, groups: dict[str, str]) -> None:
        super().__init__(info=LaunchInfo(CMDLINE, 4242, ChromeVersion("153.0.1.2"), "1.63.0"))
        self.clock = clock
        self.groups = dict(groups)
        start = clock.time()
        self.posts: dict[str, list[dict[str, Any]]] = {
            gid: [story(f"9{gid[-1]}0000000000001", gid, start - 3 * DAY, text="old")]
            for gid in groups
        }
        self.reads: list[str] = []

    def post(self, gid: str, post_id: str, text: str, *, age: float = 120.0) -> None:
        self.posts.setdefault(gid, []).insert(
            0, story(post_id, gid, self.clock.time() - age, text=text)
        )

    async def navigate(self, url: str) -> Step:
        self.calls.append(f"navigate {url}")
        self.last_url = url
        if url == FEED_URL:
            self.reads.append("feed")
            stories = sorted(
                (s for posts in self.posts.values() for s in posts),
                key=lambda s: -int(s["creation_time"]),
            )
            return nav(url, feed_document(), feed_body(*stories))
        if url == JOINS_URL:
            self.reads.append("joins")
            joined = sorted(self.groups.items())
            return nav(url, joins_document(), joins_body(*joined), joined_header_count=len(joined))
        gid = url.removeprefix("https://www.facebook.com/groups/").split("/")[0]
        self.reads.append(f"group {gid}")
        return nav(
            url,
            group_document(gid, name=self.groups.get(gid, "?")),
            group_body(*self.posts.get(gid, [])),
        )


@dataclass
class Brain:
    app: BrainApp
    sent: FakeBotClient
    port: int

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def to(self, user_id: int) -> list[SentMessage]:
        return [m for m in self.sent.sent if m.user_id == user_id]

    def notifications(self, user_id: int | None = None) -> list[SentMessage]:
        """Rendered posts: the messages that link somewhere (alerts are plain text)."""
        return [
            m
            for m in self.sent.sent
            if (user_id is None or m.user_id == user_id)
            and any(getattr(e, "url", None) for e in m.entities)
        ]

    async def alerts(self, user_id: int = OWNER) -> list[str]:
        """Plain messages to ``user_id``, once the monitor has sent what it had to (it sends
        from its own task, never inside the collector's request)."""
        await self.app.monitor.idle()
        return [
            m.text for m in self.to(user_id) if not any(getattr(e, "url", None) for e in m.entities)
        ]

    async def say(self, user_id: int, text: str) -> list[Action]:
        return await self.app.router.dispatch(Incoming(user_id=user_id, text=text))

    async def tap(self, user_id: int, data: str) -> list[Action]:
        return await self.app.router.dispatch(Incoming(user_id=user_id, data=data))

    def locked(self) -> sqlite3.Connection:
        """Another writer holding the database's write lock (release: ``rollback`` + ``close``)."""
        con = sqlite3.connect(self.app.settings.db_path, isolation_level=None)
        con.execute("BEGIN IMMEDIATE")
        return con


async def start_brain(root: Path, clock: FakeClock, *, port: int | None = None) -> Brain:
    """The brain on 127.0.0.1; the caller patches ``ddiver_fb.store.BUSY_TIMEOUT_MS`` first."""
    sent = FakeBotClient()
    chosen = port or free_port()
    app = BrainApp(
        settings_for(root, ingest_token=TOKEN, ingest_port=chosen, collector_tz=ZONE),
        clock=brain_clock(clock),
        bot_client=FakeTelethon(),
        bot=sent,
    )
    await app.setup()
    await app.storage.db.execute(f"PRAGMA busy_timeout={BUSY_MS}")  # the core's connection
    await app.storage.update_user(OWNER, lang="en", tz=ZONE)
    await app.storage.ensure_user(MOD)
    await app.storage.update_user(MOD, role=Role.ALLOWED, lang="en", tz=ZONE)
    await app.ingest.start()
    return Brain(app, sent, chosen)


@dataclass
class Collector:
    rt: Runtime
    site: Site
    alerts: RecordingAlerts
    clock: FakeClock
    grants: list[tuple[float, Grant]] = field(default_factory=list)
    """Every grant of ``turn``, with the time it was made."""
    holds: list[Hold] = field(default_factory=list)

    @property
    def store(self) -> Store:
        return self.rt.store

    @property
    def uplink(self) -> Uplink:
        assert self.rt.uplink is not None
        return self.rt.uplink

    async def turn(self) -> Grant | Hold:
        """One turn of the read loop, as ``Collector.run`` takes it (without its waiting)."""
        rt, site = self.rt, self.site
        await rt.control.load()
        running = site.running
        decision = await rt.scheduler.next(launch=not running, parked=running)
        now = self.clock.time()
        if isinstance(decision, Hold):
            self.holds.append(decision)
            await note_state(
                rt.store,
                decision.state,
                decision.reasons,
                now=now,
                until=decision.until,
                gates=decision.gates,
            )
            if decision.close_chrome and site.running:
                await site.close()
            return decision
        self.grants.append((now, decision))
        if not running:
            await site.launch(mode=SessionMode.READ)
        result = await rt.reader.read(decision)
        await note_state(
            rt.store,
            CollectorState.OK,
            [f"last read: {result.outcome.value}"],
            now=self.clock.time(),
            gates=dict.fromkeys(GATE_ORDER, True),
        )
        if result.close_chrome:
            await site.close()
        if result.sync is not None:
            await self.uplink.queue_snapshot(result.sync)
        return decision

    def granted(self, kind: ActionKind) -> list[tuple[float, Grant]]:
        return [(at, g) for at, g in self.grants if g.action.kind is kind]

    async def drive(
        self, seconds: float, *, until: Callable[[], Awaitable[bool]] | None = None
    ) -> None:
        """Run the loop and the uplink in turns for ``seconds`` of virtual time (or ``until``)."""
        end = self.clock.time() + seconds
        while self.clock.time() < end:
            await self.uplink.cycle()
            decision = await self.turn()
            if until is not None and await until():
                return
            if isinstance(decision, Hold):
                now = self.clock.time()
                wait = (decision.until if decision.until is not None else now + 60) - now
                self.clock.advance(min(max(wait, 1.0), 60.0))
            else:
                self.clock.advance(5.0)


async def start_collector(
    root: Path,
    clock: FakeClock,
    brain: Brain,
    *,
    collector_id: str = "home-1",
    groups: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    outbox_cap: int = 5000,
) -> Collector:
    settings = make_settings(
        root=root,
        ingest_url=brain.url,
        ingest_token=TOKEN,
        collector_id=collector_id,
        outbox_cap=outbox_cap,
    )
    site = Site(clock, groups if groups is not None else {G1: "Flats Varna", G2: "Flats Sofia"})
    alerts = RecordingAlerts()
    ports = Ports(
        clock=clock,
        platform="linux",
        runner=lambda args: paths.CommandResult(0, ""),
        probes=FakeProbes(),
        lookup=FakeLookup(),
        driver_factory=lambda policy: site,
        alerts=alerts,
        rng=random.Random(7),
        chrome_version=lambda: ChromeVersion("153.0.1.2"),
        uplink_transport=transport,
    )
    layout = paths.prepare(root, platform="linux")
    store = Store(layout.db, clock=clock, outbox_cap=outbox_cap)
    await store.open()
    await store.kv_set(KV_SEED, SEED)
    await store.kv_set(KV_HOME_PIN, HOME_PIN.to_json())
    await store.kv_set(KV_LOGIN_AT, repr(clock.time() - 20 * DAY))
    await store.kv_set(KV_CMDLINE, CMDLINE)
    rt = await build(settings, store, layout, ports)
    rt.lock.acquire()
    return Collector(rt, site, alerts, clock)


async def stop_collector(collector: Collector) -> None:
    collector.rt.lock.release()
    await collector.store.close()


def start_time() -> float:
    """A weekday at full cadence in the collector's zone, with an hour of it ahead."""
    return daytime(make_settings())


def local(epoch: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(epoch, zoneinfo.ZoneInfo(ZONE))


class Lossy(httpx.AsyncBaseTransport):
    """The real network, except that the next ``lose[path]`` answers are lost on the way back:
    the brain processed the request, the collector saw a timeout."""

    def __init__(self) -> None:
        self.lose: dict[str, int] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        async with httpx.AsyncHTTPTransport() as inner:
            response = await inner.handle_async_request(request)
            body = await response.aread()
        path = request.url.path
        if self.lose.get(path, 0) > 0:
            self.lose[path] -= 1
            raise httpx.ReadTimeout("the answer was lost", request=request)
        return httpx.Response(
            response.status_code, headers=response.headers, content=body, request=request
        )


def _written_as(data: Any, version: int) -> Any:
    """``data`` with every envelope's ``v`` set to ``version`` (items and events have their own)."""
    if isinstance(data, dict):
        return {k: version if k == "v" else _written_as(v, version) for k, v in data.items()}
    if isinstance(data, list):
        return [_written_as(v, version) for v in data]
    return data


class OlderCollector(httpx.AsyncBaseTransport):
    """The real network, but every request written as a collector one wire version behind
    would write it; the version of every answer is recorded."""

    def __init__(self, version: int) -> None:
        self.version = version
        self.answered: list[object] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if "v" in url.params:
            url = url.copy_set_param("v", str(self.version))
        body = await request.aread()
        if body:
            body = json.dumps(_written_as(json.loads(body), self.version)).encode()
        headers = [(k, v) for k, v in request.headers.items() if k.lower() != "content-length"]
        forwarded = httpx.Request(request.method, url, headers=headers, content=body)
        async with httpx.AsyncHTTPTransport() as inner:
            response = await inner.handle_async_request(forwarded)
            content = await response.aread()
        self.answered.append(json.loads(content).get("v"))
        return httpx.Response(
            response.status_code, headers=response.headers, content=content, request=request
        )
