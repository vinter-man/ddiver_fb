from __future__ import annotations

import dataclasses
import datetime as dt
import itertools
import math
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from fbcollector.clock import FakeClock
from fbcollector.control import RefreshResult
from fbcollector.detectors import JOINS_URL, group_url
from fbcollector.gates import KV_LOGIN_AT, Activity, GateContext, PowerSource
from fbcollector.ladder import ProbeRequest
from fbcollector.model import CollectorState, Coverage, GroupMeta, PageState, ReadOutcome, Surface
from fbcollector.rhythm import CATCH_UP_EXPIRES, DayPlan, Jitter, Phase, Rhythm, RhythmConfig
from fbcollector.scheduler import (
    BACKSTOP_STEPS,
    DEEP_STEPS,
    FLAG_NO_RECENT_POSTS,
    FLAG_PAUSED,
    KV_BACKSTOP_ONLY,
    KV_SCHEDULER,
    OBSERVE_BACKSTOP_STEPS,
    ORDINARY_STEPS,
    PROBE_DWELL,
    SESSION_BACKSTOPS,
    Action,
    ActionKind,
    Decision,
    GapTask,
    Grant,
    GroupView,
    Hold,
    Planner,
    SchedulerState,
    Session,
    Snapshot,
    gap_targets,
    local_profile,
)
from fbcollector.store import ProcessKind
from fbwire.models import BudgetKind, GroupState, Profile

from .pacing_harness import Harness
from .pacing_support import DAY, HOUR, make_settings, verdict

GROUPS = [str(1000 + i) for i in range(10)]


def planner(seed: str = "plan", **overrides: Any) -> Planner:
    settings = make_settings(**overrides)
    jitter = Jitter(seed)
    return Planner(settings, Rhythm(RhythmConfig.from_settings(settings), jitter), jitter)


def full_time(p: Planner, day: dt.date, minutes: int = 60) -> float:
    """A moment at full cadence with no break or shoulder for the next ``minutes``."""
    plan = p.rhythm.plan(day)
    t = max(plan.shoulder_end, plan.catch_up_at + CATCH_UP_EXPIRES) + 60
    while t < plan.shoulder_start:
        if all(plan.phase(t + m * 60) is Phase.FULL for m in range(minutes + 1)):
            return t
        t += 300
    raise AssertionError("no free hour")


def snap(now: float, **kw: Any) -> Snapshot:
    kw.setdefault("profile", Profile.NORMAL)
    return Snapshot(now=now, **kw)


def views(anchor: float, ids: list[str] = GROUPS, **kw: Any) -> tuple[GroupView, ...]:
    return tuple(GroupView(g, anchor=anchor, **kw) for g in ids)


def act(decision: Decision) -> Any:
    assert decision.action is not None, decision.wait
    return decision.action


DAY0 = dt.date(2026, 10, 7)  # a Wednesday


class TestProfiles:
    def test_local_profile(self) -> None:
        now = 100 * DAY
        assert local_profile(now, login_at=None, cautious_until=None) is Profile.CAUTIOUS
        assert local_profile(now, login_at=now - 13 * DAY, cautious_until=None) is Profile.CAUTIOUS
        assert local_profile(now, login_at=now - 15 * DAY, cautious_until=None) is Profile.NORMAL
        assert (
            local_profile(now, login_at=now - 30 * DAY, cautious_until=now + 1) is Profile.CAUTIOUS
        )
        assert local_profile(now, login_at=now - 30 * DAY, cautious_until=now - 1) is Profile.NORMAL


class TestWaits:
    def test_quiet_break_paused_blocked(self) -> None:
        p = planner()
        plan = p.rhythm.plan(DAY0)
        quiet = p.decide(snap(plan.quiet_start + 60))
        assert quiet.wait is not None
        assert (quiet.wait.state, quiet.wait.until, quiet.wait.close_chrome) == (
            CollectorState.QUIET,
            plan.quiet_end,
            True,
        )
        b = plan.breaks[0]
        brk = p.decide(snap(b.start + 1))
        assert brk.wait is not None
        assert (brk.wait.state, brk.wait.until) == (CollectorState.BREAK, b.end)
        noon = full_time(p, DAY0)
        paused = p.decide(snap(noon, paused=True))
        assert paused.wait is not None
        assert (paused.wait.state, paused.wait.until) == (CollectorState.PAUSED, None)
        blocked = p.decide(snap(noon, blocked=True))
        assert blocked.wait is not None and blocked.wait.state is CollectorState.BLOCKED

    def test_the_probe_is_the_only_read_while_blocked(self) -> None:
        p = planner()
        noon = full_time(p, DAY0)
        probe = ProbeRequest(block_id=7, not_before=noon + 100)
        s = snap(
            noon, blocked=True, probe=probe, last_feed_at=noon - 3600, groups=views(noon - DAY)
        )
        early = p.decide(s)
        assert early.wait is not None and early.wait.until == noon + 100
        action = act(p.decide(dataclasses.replace(s, now=noon + 100)))
        assert action.kind is ActionKind.PROBE
        assert (action.block_id, action.scroll_allowance, action.min_dwell) == (7, 0, PROBE_DWELL)
        assert action.surface is Surface.FEED
        # ...and it respects quiet hours like any other read
        plan = p.rhythm.plan(DAY0)
        night = p.decide(dataclasses.replace(s, now=plan.next_quiet_start + 60))
        assert night.wait is not None and night.wait.state is CollectorState.QUIET


