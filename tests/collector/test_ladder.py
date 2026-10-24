from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

from fbcollector.clock import FakeClock
from fbcollector.gates import Activity, GateContext
from fbcollector.ladder import (
    KV_LADDER,
    RUNGS,
    STRIKE_DECAY_DAYS,
    Ladder,
    LadderState,
    Outcome,
    ProbeRequest,
    ResumeResult,
    next_rung,
)
from fbcollector.model import CollectorState, PageState
from fbcollector.store import Block, Store
from fbwire.models import EventKind

from .pacing_support import DAY, HOUR, Events, verdict

NAV = GateContext(Activity.NAVIGATE)
PROBE = GateContext(Activity.NAVIGATE, probe=True)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def store(tmp_path: Path, clock: FakeClock) -> AsyncIterator[Store]:
    s = Store(tmp_path / "state.sqlite", clock=clock)
    await s.open()
    yield s
    await s.close()


@pytest.fixture
def events() -> Events:
    return Events()


@pytest.fixture
def ladder(store: Store, clock: FakeClock, events: Events) -> Ladder:
    return Ladder(store, clock, events=events)


async def blocked(ladder: Ladder, state: PageState = PageState.BLOCKED, **kw: object) -> int:
    outcome = await ladder.on_verdict(verdict(state, "url:checkpoint", **kw))  # type: ignore[arg-type]
    assert outcome.outcome is Outcome.BLOCKED
    assert outcome.block is not None
    return outcome.block.id


async def test_the_ladder_state_is_written_durably(ladder: Ladder, store: Store) -> None:
    """A power cut right after the first unrecognised page must not forget the backoff."""
    from .trace_support import durable_writes, traced

    async with traced(store) as statements:
        outcome = await ladder.on_verdict(verdict(PageState.UNKNOWN, "signature:english"))
    assert outcome.outcome is Outcome.BACKOFF
    assert f"kv:{KV_LADDER}" in durable_writes(statements)


class TestRungs:
    async def test_the_ladder_climbs_and_never_steps_down(
        self, ladder: Ladder, store: Store, clock: FakeClock
    ) -> None:
        expected = [6 * HOUR, 24 * HOUR, 72 * HOUR, None, None]
        for rung, window in enumerate(expected):
            block_id = await blocked(ladder)
            block = await store.current_block()
            assert block is not None and block.id == block_id
            assert block.rung == min(rung, len(RUNGS) - 1)
            assert block.until == (None if window is None else clock.time() + window)
            clock.advance(10)

    async def test_the_block_is_written_first_and_carries_its_evidence(
        self, ladder: Ladder, store: Store, events: Events
    ) -> None:
        outcome = await ladder.on_verdict(
            verdict(PageState.BLOCKED, "text:suspect_automated"), screenshot="shots/1.png"
        )
        block = await store.current_block()
        assert block is not None
        assert outcome.block == block
        assert block.reason == "text:suspect_automated"
        assert block.excerpt == "blocked"
        assert block.screenshot == "shots/1.png"
        assert events.seen == [(EventKind.BLOCK_OPENED, block.id, "text:suspect_automated")]

    async def test_a_logout_is_a_block_and_an_event(
        self, ladder: Ladder, store: Store, events: Events
    ) -> None:
        block_id = await blocked(ladder, PageState.LOGGED_OUT)
        assert [e[0] for e in events.seen] == [EventKind.BLOCK_OPENED, EventKind.LOGGED_OUT]
        gate = await ladder.gate(NAV)
        assert (gate.ok, gate.state) == (False, CollectorState.LOGGED_OUT)
        assert str(block_id) in gate.reason

    async def test_advanced_protection_needs_the_owner(
        self, ladder: Ladder, clock: FakeClock
    ) -> None:
        block_id = await blocked(ladder, advanced=True)
        clock.advance(7 * HOUR)
        result = await ladder.resume(block_id, "nonce-aaaa")
        assert result is ResumeResult.NEEDS_CONFIRMATION
        assert await ladder.probe_request() is None
        assert (
            await ladder.resume(block_id, "nonce-bbbb", lock_lifted=True) is ResumeResult.ACCEPTED
        )

    async def test_no_event_sink_is_fine(self, store: Store, clock: FakeClock) -> None:
        quiet = Ladder(store, clock)
        assert (await quiet.trip(PageState.BLOCKED, reason="x")).rung == 0


