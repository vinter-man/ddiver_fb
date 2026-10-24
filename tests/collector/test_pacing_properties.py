"""Properties of the whole pacing stack on the real SQLite store and a virtual clock.

A generated *program* drives the scheduler the way the read loop will — ask, pay, act, scroll,
classify, finish — while the program also advances time, puts the machine to sleep, flips the
home network, a VPN, a proxy, the zone and the power source (between reads and in the middle of
one), serves unrecognised pages, and crashes the process at any point between the gate check,
the budget write and the action. A fake driver keeps the ground truth of what actually reached
"Facebook", and the properties are checked against it:

- no cap is ever exceeded, whatever crashed where (the ledger is written first) — the caps of
  the profile the account *should* be on, computed here from the login (cautious for 14 days),
  not the ones the scheduler chose;
- nothing happens while any gate is closed, nor after a sleep before the sleep gate noticed it;
- overdue backstops never burst after a downtime;
- an unrecognised page never yields another navigation before its backoff, and two in a row
  stop everything;
- a stale resume never clears a newer block (a stateful machine over the ladder);
- DST days keep quiet hours and the feed cadence.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import itertools
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    initialize,
    invariant,
    rule,
)

from fbcollector.budget import knobs_for
from fbcollector.clock import FakeClock
from fbcollector.gates import PowerSource
from fbcollector.ladder import Ladder, Outcome, ResumeResult
from fbcollector.model import Coverage, PageState, ReadOutcome
from fbcollector.rhythm import Jitter, Phase, Rhythm, RhythmConfig
from fbcollector.scheduler import ActionKind, Grant, Hold, simulate
from fbcollector.store import ProcessKind, Store
from fbwire.models import BudgetKind, Profile

from .gate_fakes import HOME_FACTS
from .pacing_harness import Harness
from .pacing_support import DAY, HOUR, local, make_settings, verdict

GROUPS = [str(5000 + i) for i in range(6)]
SEED = "property-seed"


class SimulatedCrashError(Exception):
    """The process died here."""


@dataclass
class Performed:
    at: float
    kind: BudgetKind
    action: ActionKind
    caps: tuple[tuple[BudgetKind, int, int], ...]


@dataclass
class World:
    """Ground truth the gates are supposed to see."""

    power: bool = True
    router: bool = True
    zone: bool = True
    vpn: bool = False
    proxy: bool = False
    slept: bool = False
    """The machine slept and no gate check has noticed it yet."""


MAX_TICKS = 700
"""Ticks per program: an upper bound on the test's cost (shrinking included)."""