class TestFeed:
    def test_first_read_after_an_epoch(self) -> None:
        p = planner()
        noon = full_time(p, DAY0)
        first = p.decide(snap(noon))
        assert first.new_epoch
        assert first.state.epoch == noon
        # waiting for it does not move the epoch (no postponement for ever)
        t = noon
        decision = first
        while True:
            assert decision.wait is not None and decision.wait.until is not None
            t = max(decision.wait.until, t + 1)
            decision = p.decide(snap(t, state=decision.state))
            if decision.action is not None:
                break
            assert not decision.new_epoch
            assert decision.state.epoch == noon
        assert act(decision).kind is ActionKind.FEED
        assert t - noon <= 120
        # the read that resumes carries the epoch to the moment reading actually resumed
        assert decision.new_epoch
        assert decision.state.epoch == t

    @pytest.mark.parametrize(
        ("profile", "low", "high"), [(Profile.NORMAL, 4.5, 9.5), (Profile.CAUTIOUS, 10, 18)]
    )
    def test_cadence(self, profile: Profile, low: float, high: float) -> None:
        p = planner()
        noon = full_time(p, DAY0, minutes=20)
        state = SchedulerState(epoch=noon - HOUR)
        gaps = []
        for k in range(40):
            last = noon + k * 0.37
            s = snap(
                last + 1, profile=profile, state=state, last_feed_at=last, last_navigation_at=last
            )
            wait = p.decide(s).wait
            assert wait is not None and wait.until is not None
            gaps.append((wait.until - last) / 60)
        assert all(low <= g <= high for g in gaps)
        assert max(gaps) - min(gaps) > (high - low) / 3  # jittered, not a grid

    def test_shoulder_cadence(self) -> None:
        p = planner()
        plan = p.rhythm.plan(DAY0)
        t = plan.quiet_end + 120
        s = snap(
            t, state=SchedulerState(epoch=t - 60), last_feed_at=t - 60, last_navigation_at=t - 60
        )
        wait = p.decide(s).wait
        assert wait is not None and wait.until is not None
        assert wait.until == min(plan.shoulder_end, wait.until)  # re-plans at the shoulder's end
        low, high = p.feed_bounds(Profile.NORMAL, Phase.SHOULDER)
        assert (low, high) == (15 * 60, 25 * 60)

    def test_after_a_break_the_feed_waits_for_the_break_end(self) -> None:
        p = planner()
        plan = p.rhythm.plan(DAY0)
        b = plan.breaks[0]
        before = b.start - 30
        s = snap(
            b.end + 1,
            state=SchedulerState(epoch=before - 600),
            last_feed_at=before,
            last_navigation_at=before,
        )
        decision = p.decide(s)
        due = decision.wait.until if decision.wait else s.now
        assert due is not None
        assert b.end <= due <= b.end + 120

    def test_allowances(self) -> None:
        p = planner()
        noon = full_time(p, DAY0, minutes=10)
        steps = []
        for k in range(400):
            anchor = noon - 600 - k
            s = snap(
                noon,
                state=SchedulerState(epoch=anchor - 60),
                last_feed_at=anchor,
                last_navigation_at=anchor,
            )
            action = act(p.decide(s))
            steps.append(action.scroll_allowance)
            assert action.deep is (action.scroll_allowance == DEEP_STEPS)
        assert set(steps) == {ORDINARY_STEPS, DEEP_STEPS}
        assert 0.15 < steps.count(DEEP_STEPS) / len(steps) < 0.35

    def test_the_days_catch_up(self) -> None:
        p = planner()
        plan: DayPlan = p.rhythm.plan(DAY0)
        t = plan.catch_up_at + 1
        state = SchedulerState(epoch=plan.quiet_end)
        taken = []
        for _ in range(len(plan.catch_up_steps) + 1):
            s = snap(t, state=state, last_feed_at=t - 3600, last_navigation_at=t - 26 * 60)
            decision = p.decide(s)
            action = act(decision)
            taken.append((action.catch_up, action.scroll_allowance))
            assert action.deep or not action.catch_up  # a catch-up reads back to T_feed
            state = p.after_grant(decision.state, action, t)
            t += 26 * 60  # the next feed read is due by then, even at shoulder cadence
        assert [a for c, a in taken if c] == list(plan.catch_up_steps)
        assert taken[-1][0] is False
        # a catch-up not begun within three hours is skipped
        late = snap(
            plan.catch_up_at + 3 * HOUR + 1,
            state=SchedulerState(epoch=plan.quiet_end),
            last_feed_at=plan.quiet_end,
            last_navigation_at=plan.quiet_end + 3 * HOUR,
        )
        assert not act(p.decide(late)).catch_up


