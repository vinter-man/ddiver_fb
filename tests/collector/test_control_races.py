"""Control changes that land while a read runs act before the read's next browser action.

The owner's ``fbcollector pause`` is another process writing the control
row; the brain's answers (a pause, the lease, the pace cap) are applied by the uplink task beside
the read. Each race has a unit test at the door it goes through, and one property drives a real
read — the real reader, scheduler, production gates and store — through changes landing during
random steps, checking every browser action against the durable control row as it stood then.
"""

from __future__ import annotations

import asyncio
import random
import tempfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from fbcollector.budget import knobs_for
from fbcollector.clock import FakeClock
from fbcollector.config import NORMAL
from fbcollector.control import KV_CONTROL, ControlCenter, ControlState, effective_profile
from fbcollector.detectors import FEED_URL
from fbcollector.gates import Activity, GateContext
from fbcollector.ladder import Ladder
from fbcollector.model import CollectorState, ReadOutcome, Step, Surface
from fbcollector.reader import Reader, ReaderConfig
from fbcollector.rhythm import Jitter, Phase, Rhythm, RhythmConfig
from fbcollector.scheduler import Action, ActionKind, Grant, Hold
from fbcollector.store import Store
from fbwire.models import BudgetKind, Control, Profile

from .pacing_harness import Harness
from .pacing_support import DAY, HOUR, local, make_settings
from .reader_support import (
    FakeDriver,
    Recorder,
    feed_body,
    feed_document,
    nav,
    post,
    scroll,
    story,
)

GROUPS = [str(7000 + i) for i in range(3)]
CID = "home-1"


def full_cadence(seed: str = "harness-seed") -> float:
    """A weekday moment with two hours of full cadence ahead (no break, no shoulder)."""
    rhythm = Rhythm(RhythmConfig.from_settings(make_settings()), Jitter(seed))
    t = local(2026, 10, 7, 9)
    while not all(rhythm.phase(t + m * 60) is Phase.FULL for m in range(121)):
        t += 300
    return t


START = full_cadence()


def answer(*, paused: bool = False, holder: str = CID, cap: Profile | None = None) -> Control:
    """One ``GET /v1/control`` answer: the brain's whole word, as it re-sends it every poll."""
    return Control.model_validate(
        {
            "issued_at": 1_790_000_000,
            "lease": {"collector_id": holder, "granted": True, "generation": 1},
            "paused": paused,
            "pace_cap": cap.value if cap else None,
        }
    )


async def remote_harness(path: Path) -> Harness:
    """The pacing stack with an uplink: the lease granted, a login 30 days ago (normal pace)."""
    h = Harness(path, FakeClock(start=START), make_settings(), local_mode=False)
    await h.open()
    await h.prepare(login_at=START - 30 * DAY, groups=GROUPS)
    await h.control.apply(answer())
    return h


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    h = await remote_harness(tmp_path / "state.sqlite")
    yield h
    await h.close()


@asynccontextmanager
async def other_process(h: Harness) -> AsyncIterator[ControlCenter]:
    """The owner's CLI: its own connection to the same file, its own control center."""
    store = Store(h.path, clock=h.clock)
    await store.open()
    try:
        yield ControlCenter(
            store, Ladder(store, h.clock), h.clock, collector_id=CID, local_mode=False
        )
    finally:
        await store.close()


async def durable(h: Harness) -> ControlState:
    assert h.store is not None
    return ControlState.from_json(await h.store.kv_get(KV_CONTROL))


def feed_grant(h: Harness, *, steps: int = 3) -> Grant:
    """A feed read granted just now under the normal pace (as ``Scheduler.next`` would)."""
    action = Action(ActionKind.FEED, Surface.FEED, FEED_URL, scroll_allowance=steps, deep=True)
    return Grant(action, Profile.NORMAL, NORMAL, generation=h.control.generation)