@dataclass
class Runner:
    root: Path
    start: float
    login_days_ago: float
    pages: list[PageState] = field(default_factory=list)
    performed: list[Performed] = field(default_factory=list)
    world: World = field(default_factory=World)
    unknown_backoff_until: float = 0.0
    stopped_for_good: bool = False
    downtimes: list[float] = field(default_factory=list)
    """Moments navigation resumed after more than 2 h without one."""
    ledger: list[float] = field(default_factory=list)
    """Every navigation the store recorded, performed or not (read at the end)."""
    mid_read: list[tuple[str, Any]] = field(default_factory=list)
    """World changes to make right after the next navigation, before its scroll steps."""
    ticks: int = 0

    def __post_init__(self) -> None:
        self.clock = FakeClock(start=self.start)
        self.harness = self._new_harness()
        self.rhythm = Rhythm(RhythmConfig.from_settings(make_settings()), Jitter(SEED))

    def _new_harness(self) -> Harness:
        return Harness(self.root / "state.sqlite", self.clock, make_settings(), seed=SEED)

    async def boot(self, *, first: bool = False) -> None:
        h = self.harness
        await h.open()
        assert h.store is not None
        if first:
            await h.prepare(login_at=self.start - self.login_days_ago * DAY, groups=GROUPS)
        await h.store.record_process_event(ProcessKind.PROCESS_START)
        self._apply_world()
        # A new process checks every gate afresh (no cached home verdict, no Chrome): a sleep
        # before it started needs no noticing.
        self.world.slept = False

    async def crash(self) -> None:
        await self.harness.close()
        old = self.harness
        self.harness = self._new_harness()
        self.harness.probes = old.probes
        self.harness.lookup = old.lookup
        await self.boot()

    def _apply_world(self) -> None:
        probes = self.harness.probes
        world = self.world
        probes.source = PowerSource.AC if world.power else PowerSource.BATTERY
        probes.zone = "Europe/Sofia" if world.zone else "UTC"
        facts = HOME_FACTS if world.router else replace(HOME_FACTS, gateway_mac="02:00:5e:9:9:9")
        if world.vpn:
            facts = replace(facts, vpn_routes=("0/1 utun4",))
        if world.proxy:
            facts = replace(facts, proxies=("HTTPSEnable",))
        probes.facts = facts

    def change(self, op: str, arg: Any) -> None:
        """One change of the world (``sleep``: the lid closes for ``arg`` seconds)."""
        if op == "sleep":
            self.clock.suspend(arg)
            self.world.slept = True
            return
        setattr(self.world, op, arg)
        self._apply_world()

    def gates_open_now(self) -> bool:
        phase = self.rhythm.phase(self.clock.time())
        world = self.world
        return (
            world.power
            and world.router
            and world.zone
            and not world.vpn
            and not world.proxy
            and phase not in {Phase.QUIET, Phase.BREAK}
        )

    def expected_profile(self, now: float) -> Profile:
        """The profile the account should be on: cautious for 14 days after the login (these
        programs never resume a block, so no warning extends it)."""
        login_at = self.start - self.login_days_ago * DAY
        return Profile.CAUTIOUS if now < login_at + 14 * DAY else Profile.NORMAL

    def noticed(self, hold: Hold) -> None:
        if any(reason.startswith("sleep:") for reason in hold.reasons):
            self.world.slept = False

    def _perform(self, grant: Grant, kind: BudgetKind) -> None:
        assert self.gates_open_now(), "a step happened while a gate was closed"
        assert not self.world.slept, "a step happened after a sleep no gate had noticed"
        assert not self.stopped_for_good, "a step happened after two unrecognised pages"
        now = self.clock.time()
        expected = self.expected_profile(now)
        assert grant.profile is expected, f"{grant.profile.value} pacing, not {expected.value}"
        if kind is BudgetKind.NAVIGATION:
            assert now >= self.unknown_backoff_until, "a navigation during an UNKNOWN backoff"
            last = max((p.at for p in self.performed if p.kind is kind), default=None)
            if last is None or now - last > 2 * HOUR:
                self.downtimes.append(now)
        caps = knobs_for(SETTINGS, expected).windows()
        self.performed.append(Performed(now, kind, grant.action.kind, caps))

    async def tick(self, crash_at: str | None) -> None:
        h = self.harness
        assert h.store is not None
        self.ticks += 1
        real_acquire = h.budget.acquire
        if crash_at == "before_budget":

            async def crashing(*a: Any, **kw: Any) -> Any:
                raise SimulatedCrashError

            h.budget.acquire = crashing  # type: ignore[method-assign]
        try:
            result = await h.scheduler.next()
        except SimulatedCrashError:
            h.budget.acquire = real_acquire  # type: ignore[method-assign]
            await self.crash()
            return
        if isinstance(result, Hold):
            self.noticed(result)
            until = result.until if result.until is not None else self.clock.time() + HOUR
            self.clock.advance(min(max(1.0, until - self.clock.time()), 3 * HOUR))
            return
        if crash_at == "after_budget":
            await self.crash()
            return
        self._perform(result, BudgetKind.NAVIGATION)
        read_id = await h.store.start_read(result.action.surface, group_id=result.action.group_id)
        self.clock.advance(8)
        if crash_at == "after_action":
            await self.crash()
            return
        while self.mid_read:
            self.change(*self.mid_read.pop(0))
        page = self.pages.pop(0) if self.pages else PageState.OK
        steps = 0
        while page is PageState.OK:
            held = await h.scheduler.scroll_permit(result, steps_done=steps, read_id=read_id)
            if held is not None:
                self.noticed(held)
                break
            if crash_at == "in_scroll":
                await self.crash()
                return
            self._perform(result, BudgetKind.SCROLL)
            steps += 1
            self.clock.advance(7)
        probe = result.action.block_id if result.action.kind is ActionKind.PROBE else None
        outcome = await h.ladder.on_verdict(verdict(page), probe_block_id=probe)
        if outcome.outcome is Outcome.BACKOFF:
            assert outcome.backoff_until is not None
            self.unknown_backoff_until = outcome.backoff_until
        elif outcome.outcome is Outcome.BLOCKED:
            self.stopped_for_good = True  # nobody resumes in these programs
        await h.store.finish_read(
            read_id,
            coverage=Coverage(),
            steps=steps,
            outcome=ReadOutcome.COMPLETE if page is PageState.OK else ReadOutcome.FAILED,
            page_state=page,
        )
        await h.scheduler.read_finished(
            result, outcome=ReadOutcome.COMPLETE if page is PageState.OK else ReadOutcome.FAILED
        )

    async def run(self, program: list[tuple[str, Any]]) -> None:
        await self.boot(first=True)
        try:
            for op, arg in program:
                if op == "run":
                    count, crash_at = arg
                    for index in range(min(count, MAX_TICKS - self.ticks)):
                        await self.tick(crash_at if index == count - 1 else None)
                elif op == "tick":
                    if self.ticks < MAX_TICKS:
                        await self.tick(arg)
                elif op == "advance":
                    self.clock.advance(arg)
                elif op == "down":
                    # the process is not running: nothing is asked, time passes
                    await self.harness.close()
                    self.clock.advance(arg)
                    await self.boot()
                elif op == "page":
                    self.pages.append(arg)
                elif op == "mid":
                    self.mid_read.append(arg)
                else:
                    self.change(op, arg)
        finally:
            store = self.harness.store
            if store is not None:
                self.ledger = await store.budget_event_times(BudgetKind.NAVIGATION, since=0)
            await self.harness.close()