class TestBackstops:
    def setup(
        self, *, profile: Profile = Profile.NORMAL, **kw: Any
    ) -> tuple[Planner, float, Snapshot]:
        p = planner(**kw)
        noon = full_time(p, DAY0, minutes=60)
        epoch = noon - 5 * HOUR
        feed = noon - 30
        s = snap(
            noon,
            profile=profile,
            groups=views(epoch - DAY, last_backstop_at=epoch - 1),  # all long overdue
            state=SchedulerState(epoch=epoch, session=Session(started_at=feed, last_at=feed)),
            last_feed_at=feed,
            last_navigation_at=feed,
            last_read_end=feed + 20,
        )
        return p, noon, s

    def test_a_session_backstop_follows_the_feed(self) -> None:
        p, _noon, s = self.setup()
        decision = p.decide(s)
        assert decision.wait is not None and decision.wait.until is not None
        assert 45 <= decision.wait.until - (s.last_read_end or 0) <= 120
        action = act(p.decide(dataclasses.replace(s, now=decision.wait.until)))
        assert action.kind is ActionKind.BACKSTOP
        assert action.scroll_allowance == BACKSTOP_STEPS
        assert action.url.endswith(f"/groups/{action.group_id}/?sorting_setting=CHRONOLOGICAL")

    def test_at_most_two_per_session_and_never_in_the_shoulder(self) -> None:
        p, noon, s = self.setup()
        spent = Session(noon - 30, SESSION_BACKSTOPS, noon - 30)
        full = dataclasses.replace(
            s, state=dataclasses.replace(s.state, session=spent), now=noon + 200
        )
        decision = p.decide(full)
        assert decision.action is None or decision.action.kind is not ActionKind.BACKSTOP
        plan = p.rhythm.plan(DAY0)
        shoulder = dataclasses.replace(
            s, now=plan.quiet_end + 600, last_read_end=plan.quiet_end + 500
        )
        decision = p.decide(shoulder)
        assert decision.action is None or decision.action.kind is not ActionKind.BACKSTOP

    def test_an_idle_session_is_over(self) -> None:
        p, noon, s = self.setup()
        idle = dataclasses.replace(s, now=noon + 11 * 60, last_read_end=noon - 30)
        decision = p.decide(idle)
        assert decision.action is None or decision.action.kind is ActionKind.FEED

    def test_observe_mode_scrolls_deeper(self) -> None:
        p, _noon, s = self.setup(observe_mode=True)
        wait = p.decide(s).wait
        assert wait is not None and wait.until is not None
        assert (
            act(p.decide(dataclasses.replace(s, now=wait.until))).scroll_allowance
            == OBSERVE_BACKSTOP_STEPS
        )

    def test_revisit_and_paused_groups_are_skipped(self) -> None:
        p, noon, s = self.setup()
        only = dataclasses.replace(
            s,
            groups=(
                GroupView(
                    "1", anchor=0, last_backstop_at=noon - 6 * HOUR, last_navigation_at=noon - 600
                ),
                GroupView("2", anchor=0, paused=True),
            ),
        )
        decision = p.decide(only)
        until = decision.wait.until if decision.wait else noon
        assert until is not None
        later = p.decide(dataclasses.replace(only, now=until))
        assert later.action is None or later.action.group_id != "2"

    def test_gap_backstops_come_first(self) -> None:
        p, noon, s = self.setup()
        gap = dataclasses.replace(
            s, state=dataclasses.replace(s.state, gaps=(GapTask("1005", noon - 100),))
        )
        wait = p.decide(gap).wait
        assert wait is not None and wait.until is not None
        action = act(p.decide(dataclasses.replace(gap, now=wait.until)))
        assert (action.group_id, action.reason) == ("1005", "gap")
        after = p.after_grant(gap.state, action, wait.until)
        assert after.gaps == ()
        assert after.session is not None and after.session.backstops == 1
        # below 20 % of the day's budget, gap backstops are skipped
        short = dataclasses.replace(gap, now=wait.until, budget_day_remaining=0.1)
        assert act(p.decide(short)).reason == "scheduled"

    def test_a_gap_task_waiting_for_its_revisit_does_not_starve_the_others(self) -> None:
        """A busy group that gaps on every read must not take every backstop of the day."""
        p, noon, s = self.setup()
        busy = dataclasses.replace(s.groups[5], last_navigation_at=noon - 60)  # read a minute ago
        groups = (*s.groups[:5], busy, *s.groups[6:])
        waiting = dataclasses.replace(
            s,
            groups=groups,
            state=dataclasses.replace(s.state, gaps=(GapTask(busy.id, noon - 30),)),
        )
        decision = p.decide(waiting)
        assert decision.wait is not None and decision.wait.until is not None
        action = act(p.decide(dataclasses.replace(waiting, now=decision.wait.until)))
        assert action.kind is ActionKind.BACKSTOP
        assert (action.group_id, action.reason) != (busy.id, "gap")
        assert action.reason == "scheduled"

    def test_backstop_due_times(self) -> None:
        p = planner()
        interval = 2 * HOUR
        chained = [
            p.backstop_due(GroupView("g", anchor=0, last_backstop_at=1000.0 + k), 0.0, interval)
            - (1000.0 + k)
            for k in range(200)
        ]
        assert all(0.7 * interval <= d <= 1.3 * interval for d in chained)
        restarted = [
            p.backstop_due(GroupView(str(k), anchor=0, last_backstop_at=10.0), 5000.0, interval)
            - 5000.0
            for k in range(200)
        ]
        assert all(0.25 * interval <= d <= interval for d in restarted)
        fresh = p.backstop_due(GroupView("n", anchor=9000.0), 5000.0, interval)
        assert fresh >= 9000.0 + 0.25 * interval

    def test_backstop_only_mode(self) -> None:
        p, noon, s = self.setup()
        only = dataclasses.replace(s, backstop_only=True, state=SchedulerState(epoch=s.state.epoch))
        decision = p.decide(only)
        until = decision.wait.until if decision.wait else noon
        assert until is not None
        action = act(p.decide(dataclasses.replace(only, now=until)))
        assert action.kind is ActionKind.BACKSTOP  # no feed, no session needed
        assert p.interval(only) is not None
        assert p.interval(only) < (p.interval(s) or 0)  # type: ignore[operator]

    def test_no_groups_no_backstops(self) -> None:
        p, _noon, s = self.setup()
        empty = dataclasses.replace(s, groups=())
        assert p.interval(empty) is None
        decision = p.decide(empty)
        assert decision.action is None or decision.action.kind is not ActionKind.BACKSTOP