async def scrolls_in_the_hour(h: Harness) -> int:
    assert h.store is not None
    return await h.store.budget_used(BudgetKind.SCROLL, HOUR)


# -- the control row ------------------------------------------------------------------------


async def test_the_generation_counts_changes_of_the_terms_only(harness: Harness) -> None:
    h = harness
    start = h.control.generation
    await h.control.apply(answer())  # the same word again: a poll, nothing changed
    assert h.control.generation == start
    await h.control.refresh()
    await h.control.refresh_done()
    assert await h.control.take_sync_nonce() is None
    assert h.control.generation == start  # the refresh bookkeeping is not a term
    await h.control.apply(answer(cap=Profile.CAUTIOUS))
    await h.control.apply(answer(cap=Profile.CAUTIOUS, paused=True))
    await h.control.apply(answer(cap=Profile.CAUTIOUS, paused=True, holder="laptop-1"))
    async with other_process(h) as cli:
        await cli.pause()
        await cli.pause()  # already paused: no change
        await cli.unpause()
    assert (await durable(h)).generation == start + 5
    assert (await h.control.load()).generation == start + 5


async def test_a_gate_check_reads_the_owners_pause_from_another_process(
    harness: Harness,
) -> None:
    h = harness
    assert (await h.control.gate(GateContext(Activity.SCROLL))).ok
    async with other_process(h) as cli:
        await cli.pause()
    # No load() in between: the gate itself reads the durable row.
    gate = await h.control.gate(GateContext(Activity.SCROLL))
    assert (gate.ok, gate.reason) == (False, "paused by the owner")


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    cli=st.lists(st.booleans(), min_size=1, max_size=6),
    brain=st.lists(
        st.tuples(st.booleans(), st.sampled_from([None, Profile.CAUTIOUS, Profile.NORMAL])),
        min_size=1,
        max_size=6,
    ),
)
async def test_writes_from_two_processes_never_undo_each_other(
    cli: list[bool], brain: list[tuple[bool, Profile | None]]
) -> None:
    """The owner's pause/unpause and the uplink's answers race on one row: each is a
    read-modify-write in one transaction, so neither is lost and every change of the terms
    moves the generation exactly once, whatever the interleaving."""
    with tempfile.TemporaryDirectory() as tmp:
        h = await remote_harness(Path(tmp) / "state.sqlite")
        try:
            before = await durable(h)
            async with other_process(h) as owner:

                async def owner_side() -> None:
                    for paused in cli:
                        await (owner.pause() if paused else owner.unpause())

                async def uplink_side() -> None:
                    for paused, cap in brain:
                        await h.control.apply(answer(paused=paused, cap=cap))

                await asyncio.gather(owner_side(), uplink_side())
            after = await durable(h)
        finally:
            await h.close()
    assert after.local_paused is cli[-1]
    assert (after.remote_paused, after.pace_cap) == brain[-1]
    flips = sum(1 for a, b in zip([False, *cli], cli, strict=False) if a != b)
    answers = [(before.remote_paused, before.pace_cap), *brain]
    changes = sum(1 for a, b in zip(answers, brain, strict=False) if a != b)
    assert after.generation == before.generation + flips + changes


async def test_wait_stopped_wakes_only_when_reading_must_stop(harness: Harness) -> None:
    h = harness
    waiter = asyncio.create_task(h.control.wait_stopped())
    await h.control.apply(answer())  # a poll: nothing changed
    await h.control.apply(answer(cap=Profile.CAUTIOUS))  # slower, still reading
    for _ in range(5):
        await asyncio.sleep(0)
    assert not waiter.done()
    await h.control.apply(answer(cap=Profile.CAUTIOUS, paused=True))
    assert await asyncio.wait_for(waiter, 1) == "paused by the brain"
    await h.control.apply(answer())
    waiter = asyncio.create_task(h.control.wait_stopped())
    async with other_process(h) as cli:
        await cli.pause()
    await asyncio.sleep(0)
    assert not waiter.done()  # another process's write is seen at the next reload
    assert not (await h.control.gate(GateContext(Activity.SCROLL))).ok  # a gate check reloads
    assert await asyncio.wait_for(waiter, 1) == "paused by the owner"