CRASH = st.sampled_from([None, "before_budget", "after_budget", "after_action", "in_scroll"])
RUN_WITH_CRASH = st.tuples(st.just("run"), st.tuples(st.integers(min_value=1, max_value=30), CRASH))
LONG_RUN = st.tuples(st.just("run"), st.tuples(st.integers(min_value=10, max_value=80), st.none()))
FLIP = st.one_of(
    st.tuples(st.sampled_from(["power", "router", "zone", "vpn", "proxy"]), st.booleans()),
    st.tuples(st.just("sleep"), st.floats(min_value=31, max_value=2 * HOUR)),
)
OPS = st.one_of(
    RUN_WITH_CRASH,
    RUN_WITH_CRASH,
    LONG_RUN,
    LONG_RUN,
    st.tuples(st.just("advance"), st.floats(min_value=1, max_value=3 * HOUR)),
    st.tuples(st.just("down"), st.floats(min_value=0.5 * HOUR, max_value=10 * HOUR)),
    FLIP,
    st.tuples(st.just("mid"), FLIP),  # the same, in the middle of the next read
    st.tuples(st.just("page"), st.sampled_from([PageState.OK, PageState.UNKNOWN])),
)
SETTINGS = make_settings()


def check_caps(performed: list[Performed]) -> None:
    for i, p in enumerate(performed):
        for kind, window, cap in p.caps:
            if kind is not p.kind:
                continue
            count = sum(1 for q in performed[: i + 1] if q.kind is kind and q.at > p.at - window)
            assert count <= cap, f"{count} {kind.value} in {window} s (cap {cap})"