class TestSmoothing:
    def test_a_backstop_yields_the_last_slot_to_the_feed(self) -> None:
        p = planner()
        noon = full_time(p, DAY0, minutes=40)
        feed = noon - 30
        recent = (noon - 500, noon - 400, noon - 300, feed)  # 4 in the 15-minute window
        s = snap(
            noon,
            groups=views(noon - DAY, last_backstop_at=noon - 5 * HOUR - 1),
            state=SchedulerState(epoch=noon - 5 * HOUR, session=Session(feed, 0, feed)),
            last_feed_at=feed,
            last_navigation_at=feed,
            last_read_end=feed + 10,
            recent_navigations=recent,
        )
        t = p.admission_time(s, noon)
        # room for the backstop now, but not for it *and* the next feed 4.5 min after the last:
        # it waits until the oldest event has left the window
        assert t == pytest.approx(recent[0] + 900 + 1e-3)
        roomy = dataclasses.replace(
            s, recent_navigations=(noon - 800, noon - 700, noon - 600, feed)
        )
        assert p.admission_time(roomy, noon) == noon
        assert math.isinf(
            planner(normal={"nav_per_15min": 1}).admission_time(dataclasses.replace(s), noon)
        )
        quiet = dataclasses.replace(s, recent_navigations=())
        assert p.admission_time(quiet, noon) == noon

    def test_the_daily_sync_waits_for_room_too(self) -> None:
        p = planner()
        plan = p.rhythm.plan(DAY0)
        t = plan.sync_at
        if plan.phase(t) is not Phase.FULL:  # pragma: no cover - sync_at is chosen at full cadence
            pytest.skip("sync inside a break")
        s = snap(
            t, state=SchedulerState(epoch=t - HOUR), last_feed_at=t - 60, last_navigation_at=t - 60
        )
        decision = p.decide(s)
        action = decision.action
        assert action is not None
        assert (action.kind, action.reason) == (ActionKind.SYNC, "daily")
        done = dataclasses.replace(s, last_sync_at=t)
        after = p.decide(done)
        assert after.action is None or after.action.kind is not ActionKind.SYNC


class TestDowntime:
    def test_backstops_restart_at_full_cadence_not_in_the_morning_shoulder(self) -> None:
        p = planner()
        plan = p.rhythm.plan(DAY0)
        morning = plan.quiet_end + 120
        assert p.restart_anchor(morning) == plan.shoulder_end
        noon = full_time(p, DAY0)
        assert p.restart_anchor(noon) == noon
        evening = plan.shoulder_start + 60
        assert p.restart_anchor(evening) == evening  # a new epoch comes after the night anyway
        interval = 2 * HOUR
        due = [p.backstop_due(GroupView(str(k), anchor=0), morning, interval) for k in range(100)]
        assert min(due) >= plan.shoulder_end + 0.25 * interval

    def test_overdue_backstops_restart_from_a_jittered_epoch(self) -> None:
        p = planner()
        noon = full_time(p, DAY0, minutes=20)
        last = noon - 3 * HOUR  # more than 2 h without a navigation
        s = snap(
            noon,
            groups=views(noon - 10 * DAY, last_backstop_at=last - 5 * HOUR),
            state=SchedulerState(epoch=last - 6 * HOUR, session=Session(last, 0, last)),
            last_feed_at=last,
            last_navigation_at=last,
            last_read_end=last + 20,
        )
        decision = p.decide(s)
        assert decision.new_epoch
        assert decision.state.epoch == noon
        assert decision.state.session is None
        interval = p.interval(s)
        assert interval is not None
        for g in s.groups:
            assert p.backstop_due(g, noon, interval) >= noon + 0.25 * interval