# -- the scheduler's door -------------------------------------------------------------------


async def test_a_pause_from_another_process_stops_the_next_scroll(harness: Harness) -> None:
    h = harness
    grant = feed_grant(h)
    assert await h.scheduler.scroll_permit(grant, steps_done=0, read_id=None) is None
    async with other_process(h) as cli:
        await cli.pause()
    held = await h.scheduler.scroll_permit(grant, steps_done=1, read_id=None)
    assert isinstance(held, Hold)
    assert (held.state, held.close_chrome) == (CollectorState.PAUSED, True)
    assert await scrolls_in_the_hour(h) == 1  # the refused step was never paid


async def test_a_pause_landing_after_the_permits_gates_is_not_paid(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The brain's pause arrives after the permit's gates passed and before its budget event:
    the terms are read again, held still, and the step is refused unpaid."""
    h = harness
    evaluate = h.scheduler._gates.evaluate

    async def racing(context: GateContext, **kw: Any) -> Any:
        report = await evaluate(context, **kw)
        await h.control.apply(answer(paused=True))
        return report

    monkeypatch.setattr(h.scheduler._gates, "evaluate", racing)
    held = await h.scheduler.scroll_permit(feed_grant(h), steps_done=0, read_id=None)
    assert isinstance(held, Hold)
    assert (held.state, held.close_chrome) == (CollectorState.PAUSED, True)
    assert held.reasons == ("control: paused by the brain",)
    assert await scrolls_in_the_hour(h) == 0


@pytest.mark.parametrize("pinned", [True, False], ids=["in_the_permit", "before_it"])
async def test_scroll_permits_pay_under_the_current_pace_cap(
    harness: Harness, pinned: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read granted at the normal pace, then capped to cautious by the brain: from its next
    step on it pays against the cautious caps (30 scrolls an hour), not the grant's (60)."""
    h = harness
    assert h.store is not None
    cautious = knobs_for(h.settings, Profile.CAUTIOUS).scroll_per_hour
    for _ in range(cautious - 1):
        await h.store.record_budget_event(BudgetKind.SCROLL)
    grant = feed_grant(h, steps=5)
    if pinned:
        # The brain's answer lands while the permit's gates are being checked.
        evaluate = h.scheduler._gates.evaluate

        async def racing(context: GateContext, **kw: Any) -> Any:
            report = await evaluate(context, **kw)
            await h.control.apply(answer(cap=Profile.CAUTIOUS))
            return report

        monkeypatch.setattr(h.scheduler._gates, "evaluate", racing)
    else:
        await h.control.apply(answer(cap=Profile.CAUTIOUS))
    assert await h.scheduler.scroll_permit(grant, steps_done=0, read_id=None) is None
    held = await h.scheduler.scroll_permit(grant, steps_done=1, read_id=None)
    assert isinstance(held, Hold) and held.reasons[0].startswith("scroll cap 30/")
    assert await scrolls_in_the_hour(h) == cautious


async def test_a_grant_is_discarded_when_the_terms_changed_before_its_navigation(
    harness: Harness,
) -> None:
    h = harness
    grant = feed_grant(h)
    assert await h.scheduler.still_granted(grant) is None
    await h.control.apply(answer(cap=Profile.CAUTIOUS))
    slower = await h.scheduler.still_granted(grant)
    assert isinstance(slower, Hold)
    assert (slower.state, slower.close_chrome) == (CollectorState.OK, False)  # plan again
    grant = feed_grant(h)
    async with other_process(h) as cli:
        await cli.pause()
    stopped = await h.scheduler.still_granted(grant)
    assert isinstance(stopped, Hold)
    assert (stopped.state, stopped.close_chrome) == (CollectorState.PAUSED, True)


async def drive_to_grant(h: Harness) -> Grant | Hold:
    """Ask the scheduler until it grants, or holds for anything but a read not yet due."""
    for _ in range(100):
        result = await h.scheduler.next()
        if isinstance(result, Grant):
            return result
        due = result.state is CollectorState.OK and result.reasons[0].startswith("next ")
        if not due:
            return result
        until = result.until if result.until is not None else h.clock.time() + 60
        h.clock.advance(max(1.0, until - h.clock.time()))
    raise AssertionError("never granted")


@pytest.mark.parametrize("change", ["pace_cap", "owner_pause", "brain_pause"])
async def test_a_change_during_the_gates_is_not_paid_under_the_old_terms(
    harness: Harness, change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The change lands after every gate passed and before the navigation is paid: nothing is
    paid under the old terms; a pause holds (Chrome closed), another pace plans again."""
    h = harness
    assert h.store is not None
    evaluate = h.scheduler._gates.evaluate
    armed = True

    async def racing(context: GateContext, **kw: Any) -> Any:
        report = await evaluate(context, **kw)
        if armed and report.ok:
            if change == "pace_cap":
                await h.control.apply(answer(cap=Profile.CAUTIOUS))
            elif change == "brain_pause":
                await h.control.apply(answer(paused=True))
            else:
                async with other_process(h) as cli:
                    await cli.pause()
        return report

    monkeypatch.setattr(h.scheduler._gates, "evaluate", racing)
    result = await drive_to_grant(h)
    assert isinstance(result, Hold)
    assert await h.store.budget_used(BudgetKind.NAVIGATION, DAY) == 0  # nothing was paid
    if change != "pace_cap":
        assert (result.state, result.close_chrome) == (CollectorState.PAUSED, True)
        return
    assert (result.state, result.close_chrome) == (CollectorState.OK, False)
    assert result.reasons == ("the control state changed; planning again",)
    armed = False
    again = await drive_to_grant(h)
    assert isinstance(again, Grant)
    assert again.profile is Profile.CAUTIOUS  # planned and paid under the new cap
    assert again.generation == h.control.generation


# -- the property ---------------------------------------------------------------------------

CHANGES = (
    "owner_pause",
    "owner_unpause",
    "brain_pause",
    "brain_resume",
    "lease_moved",
    "lease_back",
    "cap_cautious",
    "cap_normal",
    "cap_none",
)


@dataclass
class Brain:
    """The brain's word, changed one field at a time and re-sent whole, as the uplink does."""

    paused: bool = False
    holder: str = CID
    cap: Profile | None = None


@dataclass
class RaceDriver(FakeDriver):
    """Serves a feed read that never reaches its target, applies the scheduled control changes
    while a step is in flight, and checks every browser action against the durable row."""

    harness: Harness | None = None
    grant: Grant | None = None
    brain: Brain = field(default_factory=Brain)
    changes: dict[int, list[str]] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    """Kept, not raised: the reader takes any exception from a step for a browser failure."""
    counter: int = 0

    async def change(self, what: str) -> None:
        h = self.harness
        assert h is not None
        brain = self.brain
        if what in {"owner_pause", "owner_unpause"}:
            async with other_process(h) as cli:
                await (cli.pause() if what == "owner_pause" else cli.unpause())
            return
        if what in {"brain_pause", "brain_resume"}:
            brain.paused = what == "brain_pause"
        elif what in {"lease_moved", "lease_back"}:
            brain.holder = "laptop-1" if what == "lease_moved" else CID
        else:
            brain.cap = {"cap_cautious": Profile.CAUTIOUS, "cap_normal": Profile.NORMAL}.get(what)
        await h.control.apply(answer(paused=brain.paused, holder=brain.holder, cap=brain.cap))

    async def act(self, kind: str) -> None:
        """A browser action is issued now: it must be allowed by the row as it stands."""
        h, grant = self.harness, self.grant
        assert h is not None and h.store is not None and grant is not None
        state = await durable(h)
        verdict = h.control.verdict(state)
        if not verdict.ok:
            self.violations.append(f"{kind} while the control gate was shut ({verdict.reason})")
        if kind == "navigate" and state.generation != grant.generation:
            self.violations.append("navigated under terms changed since the grant")
        if kind == "scroll":
            profile = effective_profile(Profile.NORMAL, state.pace_cap)
            cap = knobs_for(h.settings, profile).scroll_per_hour
            used = await scrolls_in_the_hour(h)
            if used > cap:
                self.violations.append(f"scroll {used} over the cap {cap} in force")
        self.actions.append(kind)

    async def during(self) -> None:
        index = len(self.actions) - 1
        for what in self.changes.get(index, ()):
            await self.change(what)
        assert self.harness is not None
        self.harness.clock.advance(5)

    def fresh(self) -> Any:
        assert self.harness is not None
        now = self.harness.clock.time()
        self.counter += 1
        return [story(post(self.counter * 10 + i), GROUPS[i], now - 30) for i in range(3)]

    async def navigate(self, url: str) -> Step:
        await self.act("navigate")
        step = nav(url, feed_document(), feed_body(*self.fresh()))
        await self.during()
        return step

    async def scroll_step(self) -> Step:
        await self.act("scroll")
        step = scroll(FEED_URL, feed_body(*self.fresh()))
        await self.during()
        return step

    async def dwell(self, seconds: float) -> Step:
        await self.act("dwell")
        step = scroll(FEED_URL, fired=False)
        await self.during()
        return step


def schedule(pairs: Sequence[tuple[int, str]]) -> dict[int, list[str]]:
    table: dict[int, list[str]] = {}
    for index, what in pairs:
        table.setdefault(index, []).append(what)
    return table


@pytest.mark.timeout(300)
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    changes=st.lists(
        st.tuples(st.integers(min_value=-1, max_value=4), st.sampled_from(CHANGES)), max_size=4
    ),
    steps=st.integers(min_value=1, max_value=4),
    preload=st.integers(min_value=26, max_value=30),
)
async def test_every_browser_action_runs_under_the_control_state_in_force(
    changes: list[tuple[int, str]], steps: int, preload: int
) -> None:
    """Changes land after the grant (index -1) or while step ``index`` is in flight. Whatever
    they are: no navigation, scroll or final look is issued while the durable row shuts the
    control gate; the navigation happens only under the terms it was granted under; and every
    scroll fits the scroll cap of the pace in force when it is issued (the hour already holds
    ``preload`` scrolls: near the cautious cap of 30, far from the normal 60)."""
    with tempfile.TemporaryDirectory() as tmp:
        h = await remote_harness(Path(tmp) / "state.sqlite")
        try:
            assert h.store is not None
            for _ in range(preload):
                await h.store.record_budget_event(BudgetKind.SCROLL)
            grant = feed_grant(h, steps=steps)
            driver = RaceDriver(harness=h, grant=grant, changes=schedule(changes))
            for what in driver.changes.get(-1, ()):
                await driver.change(what)
            reader = Reader(
                store=h.store,
                driver=driver,
                scheduler=h.scheduler,
                ladder=h.ladder,
                events=Recorder(),
                clock=h.clock,
                config=ReaderConfig(CID, Path(tmp) / "raw", Path(tmp) / "shots"),
                rng=random.Random(5),
            )
            result = await reader.read(grant)
        finally:
            await h.close()
    assert driver.violations == []
    if not changes:
        assert driver.actions[0] == "navigate"  # without a change the read goes ahead
        assert result.outcome is not ReadOutcome.ABORTED
    event(f"outcome: {result.outcome.value}")
    event(f"actions: {len(driver.actions)}")