def check_no_burst(runner: Runner) -> None:
    """After more than 2 h without a navigation (by the ledger), no backstop comes in the first
    20 minutes of full cadence: overdue backstops restart from a jittered point at least 25 % of
    the shortest interval (1.5 h) after it. Reading usually resumes in the morning shoulder,
    where backstops wait for full cadence anyway, so the 20 minutes count from there."""
    ledger = runner.ledger
    resumed = [t for i, t in enumerate(ledger) if i == 0 or t - ledger[i - 1] > 2 * HOUR]
    navs = [p for p in runner.performed if p.kind is BudgetKind.NAVIGATION]
    for start in resumed:
        plan = runner.rhythm.plan_at(start)
        full = max(start, plan.shoulder_end)
        early = [p for p in navs if start <= p.at < full + 20 * 60]
        assert not any(p.action is ActionKind.BACKSTOP for p in early), "overdue backstops burst"


@pytest.mark.timeout(600)  # the programs are bounded (MAX_TICKS), but shrinking multiplies them
@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(
    program=st.lists(OPS, min_size=5, max_size=30),
    day=st.integers(min_value=0, max_value=6),
    hour=st.integers(min_value=0, max_value=23) | st.integers(min_value=8, max_value=20),
    login_days_ago=st.sampled_from([3.0, 30.0]),
)
async def test_caps_gates_and_backoffs_hold_through_crashes(
    program: list[tuple[str, Any]], day: int, hour: int, login_days_ago: float
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = Runner(
            Path(tmp),
            start=local(2026, 10, 5 + day, hour),
            login_days_ago=login_days_ago,
        )
        await runner.run(program)
        check_caps(runner.performed)
        check_no_burst(runner)
        navigations = sum(1 for p in runner.performed if p.kind is BudgetKind.NAVIGATION)
        bucket = next(b for b in (100, 30, 10, 1, 0) if navigations >= b)
        event(f"navigations: {bucket}+")
        event(f"crashes: {sum(1 for op, arg in program if op == 'run' and arg[1])}")
        event(f"downtimes: {len(runner.downtimes)}")


async def test_a_long_honest_run_uses_the_plan_and_never_the_cap() -> None:
    """A clean day and a half: reads happen, all inside the caps, none in quiet hours."""
    with tempfile.TemporaryDirectory() as tmp:
        runner = Runner(Path(tmp), start=local(2026, 10, 7, 6), login_days_ago=30)
        await runner.run([("tick", None)] * 600)
        navs = [p for p in runner.performed if p.kind is BudgetKind.NAVIGATION]
        assert len(navs) > 100
        assert {p.action for p in navs} >= {ActionKind.FEED, ActionKind.BACKSTOP, ActionKind.SYNC}
        check_caps(runner.performed)
        check_no_burst(runner)


async def test_two_unrecognised_pages_stop_everything() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runner = Runner(Path(tmp), start=local(2026, 10, 7, 12), login_days_ago=30)
        program: list[tuple[str, Any]] = [("page", PageState.UNKNOWN), ("page", PageState.UNKNOWN)]
        program += [("tick", None)] * 200
        await runner.run(program)
        assert runner.stopped_for_good
        navs = [p for p in runner.performed if p.kind is BudgetKind.NAVIGATION]
        assert len(navs) == 2
        assert navs[1].at - navs[0].at >= 300  # the first one backed off 5 minutes


# -- DST -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("zone", "start"),
    [
        ("Europe/Sofia", dt.date(2026, 3, 27)),
        ("Europe/Sofia", dt.date(2026, 10, 23)),
        ("America/New_York", dt.date(2026, 3, 6)),
        ("America/New_York", dt.date(2026, 10, 30)),
    ],
)
@pytest.mark.parametrize("profile", list(Profile))
def test_dst_days_keep_quiet_hours_and_cadence(zone: str, start: dt.date, profile: Profile) -> None:
    settings_ = make_settings(timezone=zone)
    import zoneinfo

    begin = dt.datetime.combine(start, dt.time(12), tzinfo=zoneinfo.ZoneInfo(zone)).timestamp()
    sim = simulate(settings_, profile, 12, days=4, seed="dst", start=begin)
    rhythm = Rhythm(RhythmConfig.from_settings(settings_), Jitter("dst"))
    feeds = [t for t, kind in sim.navigations if kind is ActionKind.FEED]
    low = 4.5 * 60 if profile is Profile.NORMAL else 10 * 60
    for t, _ in sim.navigations:
        assert rhythm.phase(t) not in {Phase.QUIET, Phase.BREAK}
        local_hour = dt.datetime.fromtimestamp(t, zoneinfo.ZoneInfo(zone)).hour
        assert not 3 <= local_hour < 6  # the core of the night, whatever the jitter
    assert min(b - a for a, b in itertools.pairwise(feeds)) >= low - 1e-6
    days_with_reads = {rhythm.plan_at(t).day for t in feeds}
    assert len(days_with_reads) >= 4