class TestUnknown:
    async def test_first_backs_off_second_in_a_row_blocks(
        self, ladder: Ladder, clock: FakeClock, store: Store
    ) -> None:
        first = await ladder.on_verdict(verdict(PageState.UNKNOWN, "http:502"))
        assert first.outcome is Outcome.BACKOFF
        assert first.backoff_until == pytest.approx(clock.time() + 300)
        gate = await ladder.gate(NAV)
        assert (gate.ok, gate.state, gate.retry_at) == (
            False,
            CollectorState.UNKNOWN,
            first.backoff_until,
        )
        clock.advance(301)
        assert (await ladder.gate(NAV)).ok
        second = await ladder.on_verdict(verdict(PageState.UNKNOWN, "signature:dialogs_clear"))
        assert second.outcome is Outcome.BLOCKED
        block = await store.current_block()
        assert block is not None
        assert block.state is PageState.UNKNOWN
        assert "twice in a row" in block.reason

    async def test_backoff_escalates_over_a_day_then_blocks(
        self, ladder: Ladder, clock: FakeClock
    ) -> None:
        waits = []
        for _ in range(4):
            outcome = await ladder.on_verdict(verdict(PageState.UNKNOWN))
            assert outcome.outcome is Outcome.BACKOFF
            assert outcome.backoff_until is not None
            waits.append(outcome.backoff_until - clock.time())
            clock.advance(3 * HOUR)
            assert (await ladder.on_verdict(verdict(PageState.OK))).outcome is Outcome.CONTINUE
        assert waits == [300, 600, 1200, 2400]
        fifth = await ladder.on_verdict(verdict(PageState.UNKNOWN))
        assert fifth.outcome is Outcome.BLOCKED
        assert "too often in 24 h" in fifth.reason

    async def test_old_unknowns_are_forgotten(self, ladder: Ladder, clock: FakeClock) -> None:
        await ladder.on_verdict(verdict(PageState.UNKNOWN))
        await ladder.on_verdict(verdict(PageState.FEED_NOT_CHRONO))  # recognised: streak over
        clock.advance(DAY + 1)
        outcome = await ladder.on_verdict(verdict(PageState.UNKNOWN))
        assert outcome.backoff_until == pytest.approx(clock.time() + 300)

    async def test_backoff_survives_a_restart(self, tmp_path: Path, clock: FakeClock) -> None:
        path = tmp_path / "s.sqlite"
        async with Store(path, clock=clock) as store:
            await Ladder(store, clock).on_verdict(verdict(PageState.UNKNOWN))
        async with Store(path, clock=clock) as store:
            assert not (await Ladder(store, clock).gate(NAV)).ok


class TestOtherStates:
    async def test_group_lost_twice_in_a_day_is_a_block(
        self, ladder: Ladder, clock: FakeClock
    ) -> None:
        assert (
            await ladder.on_verdict(verdict(PageState.GROUP_LOST))
        ).outcome is Outcome.GROUP_PAUSED
        clock.advance(DAY + 1)
        assert (
            await ladder.on_verdict(verdict(PageState.GROUP_LOST))
        ).outcome is Outcome.GROUP_PAUSED
        clock.advance(HOUR)
        outcome = await ladder.on_verdict(verdict(PageState.GROUP_LOST, "text:members_only"))
        assert outcome.outcome is Outcome.BLOCKED
        assert "2 groups lost within 24 h" in outcome.reason

    @pytest.mark.parametrize(
        "state",
        [
            PageState.FEED_NOT_CHRONO,
            PageState.NOT_FOLLOWED,
            PageState.SORT_IGNORED,
            PageState.PARSER_BROKEN,
        ],
    )
    async def test_flags_are_the_callers_and_end_a_streak(
        self, ladder: Ladder, state: PageState
    ) -> None:
        await ladder.on_verdict(verdict(PageState.UNKNOWN))
        outcome = await ladder.on_verdict(verdict(state, "x"))
        assert (outcome.outcome, outcome.reason) == (Outcome.FLAG, "x")
        assert (await ladder.state()).unknown_streak == 0


