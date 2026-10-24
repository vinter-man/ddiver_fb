"""The read loop with the uplink beside it: a scripted brain (``MockTransport``), a virtual clock.

The clock here lets overlapping sleeps end together: the loop waiting a minute and the uplink
waiting a minute take one virtual minute, not two, as they would on ``FakeClock`` (which moves
time forward by every sleep as it is asked for).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from fbcollector import paths
from fbcollector.alerts import NullAlerts
from fbcollector.clock import FakeClock
from fbcollector.detectors import FEED_URL, JOINS_URL
from fbcollector.driver.launch import ChromeVersion, LaunchInfo
from fbcollector.model import ReadOutcome, Step
from fbcollector.runner import Collector, Ports, build, status_lines
from fbcollector.sink import JsonlSink
from fbcollector.store import Store
from fbcollector.uplink import KV_INTRODUCED, Uplink
from fbwire.models import CollectorState, GroupsSnapshot, ItemsRequest, Status

from .gate_fakes import FakeLookup, FakeProbes
from .pacing_support import make_settings
from .reader_support import G1, G2, joins_body, joins_document, nav
from .test_runner import CMDLINE, Env, FeedDriver, daytime, prepared, production_policy, runner_ok
from .test_uplink import FakeBrain

TOKEN = "i" * 40
URL = "http://127.0.0.1:18781"


class ParallelClock(FakeClock):
    """Sleeps that overlap end together: two tasks sleeping 60 s from t both wake at t + 60."""

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        deadline = self.time() + max(0.0, seconds)
        await asyncio.sleep(0)
        if deadline > self.time():
            self.advance(deadline - self.time())


def uplink_env(tmp_path: Path, brain: FakeBrain) -> Env:
    root = tmp_path / "root"
    settings = make_settings(root=root, ingest_url=URL, ingest_token=TOKEN)
    clock = ParallelClock(start=daytime(settings))
    driver = FeedDriver(clock, info=LaunchInfo(CMDLINE, 4242, ChromeVersion("153.0.1.2"), "1.63.0"))
    alerts = NullAlerts()
    ports = Ports(
        clock=clock,
        platform="linux",
        runner=runner_ok,
        probes=FakeProbes(),
        lookup=FakeLookup(),
        driver_factory=production_policy(driver),
        alerts=alerts,
        rng=random.Random(3),
        chrome_version=lambda: ChromeVersion("153.0.1.2"),
        uplink_transport=httpx.MockTransport(brain),
    )
    return Env(settings, clock, driver, alerts, ports, root)


@pytest.fixture
def brain() -> FakeBrain:
    brain = FakeBrain()
    brain.monitored = [{"group_id": G1}]
    return brain


@pytest.fixture
def env(tmp_path: Path, brain: FakeBrain) -> Env:
    return uplink_env(tmp_path, brain)


@pytest.fixture
async def store(env: Env) -> AsyncIterator[Store]:
    opened = await prepared(env)
    yield opened
    await opened.close()


async def run(env: Env, store: Store, turns: int) -> Collector:
    rt = await build(env.settings, store, paths.Layout(env.root), env.ports)
    rt.lock.acquire()
    loop = Collector(rt, max_turns=turns)
    try:
        async with asyncio.timeout(20):
            await loop.run(asyncio.Event())
    finally:
        rt.lock.release()
    return loop


def statuses(brain: FakeBrain) -> list[Status]:
    return [Status.model_validate(body) for body in brain.bodies("/v1/status")]


async def test_an_ingest_url_replaces_the_sink_with_the_uplink(
    env: Env, store: Store, tmp_path: Path
) -> None:
    rt = await build(env.settings, store, paths.Layout(env.root), env.ports)
    assert rt.sink is None
    assert isinstance(rt.uplink, Uplink)
    local = make_settings(root=env.root)
    rt = await build(local, store, paths.Layout(env.root), env.ports)
    assert isinstance(rt.sink, JsonlSink)
    assert rt.uplink is None


async def test_the_loop_reads_under_the_lease_and_the_brain_gets_the_posts(
    env: Env, store: Store, brain: FakeBrain
) -> None:
    loop = await run(env, store, 10)
    assert env.driver.launches == 1
    assert loop.results
    posted = [
        item.post_id
        for request in brain.to("/v1/items")
        for item in ItemsRequest.model_validate_json(request.content).items
    ]
    assert posted
    assert await store.outbox_depth() == 0  # handed over, the last of them at shutdown
    assert not (env.root / "items.jsonl").exists()
    assert await store.monitored_ids() == {G1}  # the brain's set, not the local one
    reported = statuses(brain)
    assert reported
    assert {s.collector_id for s in reported} == {"home-1"}
    assert reported[-1].state in {CollectorState.OK, CollectorState.PAUSED}
    seqs = [s.seq or 0 for s in reported]
    assert seqs == sorted(set(seqs)) and 0 not in seqs  # one counter, always rising
    assert await store.kv_get(KV_INTRODUCED) == "1"  # the groups it knew went to the brain
    (introduction,) = brain.bodies("/v1/groups")
    assert {g["id"] for g in introduction["groups"]} == {G1, G2}


async def test_an_uplink_task_that_ends_is_restarted_and_the_owner_told(
    env: Env, store: Store, brain: FakeBrain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 2 B-F3: with the task gone, no remote pause or lease move would be heard, and
    reads would go on under the last grant until it lapsed hours later."""
    original = Uplink.run
    runs = 0

    async def dies_once(self: Uplink, stop: asyncio.Event) -> None:
        nonlocal runs
        runs += 1
        if runs == 1:
            raise RuntimeError("a bug")
        await original(self, stop)

    monkeypatch.setattr(Uplink, "run", dies_once)
    await run(env, store, 6)
    assert runs == 2
    assert [key for key, _ in env.alerts.sent].count("uplink_restarted") == 1
    assert brain.to("/v1/control")  # the second task polled the brain
    assert env.driver.launches == 1  # and reading went on under its lease