class TestDelaysAndState:
    def test_delay_bounds(self) -> None:
        p = planner()
        base = snap(
            0.0,
            groups=(
                GroupView("1", 0),
                GroupView("2", 0, quiet=True),
                GroupView("3", 0, paused=True),
            ),
        )
        interval = p.interval(base)
        assert interval is not None
        bounds = p.delay_bounds(base)
        assert bounds["1"] == pytest.approx(interval * 1.3)
        assert bounds["2"] == bounds["1"]
        assert math.isinf(bounds["3"])
        short = p.delay_bounds(dataclasses.replace(base, budget_day_remaining=0.2))
        assert short["2"] == pytest.approx(2 * short["1"])
        assert all(
            math.isinf(v)
            for v in p.delay_bounds(
                dataclasses.replace(base, groups=(GroupView("1", 0, paused=True),))
            ).values()
        )

    def test_gaps(self) -> None:
        state = Planner.with_gap(SchedulerState(), ["a", "b", "a", "c", "d"], 10.0)
        assert [g.group_id for g in state.gaps] == ["a", "b", "c"]
        again = Planner.with_gap(state, ["a", "e"], 20.0)
        assert [g.group_id for g in again.gaps] == ["a", "b", "c", "e"]
        assert gap_targets(["x", "y", "z", "w", "x"], {"y": 3.0, "w": 5.0}) == ["w", "y", "x"]

    def test_pruning(self) -> None:
        p = planner()
        noon = full_time(p, DAY0)
        state = SchedulerState(
            epoch=noon - HOUR,
            gaps=(
                GapTask("done", noon - 600),
                GapTask("gone", noon - 600),
                GapTask("old", noon - 7 * HOUR),
                GapTask("keep", noon - 600),
                GapTask("paused", noon - 600),
            ),
        )
        s = snap(
            noon,
            state=state,
            groups=(
                GroupView("done", 0, last_backstop_at=noon - 300),
                GroupView("old", 0),
                GroupView("keep", 0),
                GroupView("paused", 0, paused=True),
            ),
        )
        kept = p.decide(s).state
        assert [g.group_id for g in kept.gaps] == ["keep"]

    def test_state_json(self) -> None:
        state = SchedulerState(
            epoch=1.5,
            session=Session(2.0, 1, 3.0),
            catch_up_day="2026-10-07",
            catch_up_done=2,
            gaps=(GapTask("1", 4.0),),
        )
        assert SchedulerState.from_json(state.to_json()) == state
        anchored = dataclasses.replace(state, last_feed=7.0, last_sync=8.0)
        assert SchedulerState.from_json(anchored.to_json()) == anchored
        assert SchedulerState.from_json(None) == SchedulerState()
        assert SchedulerState().catch_up_count(DAY0) == 0
        assert state.catch_up_count(DAY0) == 2

    def test_after_grant_for_sync_and_probe(self) -> None:
        p = planner()
        state = SchedulerState(session=Session(1.0, 1, 1.0))
        sync = Action(ActionKind.SYNC, Surface.JOINS, JOINS_URL)
        assert p.after_grant(state, sync, 5.0) == dataclasses.replace(state, last_sync=5.0)
        probe = dataclasses.replace(sync, kind=ActionKind.PROBE)
        after_probe = p.after_grant(state, probe, 5.0)
        assert (after_probe.session, after_probe.last_feed) == (None, 5.0)
        orphan = dataclasses.replace(sync, kind=ActionKind.BACKSTOP, group_id="1")
        assert p.after_grant(SchedulerState(), orphan, 5.0).session is None

    def test_a_granted_feed_counts_even_if_its_read_never_finished(self) -> None:
        p = planner()
        noon = full_time(p, DAY0)
        state = SchedulerState(epoch=noon - HOUR, last_feed=noon - 60)
        s = snap(noon, state=state, last_feed_at=noon - 3600, last_navigation_at=noon - 60)
        wait = p.decide(s).wait
        assert wait is not None and wait.until is not None
        assert wait.until >= noon - 60 + 4.5 * 60


# -- the door ---------------------------------------------------------------------------------

SEED = "harness-seed"


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    start = full_time(planner(SEED), DAY0, minutes=120)
    h = Harness(tmp_path / "state.sqlite", FakeClock(start=start), make_settings(), seed=SEED)
    await h.open()
    await h.prepare(login_at=start - 30 * DAY, groups=GROUPS)
    yield h
    await h.close()


