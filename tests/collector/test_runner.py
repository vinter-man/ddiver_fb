"""The collector process on a virtual clock: loop, launch checks, login, service, CLI."""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import logging
import random
import subprocess
from collections import deque
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from fbcollector import __main__ as cli
from fbcollector import paths
from fbcollector.alerts import NullAlerts
from fbcollector.clock import FakeClock
from fbcollector.config import Settings
from fbcollector.detectors import FEED_URL
from fbcollector.driver.launch import ChromeVersion, LaunchError, LaunchInfo, SessionMode
from fbcollector.driver.policy import FacebookSurfaces
from fbcollector.driver.selftest import SelfTestReport
from fbcollector.gates import KV_HOME_PIN, KV_LOGIN_AT, Activity, GateContext, PowerSource
from fbcollector.model import CollectorState, PageState, ReadOutcome, Step
from fbcollector.paths import CommandResult
from fbcollector.raw import STEP_SUFFIX, save_quarantine, save_step
from fbcollector.reader import KV_PARSER_BROKEN
from fbcollector.rhythm import KV_SEED, Jitter, Phase, Rhythm, RhythmConfig
from fbcollector.runner import (
    GATE_ORDER,
    KV_CMDLINE,
    KV_HALT,
    KV_NOT_HOME_SINCE,
    KV_PLANNED_EXIT,
    KV_STATUS,
    Collector,
    Halt,
    Ports,
    ServiceManager,
    build,
    check_command_line,
    login,
    parse_since,
    require_tty,
    run_collector,
    set_halt,
    wait_or_stop,
)
from fbcollector.scheduler import KV_BACKSTOP_ONLY, Hold
from fbcollector.store import ProcessKind, Store
from fbwire.models import BudgetKind

from .driver_fakes import SESSION, FakeContext, FakePage, FakePlaywright, chrome_ps
from .gate_fakes import HOME_PIN, FakeLookup, FakeProbes
from .pacing_support import ZONE, make_settings
from .reader_support import (
    G1,
    G2,
    FakeDriver,
    feed_body,
    feed_document,
    nav,
    post,
    seed,
    story,
)

SEED = "runner-test-seed"
CMDLINE = "chrome --no-proxy-server about:blank"


def daytime(settings: Settings) -> float:
    """A weekday moment at full cadence with an hour of it ahead (no break, no shoulder)."""
    rhythm = Rhythm(RhythmConfig.from_settings(settings), Jitter(SEED))
    plan = rhythm.plan(dt.date(2026, 10, 7))
    t = plan.shoulder_end + 4 * 3600
    while not all(plan.phase(t + m * 60) is Phase.FULL for m in range(61)):
        t += 300
    return t


class FeedDriver(FakeDriver):
    """Answers every feed navigation with fresh stories; the clock sets their times."""

    def __init__(self, clock: FakeClock, **kw: Any) -> None:
        super().__init__(**kw)
        self.clock = clock
        self.counter = 0

    async def navigate(self, url: str) -> Step:
        self.calls.append(f"navigate {url}")
        self.counter += 1
        now = self.clock.time()
        nodes = [story(post(self.counter * 10 + i), G1, now - 60 * (i + 1)) for i in range(3)]
        self.last_url = url
        return nav(url, feed_document(), feed_body(*nodes))


def runner_ok(args: Sequence[str]) -> CommandResult:
    return CommandResult(0, "")


def production_policy(driver: FakeDriver) -> Any:
    """A driver factory that insists on the production URL allow-list."""

    def make(policy: Any) -> FakeDriver:
        assert isinstance(policy, FacebookSurfaces)
        return driver

    return make


@dataclass
class Env:
    settings: Settings
    clock: FakeClock
    driver: FeedDriver
    alerts: NullAlerts
    ports: Ports
    root: Path
    local_drivers: list[FakeDriver] = field(default_factory=list)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    root = tmp_path / "root"
    settings = make_settings(root=root)
    clock = FakeClock(start=daytime(settings))
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
    )
    return Env(settings, clock, driver, alerts, ports, root)


async def prepared(env: Env, *, cmdline: str | None = CMDLINE) -> Store:
    layout = paths.prepare(env.root, platform="linux")
    store = Store(layout.db, clock=env.clock)
    await store.open()
    await store.kv_set(KV_SEED, SEED)
    await store.kv_set(KV_HOME_PIN, HOME_PIN.to_json())
    await store.kv_set(KV_LOGIN_AT, repr(env.clock.time() - 20 * 86400))
    if cmdline is not None:
        await store.kv_set(KV_CMDLINE, cmdline)
    env.clock.jump_wall(-3600)  # the groups were synced an hour ago: their baseline
    await seed(store, env.clock, G1, G2)
    env.clock.jump_wall(3600)
    return store


@pytest.fixture
async def store(env: Env) -> AsyncIterator[Store]:
    opened = await prepared(env)
    yield opened
    await opened.close()


async def collector(env: Env, store: Store, turns: int) -> Collector:
    layout = paths.Layout(env.root)
    rt = await build(env.settings, store, layout, env.ports)
    rt.lock.acquire()
    return Collector(rt, max_turns=turns)


async def run_turns(env: Env, store: Store, turns: int) -> Collector:
    loop = await collector(env, store, turns)
    try:
        await loop.run(asyncio.Event())
    finally:
        loop.rt.lock.release()
    return loop


# -- the loop -------------------------------------------------------------------------------


async def test_the_ladder_is_built_with_the_configured_strike_decay(env: Env, store: Store) -> None:
    settings = env.settings.model_copy(update={"strike_decay_days": 45})
    rt = await build(settings, store, paths.Layout(env.root), env.ports)
    assert rt.ladder._decay == 45 * 86400


async def test_the_loop_launches_reads_drains_and_closes(env: Env, store: Store) -> None:
    loop = await run_turns(env, store, 8)
    assert env.driver.launches == 1
    assert env.driver.calls[0] == "launch"
    assert env.driver.modes == [SessionMode.READ]
    assert f"navigate {FEED_URL}" in env.driver.calls
    assert loop.results
    assert env.driver.closed == 1  # at shutdown
    assert await store.process_events_since(86400, kinds=[ProcessKind.PROCESS_START]) == 1
    assert await store.process_events_since(86400, kinds=[ProcessKind.CHROME_LAUNCH]) == 1
    lines = (env.root / "items.jsonl").read_text().splitlines()
    assert lines
    assert await store.outbox_depth() == 0
    status = await store.kv_get_json(KV_STATUS)
    assert status["state"] == "ok"
    versions = await store.kv_get_json("versions")
    assert versions == {"chrome": "153.0.1.2", "patchright": "1.63.0"}


async def test_the_collector_runs_with_every_gate_in_order(env: Env, store: Store) -> None:
    loop = await collector(env, store, 1)
    loop.rt.lock.release()
    assert loop.rt.gates.names == GATE_ORDER


async def _close_gate(env: Env, store: Store, gate: str) -> None:
    """Shut one gate of ``GATE_ORDER`` (all others open) the way the world would."""
    from dataclasses import replace as dc_replace

    probes = env.ports.probes
    assert isinstance(probes, FakeProbes)
    if gate in WORLD:
        WORLD[gate](probes, dc_replace)
    else:
        await STATE[gate](env, store)


WORLD: dict[str, Any] = {
    "timezone": lambda probes, _r: setattr(probes, "zone", "UTC"),
    "power": lambda probes, _r: setattr(probes, "source", PowerSource.BATTERY),
    "home": lambda probes, r: setattr(probes, "facts", r(probes.facts, vpn_routes=("0/1 utun",))),
    "proxy": lambda probes, r: setattr(probes, "facts", r(probes.facts, proxies=("HTTPSEnable",))),
}


async def _crash_loop(env: Env, store: Store) -> None:
    # One past the cap of unplanned launches: a planned one (the owner's selftest) is refused too.
    for _ in range(4):
        await store.record_process_event(ProcessKind.CHROME_LAUNCH)


async def _moved_pin(env: Env, store: Store) -> None:
    from fbcollector.gates import HomePin

    pin = HomePin.from_json(HOME_PIN.to_json())
    moved = HomePin(pin.router_mac, pin.asn_v4, pin.asn_v6, "Europe/Kyiv", pin.pinned_at)
    await store.kv_set(KV_HOME_PIN, moved.to_json())


async def _fresh_login(env: Env, store: Store) -> None:
    await store.kv_set(KV_LOGIN_AT, repr(env.clock.time() - 3600))


async def _parser_broken(env: Env, store: Store) -> None:
    await store.kv_set(KV_PARSER_BROKEN, '["x"]')


async def _blocked(env: Env, store: Store) -> None:
    from fbcollector.ladder import Ladder

    await Ladder(store, env.clock).trip(PageState.BLOCKED, reason="url:checkpoint")


async def _backing_off(env: Env, store: Store) -> None:
    from fbcollector.ladder import Ladder

    ladder = Ladder(store, env.clock)
    # A fourth unrecognised page in a day (recognised ones in between): 40 minutes off.
    for _ in range(3):
        await ladder.on_verdict(verdict_of(PageState.UNKNOWN, "signature:english"))
        await ladder.on_verdict(verdict_of(PageState.OK))
    await ladder.on_verdict(verdict_of(PageState.UNKNOWN, "signature:english"))


async def _quiet(env: Env, store: Store) -> None:
    rhythm = Rhythm(RhythmConfig.from_settings(env.settings), Jitter(SEED))
    now = env.clock.time()
    env.clock.advance(rhythm.plan_at(now).next_quiet_start + 60 - now)


async def _paused(env: Env, store: Store) -> None:
    from fbcollector.control import ControlCenter
    from fbcollector.ladder import Ladder

    ladder = Ladder(store, env.clock)
    control = ControlCenter(store, ladder, env.clock, collector_id="home-1", local_mode=True)
    await control.load()
    await control.pause()


async def _nothing(env: Env, store: Store) -> None:
    return None  # the lock: the test itself does not hold it


STATE: dict[str, Any] = {
    "crash_loop": _crash_loop,
    "timezone_pin": _moved_pin,
    "warmup": _fresh_login,
    "halt": _parser_broken,
    "block": _blocked,
    "backoff": _backing_off,
    "quiet": _quiet,
    "control": _paused,
    "lock": _nothing,
}


def verdict_of(state: PageState, *reasons: str) -> Any:
    from fbcollector.detectors import Verdict

    return Verdict(state=state, reasons=reasons, excerpt=state.value)


@pytest.mark.parametrize(
    "gate",
    [
        "crash_loop",
        "timezone",
        "timezone_pin",
        "power",
        "home",
        "proxy",
        "warmup",
        "lock",
        "halt",
        "block",
        "backoff",
        "control",
        "quiet",
    ],
)
async def test_nothing_reaches_facebook_while_any_gate_is_closed(
    env: Env, store: Store, gate: str
) -> None:
    await _close_gate(env, store, gate)
    loop = await collector(env, store, 12)
    if gate == "lock":
        loop.rt.lock.release()  # another process holds the profile
    try:
        await loop.run(asyncio.Event())
    finally:
        loop.rt.lock.release()
    assert not any(c.startswith(("navigate", "scroll")) for c in env.driver.calls)
    assert await store.budget_used(BudgetKind.NAVIGATION, 86400) == 0
    assert loop.holds
    assert all(h.close_chrome for h in loop.holds if h.state is not CollectorState.OK)


async def test_a_changed_command_line_halts_before_any_navigation(env: Env) -> None:
    store = await prepared(env, cmdline="chrome --other about:blank")
    try:
        loop = await run_turns(env, store, 4)
        assert not any(c.startswith("navigate") for c in env.driver.calls)
        assert env.driver.closed == 1
        halt = await store.kv_get_json(KV_HALT)
        assert halt["kind"] == Halt.COMMAND_LINE
        assert "--no-proxy-server" in halt["reason"]
        assert env.alerts.sent[0][0] == "command_line"
        report = await loop.rt.gates.evaluate(GateContext(Activity.NAVIGATE), stop_early=False)
        halt_gate = next(r for r in report.results if r.gate == "halt")
        assert halt_gate.state is CollectorState.NEEDS_LOGIN
    finally:
        await store.close()


async def test_no_recorded_command_line_is_a_mismatch(env: Env) -> None:
    store = await prepared(env, cmdline=None)
    try:
        await run_turns(env, store, 3)
        halt = await store.kv_get_json(KV_HALT)
        assert "no Chrome command line" in halt["reason"]
    finally:
        await store.close()


class BrokenLaunch(FeedDriver):
    async def launch(self, *, mode: SessionMode) -> LaunchInfo:
        self.calls.append("launch")
        raise RuntimeError("Chrome is not installed")


async def test_a_failed_launch_waits_and_alerts(env: Env, store: Store) -> None:
    broken = BrokenLaunch(env.clock)
    env.ports.driver_factory = lambda _policy: broken
    await run_turns(env, store, 3)
    assert broken.calls.count("launch") >= 1
    assert not any(c.startswith("navigate") for c in broken.calls)
    assert [k for k, _ in env.alerts.sent] == ["launch_failed"] * broken.calls.count("launch")
    assert 300 in env.clock.sleeps