class JoiningDriver(FeedDriver):
    """The feed as ``FeedDriver`` serves it, and the joined-groups page with G1 and G2."""

    async def navigate(self, url: str) -> Step:
        if url != JOINS_URL:
            return await super().navigate(url)
        self.calls.append(f"navigate {url}")
        self.last_url = url
        joined = ((G1, "One"), (G2, "Two"))
        return nav(url, joins_document(), joins_body(*joined), joined_header_count=len(joined))


async def test_a_commanded_sync_in_the_loop_sends_its_snapshot_with_the_nonce(
    tmp_path: Path, brain: FakeBrain
) -> None:
    env = uplink_env(tmp_path, brain)
    driver = JoiningDriver(env.clock, info=env.driver.info)
    env = replace(env, driver=driver, ports=replace(env.ports, driver_factory=lambda p: driver))
    store = await prepared(env)
    try:
        brain.commands = [{"kind": "refresh_groups", "nonce": "refresh-0099"}]
        await run(env, store, 30)
        assert f"navigate {JOINS_URL}" in driver.calls
        snapshots = [GroupsSnapshot.model_validate(b) for b in brain.bodies("/v1/groups")]
        synced = [s for s in snapshots if s.nonce is not None]
        assert [s.nonce for s in synced] == ["refresh-0099"]  # once, whatever the re-sends
        assert synced[0].complete and {g.id for g in synced[0].groups if g.listed} == {G1, G2}
    finally:
        await store.close()


async def test_without_the_lease_nothing_is_read(env: Env, store: Store, brain: FakeBrain) -> None:
    brain.lease = {"collector_id": "laptop-1", "granted": True, "generation": 3}
    await run(env, store, 6)
    assert env.driver.launches == 0
    assert not any(call.startswith("navigate") for call in env.driver.calls)
    # Every turn that reached the gates says why; a planner's wait between them asks no gate
    # (Chrome is closed) and reports "next feed", so the last status depends on the turns' timing.
    paused = [s for s in statuses(brain) if s.state is CollectorState.PAUSED]
    assert paused
    assert all("no lease (held by laptop-1)" in (s.detail or "") for s in paused)


async def test_an_unreachable_brain_never_opens_the_first_read(
    env: Env, store: Store, brain: FakeBrain
) -> None:
    async def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("tunnel down", request=request)

    for path in ("/v1/control", "/v1/groups", "/v1/status", "/v1/events", "/v1/items"):
        brain.then(path, *([down] * 10_000))  # unreachable for the whole run
    await run(env, store, 6)
    assert env.driver.launches == 0  # fail closed: no lease was ever granted
    assert await store.outbox_depth() == 0  # and nothing was read to queue