# -- the ladder, statefully ------------------------------------------------------------------


class LadderMachine(RuleBasedStateMachine):
    """Random trips, resumes (fresh, replayed, for old or current blocks) and probe results.

    Model: the only way an open block closes is a newer trip (superseded) or a clean probe
    that followed an *accepted* resume carrying that very block's id after its ``until``.
    """

    blocks = Bundle("blocks")
    nonces = Bundle("nonces")

    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.store = Store(Path(self.tmp.name) / "s.sqlite", clock=self.clock)
        self.loop.run_until_complete(self.store.open())
        self.ladder = Ladder(self.store, self.clock)
        self.accepted: dict[int, float] = {}
        self.counter = 0

    def run(self, coro: Any) -> Any:
        return self.loop.run_until_complete(coro)

    def teardown(self) -> None:
        self.run(self.store.close())
        self.loop.close()
        self.tmp.cleanup()

    @initialize(target=blocks)
    def first_block(self) -> int:
        return int(self.run(self.ladder.trip(PageState.BLOCKED, reason="first")).id)

    @rule(target=blocks)
    def trip(self) -> int:
        return int(self.run(self.ladder.trip(PageState.BLOCKED, reason="again")).id)

    @rule(target=nonces)
    def new_nonce(self) -> str:
        self.counter += 1
        return f"nonce-{self.counter:06d}"

    @rule(hours=st.floats(min_value=0, max_value=100))
    def wait(self, hours: float) -> None:
        self.clock.advance(hours * HOUR)

    @rule(block=blocks, nonce=nonces, lifted=st.booleans())
    def resume(self, block: int, nonce: str, lifted: bool) -> None:
        current = self.run(self.store.current_block())
        result = self.run(self.ladder.resume(block, nonce, lock_lifted=lifted, replanned=True))
        if result is ResumeResult.ACCEPTED:
            assert current is not None and current.id == block
            assert current.until is None or self.clock.time() >= current.until
            self.accepted[block] = self.clock.time()
        elif current is not None and block != current.id and result is not ResumeResult.REPLAYED:
            assert result is ResumeResult.STALE

    @rule(nonce=consumes(nonces), block=blocks)
    def replay(self, nonce: str, block: int) -> None:
        # a nonce used once never works again, whatever it names
        first = self.run(self.ladder.resume(block, nonce, replanned=True, lock_lifted=True))
        if first is ResumeResult.ACCEPTED:
            self.accepted[block] = self.clock.time()
        again = self.run(self.ladder.resume(block, nonce, replanned=True, lock_lifted=True))
        assert again is ResumeResult.REPLAYED

    @rule(block=blocks, clean=st.booleans())
    def probe(self, block: int, clean: bool) -> None:
        before = self.run(self.store.current_block())
        state = PageState.OK if clean else PageState.BLOCKED
        outcome = self.run(self.ladder.on_verdict(verdict(state), probe_block_id=block))
        if outcome.outcome is Outcome.RESUMED:
            assert before is not None and before.id == block
            assert block in self.accepted

    @invariant()
    def closed_only_by_a_clean_probe_or_a_newer_block(self) -> None:
        async def rows() -> list[Any]:
            cur = await self.store.db.execute("SELECT id, close_reason FROM blocks")
            return list(await cur.fetchall())

        for block_id, reason in self.run(rows()):
            if reason is None or reason == "superseded":
                continue
            assert reason == "resumed after a clean probe"
            assert block_id in self.accepted


TestLadderMachine = LadderMachine.TestCase
TestLadderMachine.settings = settings(max_examples=40, stateful_step_count=30, deadline=None)