class TestResume:
    async def test_the_full_path(
        self, ladder: Ladder, store: Store, clock: FakeClock, events: Events
    ) -> None:
        block_id = await blocked(ladder)
        assert await ladder.resume(block_id, "early-nonce") is ResumeResult.TOO_EARLY
        clock.advance(6 * HOUR)
        # the early command is used up: replaying it now that the time has passed does nothing
        assert await ladder.resume(block_id, "early-nonce") is ResumeResult.REPLAYED
        assert (
            await ladder.resume(block_id, "fresh-nonce", probe_delay=120) is ResumeResult.ACCEPTED
        )
        assert await ladder.probe_request() == ProbeRequest(block_id, clock.time() + 120)
        assert not (await ladder.gate(PROBE)).ok  # the command's delay first
        assert not (await ladder.gate(NAV)).ok
        clock.advance(120)
        assert (await ladder.gate(PROBE)).ok
        assert not (await ladder.gate(NAV)).ok  # only the probe goes through
        outcome = await ladder.on_verdict(verdict(PageState.OK), probe_block_id=block_id)
        assert outcome.outcome is Outcome.RESUMED
        assert await store.current_block() is None
        assert (await ladder.gate(NAV)).ok
        assert await ladder.cautious_until() == pytest.approx(clock.time() + 7 * DAY)
        assert events.seen[-1][0] is EventKind.BLOCK_CLOSED

    async def test_after_a_logout_cautious_lasts_14_days(
        self, ladder: Ladder, clock: FakeClock
    ) -> None:
        block_id = await blocked(ladder, PageState.LOGGED_OUT)
        clock.advance(6 * HOUR)
        await ladder.resume(block_id, "nonce-0001")
        await ladder.on_verdict(verdict(PageState.OK), probe_block_id=block_id)
        until = await ladder.cautious_until()
        assert until == pytest.approx(clock.time() + 14 * DAY)
        # a later, shorter warning never shortens it
        clock.advance(DAY)
        second = await blocked(ladder)
        clock.advance(DAY)
        await ladder.resume(second, "nonce-0002")
        await ladder.on_verdict(verdict(PageState.OK), probe_block_id=second)
        assert await ladder.cautious_until() == until

    async def test_a_failed_probe_climbs(
        self, ladder: Ladder, store: Store, clock: FakeClock
    ) -> None:
        block_id = await blocked(ladder)
        clock.advance(6 * HOUR)
        await ladder.resume(block_id, "nonce-0003")
        outcome = await ladder.on_verdict(
            verdict(PageState.UNKNOWN, "http:500"), probe_block_id=block_id
        )
        assert outcome.outcome is Outcome.BLOCKED
        new = await store.current_block()
        assert new is not None
        assert (new.rung, new.state) == (1, PageState.UNKNOWN)
        assert "probe failed: http:500" in new.reason
        assert await ladder.probe_request() is None

    @pytest.mark.parametrize(
        ("state", "lifted"),
        [
            (PageState.FEED_NOT_CHRONO, True),
            (PageState.NOT_FOLLOWED, True),
            (PageState.SORT_IGNORED, True),
            (PageState.GROUP_LOST, False),
            (PageState.LOGGED_OUT, False),
            (PageState.BLOCKED, False),
        ],
    )
    async def test_only_a_warning_fails_the_probe(
        self, ladder: Ladder, store: Store, clock: FakeClock, state: PageState, lifted: bool
    ) -> None:
        block_id = await blocked(ladder)
        clock.advance(6 * HOUR)
        await ladder.resume(block_id, f"nonce-{state.value}")
        outcome = await ladder.on_verdict(verdict(state, "x"), probe_block_id=block_id)
        assert (outcome.outcome is Outcome.RESUMED) is lifted
        current = await store.current_block()
        assert (current is None) is lifted
        if not lifted:
            assert current is not None and current.rung == 1

    async def test_stale_and_absent_blocks(self, ladder: Ladder, clock: FakeClock) -> None:
        assert await ladder.resume(1, "nonce-0004") is ResumeResult.NO_BLOCK
        old = await blocked(ladder)
        clock.advance(6 * HOUR)
        assert await ladder.resume(old, "nonce-0005") is ResumeResult.ACCEPTED
        new = await blocked(ladder)  # a new wall before the probe ran
        assert new != old
        assert await ladder.probe_request() is None
        assert not (await ladder.gate(PROBE)).ok
        ignored = await ladder.on_verdict(verdict(PageState.OK), probe_block_id=old)
        assert ignored.outcome is Outcome.IGNORED
        clock.advance(24 * HOUR)
        assert await ladder.resume(old, "nonce-0006") is ResumeResult.STALE

    async def test_the_last_rung_needs_a_replan(self, ladder: Ladder, clock: FakeClock) -> None:
        for _ in range(4):
            block_id = await blocked(ladder)
        clock.advance(365 * DAY)
        assert await ladder.resume(block_id, "nonce-0007") is ResumeResult.PERMANENT
        assert await ladder.resume(block_id, "nonce-0008", replanned=True) is ResumeResult.ACCEPTED

    async def test_probe_without_resume_is_ignored(self, ladder: Ladder) -> None:
        block_id = await blocked(ladder)
        outcome = await ladder.on_verdict(verdict(PageState.OK), probe_block_id=block_id)
        assert outcome.outcome is Outcome.IGNORED