async def test_the_cookie_gate_needs_a_session(env: Env, store: Store) -> None:
    env.driver.cookies = frozenset({"datr"})
    await run_turns(env, store, 3)
    assert not any(c.startswith("navigate") for c in env.driver.calls)
    halt = await store.kv_get_json(KV_HALT)
    assert halt["kind"] == Halt.NEEDS_LOGIN
    assert ("needs_login", env.alerts.sent[0][1]) == env.alerts.sent[0]


@dataclass
class SelftestDriver(FakeDriver):
    attributes: dict[str, str] = field(default_factory=dict)

    async def read_attribute(self, selector: str, name: str) -> str | None:
        return self.attributes.get(name)


@dataclass
class SelfTests:
    """Stands in for ``runner.selftest``: scripted answers, and how many Chrome launches the
    ledger held when each self-test started its Chrome."""

    store: Store
    results: deque[bool] = field(default_factory=deque)
    ledger: list[int] = field(default_factory=list)
    during: Any = None
    """Called while the self-test runs (the world changing under it)."""

    async def __call__(self, factory: Any) -> SelfTestReport:
        self.ledger.append(await self.store.process_events_since(DAY, kinds=[CHROME]))
        if self.during is not None:
            self.during()
        ok = self.results.popleft() if self.results else True
        return SelfTestReport((("webdriver_false", ok),), 160)


DAY = 86400.0
CHROME = ProcessKind.CHROME_LAUNCH


async def launch_log(store: Store) -> list[tuple[float, str | None]]:
    """Every Chrome launch recorded, oldest first: when, and what for (``selftest``, ...)."""
    cur = await store.db.execute(
        "SELECT at, detail FROM process_events WHERE kind=? ORDER BY id", (CHROME.value,)
    )
    return [(float(r[0]), r[1]) for r in await cur.fetchall()]


def most_within(times: Sequence[float], window: float) -> int:
    """The most launches any sliding window of ``window`` seconds holds."""
    ordered = sorted(times)
    return max(
        (sum(1 for u in ordered if t <= u < t + window) for t in ordered),
        default=0,
    )


def untested_chrome(env: Env, store: Store, monkeypatch: pytest.MonkeyPatch) -> SelfTests:
    import fbcollector.runner as runner_module

    env.ports.chrome_version = lambda: ChromeVersion("160.0.0.1")
    env.ports.local_driver_factory = lambda port: FakeDriver()
    tests = SelfTests(store)
    monkeypatch.setattr(runner_module, "selftest", tests)
    return tests


def full_cadence_after(env: Env, delay: float) -> float:
    """The first moment ``delay`` seconds from now with an hour of full cadence ahead."""
    rhythm = Rhythm(RhythmConfig.from_settings(env.settings), Jitter(SEED))
    t = env.clock.time() + delay
    while not all(rhythm.phase(t + m * 60) is Phase.FULL for m in range(61)):
        t += 300
    return t