async def perform(h: Harness, grant: Grant, *, outcome: ReadOutcome = ReadOutcome.COMPLETE) -> int:
    """What the read loop does with a grant: a read row, its scroll steps, then the end."""
    assert h.store is not None
    read_id = await h.store.start_read(grant.action.surface, group_id=grant.action.group_id)
    steps = 0
    while await h.scheduler.scroll_permit(grant, steps_done=steps, read_id=read_id) is None:
        steps += 1
        h.clock.advance(6)
    h.clock.advance(15)
    await h.store.finish_read(read_id, coverage=Coverage(), steps=steps, outcome=outcome)
    await h.scheduler.read_finished(grant, outcome=outcome)
    return steps


async def drive(h: Harness, *, limit: float, stop: Any = None) -> list[tuple[float, Grant]]:
    grants: list[tuple[float, Grant]] = []
    end = h.clock.time() + limit
    while h.clock.time() < end:
        result = await h.scheduler.next()
        if isinstance(result, Grant):
            grants.append((h.clock.time(), result))
            await perform(h, result)
            if stop is not None and stop(result):
                break
            continue
        until = result.until if result.until is not None else h.clock.time() + 600
        h.clock.advance(max(1.0, until - h.clock.time()))
    return grants


class TestScheduler:
    async def test_grant_pays_first_and_sessions_carry_backstops(self, harness: Harness) -> None:
        h = harness
        assert h.store is not None
        result = await h.scheduler.next()
        assert isinstance(result, Hold)  # the first read of a fresh epoch waits a little
        raw = await h.store.kv_get(KV_SCHEDULER)
        assert raw is not None and SchedulerState.from_json(raw).epoch == h.clock.time()
        grants = await drive(h, limit=3 * HOUR)
        kinds = [g.action.kind for _, g in grants]
        assert kinds[0] is ActionKind.FEED
        assert ActionKind.BACKSTOP in kinds
        assert await h.store.budget_used(BudgetKind.NAVIGATION, 4 * HOUR) == len(grants)
        # every backstop rides a session: at most two after each feed read, within minutes
        since_feed = 0
        last_feed_at = 0.0
        for at, grant in grants:
            if grant.action.kind is ActionKind.FEED:
                since_feed, last_feed_at = 0, at
            elif grant.action.kind is ActionKind.BACKSTOP:
                since_feed += 1
                assert since_feed <= SESSION_BACKSTOPS
                assert at - last_feed_at < 15 * 60
        gaps = [b[0] - a[0] for a, b in itertools.pairwise(grants)]
        assert min(gaps) >= 45

    async def test_a_recent_login_is_paced_cautiously(self, harness: Harness) -> None:
        h = harness
        assert h.store is not None
        await h.store.kv_set(KV_LOGIN_AT, repr(h.clock.time() - 3 * DAY))
        grant = (await drive(h, limit=HOUR, stop=lambda g: True))[0][1]
        assert grant.profile is Profile.CAUTIOUS
        assert grant.knobs == h.settings.cautious
        await h.store.kv_set(KV_LOGIN_AT, repr(h.clock.time() - 15 * DAY))
        later = (await drive(h, limit=HOUR, stop=lambda g: True))[0][1]
        assert later.knobs == h.settings.normal

    async def test_nothing_while_a_gate_is_closed(self, harness: Harness) -> None:
        h = harness
        assert h.store is not None
        h.probes.source = PowerSource.BATTERY
        grants = await drive(h, limit=HOUR)
        assert grants == []
        assert await h.store.budget_used(BudgetKind.NAVIGATION, DAY) == 0
        result = await h.scheduler.next()
        assert isinstance(result, Hold)
        assert result.close_chrome

    async def test_scroll_stops_when_a_gate_closes(self, harness: Harness) -> None:
        h = harness
        assert h.store is not None
        grant = (await drive(h, limit=HOUR, stop=lambda g: True))[0][1]
        h.probes.source = PowerSource.BATTERY
        held = await h.scheduler.scroll_permit(grant, steps_done=0, read_id=None)
        assert isinstance(held, Hold) and held.state is CollectorState.NOT_HOME

    async def test_quiet_hours_need_no_gate_checks(self, harness: Harness) -> None:
        h = harness
        plan = h.planner.rhythm.plan_at(h.clock.time())
        h.clock.advance(plan.next_quiet_start + 60 - h.clock.time())
        calls = h.lookup.calls
        result = await h.scheduler.next()
        assert isinstance(result, Hold) and result.state is CollectorState.QUIET
        assert h.lookup.calls == calls
        gate = await h.scheduler.rhythm_gate(GateContext(Activity.SCROLL))
        assert (gate.ok, gate.state) == (False, CollectorState.QUIET)
        b = h.planner.rhythm.plan_at(h.clock.time() + DAY).breaks[0]
        h.clock.advance(b.start + 1 - h.clock.time())
        gate = await h.scheduler.rhythm_gate(GateContext(Activity.SCROLL))
        assert (gate.ok, gate.state) == (False, CollectorState.BREAK)

    async def test_warmup_and_needs_login(self, harness: Harness) -> None:
        h = harness
        assert h.store is not None
        await h.store.kv_set(KV_LOGIN_AT, repr(h.clock.time() - HOUR))
        result = await h.scheduler.next()
        while isinstance(result, Hold) and result.state is CollectorState.OK:
            assert result.until is not None
            h.clock.advance(max(1.0, result.until - h.clock.time()))
            result = await h.scheduler.next()
        assert isinstance(result, Hold) and result.state is CollectorState.WARMUP
        assert await h.scheduler.profile() is Profile.CAUTIOUS
        await h.store.kv_delete(KV_LOGIN_AT)
        result = await h.scheduler.next()
        while isinstance(result, Hold) and result.state is CollectorState.OK:
            assert result.until is not None
            h.clock.advance(max(1.0, result.until - h.clock.time()))
            result = await h.scheduler.next()
        assert isinstance(result, Hold) and result.state is CollectorState.NEEDS_LOGIN

    async def test_paused_blocked_and_the_probe(self, harness: Harness) -> None:
        h = harness
        await h.control.pause()
        result = await h.scheduler.next()
        assert isinstance(result, Hold) and result.state is CollectorState.PAUSED
        await h.control.unpause()
        block = await h.ladder.trip(PageState.BLOCKED, reason="x")
        result = await h.scheduler.next()
        assert isinstance(result, Hold) and result.state is CollectorState.BLOCKED
        assert result.until is None
        h.clock.advance(6 * HOUR)
        assert await h.control.resume(block.id) == "accepted"
        grants = await drive(h, limit=DAY, stop=lambda g: True)
        grant = grants[0][1]
        assert grant.action.kind is ActionKind.PROBE
        assert grant.action.block_id == block.id
        outcome = await h.ladder.on_verdict(verdict(PageState.OK), probe_block_id=block.id)
        assert outcome.outcome.value == "resumed"
        assert await h.scheduler.profile() is Profile.CAUTIOUS  # 7 days after a warning

    async def test_budget_denial_holds_with_a_retry(self, harness: Harness) -> None:
        h = harness
        assert h.store is not None
        for _ in range(5):
            await h.store.record_budget_event(BudgetKind.NAVIGATION)
        h.clock.advance(60)
        result = await h.scheduler.next()
        for _ in range(10):
            assert isinstance(result, Hold)
            if result.reasons[0].startswith("navigation cap"):
                break
            assert result.until is not None
            h.clock.advance(max(1.0, result.until - h.clock.time()))
            result = await h.scheduler.next()
        assert isinstance(result, Hold)
        assert result.reasons[0].startswith("navigation cap")
        assert result.until is not None and result.until > h.clock.time()
        assert not result.close_chrome

    async def test_a_grant_that_starts_chrome_carries_its_launch(self, harness: Harness) -> None:
        h = harness
        assert h.store is not None
        launched: list[Grant] = []
        while len(launched) < 3:
            result = await h.scheduler.next(launch=True)
            if isinstance(result, Grant):
                launched.append(result)
                await perform(h, result)
            else:
                assert result.state is CollectorState.OK
                assert result.until is not None
                h.clock.advance(max(1.0, result.until - h.clock.time()))
        launches = await h.store.process_events_since(HOUR, kinds=[ProcessKind.CHROME_LAUNCH])
        assert launches == 3
        # The 4th launch within the hour is refused at the door, before anything is paid.
        paid = await h.store.budget_used(BudgetKind.NAVIGATION, DAY)
        state = h.scheduler.state
        result = await h.scheduler.next(launch=True)
        while isinstance(result, Hold) and result.state is CollectorState.OK:
            assert result.until is not None
            h.clock.advance(max(1.0, result.until - h.clock.time()))
            result = await h.scheduler.next(launch=True)
        assert isinstance(result, Hold) and result.state is CollectorState.CRASH_LOOP
        assert result.close_chrome
        assert await h.store.budget_used(BudgetKind.NAVIGATION, DAY) == paid
        assert h.scheduler.state.last_feed == state.last_feed  # no phantom read either
        # With Chrome already running the same read needs no launch and is granted.
        assert isinstance(await h.scheduler.next(parked=True), Grant)

    async def test_the_launchs_planned_word_reaches_the_crash_loop_gate(
        self, harness: Harness
    ) -> None:
        """Review finding: both gate checks of a launch, the self-test's too, must leave a
        planned launch out of the unplanned count, and record it planned."""
        h = harness
        assert h.store is not None
        for _ in range(3):
            await h.store.record_process_event(ProcessKind.CHROME_LAUNCH)
        result = await h.scheduler.next(launch=True)
        while isinstance(result, Hold) and result.state is CollectorState.OK:
            assert result.until is not None
            h.clock.advance(max(1.0, result.until - h.clock.time()))
            result = await h.scheduler.next(launch=True)
        assert isinstance(result, Hold) and result.state is CollectorState.CRASH_LOOP
        assert result.reasons == ("crash_loop: 4 unplanned chrome launches in the last hour",)

        async def untested() -> bool:
            return True

        result = await h.scheduler.next(launch=True, selftest=untested, launch_planned=True)
        while isinstance(result, Hold) and result.state is CollectorState.OK:
            assert result.until is not None
            h.clock.advance(max(1.0, result.until - h.clock.time()))
            result = await h.scheduler.next(launch=True, selftest=untested, launch_planned=True)
        assert isinstance(result, Grant) and result.selftest
        assert await h.store.process_events_since(HOUR, planned=True) == 2
        assert await h.store.process_events_since(HOUR) == 3

    async def test_the_resume_probe_passes_the_launch_door(self, harness: Harness) -> None:
        h = harness
        assert h.store is not None
        block = await h.ladder.trip(PageState.BLOCKED, reason="x")
        h.clock.advance(6 * HOUR)
        assert await h.ladder.resume(block.id, "nonce-launch-1") == "accepted"
        result = await h.scheduler.next(launch=True)
        assert isinstance(result, Grant)
        assert result.action.kind is ActionKind.PROBE
        launches = await h.store.process_events_since(HOUR, kinds=[ProcessKind.CHROME_LAUNCH])
        assert launches == 1
        assert await h.scheduler.recheck(result) is None  # the probe passes the block gate

    async def test_a_parked_chrome_closes_when_a_gate_closes(self, harness: Harness) -> None:
        h = harness
        grant = (await drive(h, limit=HOUR, stop=lambda g: True))[0][1]
        waiting = await h.scheduler.next(parked=True)
        assert isinstance(waiting, Hold) and not waiting.close_chrome  # parked, all open
        h.probes.source = PowerSource.BATTERY
        plain = await h.scheduler.next()
        assert isinstance(plain, Hold) and not plain.close_chrome  # a plain wait checks nothing
        closed = await h.scheduler.next(parked=True)
        assert isinstance(closed, Hold)
        assert closed.close_chrome
        assert closed.state is CollectorState.NOT_HOME
        assert any(r.startswith("power:") for r in closed.reasons)
        assert closed.until == waiting.until  # the next read's time, not the gate's retry
        held = await h.scheduler.recheck(grant)
        assert held is not None and held.close_chrome

    async def test_a_refresh_becomes_a_sync(self, harness: Harness) -> None:
        h = harness
        assert await h.control.refresh() is RefreshResult.SCHEDULED
        grants = await drive(h, limit=2 * HOUR, stop=lambda g: g.action.kind is ActionKind.SYNC)
        assert grants[-1][1].action.reason == "refresh"
        assert h.control.refresh_due() is None

    async def test_a_listings_group_is_read_through_its_listings_url(
        self, harness: Harness
    ) -> None:
        from fbwire.models import MonitoredGroup

        h = harness
        assert h.store is not None
        await h.store.set_monitored(
            [MonitoredGroup(group_id=g, listings=g == GROUPS[0]) for g in GROUPS]
        )
        snapshot = await h.scheduler.snapshot()
        assert {g.id for g in snapshot.groups if g.listings} == {GROUPS[0]}
        grants = await drive(h, limit=6 * HOUR, stop=lambda g: g.action.group_id == GROUPS[0])
        action = grants[-1][1].action
        assert action.group_id == GROUPS[0]
        assert action.url == group_url(GROUPS[0], listings=True)

    async def test_backstop_only_and_flags(self, harness: Harness) -> None:
        h = harness
        assert h.store is not None
        await h.store.kv_set(KV_BACKSTOP_ONLY, "1")
        await h.store.set_group_flags(GROUPS[0], [FLAG_PAUSED])
        await h.store.set_group_flags(GROUPS[1], [FLAG_NO_RECENT_POSTS])
        await h.store.upsert_group(GroupMeta(id=GROUPS[2]), state=GroupState.LOST)
        snapshot = await h.scheduler.snapshot()
        assert snapshot.backstop_only
        by_id = {g.id: g for g in snapshot.groups}
        assert by_id[GROUPS[0]].paused and by_id[GROUPS[2]].paused
        assert by_id[GROUPS[1]].quiet
        grants = await drive(h, limit=3 * HOUR)
        kinds = {g.action.kind for _, g in grants}
        assert ActionKind.FEED not in kinds
        assert ActionKind.BACKSTOP in kinds
        assert not {GROUPS[0], GROUPS[2]} & {g.action.group_id for _, g in grants}

    async def test_read_finished_bookkeeping(self, harness: Harness) -> None:
        h = harness
        grant = (await drive(h, limit=HOUR, stop=lambda g: True))[0][1]
        await h.scheduler.read_finished(grant, outcome=ReadOutcome.GAP, gap_groups=GROUPS[:5])
        assert [g.group_id for g in h.scheduler.state.gaps] == GROUPS[:3]
        await h.scheduler.read_finished(grant, outcome=ReadOutcome.FAILED)
        assert h.scheduler.state.session is None
        sync = Grant(
            dataclasses.replace(grant.action, kind=ActionKind.SYNC), grant.profile, grant.knobs
        )
        await h.scheduler.read_finished(sync, outcome=ReadOutcome.COMPLETE)
        assert h.scheduler.state.session is None