class PausingStore:
    """The real store, except that one save of the ladder's row waits for ``go``."""

    def __init__(self, inner: Store) -> None:
        self._inner = inner
        self.armed = False
        self.waiting = asyncio.Event()
        self.go = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def kv_set(self, key: str, value: str, *, durable: bool = False) -> None:
        if key == KV_LADDER and self.armed:
            self.armed = False
            self.waiting.set()
            await self.go.wait()
        await self._inner.kv_set(key, value, durable=durable)


async def settled(predicate: Callable[[], bool]) -> None:
    for _ in range(100_000):  # yields only: the other side is SQLite's worker thread
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("never happened")


async def test_a_resume_during_a_clean_probe_keeps_the_cautious_period(
    store: Store, clock: FakeClock
) -> None:
    """Round 2 B-F7: the uplink's resume and the loop's probe result each read the ladder's
    row and wrote it back; the resume's stale copy wiped the probe's cautious period."""
    paused = PausingStore(store)
    ladder = Ladder(paused, clock)
    block = await ladder.trip(PageState.BLOCKED, reason="suspect automated behaviour")
    clock.advance(RUNGS[0] + 1)  # type: ignore[operator]
    assert await ladder.resume(block.id, "nonce-0001") is ResumeResult.ACCEPTED
    paused.armed = True
    second = asyncio.create_task(ladder.resume(block.id, "nonce-0002", probe_delay=30))
    await paused.waiting.wait()  # it has read the row and is writing its copy back
    probe = asyncio.create_task(ladder.on_verdict(verdict(PageState.OK), probe_block_id=block.id))
    await settled(lambda: probe.done() or bool(getattr(ladder._lock, "_waiters", None)))
    paused.go.set()
    await asyncio.gather(second, probe)
    assert probe.result().outcome is Outcome.RESUMED
    state = await ladder.state()
    assert state.cautious_until == pytest.approx(clock.time() + 7 * DAY)


class TestState:
    def test_json_round_trip(self) -> None:
        state = LadderState(
            unknown_streak=1,
            unknown_at=(1.0, 2.5),
            group_lost_at=(3.0,),
            backoff_until=9.0,
            probe=ProbeRequest(4, 10.0),
            cautious_until=11.0,
        )
        assert LadderState.from_json(state.to_json()) == state
        assert LadderState.from_json(None) == LadderState()
        assert LadderState.from_json('{"probe": null}') == LadderState()

    async def test_state_lives_in_kv(self, ladder: Ladder, store: Store) -> None:
        await ladder.on_verdict(verdict(PageState.UNKNOWN))
        raw = await store.kv_get(KV_LADDER)
        assert raw is not None
        assert LadderState.from_json(raw).unknown_streak == 1


# -- strikes expire (owner decision of 2026-09-25) --------------------------------------------

DECAY = STRIKE_DECAY_DAYS * DAY


async def lift(ladder: Ladder, clock: FakeClock, block_id: int, nonce: str) -> None:
    """Wait out the block, resume it (whatever it needs) and pass a clean probe."""
    block = await ladder.current_block()
    assert block is not None and block.id == block_id
    if block.until is not None and clock.time() < block.until:
        clock.advance(block.until - clock.time())
    result = await ladder.resume(block_id, nonce, lock_lifted=True, replanned=True)
    assert result is ResumeResult.ACCEPTED
    outcome = await ladder.on_verdict(verdict(PageState.OK), probe_block_id=block_id)
    assert outcome.outcome is Outcome.RESUMED