async def test_a_stuck_request_does_not_hold_the_shutdown(
    env: Env, store: Store, brain: FakeBrain, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fbcollector import runner as runner_module

    monkeypatch.setattr(runner_module, "UPLINK_STOP_GRACE", 0.05)
    hung = asyncio.Event()

    async def tunnel_without_an_end(request: httpx.Request) -> httpx.Response:
        hung.set()
        await asyncio.Event().wait()  # never answers (the client's own timeout is real time)
        raise AssertionError("unreachable")

    brain.then("/v1/control", tunnel_without_an_end)
    await run(env, store, 3)
    assert hung.is_set()
    assert env.driver.launches == 0  # and without an answer there was no lease to read under


class AnsweredMidStep(FeedDriver):
    """The brain's answer arrives while the navigation is in flight; a real step fails once
    its tab is gone, so this one waits to see whether Chrome is closed under it."""

    def __init__(self, clock: FakeClock, brain: FakeBrain, change: str, **kw: Any) -> None:
        super().__init__(clock, **kw)
        self.brain = brain
        self.change = change
        self.uplink: Uplink | None = None
        self.closed_mid_step: bool | None = None

    async def navigate(self, url: str) -> Step:
        step = await super().navigate(url)
        if self.closed_mid_step is not None:
            return step
        if self.change == "pause":
            self.brain.paused = True
        else:
            self.brain.lease = {"collector_id": "laptop-1", "granted": True, "generation": 2}
        assert self.uplink is not None
        await self.uplink.poll_control()
        for _ in range(100):
            if not self.running:
                break
            await asyncio.sleep(0)
        self.closed_mid_step = not self.running
        if not self.running:
            raise RuntimeError("Target page, context or browser has been closed")
        return step


@pytest.mark.parametrize(
    ("change", "reason"),
    [("pause", "paused by the brain"), ("lease", "no lease (held by laptop-1)")],
)
async def test_the_brains_answer_mid_step_closes_chrome_under_the_read(
    env: Env, store: Store, brain: FakeBrain, change: str, reason: str
) -> None:
    """An answer applied beside a read ends it as a sleep does —
    Chrome closed under the step in flight, nothing judged — not at the next turn."""
    from fbcollector.ladder import Ladder

    driver = AnsweredMidStep(env.clock, brain, change, info=env.driver.info)
    env = replace(env, driver=driver, ports=replace(env.ports, driver_factory=lambda p: driver))
    rt = await build(env.settings, store, paths.Layout(env.root), env.ports)
    driver.uplink = rt.uplink
    rt.lock.acquire()
    loop = Collector(rt, max_turns=8)
    try:
        async with asyncio.timeout(20):
            await loop.run(asyncio.Event())
    finally:
        rt.lock.release()
    assert driver.closed_mid_step is True
    first = loop.results[0]
    assert (first.outcome, first.reason) == (ReadOutcome.ABORTED, f"control: {reason}")
    after = driver.calls[driver.calls.index(f"navigate {FEED_URL}") + 1 :]
    assert not any(c.startswith(("navigate", "scroll", "dwell")) for c in after)
    assert driver.launches == 1  # and no Chrome since
    assert await store.current_block() is None  # nothing was judged
    assert (await Ladder(store, env.clock).state()).backoff_until is None


class ParkedWaits(FeedDriver):
    """Once a read has parked Chrome, every wait on the clock (the loop's between reads, the
    uplink's between polls) lasts until something wakes it: a wait that ends only on its own
    time never ends here."""

    def __init__(self, clock: FakeClock, **kw: Any) -> None:
        super().__init__(clock, **kw)
        self.parked = asyncio.Event()
        self.closed_now = asyncio.Event()
        real = clock.sleep

        async def sleep(seconds: float) -> None:
            if self.parked.is_set():
                await asyncio.Event().wait()  # cancelled by whatever woke the waiter
            await real(seconds)

        clock.sleep = sleep  # type: ignore[method-assign]

    async def park(self) -> None:
        await super().park()
        self.parked.set()

    async def close(self) -> None:
        await super().close()
        self.closed_now.set()


class WaitWatcher(Collector):
    """Notes the first wait between reads that begins with Chrome parked."""

    def __init__(self, rt: Any, driver: ParkedWaits) -> None:
        super().__init__(rt)
        self.driver = driver
        self.waiting = asyncio.Event()

    async def _hold(self, hold: Any, stop: asyncio.Event) -> None:
        if self.driver.parked.is_set() and self.driver.running:
            self.waiting.set()
        await super()._hold(hold, stop)


@pytest.mark.parametrize("change", ["pause", "lease"])
async def test_the_brains_answer_between_reads_closes_the_parked_chrome_at_once(
    env: Env, store: Store, brain: FakeBrain, change: str
) -> None:
    """Review F2 follow-up: between reads the loop waited out its minute whatever the brain
    answered meanwhile, so Chrome stayed open on the account's profile after the lease had moved
    to another machine: two Chromes on one account."""
    driver = ParkedWaits(env.clock, info=env.driver.info)
    env = replace(env, driver=driver, ports=replace(env.ports, driver_factory=lambda p: driver))
    rt = await build(env.settings, store, paths.Layout(env.root), env.ports)
    assert rt.uplink is not None
    rt.lock.acquire()
    loop = WaitWatcher(rt, driver)
    stop = asyncio.Event()
    running = asyncio.create_task(loop.run(stop))
    try:
        async with asyncio.timeout(10):
            await loop.waiting.wait()  # a read ended; the loop waits with Chrome parked
            assert driver.running
            if change == "pause":
                brain.paused = True
            else:
                brain.lease = {"collector_id": "laptop-1", "granted": True, "generation": 2}
            await rt.uplink.poll_control()
            await driver.closed_now.wait()
    finally:
        stop.set()
        async with asyncio.timeout(10):
            await running
        rt.lock.release()
    closing = next(h for h in loop.holds if h.close_chrome)
    assert closing.state is CollectorState.PAUSED
    assert driver.launches == 1
    assert driver.calls[-2:] == ["park", "close"]


async def test_with_the_brain_a_restart_does_not_announce_the_quarantine_again(
    env: Env, store: Store, brain: FakeBrain
) -> None:
    """The event went into the durable queue when it was raised: only local mode, which has no
    such queue, raises it again at a start (review F3 follow-up)."""
    from fbcollector.events import KV_QUARANTINE
    from fbcollector.model import Sighting, Surface

    await store.quarantine(
        Sighting(post_id="9001", group_id=G1, surface=Surface.FEED), errors=["url:value_error"]
    )
    await store.kv_set(KV_QUARANTINE, "1", durable=True)
    await run(env, store, 2)
    sent = [e["kind"] for body in brain.bodies("/v1/events") for e in body.get("events", [])]
    queued = [e.kind for e in await store.pending_events()]
    assert "items_invalid" not in sent + queued
    assert await store.kv_get(KV_QUARANTINE) == "1"


async def test_the_uplink_goes_through_the_tunnels_socket(
    env: Env, store: Store, brain: FakeBrain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``FBC_INGEST_SOCKET`` (the Mac: a socket in fbc's own 0700 ``~/.ssh``) every request
    goes through that socket: no loopback TCP port that another local account could bind while
    ssh is down, and so be handed the bearer token."""
    socket = "/Users/fbc/.ssh/fbtunnel.sock"
    made: list[dict[str, Any]] = []

    def transport(**kwargs: Any) -> httpx.AsyncBaseTransport:
        made.append(kwargs)
        return httpx.MockTransport(brain)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)
    settings = make_settings(
        root=env.root, ingest_url=URL, ingest_token=TOKEN, ingest_socket=socket
    )
    await run(
        replace(env, settings=settings, ports=replace(env.ports, uplink_transport=None)), store, 2
    )
    assert brain.to("/v1/control")
    assert made and all(kwargs == {"uds": socket} for kwargs in made)


def test_an_uplink_needs_its_url_and_token(env: Env) -> None:
    from fbcollector.runner import make_uplink

    rt_settings = make_settings(root=env.root)
    with pytest.raises(ValueError, match="FBC_INGEST_URL"):
        make_uplink(
            rt_settings,
            None,  # type: ignore[arg-type]
            control=None,  # type: ignore[arg-type]
            scheduler=None,  # type: ignore[arg-type]
            alerts=NullAlerts(),
            ports=env.ports,
        )


async def test_the_cli_status_names_the_lease(env: Env, store: Store, brain: FakeBrain) -> None:
    await run(env, store, 2)
    lines = await status_lines(env.settings, store, env.clock)
    assert any(line.startswith("brain: lease held; last answer ") for line in lines)
    brain.lease = {"collector_id": "laptop-1", "granted": True}
    await run(env, store, 2)
    lines = await status_lines(env.settings, store, env.clock)
    assert any("not held (holder: laptop-1)" in line for line in lines)