async def test_an_untested_chrome_major_runs_the_selftest_first(
    env: Env, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    tests = untested_chrome(env, store, monkeypatch)
    tests.results.append(False)
    await run_turns(env, store, 3)
    halt = await store.kv_get_json(KV_HALT)
    assert halt["kind"] == Halt.SELFTEST
    assert env.driver.launches == 0
    assert env.alerts.sent[0][0] == "selftest:160"
    # Both launches — the self-test's and the read's — were on disk before its Chrome started.
    assert tests.ledger == [2]
    assert [detail for _, detail in await launch_log(store)] == ["selftest", None]
    await store.kv_delete(KV_HALT)
    # In the same hour the pair no longer fits under the cap (2 recorded + 2 pending > 3).
    loop = await run_turns(env, store, 30)
    assert tests.ledger == [2]
    assert env.driver.launches == 0
    assert any(h.state is CollectorState.CRASH_LOOP for h in loop.holds)
    env.clock.advance(full_cadence_after(env, 3600) - env.clock.time())
    await run_turns(env, store, 40)
    assert await store.kv_get("selftest:160") == "passed"
    assert env.driver.launches == 1
    assert tests.ledger == [2, 4]
    times = [at for at, _ in await launch_log(store)]
    assert len(times) == 4
    assert most_within(times, 3600) <= 3  # the crash-loop cap held for every launch


@pytest.mark.parametrize(
    ("window", "earlier", "admitted"),
    [("hour", 1, True), ("hour", 2, False), ("day", 6, True), ("day", 7, False)],
)
async def test_the_selftest_and_the_reads_launch_pass_the_crash_loop_gate_together(
    env: Env, monkeypatch: pytest.MonkeyPatch, window: str, earlier: int, admitted: bool
) -> None:
    store = await prepared(env)
    try:
        # Launches earlier this hour, or spread over the day before the last hour.
        for n in range(earlier):
            back = 600.0 if window == "hour" else 7200.0 + n * 3600.0
            env.clock.jump_wall(-back)
            await store.record_process_event(CHROME)
            env.clock.jump_wall(back)
        tests = untested_chrome(env, store, monkeypatch)
        loop = await run_turns(env, store, 8)
        times = [at for at, _ in await launch_log(store)]
        if admitted:
            assert tests.ledger == [earlier + 2]
            assert env.driver.launches == 1
            assert loop.results
        else:
            # Nothing started, nothing was recorded, nothing was paid.
            assert tests.ledger == []
            assert env.driver.launches == 0
            assert len(times) == earlier
            assert await store.budget_used(BudgetKind.NAVIGATION, DAY) == 0
            assert any(h.state is CollectorState.CRASH_LOOP for h in loop.holds)
        assert most_within(times, 3600) <= 3
        assert len(times) <= 8
    finally:
        await store.close()


@pytest.mark.parametrize("gate", ["warmup", "quiet", "control", "block", "power", "halt"])
async def test_the_selftest_waits_for_every_gate_of_the_launch(
    env: Env, store: Store, monkeypatch: pytest.MonkeyPatch, gate: str
) -> None:
    tests = untested_chrome(env, store, monkeypatch)
    await _close_gate(env, store, gate)
    await run_turns(env, store, 8)
    assert tests.ledger == []  # the account's profile never started
    assert await launch_log(store) == []
    assert env.driver.launches == 0


async def test_a_gate_that_closes_during_the_selftest_stops_the_reads_launch(
    env: Env, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes = FakeProbes()
    env.ports.probes = probes
    tests = untested_chrome(env, store, monkeypatch)
    tests.during = lambda: setattr(probes, "source", PowerSource.BATTERY)
    loop = await run_turns(env, store, 4)
    assert tests.ledger == [2]
    assert await store.kv_get("selftest:160") == "passed"
    assert env.driver.launches == 0  # the read's own Chrome never started
    assert not any(c.startswith("navigate") for c in env.driver.calls)
    assert any(h.state is CollectorState.NOT_HOME and h.close_chrome for h in loop.holds)


async def test_a_probe_after_a_chrome_update_runs_the_selftest_first(
    env: Env, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fbcollector.ladder import Ladder, ResumeResult

    ladder = Ladder(store, env.clock)
    block = await ladder.trip(PageState.BLOCKED, reason="url:checkpoint")
    env.clock.advance(6 * 3600 + 60)
    assert await ladder.resume(block.id, "nonce-probe-2") is ResumeResult.ACCEPTED
    tests = untested_chrome(env, store, monkeypatch)
    await run_turns(env, store, 3)
    # The block gate lets the probe's launch through, and with it the self-test before it.
    assert tests.ledger == [2]
    assert env.driver.launches == 1
    assert await store.current_block() is None


async def test_an_unreadable_chrome_version_halts_before_any_launch(env: Env, store: Store) -> None:
    env.ports.chrome_version = lambda: None
    await run_turns(env, store, 4)
    halt = await store.kv_get_json(KV_HALT)
    assert halt["kind"] == Halt.SELFTEST
    assert "version" in halt["reason"]
    assert env.driver.launches == 0
    assert env.alerts.sent[0][0] == "selftest:unknown"


async def test_an_unreadable_chrome_version_halts_and_alerts_once(env: Env, store: Store) -> None:
    """Review F1 follow-up: the version was read before the gates on every turn a read was due,
    so the halt was written again and the critical alert raised again every turn it lasted."""
    reads: list[float] = []

    def unreadable() -> ChromeVersion | None:
        reads.append(env.clock.time())
        return None

    env.ports.chrome_version = unreadable
    loop = await run_turns(env, store, 8)
    assert len(reads) == 1  # the halt gate refuses every later turn before the question
    assert [key for key, _ in env.alerts.sent] == ["selftest:unknown"]
    assert len(loop.holds) == 8
    assert env.driver.launches == 0


@pytest.mark.parametrize(
    "gate", ["warmup", "power", "home", "crash_loop", "block", "control", "halt", "command_line"]
)
async def test_the_chrome_version_is_read_only_for_a_launch_every_gate_admits(
    env: Env, store: Store, gate: str
) -> None:
    """Review F1 follow-up: a Chrome update in progress (an unreadable version) halted the
    collector during the warm-up, away or on battery, and a SELFTEST halt replaced a
    COMMAND_LINE one, which `pause --off` then cleared without `fbcollector login`."""
    reads: list[float] = []

    def unreadable() -> ChromeVersion | None:
        reads.append(env.clock.time())
        return None

    env.ports.chrome_version = unreadable
    if gate == "command_line":
        await set_halt(store, Halt.COMMAND_LINE, "a switch was added")
    else:
        await _close_gate(env, store, gate)
    await run_turns(env, store, 6)
    assert reads == []
    halt = await store.kv_get_json(KV_HALT)
    if gate == "command_line":
        assert halt == {"kind": Halt.COMMAND_LINE, "reason": "a switch was added"}
    else:
        assert halt is None
    assert not any(key.startswith("selftest") for key, _ in env.alerts.sent)
    assert env.driver.launches == 0


class Checkpointed(FeedDriver):
    """Answers the first navigation with ``page``; notes what the store held at ``close``."""

    def __init__(self, clock: FakeClock, page: Step, store: Store, **kw: Any) -> None:
        super().__init__(clock, **kw)
        self.page = page
        self.store = store
        self.loop: Collector | None = None
        self.holds_at_navigate = -1
        self.at_close: list[tuple[bool, float | None, int]] = []

    async def navigate(self, url: str) -> Step:
        self.calls.append(f"navigate {url}")
        self.last_url = self.page.final_url
        assert self.loop is not None
        self.holds_at_navigate = len(self.loop.holds)
        return self.page

    async def close(self) -> None:
        from fbcollector.ladder import Ladder

        assert self.loop is not None
        block = await self.store.current_block()
        backoff = (await Ladder(self.store, self.clock).state()).backoff_until
        self.at_close.append((block is not None, backoff, len(self.loop.holds)))
        await super().close()


@pytest.mark.parametrize(
    ("page", "state"),
    [
        (
            nav(
                "https://www.facebook.com/checkpoint/1501092823525282/?next=x",
                feed_document(),
                feed_body(story(post(1), G1, 1_790_000_000)),
            ),
            PageState.BLOCKED,
        ),
        (
            nav(
                FEED_URL,
                feed_document(lang="de"),
                feed_body(story(post(1), G1, 1_790_000_000)),
                html_lang="de",
            ),
            PageState.UNKNOWN,
        ),
    ],
    ids=["checkpoint", "not_english"],
)
async def test_a_warning_page_closes_chrome_in_the_same_turn(
    env: Env, store: Store, page: Step, state: PageState
) -> None:
    driver = Checkpointed(env.clock, page, store, info=env.driver.info)
    env.ports.driver_factory = lambda _policy: driver
    loop = await collector(env, store, 3)  # the first turn of a fresh epoch waits a little
    driver.loop = loop
    try:
        await loop.run(asyncio.Event())
    finally:
        loop.rt.lock.release()
    assert [r.page_state for r in loop.results] == [state]
    first_close = driver.calls.index("close")
    assert "park" not in driver.calls[:first_close]  # never parked on the warning page
    blocked, backoff, holds = driver.at_close[0]
    assert holds == driver.holds_at_navigate  # closed by the read's own turn, not a later hold
    # The block (or the backoff) was on disk before Chrome was closed.
    assert blocked if state is PageState.BLOCKED else backoff is not None


class CallAlerts:
    """Owner alerts that land in the driver's call log, to see what came first."""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def alert(
        self, key: str, text: str, *, critical: bool = False, dedup: float | None = None
    ) -> Any:
        from fbcollector.alerts import AlertResult

        self.calls.append(f"alert {key.split(':', maxsplit=1)[0]}")
        return AlertResult.SENT


async def test_the_owner_is_alerted_only_after_chrome_is_closed(env: Env, store: Store) -> None:
    page = nav(
        "https://www.facebook.com/checkpoint/1501092823525282/",
        feed_document(),
        feed_body(story(post(1), G1, 1_790_000_000)),
    )
    driver = Checkpointed(env.clock, page, store, info=env.driver.info)
    env.ports.driver_factory = lambda _policy: driver
    env.ports.alerts = CallAlerts(driver.calls)
    loop = await collector(env, store, 3)
    driver.loop = loop
    try:
        await loop.run(asyncio.Event())
    finally:
        loop.rt.lock.release()
    calls = driver.calls
    assert calls.index("screenshot") < calls.index("close") < calls.index("alert block_opened")


async def test_the_resume_probe_launches_chrome_and_lifts_the_block(env: Env, store: Store) -> None:
    from fbcollector.ladder import Ladder, ResumeResult

    ladder = Ladder(store, env.clock)
    block = await ladder.trip(PageState.BLOCKED, reason="url:checkpoint")
    env.clock.advance(6 * 3600 + 60)
    assert await ladder.resume(block.id, "nonce-probe-1") is ResumeResult.ACCEPTED
    loop = await run_turns(env, store, 3)
    assert await store.current_block() is None
    assert env.driver.launches == 1
    navigations = [c for c in env.driver.calls if c.startswith("navigate")]
    assert navigations
    assert "dwell 15" in env.driver.calls  # the probe's final look
    # Every paid navigation was sent: nothing was paid for a launch the gates refused.
    assert await store.budget_used(BudgetKind.NAVIGATION, 86400) == len(navigations)
    assert loop.results[0].page_state is PageState.OK
    assert loop.results[0].outcome is not ReadOutcome.FAILED


class PausedMidRead(FeedDriver):
    """The owner runs ``fbcollector pause`` in another terminal while the navigation is in
    flight: another process, its own connection to the state file."""

    def __init__(self, clock: FakeClock, root: Path, **kw: Any) -> None:
        super().__init__(clock, **kw)
        self.root = root
        self.paused_at: int | None = None

    async def navigate(self, url: str) -> Step:
        from fbcollector.control import ControlCenter
        from fbcollector.ladder import Ladder

        step = await super().navigate(url)
        if self.paused_at is None:
            async with Store(paths.Layout(self.root).db, clock=self.clock) as store:
                ladder = Ladder(store, self.clock)
                cli = ControlCenter(
                    store, ladder, self.clock, collector_id="home-1", local_mode=True
                )
                await cli.pause()
            self.paused_at = len(self.calls)
        return step


async def test_the_owners_pause_from_another_process_ends_the_read_at_its_next_step(
    env: Env, store: Store
) -> None:
    """The pause took effect only at the next turn, so the read's
    scrolls and final look went on after `fbcollector pause` had said "Paused"."""
    driver = PausedMidRead(env.clock, env.root, info=env.driver.info)
    env.ports.driver_factory = lambda _policy: driver
    loop = await run_turns(env, store, 6)
    assert driver.paused_at is not None
    after = driver.calls[driver.paused_at :]
    assert not any(c.startswith(("navigate", "scroll", "dwell")) for c in after)
    assert "close" in after
    first = loop.results[0]
    assert (first.outcome, first.close_chrome) == (ReadOutcome.ABORTED, True)
    assert first.reason == "control: paused by the owner"
    assert driver.launches == 1
    assert loop.holds[-1].state is CollectorState.PAUSED


class Unplugged(FeedDriver):
    """The power goes as soon as the first read is parked."""

    def __init__(self, clock: FakeClock, probes: FakeProbes, **kw: Any) -> None:
        super().__init__(clock, **kw)
        self.probes = probes

    async def park(self) -> None:
        await super().park()
        self.probes.source = PowerSource.BATTERY


async def test_a_gate_that_closes_between_reads_closes_the_parked_chrome(
    env: Env, store: Store
) -> None:
    probes = FakeProbes()
    env.ports.probes = probes
    driver = Unplugged(env.clock, probes, info=env.driver.info)
    env.ports.driver_factory = lambda _policy: driver
    loop = await run_turns(env, store, 3)
    assert len(loop.results) == 1
    parked = driver.calls.index("park")
    assert "close" in driver.calls[parked:]
    closing = next(h for h in loop.holds if h.close_chrome)
    assert closing.state is CollectorState.NOT_HOME
    assert driver.closed >= 1
    assert driver.calls.count("launch") == 1


class SleepyLaunch(FeedDriver):
    """The lid closes while Chrome starts, or a VPN comes up."""

    def __init__(self, clock: FakeClock, probes: FakeProbes, how: str, **kw: Any) -> None:
        super().__init__(clock, **kw)
        self.probes = probes
        self.how = how

    async def launch(self, *, mode: SessionMode) -> LaunchInfo:
        info = await super().launch(mode=mode)
        if self.launches > 1:
            return info
        if self.how == "sleep":
            self.clock.suspend(120)
        else:
            from dataclasses import replace as dc_replace

            self.probes.facts = dc_replace(self.probes.facts, vpn_routes=("0/1 utun4",))
        return info


@pytest.mark.parametrize("how", ["sleep", "vpn"])
async def test_gates_are_checked_again_after_chrome_started(
    env: Env, store: Store, how: str
) -> None:
    probes = FakeProbes()
    env.ports.probes = probes
    driver = SleepyLaunch(env.clock, probes, how, info=env.driver.info)
    env.ports.driver_factory = lambda _policy: driver
    loop = await collector(env, store, 12)
    try:
        await loop.run(asyncio.Event())
    finally:
        loop.rt.lock.release()
    first_launch = driver.calls.index("launch")
    assert driver.calls[first_launch + 1] == "close"  # closed before anything was navigated
    if how == "vpn":
        assert not any(c.startswith("navigate") for c in driver.calls)
    else:
        # A sleep closes the gate once; the next launch reads normally.
        assert loop.results


async def test_parser_broken_and_halts_close_the_gate(store: Store) -> None:
    from fbcollector.runner import _halt_result, set_halt

    assert (await _halt_result(store)).ok
    await store.kv_set(KV_PARSER_BROKEN, '["x"]')
    result = await _halt_result(store)
    assert not result.ok
    assert result.state is CollectorState.PAUSED
    await store.kv_delete(KV_PARSER_BROKEN)
    await set_halt(store, Halt.SELFTEST, "bad")
    assert (await _halt_result(store)).state is CollectorState.PAUSED
    from .trace_support import durable_writes, traced

    async with traced(store) as statements:
        await set_halt(store, Halt.NEEDS_LOGIN, "no session")
    assert f"kv:{KV_HALT}" in durable_writes(statements)  # a power cut keeps the halt
    assert (await _halt_result(store)).state is CollectorState.NEEDS_LOGIN


async def test_the_quarantine_is_in_the_status_and_announced_by_the_loop(
    env: Env, store: Store
) -> None:
    """Posts a replay left in quarantine are announced by the loop's
    housekeeping (once), and `fbcollector status` lists them — ids and error kinds only."""
    from fbcollector.model import Sighting, Surface
    from fbcollector.runner import STATUS_QUARANTINE, status_lines

    for n in range(STATUS_QUARANTINE + 2):
        await store.quarantine(
            Sighting(post_id=post(n), group_id=G1, surface=Surface.FEED),
            errors=["url:value_error"],
        )
    lines = await status_lines(env.settings, store, env.clock)
    head = next(i for i, line in enumerate(lines) if line.startswith("quarantine: "))
    assert lines[head].startswith(f"quarantine: {STATUS_QUARANTINE + 2} post(s)")
    listed = lines[head + 1 : head + 1 + STATUS_QUARANTINE]
    assert listed[0].startswith(f"  {G1}/{post(0)} x1 ") and listed[0].endswith(": url:value_error")
    assert lines[head + 1 + STATUS_QUARANTINE] == "  ... and 2 more"
    await run_turns(env, store, 2)
    assert [key for key, _ in env.alerts.sent].count("items_invalid:") == 1


@dataclass
class TelegramApi:
    """The Bot API's sendMessage, answered ok; notes each text."""

    texts: list[str] = field(default_factory=list)

    def __call__(self, request: Any) -> Any:
        import httpx

        self.texts.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})


async def test_a_quarantine_announcement_lost_with_the_process_is_made_at_the_next_start(
    env: Env, store: Store
) -> None:
    """Review F3 follow-up: the reader writes the announced flag (fsynced) mid-read while the
    alert waits for the end of the read; in local mode no event row is queued. A process that
    died in between kept the flag and lost the alert, so the owner was never told."""
    import httpx

    from fbcollector.alerts import AlertBot
    from fbcollector.events import KV_QUARANTINE
    from fbcollector.model import Sighting, Surface

    api = TelegramApi()
    env.ports.alerts = AlertBot(
        "1:x", 4242, clock=env.clock, ledger=store, transport=httpx.MockTransport(api)
    )
    await store.quarantine(
        Sighting(post_id=post(1), group_id=G1, surface=Surface.FEED), errors=["url:value_error"]
    )
    await store.kv_set(KV_QUARANTINE, "1", durable=True)  # announced, and then the power went
    await run_turns(env, store, 2)

    def announced() -> int:
        return sum("did not fit the wire model" in text for text in api.texts)

    assert announced() == 1
    await run_turns(env, store, 2)  # a restart after it went out: the bot's ledger (24 h) holds
    assert announced() == 1
    assert await store.kv_get(KV_QUARANTINE) is not None


async def test_holds_close_chrome_and_long_absences_alert(env: Env, store: Store) -> None:
    loop = await collector(env, store, 1)
    env.driver.launches = 1  # Chrome is running
    stop = asyncio.Event()
    await loop._hold(Hold(None, CollectorState.NOT_HOME, ("home: VPN",), True), stop)
    assert env.driver.closed == 1
    assert await store.kv_get(KV_NOT_HOME_SINCE) is not None
    await loop._note_state(Hold(None, CollectorState.CRASH_LOOP, ("5 launches",), True))
    assert any(k.startswith("crash_loop") for k in [k for k, _ in env.alerts.sent])
    assert await store.kv_get(KV_NOT_HOME_SINCE) is None
    loop.rt.lock.release()


async def test_a_crash_loop_is_one_event_while_it_lasts(env: Env, store: Store) -> None:
    kinds: list[str] = []

    async def record(kind: Any, **kw: Any) -> None:
        kinds.append(kind.value)

    loop = await collector(env, store, 1)
    loop.rt.events = record  # type: ignore[assignment]
    looping = Hold(None, CollectorState.CRASH_LOOP, ("crash_loop: 4 launches",), True)
    try:
        for _ in range(100):
            await loop._note_state(looping)
        await loop._note_state(Hold(None, CollectorState.QUIET, ("quiet hours",), True))
        await loop._note_state(looping)
    finally:
        loop.rt.lock.release()
    assert kinds == ["crash_loop", "crash_loop"]  # entered twice, not 101 times


class Bot:
    """A Telegram double for the real ``AlertBot`` (so its dedup is the real one)."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def __call__(self, request: Any) -> Any:
        import httpx

        self.texts.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True, "result": {}})


@pytest.mark.parametrize(("hour", "alerted"), [(14, True), (23, False)])
async def test_a_long_absence_alerts_once_in_the_daytime_only(
    env: Env, store: Store, hour: int, alerted: bool
) -> None:
    import httpx

    from fbcollector.alerts import AlertBot

    bot = Bot()
    env.ports.alerts = AlertBot(
        "123456789:" + "A" * 35,
        42,
        clock=env.clock,
        ledger=store,
        transport=httpx.MockTransport(bot),
    )
    local = dt.datetime.fromtimestamp(env.clock.time(), env.settings.zone)
    target = local.replace(hour=hour, minute=0) + dt.timedelta(days=1)
    away = Hold(None, CollectorState.NOT_HOME, ("home: VPN",), True)
    loop = await collector(env, store, 1)
    try:
        env.clock.advance(target.timestamp() - 13 * 3600 - env.clock.time())
        await loop._note_state(away)  # the absence starts
        env.clock.advance(13 * 3600)
        await loop._note_state(away)
        await loop._note_state(away)  # the next turn: deduplicated, not sent twice
    finally:
        loop.rt.lock.release()
    sent = [t for t in bot.texts if "NOT_HOME" in t]
    assert len(sent) == (1 if alerted else 0)


async def test_wait_or_stop_wakes_on_shutdown() -> None:
    clock = FakeClock()
    stop = asyncio.Event()
    await wait_or_stop(clock, 30, stop)
    assert clock.sleeps == [30]
    stop.set()
    await wait_or_stop(clock, 30, stop)
    assert clock.sleeps == [30]
    await wait_or_stop(clock, 0, asyncio.Event())


async def test_run_collector_holds_the_lock_and_stops(env: Env) -> None:
    store = await prepared(env)
    await store.close()
    stop = asyncio.Event()
    stop.set()
    assert await run_collector(env.settings, env.ports, observe=True, stop=stop) == 0
    from fbcollector.gates import ProfileLock

    lock = ProfileLock(paths.Layout(env.root).lock)
    lock.acquire()  # released on the way out
    lock.release()


# -- planned and unplanned launches (the crash-loop gate) ------------------------------------

START = ProcessKind.PROCESS_START


async def planned_flags(store: Store, kind: ProcessKind) -> list[int]:
    """The ``planned`` flag of every ``kind`` event on disk, oldest first."""
    cur = await store.db.execute(
        "SELECT planned FROM process_events WHERE kind=? ORDER BY id", (kind.value,)
    )
    return [int(r[0]) for r in await cur.fetchall()]


async def planned_launches(env: Env) -> list[int]:
    async with Store(paths.Layout(env.root).db, clock=env.clock) as store:
        return await planned_flags(store, CHROME)


Then = Any
"""``async (reads so far) -> Hold | None``: what happens after a read (``after_each_read``)."""


async def after_each_read(
    env: Env, store: Store, turns: int, then: Then, *, stop: asyncio.Event | None = None
) -> Collector:
    """Run the loop with ``then`` in front of the scheduler: once after every read, before the
    next plan, with the number of reads so far. A ``Hold`` it returns is that turn's answer
    (one that closes Chrome is closed by the loop's own ``_hold``). With ``stop``, the first
    crash-loop hold ends the run."""
    loop = await collector(env, store, turns)
    real_next = loop.rt.scheduler.next
    done = [0]

    async def scripted(**kwargs: Any) -> Any:
        if len(loop.results) > done[0]:
            done[0] = len(loop.results)
            hold = await then(done[0])
            if hold is not None:
                return hold
        decision = await real_next(**kwargs)
        if stop is not None and getattr(decision, "state", None) is CollectorState.CRASH_LOOP:
            stop.set()  # what the test waited for, or what it must not see go on
        return decision

    loop.rt.scheduler.next = scripted  # type: ignore[method-assign]
    try:
        await loop.run(stop or asyncio.Event())
    finally:
        loop.rt.lock.release()
    return loop


def waiting() -> Hold:
    """A wait that leaves Chrome alone."""
    return Hold(None, CollectorState.OK, ("waiting",), False)


PLANNED_CLOSES = [
    (CollectorState.QUIET, "rhythm: quiet hours"),
    (CollectorState.BREAK, "rhythm: a break"),
    (CollectorState.NOT_HOME, "sleep: the OS announced a sleep"),
]


@pytest.mark.parametrize(("state", "reason"), PLANNED_CLOSES, ids=["quiet", "break", "sleep"])
async def test_a_relaunch_after_the_loops_own_close_is_planned(
    env: Env, store: Store, state: CollectorState, reason: str
) -> None:
    async def close(reads: int) -> Hold | None:
        if reads > 1:
            return None
        env.clock.advance(full_cadence_after(env, 3600) - env.clock.time())
        return Hold(None, state, (reason,), True)

    await after_each_read(env, store, 40, close)
    assert env.driver.launches == 2
    # The first launch of a start nobody asked for, then the relaunch after a hold's close.
    assert await planned_flags(store, CHROME) == [0, 1]
    assert await planned_flags(store, START) == [0]


@pytest.mark.parametrize("asked", [True, False], ids=["planned_start", "unplanned_start"])
async def test_the_selftest_launch_is_planned_when_the_reads_launch_is(
    env: Env, store: Store, monkeypatch: pytest.MonkeyPatch, asked: bool
) -> None:
    if asked:
        await store.kv_set(KV_PLANNED_EXIT, "0")
    tests = untested_chrome(env, store, monkeypatch)
    await run_turns(env, store, 3)
    assert tests.ledger == [0 if asked else 2]  # the unplanned launches on disk
    assert [detail for _, detail in await launch_log(store)] == ["selftest", None]
    assert await planned_flags(store, CHROME) == [int(asked)] * 2


async def test_a_read_that_failed_makes_the_next_start_and_its_launch_unplanned(
    env: Env, store: Store
) -> None:
    await store.kv_set(KV_PLANNED_EXIT, "0")  # the process before stopped on a SIGTERM
    loop = await collector(env, store, 8)

    async def failing(grant: Any, **kw: Any) -> Any:
        raise RuntimeError("the reader crashed")

    loop.rt.reader.read = failing  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="the reader crashed"):
            await loop.run(asyncio.Event())
    finally:
        loop.rt.lock.release()
    assert (await planned_flags(store, START), await planned_flags(store, CHROME)) == ([1], [1])
    assert env.driver.closed == 1  # "the read failed"
    assert loop._launch_planned is False
    assert await store.kv_get(KV_PLANNED_EXIT) is None  # an exception leaves no marker
    env.clock.advance(full_cadence_after(env, 1800) - env.clock.time())
    await run_turns(env, store, 8)  # launchd's KeepAlive starts it again
    assert await planned_flags(store, START) == [1, 0]
    assert await planned_flags(store, CHROME) == [1, 0]


async def test_a_chrome_that_ended_on_its_own_makes_the_relaunch_unplanned(
    env: Env, store: Store
) -> None:
    await store.kv_set(KV_PLANNED_EXIT, "0")

    async def crash(reads: int) -> Hold | None:
        if reads > 1:
            return None
        env.driver.closed += 1  # Chrome died while parked: no close of ours
        env.clock.advance(full_cadence_after(env, 3600) - env.clock.time())
        return waiting()

    await after_each_read(env, store, 40, crash)
    assert env.driver.launches == 2
    assert await planned_flags(store, CHROME) == [1, 0]
    assert env.driver.calls.count("close") == 1  # the shutdown's, of the second Chrome


class SlowClose(FeedDriver):
    """A Chrome whose close takes until ``release`` is set."""

    def __init__(self, clock: FakeClock, **kw: Any) -> None:
        super().__init__(clock, **kw)
        self.release = asyncio.Event()

    async def close(self) -> None:
        self.calls.append("close")
        await self.release.wait()
        self.closed = self.launches


@pytest.mark.parametrize(
    ("first", "second", "planned"),
    [(True, False, False), (False, True, False), (True, True, True)],
    ids=["sleep_then_failure", "failure_then_sleep", "both_planned"],
)
async def test_overlapping_closes_are_planned_only_when_all_are(
    env: Env, store: Store, first: bool, second: bool, planned: bool
) -> None:
    """The sleep watcher's close and the read's own can overlap: one failure among them makes
    the next launch unplanned, whichever finishes first."""
    driver = SlowClose(env.clock, info=env.driver.info)
    env.ports.driver_factory = lambda _policy: driver
    loop = await collector(env, store, 1)
    loop.rt.lock.release()
    driver.launches = 1  # Chrome runs, launched by this loop
    loop._chrome_up = True
    one = asyncio.create_task(loop._close_chrome("one", planned=first))
    await asyncio.sleep(0)
    two = asyncio.create_task(loop._close_chrome("two", planned=second))
    await asyncio.sleep(0)
    driver.release.set()
    await asyncio.gather(one, two)
    assert not driver.running
    assert loop._launch_planned is planned


class FlakyLaunch(FeedDriver):
    """Chrome does not start the first time."""

    async def launch(self, *, mode: SessionMode) -> LaunchInfo:
        if "launch failed" not in self.calls:
            self.calls.append("launch failed")
            raise LaunchError("Chrome did not start")
        return await super().launch(mode=mode)


async def test_a_failed_launch_makes_the_next_one_unplanned(env: Env, store: Store) -> None:
    await store.kv_set(KV_PLANNED_EXIT, "0")
    driver = FlakyLaunch(env.clock, info=env.driver.info)
    env.ports.driver_factory = lambda _policy: driver
    await run_turns(env, store, 12)
    assert [c for c in driver.calls if c.startswith("launch")][:2] == ["launch failed", "launch"]
    assert (await planned_flags(store, CHROME))[:2] == [1, 0]


async def test_a_start_after_a_stop_someone_asked_for_is_planned(env: Env, store: Store) -> None:
    stop = asyncio.Event()

    async def sigterm(reads: int) -> Hold:
        stop.set()  # what SIGTERM, SIGINT and SIGHUP do (run_collector's handlers)
        return waiting()

    await after_each_read(env, store, 40, sigterm, stop=stop)
    assert not env.driver.running
    assert await store.kv_get(KV_PLANNED_EXIT) is not None
    env.clock.advance(full_cadence_after(env, 1800) - env.clock.time())
    await run_turns(env, store, 8)  # its successor; it ends on max_turns, not on a signal
    assert await store.kv_get(KV_PLANNED_EXIT) is None  # taken, and not written again
    assert await planned_flags(store, START) == [0, 1]
    assert await planned_flags(store, CHROME) == [0, 1]  # the first launch of a planned start
    env.clock.advance(full_cadence_after(env, 1800) - env.clock.time())
    await run_turns(env, store, 8)
    assert await planned_flags(store, START) == [0, 1, 0]
    assert await planned_flags(store, CHROME) == [0, 1, 0]


async def test_a_chrome_that_died_before_a_stop_someone_asked_for_still_counts(
    env: Env, store: Store
) -> None:
    """The stop is asked for, so the successor's start is planned; its Chrome
    had ended on its own, so the successor's first launch is not."""
    await store.kv_set(KV_PLANNED_EXIT, "0")
    stop = asyncio.Event()

    async def crash_then_sigterm(reads: int) -> Hold:
        env.driver.closed += 1  # Chrome died while parked: no close of ours
        stop.set()
        return waiting()

    await after_each_read(env, store, 40, crash_then_sigterm, stop=stop)
    assert await store.kv_get(KV_PLANNED_EXIT) == "chrome unplanned"
    env.clock.advance(full_cadence_after(env, 1800) - env.clock.time())
    await run_turns(env, store, 8)
    assert await planned_flags(store, START) == [1, 1]
    assert await planned_flags(store, CHROME) == [1, 0]


class StuckClose(FeedDriver):
    """A Chrome that does not close."""

    async def close(self) -> None:
        self.calls.append("close")
        raise LaunchError("Chrome did not exit")


async def test_a_stop_that_left_chrome_running_is_not_planned(env: Env, store: Store) -> None:
    driver = StuckClose(env.clock, info=env.driver.info)
    env.ports.driver_factory = lambda _policy: driver
    stop = asyncio.Event()

    async def sigterm(reads: int) -> Hold:
        stop.set()
        return waiting()

    await after_each_read(env, store, 40, sigterm, stop=stop)
    assert driver.running and "close" in driver.calls
    assert await store.kv_get(KV_PLANNED_EXIT) is None


async def test_the_signal_handlers_set_the_stop_event(monkeypatch: pytest.MonkeyPatch) -> None:
    import signal

    from fbcollector.runner import _install_signals

    handlers: dict[int, Any] = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", handlers.setdefault)
    stop = asyncio.Event()
    _install_signals(stop)
    assert set(handlers) == {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}
    handlers[signal.SIGTERM]()
    assert stop.is_set()


async def test_a_chrome_that_died_during_housekeeping_makes_the_relaunch_unplanned(
    env: Env, store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    """Review finding: Chrome died after the turn's first look at it, while the sink drained,
    and the relaunch went out with the planned word of the start before it."""
    caplog.set_level(logging.WARNING, logger="fbcollector")
    await store.kv_set(KV_PLANNED_EXIT, "0")
    loop = await collector(env, store, 40)
    real = loop._housekeeping
    died = [False]

    async def housekeeping() -> None:
        await real()
        if loop.results and not died[0]:
            died[0] = True
            env.driver.closed += 1  # Chrome died while parked: no close of ours
            env.clock.advance(full_cadence_after(env, 3600) - env.clock.time())

    loop._housekeeping = housekeeping  # type: ignore[method-assign]
    try:
        await loop.run(asyncio.Event())
    finally:
        loop.rt.lock.release()
    assert env.driver.launches == 2
    assert await planned_flags(store, CHROME) == [1, 0]
    assert "Chrome ended without the collector closing it" in caplog.text


@pytest.mark.parametrize("planned", [False, True], ids=["failure", "hold"])
async def test_a_read_that_closed_chrome_passes_its_word_to_the_relaunch(
    env: Env, store: Store, planned: bool
) -> None:
    """Review findings: the read's own close (a page the detectors did not pass, a browser
    failure; or a gate that closed mid-read) decides the relaunch, and an unplanned close of
    one Chrome leaves the next Chrome's planned close planned."""
    import dataclasses

    await store.kv_set(KV_PLANNED_EXIT, "0")
    loop = await collector(env, store, 40)
    real_read = loop.rt.reader.read

    async def read(grant: Any, **kw: Any) -> Any:
        result = await real_read(grant, **kw)
        if len(loop.results) == 0:
            return dataclasses.replace(
                result, close_chrome=True, planned_close=planned, reason="scripted"
            )
        return result

    loop.rt.reader.read = read  # type: ignore[method-assign]
    real_next = loop.rt.scheduler.next
    done = [0]

    async def scripted(**kwargs: Any) -> Any:
        if len(loop.results) > done[0] and done[0] < 2:
            done[0] = len(loop.results)
            env.clock.advance(full_cadence_after(env, 3600) - env.clock.time())
            if done[0] == 2:
                return Hold(None, CollectorState.QUIET, ("rhythm: quiet hours",), True)
        return await real_next(**kwargs)

    loop.rt.scheduler.next = scripted  # type: ignore[method-assign]
    try:
        await loop.run(asyncio.Event())
    finally:
        loop.rt.lock.release()
    assert env.driver.launches == 3
    assert await planned_flags(store, CHROME) == [1, int(planned), 1]


async def test_a_failure_page_relaunches_unplanned(env: Env, store: Store) -> None:
    """The reader's own verdict, end to end: a page the detectors did not recognise closes
    Chrome, and the launch after the backoff is unplanned."""
    await store.kv_set(KV_PLANNED_EXIT, "0")
    german = nav(
        FEED_URL,
        feed_document(lang="de"),
        feed_body(story(post(1), G1, 1_790_000_000)),
        html_lang="de",
    )

    class OnceGerman(FeedDriver):
        async def navigate(self, url: str) -> Step:
            if self.counter == 0:
                self.counter += 1
                self.calls.append(f"navigate {url}")
                self.last_url = url
                return german
            return await super().navigate(url)

    driver = OnceGerman(env.clock, info=env.driver.info)
    env.ports.driver_factory = lambda _policy: driver
    loop = await run_turns(env, store, 60)
    assert loop.results[0].page_state is PageState.UNKNOWN
    assert driver.launches >= 2
    assert (await planned_flags(store, CHROME))[:2] == [1, 0]


@pytest.mark.parametrize(
    ("first", "second", "planned"),
    [(True, False, False), (False, True, False), (True, True, True)],
    ids=["sleep_then_failure", "failure_then_sleep", "both_planned"],
)
async def test_a_close_after_another_close_finished_still_counts(
    env: Env, store: Store, first: bool, second: bool, planned: bool
) -> None:
    """The sleep watcher's close finished before the read's own close of a
    failure page was asked for, and the failure was dropped."""
    loop = await collector(env, store, 1)
    loop.rt.lock.release()
    env.driver.launches = 1  # Chrome runs, launched by this loop
    loop._chrome_up = True
    await loop._close_chrome("one", planned=first)
    assert not env.driver.running
    await loop._close_chrome("two", planned=second)
    assert loop._launch_planned is planned


async def test_a_successor_that_died_while_starting_took_the_marker(
    env: Env, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process that dies before its loop runs (``build`` raised) must not leave
    the marker of the stop before it to the next start, which follows a crash."""
    import fbcollector.runner as runner_module

    await store.kv_set(KV_PLANNED_EXIT, "0")

    async def broken(*args: Any, **kw: Any) -> Any:
        raise RuntimeError("build failed")

    with monkeypatch.context() as patch:
        patch.setattr(runner_module, "build", broken)
        with pytest.raises(RuntimeError, match="build failed"):
            await run_collector(env.settings, env.ports, stop=asyncio.Event())
    assert await store.kv_get(KV_PLANNED_EXIT) is None
    stop = asyncio.Event()
    stop.set()
    assert await run_collector(env.settings, env.ports, stop=stop) == 0
    assert await planned_flags(store, START) == [1, 0]


async def test_the_marker_is_taken_with_the_start_in_one_transaction(
    env: Env, store: Store
) -> None:
    """The marker's removal and the start's row are one durable commit, so a
    power cut cannot keep one and lose the other."""
    await store.kv_set(KV_PLANNED_EXIT, "0")
    await store.db.execute(
        "CREATE TRIGGER no_start BEFORE INSERT ON process_events "
        "BEGIN SELECT RAISE(ABORT, 'the disk is gone'); END"
    )
    with pytest.raises(Exception, match="the disk is gone"):
        await run_turns(env, store, 1)
    assert await store.kv_get(KV_PLANNED_EXIT) is not None


class MemberPages(FeedDriver):
    """The feed as ``FeedDriver``; a member's page of ``G1`` or ``G2`` for their backstops, and
    both in the joined-groups list for a sync: every read is clean."""

    async def navigate(self, url: str) -> Step:
        from fbcollector.detectors import JOINS_URL, group_url

        from .reader_support import group_body, group_document, joins_body, joins_document

        if url == JOINS_URL:
            self.calls.append(f"navigate {url}")
            self.last_url = url
            return nav(url, joins_document(), joins_body((G1, "One"), (G2, "Two")))
        for gid in (G1, G2):
            if url == group_url(gid):
                self.calls.append(f"navigate {url}")
                self.counter += 1
                self.last_url = url
                fresh = story(post(self.counter), gid, self.clock.time() - 120)
                return nav(url, group_document(gid), group_body(fresh))
        return await super().navigate(url)


async def relaunches(
    env: Env, store: Store, count: int, *, planned: bool, apart: float
) -> Collector:
    """Chrome closed after every read and relaunched for the next, ``count`` times, ``apart``
    seconds or more apart: by the loop's own holds (quiet hours, breaks, sleeps), or by dying
    on its own. The run stops there, or at the first crash-loop hold."""
    env.driver = MemberPages(env.clock, info=env.driver.info)
    env.ports.driver_factory = production_policy(env.driver)
    stop = asyncio.Event()

    async def then(reads: int) -> Hold:
        if reads > count:
            stop.set()
            return waiting()
        env.clock.advance(full_cadence_after(env, apart) - env.clock.time())
        if not planned:
            env.driver.closed += 1
            return waiting()
        state, reason = PLANNED_CLOSES[reads % len(PLANNED_CLOSES)]
        return Hold(None, state, (reason,), True)

    return await after_each_read(env, store, 3000, then, stop=stop)


async def test_a_day_of_planned_relaunches_is_no_crash_loop(env: Env, store: Store) -> None:
    # 15 min apart: 4 or more in some hours, which the unplanned cap (3) would refuse.
    loop = await relaunches(env, store, 12, planned=True, apart=15 * 60)
    assert not any(h.state is CollectorState.CRASH_LOOP for h in loop.holds)
    assert env.driver.launches == 13
    assert await planned_flags(store, CHROME) == [0] + [1] * 12
    times = [at for at, _ in await launch_log(store)]
    assert max(times) - min(times) < DAY  # twelve relaunches in one day
    assert most_within(times, 3600) > 3


async def test_nine_unplanned_launches_in_a_day_are_a_crash_loop(env: Env, store: Store) -> None:
    loop = await relaunches(env, store, 12, planned=False, apart=65 * 60)
    looping = [h for h in loop.holds if h.state is CollectorState.CRASH_LOOP]
    assert looping
    assert looping[0].reasons == ("crash_loop: 9 unplanned chrome launches in the last day",)
    assert env.driver.launches == 8  # the 9th is refused before Chrome starts
    assert await planned_flags(store, CHROME) == [0] * 8


# -- login ----------------------------------------------------------------------------------


class LoginDriver(FakeDriver):
    """Chrome that the owner logs into (cookies change between polls), then quits."""

    def __init__(self, sequence: Sequence[frozenset[str]]) -> None:
        super().__init__()
        self.sequence = list(sequence)
        self.polls = 0

    async def cookie_names(self, url: str = "https://www.facebook.com/") -> frozenset[str]:
        current = self.sequence[min(self.polls, len(self.sequence) - 1)]
        self.polls += 1
        return current

    async def wait_closed(self) -> None:
        if self.polls < len(self.sequence):
            await asyncio.sleep(3600)  # still logging in; the poll timeout cancels this
        self.closed = self.launches


class Service:
    def __init__(self, running: bool = True) -> None:
        self.running = running
        self.calls: list[str] = []

    def stop(self) -> bool:
        self.calls.append("stop")
        return self.running

    def start(self) -> bool:
        self.calls.append("start")
        return True


async def test_login_records_a_fresh_session_and_the_command_line(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fbcollector.runner as runner_module

    monkeypatch.setattr(runner_module, "LOGIN_POLL", 0.01)
    await pinned(env)
    driver = LoginDriver([frozenset(), frozenset({"c_user", "xs", "datr"})])
    env.ports.driver_factory = lambda _policy: driver
    service = Service()
    out = io.StringIO()
    assert await login(env.settings, env.ports, out=out, service=service) == 0  # type: ignore[arg-type]
    assert service.calls == ["stop", "start"]
    assert "pre-login checklist" in out.getvalue()
    assert "Login recorded" in out.getvalue()
    async with Store(paths.Layout(env.root).db, clock=env.clock) as store:
        assert await store.kv_get(KV_LOGIN_AT) == repr(env.clock.time())
        assert await store.kv_get(KV_CMDLINE) == driver.info.command_line
    assert driver.calls[0] == "launch"
    assert driver.modes == [SessionMode.LOGIN]  # the owner's Chrome: no tab of theirs is closed
    assert not any(c.startswith("navigate") for c in driver.calls)  # never navigates


class OwnerAtTheKeyboard(FakeContext):
    """A login across several tabs: each cookie poll finds the owner one move further.

    They open a tab by hand, click through to a popup, finish the login (2FA) in it, close the
    tab Chrome started on, and quit Chrome.
    """

    def __init__(self) -> None:
        super().__init__(cookies=frozenset())
        self.popup: FakePage | None = None
        self.own: FakePage | None = None
        self.moves = [self.open_tabs_by_hand, self.log_in_in_popup, self.close_start, self.quits]

    async def open_tabs_by_hand(self) -> None:
        self.own = await self.open_tab()
        self.popup = await self.open_tab()

    async def log_in_in_popup(self) -> None:
        self.jar |= SESSION | {"datr"}

    async def close_start(self) -> None:
        await self.pages[0].close()

    async def quits(self) -> None:
        self.quit()

    async def cookies(self, urls: list[str]) -> list[dict[str, str]]:
        jar = await super().cookies(urls)  # the profile's cookies at this poll
        if self.moves:
            await self.moves.pop(0)()
        return jar


async def test_a_login_across_several_tabs_records_login_at_and_closes_none(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    chrome_pid: subprocess.Popen[bytes],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The real driver in the login mode, over a fake Chrome the owner uses in three tabs."""
    import fbcollector.runner as runner_module
    from fbcollector.driver._patchright import PatchrightDriver
    from fbcollector.driver.launch import launch_spec

    monkeypatch.setattr(runner_module, "LOGIN_POLL", 0.01)
    caplog.set_level(logging.INFO, logger="fbcollector.driver._patchright")
    await pinned(env)
    profile = paths.Layout(env.root).profile
    chrome = OwnerAtTheKeyboard()
    playwright = FakePlaywright(chrome)

    def make(policy: Any) -> PatchrightDriver:
        assert isinstance(policy, FacebookSurfaces)
        return PatchrightDriver(
            launch_spec(profile, platform="linux"),
            policy=policy,
            clock=env.clock,
            runner=chrome_ps(profile, chrome_pid.pid),
            factory=lambda: playwright,
        )

    env.ports.driver_factory = make
    out = io.StringIO()
    assert await login(env.settings, env.ports, out=out, service=Service(False)) == 0  # type: ignore[arg-type]
    assert "Login recorded" in out.getvalue()
    async with Store(paths.Layout(env.root).db, clock=env.clock) as store:
        assert await store.kv_get(KV_LOGIN_AT) == repr(env.clock.time())
        recorded = await store.kv_get(KV_CMDLINE)
    assert recorded == "/opt/chrome --no-proxy-server --user-data-dir=<profile> about:blank"
    start, own, popup = chrome.pages
    assert (own, popup) == (chrome.own, chrome.popup)
    assert start.closed  # by the owner
    assert not own.closed
    assert not popup.closed  # the driver closed nothing of theirs
    assert "closing a tab the page opened by itself" not in caplog.messages
    assert all(page.gotos == [] for page in chrome.pages)  # and sent none of them anywhere
    assert chrome.cdp_sessions == 0
    assert chrome.moves == []
    assert chrome.closes == 1
    assert chrome_pid.poll() is None  # Chrome exited on the owner's quit; no SIGKILL


async def pinned(env: Env) -> None:
    """A fresh root with only the home pin (the RUNBOOK's order: pin-home, then login)."""
    layout = paths.prepare(env.root, platform="linux")
    async with Store(layout.db, clock=env.clock) as store:
        await store.kv_set(KV_HOME_PIN, HOME_PIN.to_json())


@pytest.mark.parametrize("where", ["unpinned", "vpn", "battery", "zone"])
async def test_login_refuses_away_from_the_pinned_home(env: Env, where: str) -> None:
    from dataclasses import replace as dc_replace

    from fbcollector.runner import LOGIN_REFUSED

    if where != "unpinned":
        await pinned(env)
    probes = FakeProbes()
    if where == "vpn":
        probes.facts = dc_replace(probes.facts, vpn_routes=("0/1 utun4",))
    elif where == "battery":
        probes.source = PowerSource.BATTERY
    elif where == "zone":
        probes.zone = "UTC"
    env.ports.probes = probes
    driver = LoginDriver([frozenset()])
    env.ports.driver_factory = lambda _policy: driver
    service = Service()
    out = io.StringIO()
    code = await login(env.settings, env.ports, out=out, service=service)  # type: ignore[arg-type]
    assert code == LOGIN_REFUSED
    assert "Refusing" in out.getvalue()
    assert driver.launches == 0
    assert service.calls == ["stop", "start"]  # the collector's own gates keep it waiting


async def test_login_without_a_new_session_keeps_login_at(env: Env) -> None:
    store = await prepared(env)
    await store.kv_set_json(KV_HALT, {"kind": Halt.COMMAND_LINE, "reason": "x"})
    before = await store.kv_get(KV_LOGIN_AT)
    await store.close()
    driver = LoginDriver([frozenset({"c_user", "xs"})])
    env.ports.driver_factory = lambda _policy: driver
    out = io.StringIO()
    await login(env.settings, env.ports, out=out, service=Service(running=False))  # type: ignore[arg-type]
    async with Store(paths.Layout(env.root).db, clock=env.clock) as store:
        assert await store.kv_get(KV_LOGIN_AT) == before
        assert await store.kv_get(KV_HALT) is None  # re-recorded: the halt is lifted
    assert "already logged in" in out.getvalue()
    driver = LoginDriver([frozenset()])
    env.ports.driver_factory = lambda _policy: driver
    out = io.StringIO()
    await login(env.settings, env.ports, out=out, service=Service(False), record_login=True)  # type: ignore[arg-type]
    assert "Login recorded" in out.getvalue()


async def test_login_refuses_while_the_collector_runs(env: Env) -> None:
    from fbcollector.gates import ProfileLock

    layout = paths.prepare(env.root, platform="linux")
    held = ProfileLock(layout.lock)
    held.acquire()
    try:
        service = Service()
        out = io.StringIO()
        assert await login(env.settings, env.ports, out=out, service=service) == 4  # type: ignore[arg-type]
        assert service.calls == ["stop", "start"]
    finally:
        held.release()


def test_service_manager_commands() -> None:
    calls: list[list[str]] = []

    def runner(codes: dict[str, int]) -> Any:
        def run(args: Sequence[str]) -> CommandResult:
            calls.append(list(args))
            return CommandResult(codes.get(args[1] if args[0] != "systemctl" else args[2], 0))

        return run

    mac = ServiceManager("darwin", runner({}), uid=501, home=Path("/Users/x"))
    assert mac.stop()
    assert calls[-1] == ["/bin/launchctl", "bootout", "gui/501/com.ddiver.fbcollector"]
    assert mac.start()
    assert calls[-1][:3] == ["/bin/launchctl", "bootstrap", "gui/501"]
    assert calls[-1][3].endswith("LaunchAgents/com.ddiver.fbcollector.plist")
    not_loaded = ServiceManager("darwin", runner({"print": 113}), uid=501)
    assert not not_loaded.stop()
    linux = ServiceManager("linux", runner({}), uid=1000)
    assert linux.stop()
    assert calls[-1] == ["systemctl", "--user", "stop", "fbcollector.service"]
    assert linux.start()
    inactive = ServiceManager("linux", runner({"is-active": 3}), uid=1000)
    assert not inactive.stop()


def test_small_helpers() -> None:
    assert check_command_line(None, "x") is not None
    assert check_command_line("a --b", "a --b") is None
    assert "new: --c" in (check_command_line("a --b", "a --b --c") or "")
    now = 1_790_000_000.0
    assert parse_since("6h", now) == now - 6 * 3600
    assert parse_since("2d", now) == now - 2 * 86400
    assert parse_since("30m", now) == now - 1800
    assert (
        parse_since("2026-09-24T12:00:00+00:00", now)
        == dt.datetime(2026, 9, 24, 12, tzinfo=dt.UTC).timestamp()
    )
    assert parse_since("2026-09-24", now) == dt.datetime(2026, 9, 24, tzinfo=dt.UTC).timestamp()
    with pytest.raises(ValueError):
        parse_since("soon", now)

    class Tty:
        def isatty(self) -> bool:
            return True

    assert require_tty(Tty())
    assert not require_tty(io.StringIO())
    assert not require_tty(object())


# -- the CLI --------------------------------------------------------------------------------


@pytest.fixture
def cli_env(env: Env, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    import os

    for key in [k for k in os.environ if k.upper().startswith("FBC_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("FBC_ROOT", str(env.root))
    monkeypatch.setenv("FBC_TIMEZONE", ZONE)
    yield env


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def run_cli(env: Env, *argv: str, tty: bool = True) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(list(argv), ports=env.ports, stdin=Tty() if tty else io.StringIO(), out=out)
    return code, out.getvalue()


async def seeded(env: Env) -> None:
    store = await prepared(env)
    await store.close()


def test_cli_config_errors(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    code, out = run_cli(cli_env, "check-config")
    assert code == cli.EXIT_CONFIG
    assert "FBC_ALERT_BOT_TOKEN" in out
    code, out = run_cli(cli_env, "run")
    assert code == cli.EXIT_CONFIG
    monkeypatch.setenv("FBC_TIMEZONE", "Mars/Olympus")
    code, out = run_cli(cli_env, "status")
    assert code == cli.EXIT_CONFIG
    assert "FBC_TIMEZONE" in out
    assert "Mars" not in out.split(":", 1)[0]


def test_cli_check_config_ok(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FBC_ALERT_BOT_TOKEN", "123456789:" + "A" * 35)
    monkeypatch.setenv("FBC_ALERT_OWNER_ID", "42")
    code, out = run_cli(cli_env, "check-config")
    assert code == cli.EXIT_OK
    assert "local (phase 0)" in out


UPLINK_TOKEN = "u" * 29 + "-Secret-41"
"""A made-up ingest token, 39 characters."""


def test_cli_uplink_in_local_mode(cli_env: Env) -> None:
    asyncio.run(seeded(cli_env))
    code, out = run_cli(cli_env, "uplink")
    assert code == cli.EXIT_OK
    assert out.splitlines() == [
        "mode: local (phase 0)",
        "brain: none configured (FBC_INGEST_URL unset)",
        "token: unset",
        "collector id: home-1 (the default)",
        "lease: no answer from the brain yet",
        "last control answer: never",
        "last cycle: none recorded",
        "waiting: 0 item(s) in the outbox, 0 event(s)",
        "groups snapshot: none waiting; groups introduced to the brain: not yet",
        (
            "refused by the brain: control 0, groups 0, events 0, items 0, status 0; "
            "items rejected 0, events rejected 0"
        ),
    ]


def test_cli_uplink_says_how_the_link_to_the_brain_is_without_ids_or_the_token(
    cli_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator door's `uplink`: what the switch to the brain needs checked, and nothing
    that may not leave the collector's account (no group or post id, no text, no token)."""
    from fbcollector.control import KV_CONTROL, ControlState
    from fbcollector.store import KV_OUTBOX_REJECTED, KV_REFUSED
    from fbcollector.uplink import KV_EVENTS_REJECTED, KV_INTRODUCED, KV_LAST, KV_SNAPSHOT
    from fbwire.models import EventKind

    monkeypatch.setenv("FBC_INGEST_URL", "http://127.0.0.1:8781")
    monkeypatch.setenv("FBC_INGEST_SOCKET", "/Users/fbc/.ssh/fbtunnel.sock")
    monkeypatch.setenv("FBC_INGEST_TOKEN", UPLINK_TOKEN)
    monkeypatch.setenv("FBC_COLLECTOR_ID", "mac-1")
    now = cli_env.clock.time()

    async def seed_uplink(lease_holder: str) -> None:
        store = await prepared(cli_env)
        try:
            state = ControlState(
                lease_granted=True,
                lease_holder=lease_holder,
                lease_generation=3,
                lease_valid_for=6 * 3600,
                lease_at=now - 600,
                polled_at=now - 90,
            )
            await store.kv_set(KV_CONTROL, state.to_json())
            await store.kv_set_json(
                KV_LAST,
                {
                    "at": now - 30,
                    "ok": False,
                    "reason": "transport: ConnectError\x1b[31m",
                    "down_since": now - 900,
                },
            )
            await store.enqueue_event(EventKind.CRASH_LOOP, collector_id="mac-1")
            await store.kv_set(KV_SNAPSHOT, "{}")
            await store.kv_incr(KV_REFUSED + "items", 2)
            await store.kv_incr(KV_OUTBOX_REJECTED, 1)
            await store.kv_incr(KV_EVENTS_REJECTED, 3)
        finally:
            await store.close()

    asyncio.run(seed_uplink("mac-1"))
    code, out = run_cli(cli_env, "uplink")
    assert code == cli.EXIT_OK
    lines = out.splitlines()
    assert lines[:4] == [
        "mode: uplink",
        "brain: 127.0.0.1:8781 through the socket /Users/fbc/.ssh/fbtunnel.sock",
        "token: set",
        "collector id: mac-1",
    ]
    assert lines[4] == "lease: held by mac-1, generation 3, granted 10 min ago, lapses in 5.8 h"
    assert lines[5].startswith("last control answer: 20") and lines[5].endswith(" (90 s ago)")
    assert (
        lines[6].startswith("last cycle: ")
        and "(30 s ago), failed: transport: ConnectError?[31m" in lines[6]
    )
    assert lines[6].endswith("; the brain not usable since " + lines[6].split("since ")[1])
    assert "(15 min ago)" in lines[6]
    assert lines[7:] == [
        "waiting: 0 item(s) in the outbox, 1 event(s)",
        "groups snapshot: waiting; groups introduced to the brain: not yet",
        (
            "refused by the brain: control 0, groups 0, events 0, items 2, status 0; "
            "items rejected 1, events rejected 3"
        ),
    ]
    assert UPLINK_TOKEN not in out
    assert str(len(UPLINK_TOKEN)) not in out
    for group_id in (G1, G2):
        assert group_id not in out

    asyncio.run(seed_uplink("laptop-1"))
    lease = run_cli(cli_env, "uplink")[1].splitlines()[4]
    assert lease == "lease: not held (holder: laptop-1)"

    async def lapsed_and_answering() -> None:
        async with Store(paths.Layout(cli_env.root).db, clock=cli_env.clock) as store:
            state = ControlState.from_json(await store.kv_get(KV_CONTROL))
            await store.kv_set(
                KV_CONTROL,
                ControlState(
                    lease_granted=True,
                    lease_holder="mac-1",
                    lease_generation=4,
                    lease_valid_for=600,
                    lease_at=now - 700,
                    polled_at=state.polled_at,
                ).to_json(),
            )
            await store.kv_set_json(KV_LAST, {"at": now, "ok": True, "reason": None})
            await store.kv_set(KV_INTRODUCED, "1")

    asyncio.run(lapsed_and_answering())
    lines = run_cli(cli_env, "uplink")[1].splitlines()
    assert lines[4] == "lease: held by mac-1, generation 4, granted 12 min ago, LAPSED"
    assert lines[6].endswith("(0 s ago), ok")
    assert lines[8] == "groups snapshot: waiting; groups introduced to the brain: yes"


def test_cli_status_pause_and_resume(cli_env: Env) -> None:
    asyncio.run(seeded(cli_env))
    code, out = run_cli(cli_env, "status")
    assert code == 0
    assert "groups: 2 known, 2 monitored" in out
    assert run_cli(cli_env, "pause", tty=False)[0] == cli.EXIT_NO_TTY
    assert run_cli(cli_env, "resume", "1", tty=False)[0] == cli.EXIT_NO_TTY
    code, out = run_cli(cli_env, "pause")
    assert (code, "Paused" in out) == (0, True)
    assert "paused by the owner" in run_cli(cli_env, "status")[1]

    async def flags() -> None:
        async with Store(paths.Layout(cli_env.root).db, clock=cli_env.clock) as store:
            await store.kv_set(KV_PARSER_BROKEN, '["x"]')
            await store.kv_set(KV_BACKSTOP_ONLY, "1")
            await store.kv_set_json(KV_HALT, {"kind": Halt.SELFTEST, "reason": "x"})
            await store.open_block(state=PageState.BLOCKED, reason="test", until=None, rung=0)

    asyncio.run(flags())
    status = run_cli(cli_env, "status")[1]
    assert "parser broken" in status
    assert "backstop-only" in status
    assert "block #1" in status
    code, _ = run_cli(cli_env, "pause", "--off", "--feed-check")
    assert code == 0

    async def check() -> None:
        async with Store(paths.Layout(cli_env.root).db, clock=cli_env.clock) as store:
            assert await store.kv_get(KV_PARSER_BROKEN) is None
            assert await store.kv_get(KV_BACKSTOP_ONLY) is None
            assert await store.kv_get(KV_HALT) is None

    asyncio.run(check())
    code, out = run_cli(cli_env, "resume", "1")
    assert code == cli.EXIT_ERROR
    assert "permanent" in out  # rung without an until: only --replanned lifts it
    code, out = run_cli(cli_env, "resume", "1", "--replanned")
    assert code == 0
    assert "accepted" in out


def test_cli_pin_home(cli_env: Env) -> None:
    code, out = run_cli(cli_env, "pin-home")
    assert code == 0
    assert "Home pinned" in out
    cli_env.ports.probes = FakeProbes(facts=FakeProbes().facts.__class__(gateway="1.1.1.1"))
    code, out = run_cli(cli_env, "pin-home")
    assert code == cli.EXIT_ERROR
    assert "cannot pin home" in out


def test_cli_dialogs(cli_env: Env) -> None:
    """The dialog texts of the last saved steps, newest last, for an unrecognised page."""
    from fbcollector.model import DomFacts

    asyncio.run(seeded(cli_env))
    layout = paths.Layout(cli_env.root)
    code, out = run_cli(cli_env, "dialogs")
    assert (code, out) == (0, "No saved steps.\n")
    now = cli_env.clock.time()
    for n, dialogs in enumerate(((), ("",), ("Turn on notifications? Not now Allow", ""))):
        step = Step(
            final_url=f"{FEED_URL}&x=secret",
            status=200,
            dom=DomFacts(dialog_texts=dialogs),
        )
        save_step(
            layout.raw,
            read_id=n + 1,
            index=0,
            surface=_feed(),
            group_id=None,
            at=now + n,
            step=step,
        )
    bad = layout.raw / f"0000000001-r00000009-s00{STEP_SUFFIX}"
    bad.write_bytes(b"x")  # sorts first
    bad.chmod(0o600)
    code, out = run_cli(cli_env, "dialogs", "--last", "2")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].endswith("read 2 step 0 feed /: 1 dialog(s)")
    assert lines[1:] == [
        "  [1] (empty: recognised) (empty)",
        lines[2],
        "  [1] (unrecognised) Turn on notifications? Not now Allow",
        "  [2] (empty: recognised) (empty)",
    ]
    assert lines[2].endswith("read 3 step 0 feed /: 2 dialog(s)")
    assert "secret" not in out  # the path only, never the query
    code, out = run_cli(cli_env, "dialogs", "--last", "20")
    assert out.splitlines()[0] == f"0000000001-r00000009-s00{STEP_SUFFIX}: unreadable"
    assert "read 1 step 0 feed /: 0 dialog(s)" in out


def test_cli_dialog_labels_for_the_notifications_panel() -> None:
    """A panel with an unread login notice passes, counted for the owner; trouble does not."""
    head, foot = "Notifications All Unread New See all", "See previous notifications"
    login = "Unread We noticed a new login from a device or location you don't usually use. 6d"
    assert cli._dialog_label(f"{head} {login} {foot}") == (
        "the notifications panel: recognised; unread notices for the owner: 1"
    )
    assert cli._dialog_label(f"{head} Ana liked your post. 1h {foot}") == (
        "the notifications panel: recognised"
    )
    assert cli._dialog_label(f"{head} Unread Your account is locked. 1h {foot}") == (
        "the notifications panel with an unread account notice ('account is locked'): unrecognised"
    )


def test_cli_replay_report_upgrade(cli_env: Env, tmp_path: Path) -> None:
    asyncio.run(seeded(cli_env))
    layout = paths.Layout(cli_env.root)
    now = cli_env.clock.time()
    fixed = story(post(1), G1, now - 60)
    step = nav(FEED_URL, feed_document(), feed_body(fixed))
    save_step(layout.raw, read_id=1, index=0, surface=_feed(), group_id=None, at=now, step=step)
    save_quarantine(
        layout.raw,
        read_id=1,
        index=1,
        at=now,
        post_id=post(2),
        raw=json.dumps(story(post(2), G1, now - 30)).encode(),
        surface=_feed(),
        group_id=None,
    )
    (layout.raw / "quarantine-0000000009-bad.json.gz").write_bytes(b"x")

    async def defer() -> None:
        from fbcollector.model import DeferredReason

        async with Store(layout.db, clock=cli_env.clock) as store:
            await store.defer(post(1), DeferredReason.NO_TEXT, group_id=G1)

    asyncio.run(defer())
    code, out = run_cli(cli_env, "replay", "--since", "1h")
    assert code == 0
    assert "2 queued" in out
    code, out = run_cli(cli_env, "replay", "--since", "1h")
    assert "0 queued" in out  # never re-sends
    assert run_cli(cli_env, "replay", "--since", "later")[0] == cli.EXIT_USAGE
    target = tmp_path / "report.md"
    code, out = run_cli(cli_env, "report", "--out", str(target))
    assert code == 0
    assert target.read_text().startswith("# Phase-0 report")
    code, out = run_cli(cli_env, "report", "--since", "1d")
    assert "## Account health" in out
    assert "## Parser diagnostics (structure only)" in out
    assert "Saved steps read: 1 of 1" in out  # the collector's own raw/ directory
    assert G1 in out
    code, out = run_cli(cli_env, "report", "--diagnostics-only")
    assert code == 0
    assert out.startswith("# Parser diagnostics")
    assert "Saved steps read: 1 of 1" in out
    assert G1 not in out
    assert run_cli(cli_env, "upgrade-chrome")[0] == cli.EXIT_USAGE
    cli_env.ports.platform = "darwin"
    assert "Not on macOS" in run_cli(cli_env, "upgrade-chrome")[1]


def _feed() -> Any:
    from fbcollector.model import Surface

    return Surface.FEED


class CliSelfTests:
    """Stands in for the CLI's ``selftest``: notes the launches on disk when its Chrome starts."""

    def __init__(self, env: Env, *, ok: bool = True) -> None:
        self.env = env
        self.ok = ok
        self.ledger: list[int] = []

    async def __call__(self, factory: Any) -> SelfTestReport:
        async with Store(paths.Layout(self.env.root).db, clock=self.env.clock) as store:
            self.ledger.append(await store.process_events_since(DAY, kinds=[CHROME], planned=None))
        return SelfTestReport((("webdriver_false", self.ok),), 153)


def cli_selftests(env: Env, monkeypatch: pytest.MonkeyPatch, *, ok: bool = True) -> CliSelfTests:
    import fbcollector.__main__ as main_module

    env.ports.local_driver_factory = lambda port: FakeDriver()
    tests = CliSelfTests(env, ok=ok)
    monkeypatch.setattr(main_module, "selftest", tests)
    return tests


async def with_store(env: Env, change: Any) -> None:
    async with Store(paths.Layout(env.root).db, clock=env.clock) as store:
        await change(env, store)


def test_cli_selftest(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    tests = cli_selftests(cli_env, monkeypatch)
    code, out = run_cli(cli_env, "selftest")
    assert code == cli.EXIT_ERROR  # not pinned yet: the profile's Chrome stays shut
    assert "Refusing" in out
    assert "home is not pinned" in out
    assert tests.ledger == []
    asyncio.run(seeded(cli_env))
    code, out = run_cli(cli_env, "selftest")
    assert (code, "passed" in out) == (0, True)
    assert tests.ledger == [1]  # its launch was recorded before its Chrome started
    assert asyncio.run(planned_launches(cli_env)) == [1]  # a human's command: planned
    tests.ok = False
    code, out = run_cli(cli_env, "selftest")
    assert code == cli.EXIT_ERROR
    assert "FAIL webdriver_false" in out
    assert tests.ledger == [1, 2]


@pytest.mark.parametrize(
    ("gate", "reason"),
    [
        ("crash_loop", "crash_loop: 4 unplanned chrome launches in the last hour"),
        ("block", "block: block 1"),
        ("backoff", "block: backing off"),
        ("control", "control: paused by the owner"),
        ("power", "power: on battery"),
        ("home", "home: VPN routes"),
    ],
)
def test_cli_selftest_respects_the_crash_loop_cap_blocks_pauses_and_home(
    cli_env: Env, monkeypatch: pytest.MonkeyPatch, gate: str, reason: str
) -> None:
    asyncio.run(seeded(cli_env))
    asyncio.run(with_store(cli_env, lambda env, store: _close_gate(env, store, gate)))
    tests = cli_selftests(cli_env, monkeypatch)
    code, out = run_cli(cli_env, "selftest")
    assert code == cli.EXIT_ERROR
    assert "Refusing to start the account's Chrome now" in out
    assert reason in out
    assert tests.ledger == []

    async def launches() -> int:
        async with Store(paths.Layout(cli_env.root).db, clock=cli_env.clock) as store:
            return await store.process_events_since(DAY, kinds=[CHROME], planned=None)

    assert asyncio.run(launches()) == (4 if gate == "crash_loop" else 0)  # none recorded


def test_cli_selftest_needs_the_lease_with_a_brain(
    cli_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(seeded(cli_env))
    monkeypatch.setenv("FBC_INGEST_URL", "http://127.0.0.1:18781")
    monkeypatch.setenv("FBC_INGEST_TOKEN", "t" * 32)
    tests = cli_selftests(cli_env, monkeypatch)
    code, out = run_cli(cli_env, "selftest")
    assert code == cli.EXIT_ERROR
    assert "control: no lease" in out
    assert tests.ledger == []


async def _selftest_halt(env: Env, store: Store) -> None:
    await store.kv_set_json(KV_HALT, {"kind": Halt.SELFTEST, "reason": "Chrome 160 failed"})


async def _no_login(env: Env, store: Store) -> None:
    await store.kv_delete(KV_LOGIN_AT)


@pytest.mark.parametrize(
    "state",
    [_no_login, _fresh_login, _quiet, _selftest_halt, _parser_broken],
    ids=lambda state: state.__name__.strip("_"),
)
def test_cli_selftest_runs_before_the_login_in_quiet_hours_and_under_a_halt(
    cli_env: Env, monkeypatch: pytest.MonkeyPatch, state: Any
) -> None:
    """The exempt gates (``SELFTEST_EXEMPT``): the RUNBOOK runs it before the login, a Linux
    upgrade in quiet hours, and a ``SELFTEST`` halt asks for it."""
    asyncio.run(seeded(cli_env))
    asyncio.run(with_store(cli_env, state))
    tests = cli_selftests(cli_env, monkeypatch)
    code, out = run_cli(cli_env, "selftest")
    assert (code, out.strip().splitlines()[-1]) == (0, "self-test passed")
    assert tests.ledger == [1]


async def test_the_selftest_door_is_the_production_gates_but_the_exempt_ones(
    env: Env, store: Store
) -> None:
    from fbcollector.gates import ProfileLock
    from fbcollector.runner import SELFTEST_EXEMPT, selftest_gates

    assert frozenset({"warmup", "rhythm", "halt"}) == SELFTEST_EXEMPT
    lock = ProfileLock(paths.Layout(env.root).lock)
    with lock:
        door = await selftest_gates(env.settings, store, env.ports, lock=lock)
        assert door.names == tuple(n for n in GATE_ORDER if n not in SELFTEST_EXEMPT)
        assert (await door.evaluate(GateContext(Activity.LAUNCH))).ok
    report = await door.evaluate(GateContext(Activity.LAUNCH))
    assert report.reasons == ("lock: the profile lock is not held",)


def test_cli_login_and_run_paths(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    import fbcollector.__main__ as main_module

    async def fake_login(*args: Any, **kw: Any) -> int:
        return 0

    async def fake_run(*args: Any, **kw: Any) -> int:
        return 0

    monkeypatch.setattr(main_module, "login", fake_login)
    monkeypatch.setattr(main_module, "run_collector", fake_run)
    assert run_cli(cli_env, "login")[0] == 0
    monkeypatch.setenv("FBC_ALERT_BOT_TOKEN", "123456789:" + "A" * 35)
    monkeypatch.setenv("FBC_ALERT_OWNER_ID", "42")
    assert run_cli(cli_env, "run", "--observe")[0] == 0


def test_cli_reports_an_unsafe_root_and_a_locked_profile(
    cli_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fbcollector.__main__ as main_module
    from fbcollector.gates import ProfileLockedError

    asyncio.run(seeded(cli_env))
    cli_env.root.chmod(0o755)
    code, out = run_cli(cli_env, "report")
    assert code == cli.EXIT_CONFIG
    assert "not safe" in out
    cli_env.root.chmod(0o700)

    async def locked(*args: Any, **kw: Any) -> int:
        raise ProfileLockedError("held")

    monkeypatch.setattr(main_module, "login", locked)
    assert run_cli(cli_env, "login")[0] == cli.EXIT_LOCKED


def test_json_logging() -> None:
    import logging

    formatter = cli.JsonFormatter()
    record = logging.LogRecord("fbcollector", logging.INFO, __file__, 1, "hello %s", ("x",), None)
    assert json.loads(formatter.format(record))["msg"] == "hello x"

    record.exc_info = (ValueError, ValueError("boom"), None)
    assert "boom" in json.loads(formatter.format(record))["exc"]
    cli.configure_logging("INFO", "json")
    cli.configure_logging("DEBUG", "text")
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


# -- phase 0: groups confirmed by hand ------------------------------------------------------


class GroupsDriver(FeedDriver):
    """The feed as ``FeedDriver``; a group page (a member's, with one fresh post) otherwise."""

    async def navigate(self, url: str) -> Step:
        from fbcollector.detectors import group_url

        from .reader_support import group_body, group_document

        for gid in CONFIRMED:
            if url == group_url(gid):
                self.calls.append(f"navigate {url}")
                self.counter += 1
                self.last_url = url
                fresh = story(post(self.counter), gid, self.clock.time() - 120)
                return nav(url, group_document(gid), group_body(fresh))
        return await super().navigate(url)


CONFIRMED = ("390000000000001", "150000000000002")


async def test_confirmed_groups_are_backstopped_before_any_sync(env: Env) -> None:
    """Phase 0 with no complete sync ever: the owner's word opens the groups, their pages
    prove it, and their posts become items (the joins page is never needed)."""
    from fbcollector.confirm import load_confirmations
    from fbcollector.detectors import group_url

    driver = GroupsDriver(env.clock, info=env.driver.info)
    env.ports.driver_factory = production_policy(driver)
    layout = paths.prepare(env.root, platform="linux")
    (env.root / "monitored.toml").write_text(
        "".join(f'[[group]]\nid = "{g}"\nconfirmed_member = true\n' for g in CONFIRMED)
    )
    store = Store(layout.db, clock=env.clock)
    await store.open()
    try:
        await store.kv_set(KV_SEED, SEED)
        await store.kv_set(KV_HOME_PIN, HOME_PIN.to_json())
        await store.kv_set(KV_LOGIN_AT, repr(env.clock.time() - 20 * 86400))
        await store.kv_set(KV_CMDLINE, CMDLINE)
        assert await store.list_groups() == []
        await run_turns(env, store, 120)
        opened = {g for g in CONFIRMED if f"navigate {group_url(g)}" in driver.calls}
        assert opened == set(CONFIRMED)
        records = await load_confirmations(store)
        assert all(records[g].verified_at is not None for g in CONFIRMED)
        assert await store.monitored_ids() == set(CONFIRMED)
        items = [json.loads(line) for line in (env.root / "items.jsonl").read_text().splitlines()]
        assert {i["group_id"] for i in items if i["via"] == "backstop"} == set(CONFIRMED)
        assert not any("groups/joins" in call for call in driver.calls)
    finally:
        await store.close()


# -- the OS sleep hook ----------------------------------------------------------------------


def start_announcer(notice: Any) -> Any:
    """The OS announcing a sleep from the hook's own thread; waits until it is pending."""
    import threading
    import time as real_time

    outcome: dict[str, bool] = {}
    thread = threading.Thread(
        target=lambda: outcome.setdefault("closed", notice.will_sleep(5.0)), daemon=True
    )
    thread.start()
    end = real_time.monotonic() + 5
    while not notice.pending:
        assert real_time.monotonic() < end
        real_time.sleep(0.001)
    return thread, outcome


class SleepyDriver(FeedDriver):
    """The OS announces a sleep while the ``announce_on``-th navigation is in flight; the step
    only comes back once Chrome is being closed, as a torn-down tab with no status."""

    def __init__(self, clock: FakeClock, **kw: Any) -> None:
        super().__init__(clock, **kw)
        self.announce_on = 0
        self.notice: Any = None
        self.announced: Any = None
        self.closing = asyncio.Event()

    async def navigate(self, url: str) -> Step:
        if self.counter + 1 != self.announce_on:
            return await super().navigate(url)
        self.calls.append(f"navigate {url}")
        self.counter += 1
        self.announced = start_announcer(self.notice)
        await asyncio.wait_for(self.closing.wait(), 5)
        return nav(url, None, status=None, posinset=0)

    async def close(self) -> None:
        self.closing.set()
        await super().close()


class RecordingHook:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self, notice: Any) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")


async def test_a_sleep_announced_mid_read_aborts_it_without_a_verdict(
    env: Env, store: Store
) -> None:
    driver = SleepyDriver(env.clock, info=env.driver.info)
    driver.announce_on = 2
    env.ports.driver_factory = production_policy(driver)
    hook = RecordingHook()
    env.ports.sleep_hook = hook
    loop = await collector(env, store, 12)
    loop.rt.sleep._pending_max = 86400.0  # no lapse within the test's turns: only a wake ends it
    driver.notice = loop.rt.sleep
    try:
        await loop.run(asyncio.Event())
    finally:
        loop.rt.lock.release()
    assert hook.calls == ["start", "stop"]
    thread, outcome = driver.announced
    thread.join(5)
    assert outcome == {"closed": True}  # the OS was let go once Chrome was closed
    aborted = [r for r in loop.results if r.reason == "the machine is going to sleep"]
    assert len(aborted) == 1
    assert aborted[0].outcome is ReadOutcome.ABORTED
    assert await store.current_block() is None  # a torn-down tab is not a warning
    ladder = await store.kv_get_json("ladder")
    assert not (ladder or {}).get("backoff_until")
    navigations = [c for c in driver.calls if c.startswith("navigate")]
    assert len(navigations) == 2  # nothing more while the sleep is pending
    assert driver.launches == driver.closed == 1
    # Still pending (no wake yet): every gate pass stays shut, whatever the planner wants.
    report = await loop.rt.gates.evaluate(GateContext(Activity.LAUNCH))
    assert not report.ok
    assert report.reasons[0] == "sleep: the OS announced a sleep"


class SlowCloseDriver(FeedDriver):
    """Chrome that takes a while to exit, as the real one does: ``close`` tears the tab down at
    once — the navigation in flight fails there and then — and only later has Chrome gone.

    ``running_while_closing`` picks what ``running`` says meanwhile: ``True`` is the driver's
    contract now, ``False`` the Patchright driver before the fix (not running from the close's
    first line), which the loop must survive too.
    """

    def __init__(self, clock: FakeClock, *, running_while_closing: bool, **kw: Any) -> None:
        super().__init__(clock, **kw)
        self.running_while_closing = running_while_closing
        self.notice: Any = None
        self.announced: Any = None
        self.torn = asyncio.Event()
        self.closing: asyncio.Event | None = None
        self.cancelled = 0
        self.order: list[str] = []

    @property
    def running(self) -> bool:
        if self.closing is not None:
            return self.running_while_closing
        return self.launches > self.closed

    async def navigate(self, url: str) -> Step:
        if self.counter != 1:  # the second read's navigation is in flight when the OS asks
            return await super().navigate(url)
        self.calls.append(f"navigate {url}")
        self.counter += 1
        self.announced = start_announcer(self.notice)
        await asyncio.wait_for(self.torn.wait(), 5)
        raise RuntimeError("Target page, context or browser has been closed")

    async def close(self) -> None:
        if self.closing is not None:  # a close under way: wait for it, as the real driver does
            await self.closing.wait()
            return
        self.calls.append("close")
        done = self.closing = asyncio.Event()
        self.torn.set()
        try:
            await asyncio.sleep(0.2)  # Chrome exiting, well after the step in flight failed
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.closing = None
            done.set()
        self.closed += 1
        self.order.append("chrome exited")


@pytest.mark.parametrize("running_while_closing", [True, False])
async def test_the_os_may_sleep_only_once_chrome_has_exited(
    env: Env, store: Store, running_while_closing: bool
) -> None:
    driver = SlowCloseDriver(
        env.clock, running_while_closing=running_while_closing, info=env.driver.info
    )
    env.ports.driver_factory = production_policy(driver)
    env.ports.sleep_hook = RecordingHook()
    loop = await collector(env, store, 6)
    notice = loop.rt.sleep
    notice._pending_max = 86400.0
    driver.notice = notice
    let_go = notice.ready

    def ready() -> None:
        driver.order.append("sleep let go")
        let_go()

    notice.ready = ready  # type: ignore[method-assign]
    try:
        await loop.run(asyncio.Event())
    finally:
        loop.rt.lock.release()
    thread, outcome = driver.announced
    thread.join(5)
    assert outcome == {"closed": True}
    assert driver.cancelled == 0  # the watcher's close ran to its end
    assert driver.calls.count("close") == 1
    assert driver.order[0] == "chrome exited"
    assert "sleep let go" in driver.order
    aborted = [r for r in loop.results if r.reason == "the machine is going to sleep"]
    assert len(aborted) == 1
    assert await store.current_block() is None
    assert not ((await store.kv_get_json("ladder")) or {}).get("backoff_until")


class LateAnnouncementDriver(FeedDriver):
    """The OS announces the sleep just after the second navigation came back whole; while the
    reader reads the step's cookies, the watcher closes Chrome, and a closed Chrome's cookie
    read gives nothing (``empty``, the driver before the fix) or raises (the driver now)."""

    def __init__(self, clock: FakeClock, *, closed_cookies: str, **kw: Any) -> None:
        super().__init__(clock, **kw)
        self.closed_cookies = closed_cookies
        self.notice: Any = None
        self.announced: Any = None

    async def navigate(self, url: str) -> Step:
        step = await super().navigate(url)
        if self.counter == 2:
            self.announced = start_announcer(self.notice)
        return step

    async def cookie_names(self, url: str = "https://www.facebook.com/") -> frozenset[str]:
        await asyncio.sleep(0.05)  # the watcher runs meanwhile and closes Chrome
        if self.running:
            return self.cookies
        if self.closed_cookies == "empty":
            return frozenset()
        raise LaunchError("Chrome is not running")


@pytest.mark.parametrize("closed_cookies", ["empty", "raises"])
async def test_a_sleep_announced_after_a_step_came_back_is_not_a_logout(
    env: Env, store: Store, closed_cookies: str
) -> None:
    driver = LateAnnouncementDriver(env.clock, closed_cookies=closed_cookies, info=env.driver.info)
    env.ports.driver_factory = production_policy(driver)
    env.ports.sleep_hook = RecordingHook()
    loop = await collector(env, store, 6)
    loop.rt.sleep._pending_max = 86400.0
    driver.notice = loop.rt.sleep
    try:
        await loop.run(asyncio.Event())
    finally:
        loop.rt.lock.release()
    thread, outcome = driver.announced
    thread.join(5)
    assert outcome == {"closed": True}
    second = loop.results[1]
    assert second.outcome is ReadOutcome.ABORTED
    assert second.reason == "the machine is going to sleep"
    assert loop._launch_planned  # the watcher closed Chrome for the sleep: a planned close
    assert second.page_state is None  # not judged at all
    ladder = await store.kv_get_json("ladder") or {}
    assert not ladder.get("backoff_until")
    assert not ladder.get("unknown_at")
    assert not ladder.get("unknown_streak")
    assert await store.current_block() is None


async def test_a_watcher_that_never_fired_is_cancelled(env: Env, store: Store) -> None:
    from fbcollector.runner import _end_watch

    started = asyncio.Event()

    async def waiting() -> None:
        started.set()
        await asyncio.Event().wait()

    watcher = asyncio.create_task(waiting())
    await started.wait()
    await _end_watch(watcher, asyncio.Event())
    assert watcher.cancelled()

    async def failing() -> None:
        raise RuntimeError("the close failed")

    fired = asyncio.Event()
    fired.set()
    await _end_watch(asyncio.create_task(failing()), fired)  # awaited, and its failure contained


async def test_an_idle_collector_lets_the_os_sleep_before_it_looks_at_the_gates(
    env: Env, store: Store
) -> None:
    # Warming up: no Chrome, the loop holds. When the OS announces a sleep, the answer must not
    # wait for the gates: at that moment their network checks hang until the network is gone,
    # and the OS sleeps after its grace anyway (seen live: a 25 s delay on every sleep).
    loop = await collector(env, store, 6)
    await store.kv_set(KV_LOGIN_AT, repr(env.clock.time()))
    notice = loop.rt.sleep
    real_next = loop.rt.scheduler.next
    real_sleep = env.clock.sleep
    state: dict[str, Any] = {}

    async def watched_next(**kwargs: Any) -> Any:
        if notice.pending and "ready_before_gates" not in state:
            state["ready_before_gates"] = notice._ready.is_set()
        return await real_next(**kwargs)

    async def scripted_sleep(seconds: float) -> None:
        await real_sleep(seconds)
        if "announced" not in state and loop.holds:
            state["announced"] = start_announcer(notice)

    loop.rt.scheduler.next = watched_next  # type: ignore[method-assign]
    env.clock.sleep = scripted_sleep  # type: ignore[method-assign]
    try:
        await loop.run(asyncio.Event())
    finally:
        loop.rt.lock.release()
    thread, outcome = state["announced"]
    thread.join(5)
    assert outcome == {"closed": True}
    assert state["ready_before_gates"] is True
    assert not env.driver.calls  # nothing towards Facebook while warming up


async def test_a_parked_chrome_is_closed_for_the_sleep_and_reads_resume_after_the_wake(
    env: Env, store: Store
) -> None:
    lookup = env.ports.lookup
    assert isinstance(lookup, FakeLookup)
    loop = await collector(env, store, 40)
    notice = loop.rt.sleep
    start = env.clock.time()
    real_sleep = env.clock.sleep
    state: dict[str, Any] = {}

    async def scripted_sleep(seconds: float) -> None:
        await real_sleep(seconds)
        elapsed = env.clock.time() - start
        if "announced" not in state and env.driver.running and elapsed > 120:
            state["announced"] = start_announcer(notice)  # while Chrome sits parked
            state["at"] = env.clock.time()
            state["navigations"] = len(env.driver.calls)
        elif "announced" in state and "woke" not in state and env.clock.time() - state["at"] > 180:
            # Within PENDING_MAX of awake time: the wake message, not the lapse, ends it.
            state["lookups"] = lookup.calls
            notice.did_wake()
            state["woke"] = len(loop.holds)

    env.clock.sleep = scripted_sleep  # type: ignore[method-assign]
    try:
        await loop.run(asyncio.Event())
    finally:
        loop.rt.lock.release()
    thread, outcome = state["announced"]
    thread.join(5)
    assert outcome == {"closed": True}
    assert env.driver.closed >= 1
    # Between the announcement and the wake: holds, and not one step towards Facebook.
    woke_at = state["woke"]
    pending = [h for h in loop.holds[:woke_at] if "sleep: the OS announced a sleep" in h.reasons]
    assert pending
    assert all(h.close_chrome for h in pending)
    after = loop.holds[woke_at]
    assert after.reasons == ("sleep: woke from sleep (the OS said so)",)
    # The pass after the wake re-verified the network before the next read.
    assert lookup.calls > state["lookups"]
    assert env.driver.launches == 2
    assert await planned_flags(store, CHROME) == [0, 1]  # the relaunch after the sleep: planned
    assert f"navigate {FEED_URL}" in env.driver.calls[state["navigations"] :]