class TestStrikeDecay:
    async def test_thirty_quiet_days_start_the_ladder_again(
        self, ladder: Ladder, store: Store, clock: FakeClock
    ) -> None:
        first = await blocked(ladder)
        await lift(ladder, clock, first, "nonce-d001")
        clock.advance(DECAY - 1)
        second = await blocked(ladder)  # one second short of 30 days: climbs
        block = await store.current_block()
        assert block is not None and (block.id, block.rung) == (second, 1)
        await lift(ladder, clock, second, "nonce-d002")
        clock.advance(DECAY)  # exactly 30 days after the lift: expired
        await blocked(ladder)
        block = await store.current_block()
        assert block is not None
        assert (block.rung, block.until) == (0, clock.time() + RUNGS[0])  # type: ignore[operator]

    async def test_an_open_block_never_decays(
        self, ladder: Ladder, store: Store, clock: FakeClock
    ) -> None:
        block_id = await blocked(ladder)
        clock.advance(10 * DECAY)
        await ladder.resume(block_id, "nonce-d003")
        failed = await ladder.on_verdict(verdict(PageState.BLOCKED), probe_block_id=block_id)
        assert failed.block is not None and failed.block.rung == 1  # a failed probe climbs

    async def test_the_last_rung_still_needs_a_replan_and_decays_only_after_it(
        self, ladder: Ladder, store: Store, clock: FakeClock
    ) -> None:
        for _ in range(4):
            last = await blocked(ladder)
        clock.advance(3 * DECAY)
        assert await ladder.resume(last, "nonce-d004") is ResumeResult.PERMANENT
        await lift(ladder, clock, last, "nonce-d005")  # the local re-plan lifts it
        clock.advance(DECAY / 2)
        again = await blocked(ladder)
        block = await store.current_block()
        assert block is not None and (block.id, block.rung, block.until) == (again, 3, None)
        await lift(ladder, clock, again, "nonce-d006")
        clock.advance(DECAY)
        await blocked(ladder)
        block = await store.current_block()
        assert block is not None and block.rung == 0

    async def test_advanced_protection_is_unchanged_after_a_decay(
        self, ladder: Ladder, store: Store, clock: FakeClock
    ) -> None:
        first = await blocked(ladder)
        await lift(ladder, clock, first, "nonce-d007")
        clock.advance(DECAY)
        locked = await blocked(ladder, advanced=True)
        clock.advance(RUNGS[0])  # type: ignore[arg-type]
        assert await ladder.resume(locked, "nonce-d008") is ResumeResult.NEEDS_CONFIRMATION
        assert await ladder.resume(locked, "nonce-d009", lock_lifted=True) is ResumeResult.ACCEPTED

    async def test_the_decay_is_configurable(self, store: Store, clock: FakeClock) -> None:
        ladder = Ladder(store, clock, strike_decay_days=45)
        first = await blocked(ladder)
        await lift(ladder, clock, first, "nonce-d010")
        clock.advance(DECAY)
        await blocked(ladder)
        block = await store.current_block()
        assert block is not None and block.rung == 1  # 30 days are not 45

    async def test_a_clock_stepped_back_does_not_expire_strikes(
        self, ladder: Ladder, store: Store, clock: FakeClock
    ) -> None:
        first = await blocked(ladder)
        await lift(ladder, clock, first, "nonce-d011")
        clock.jump_wall(-DECAY)
        await blocked(ladder)
        block = await store.current_block()
        assert block is not None and block.rung == 1


def _block(rung: int, closed_at: float | None) -> Block:
    return Block(
        id=1,
        opened_at=0.0,
        until=None,
        rung=rung,
        state=PageState.BLOCKED,
        reason="x",
        excerpt=None,
        screenshot=None,
        no_probe=False,
        closed_at=closed_at,
        close_reason=None if closed_at is None else "resumed after a clean probe",
    )


@given(
    rung=st.integers(min_value=0, max_value=len(RUNGS) - 1),
    closed_after=st.none() | st.floats(min_value=0, max_value=365 * DAY),
    quiet=st.floats(min_value=-DAY, max_value=365 * DAY),
    decay_days=st.integers(min_value=30, max_value=400),
)
def test_next_rung_climbs_one_or_starts_over(
    rung: int, closed_after: float | None, quiet: float, decay_days: int
) -> None:
    """The next rung is the first after a long enough quiet, else exactly one up (capped)."""
    closed_at = None if closed_after is None else 1_000.0 + closed_after
    now = (closed_at if closed_at is not None else 1_000.0) + quiet
    got = next_rung(_block(rung, closed_at), now, decay=decay_days * DAY)
    expired = closed_at is not None and now - closed_at >= decay_days * DAY
    assert got == (0 if expired else min(rung + 1, len(RUNGS) - 1))
    assert next_rung(None, now) == 0


class DecayMachine(RuleBasedStateMachine):
    """Warnings, waits of up to 60 days, resumes (with and without a re-plan or the owner's
    lock confirmation) and probes, on the real store.

    The oracle is the blocks table itself: every block's rung follows from the block before it
    — the first rung after 30 quiet days since that one was lifted, else one rung up (the last
    one repeating). The last rung never lifts without a re-plan, an advanced-protection block
    never without the owner's word, however long ago anything was.
    """

    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.store = Store(Path(self.tmp.name) / "s.sqlite", clock=self.clock)
        self.loop.run_until_complete(self.store.open())
        self.ladder = Ladder(self.store, self.clock)
        self.counter = 0

    def run(self, coro: Any) -> Any:
        return self.loop.run_until_complete(coro)

    def teardown(self) -> None:
        self.run(self.store.close())
        self.loop.close()
        self.tmp.cleanup()

    def nonce(self) -> str:
        self.counter += 1
        return f"nonce-{self.counter:06d}"

    def current(self) -> Block | None:
        return self.run(self.store.current_block())  # type: ignore[no-any-return]

    @initialize()
    def start(self) -> None:
        self.run(self.ladder.trip(PageState.BLOCKED, reason="first"))

    @rule(
        state=st.sampled_from([PageState.BLOCKED, PageState.LOGGED_OUT, PageState.UNKNOWN]),
        advanced=st.booleans(),
    )
    def warning(self, state: PageState, advanced: bool) -> None:
        self.run(self.ladder.trip(state, reason="warning", advanced_protection=advanced))

    @rule(days=st.floats(min_value=0, max_value=60))
    def wait(self, days: float) -> None:
        self.clock.advance(days * DAY)

    @precondition(lambda self: self.current() is not None)
    @rule(replanned=st.booleans(), lifted=st.booleans(), clean=st.booleans())
    def resume_and_probe(self, replanned: bool, lifted: bool, clean: bool) -> None:
        block = self.current()
        assert block is not None
        result = self.run(
            self.ladder.resume(block.id, self.nonce(), lock_lifted=lifted, replanned=replanned)
        )
        if block.until is None and not replanned:
            assert result is ResumeResult.PERMANENT
        elif block.until is not None and self.clock.time() < block.until:
            assert result is ResumeResult.TOO_EARLY
        elif block.no_probe and not lifted:
            assert result is ResumeResult.NEEDS_CONFIRMATION
        else:
            assert result is ResumeResult.ACCEPTED
            state = PageState.OK if clean else PageState.BLOCKED
            outcome = self.run(self.ladder.on_verdict(verdict(state), probe_block_id=block.id))
            assert outcome.outcome is (Outcome.RESUMED if clean else Outcome.BLOCKED)

    @invariant()
    def every_rung_follows_from_the_block_before(self) -> None:
        async def rows() -> list[Any]:
            cur = await self.store.db.execute(
                "SELECT rung, opened_at, closed_at, until FROM blocks ORDER BY id"
            )
            return list(await cur.fetchall())

        previous: tuple[int, float | None] | None = None
        for rung, opened_at, closed_at, until in self.run(rows()):
            if previous is None:
                assert rung == 0
            else:
                last_rung, last_closed = previous
                quiet = last_closed is not None and opened_at - last_closed >= DECAY
                assert rung == (0 if quiet else min(last_rung + 1, len(RUNGS) - 1))
            window = RUNGS[rung]
            assert until == (None if window is None else opened_at + window)
            previous = (rung, closed_at)


TestDecayMachine = DecayMachine.TestCase
TestDecayMachine.settings = settings(max_examples=40, stateful_step_count=25, deadline=None)
