"""The scheduler: what to read next, and when — and the one door every step goes through.

Two layers:

- ``Planner`` is pure. Given a ``Snapshot`` (the clock, the profile, what was read when, the
  persisted ``SchedulerState``) it returns one ``Decision``: a feed read, a backstop, a group
  sync, the resume probe — or nothing until a given time and why. All its randomness comes from
  the installation's ``Jitter`` keyed by the event a due time follows, so a restart re-derives
  exactly the same plan instead of re-rolling it (see ``rhythm``).
- ``Scheduler`` wraps it for the read loop: snapshot from the store → plan → gates → write-ahead
  budget event → hand the step to the caller. Scroll steps go through the same door
  (``scroll_permit``). A read that must start Chrome first is checked against the *launch* door
  before anything is paid (``next(launch=True)``: the crash-loop gate counts the launch, the
  block gate admits the armed probe) and the launch is recorded with the grant, so a refused
  launch never leaves a paid navigation behind. A launch that needs the local self-test first
  (an untested Chrome major) is two launches: they pass that door together and are recorded
  together, before either Chrome starts. While Chrome is parked between reads every wait
  re-checks the gates (``next(parked=True)``), so a gate that closes between reads — the network,
  the power, the zone, a halt — closes Chrome at once instead of at the next due read.

Rhythm (DESIGN, Scheduler): reads come in *sessions* — a feed read, then 0–2 backstops 45–120 s
apart, then idle — at the profile's feed cadence, slower in the shoulders around quiet hours,
not at all in quiet hours and breaks. Backstops run only at full cadence. After more than 2 h
without a navigation (every night, too) the schedule restarts from a new *epoch*: overdue
backstops are not caught up, each group's next one is re-drawn somewhere in the first
``[25 %, 100 %]`` of its interval after the epoch — never a burst (D10: no backfill).

Degradation when the day's budget runs short: groups with no posts in 7 days are backstopped
half as often once less than 25 % of any 24 h cap remains, gap backstops are skipped below 20 %,
and ``delay_bounds`` tells users the honest per-group delay.
"""

from __future__ import annotations

import bisect
import datetime as dt
import enum
import json
import logging
import math
import random
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final

from fbcollector.budget import (
    BACKSTOP_JITTER,
    Budget,
    PlannedLoad,
    ensure_fits,
    knobs_for,
    planned_load,
)
from fbcollector.clock import Clock
from fbcollector.config import ProfileKnobs, Settings
from fbcollector.detectors import FEED_URL, JOINS_URL, group_url
from fbcollector.gates import Activity, GateContext, GateReport, GateResult, GateSet
from fbcollector.model import CollectorState, ReadOutcome, Surface
from fbcollector.rhythm import (
    CATCH_UP_EXPIRES,
    DAY,
    HOUR,
    MINUTE,
    Jitter,
    Phase,
    Rhythm,
    RhythmConfig,
)
from fbwire.models import BudgetKind, GroupState, Profile

if TYPE_CHECKING:
    from fbcollector.control import ControlCenter
    from fbcollector.ladder import Ladder, ProbeRequest
    from fbcollector.store import Store

log = logging.getLogger(__name__)

SESSION_BACKSTOPS: Final = 2
SESSION_GAP: Final = (45.0, 120.0)
SESSION_IDLE: Final = 10 * MINUTE
REST_JITTER: Final = 120.0
"""The first feed after quiet hours, a break or an epoch starts within this many seconds."""
DEEP_SHARE: Final = 0.25
"""Every ~4th feed read (jittered) may scroll deeper."""
ORDINARY_STEPS: Final = 1
DEEP_STEPS: Final = 3
BACKSTOP_STEPS: Final = 2
OBSERVE_BACKSTOP_STEPS: Final = 6
SYNC_STEPS: Final = 6
PROBE_DWELL: Final = 15.0
GAP_BACKSTOPS: Final = 3
RESTART_SPREAD: Final = (0.25, 1.0)
STRETCH_BELOW: Final = 0.25
SKIP_GAPS_BELOW: Final = 0.20
QUIET_GROUP_STRETCH: Final = 2.0
FLAG_NO_RECENT_POSTS: Final = "no_posts_7d"
FLAG_PAUSED: Final = "paused"
KV_SCHEDULER: Final = "scheduler"
KV_BACKSTOP_ONLY: Final = "feed_not_chrono"


def local_profile(
    now: float,
    *,
    login_at: float | None,
    cautious_until: float | None,
    days_after_login: float = 14,
) -> Profile:
    """Cautious for 14 days after every login and until any post-warning deadline; else normal.

    No recorded login at all is cautious too (there is nothing to be normal about yet).
    """
    if login_at is None or now < login_at + days_after_login * DAY:
        return Profile.CAUTIOUS
    if cautious_until is not None and now < cautious_until:
        return Profile.CAUTIOUS
    return Profile.NORMAL


# -- persisted state --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Session:
    started_at: float
    backstops: int = 0
    last_at: float = 0.0


@dataclass(frozen=True, slots=True)
class GapTask:
    group_id: str
    at: float


@dataclass(frozen=True, slots=True)
class SchedulerState:
    epoch: float | None = None
    session: Session | None = None
    catch_up_day: str | None = None
    catch_up_done: int = 0
    gaps: tuple[GapTask, ...] = ()
    last_feed: float | None = None
    """When the last feed read (or probe) was granted: a read that crashed before it was
    finished must still count, or the next one would be due 45 s later."""
    last_sync: float | None = None

    def catch_up_count(self, day: dt.date) -> int:
        return self.catch_up_done if self.catch_up_day == day.isoformat() else 0

    def to_json(self) -> str:
        data: dict[str, Any] = {
            "epoch": self.epoch,
            "session": None,
            "catch_up_day": self.catch_up_day,
            "catch_up_done": self.catch_up_done,
            "gaps": [[g.group_id, g.at] for g in self.gaps],
            "last_feed": self.last_feed,
            "last_sync": self.last_sync,
        }
        if self.session:
            data["session"] = [
                self.session.started_at,
                self.session.backstops,
                self.session.last_at,
            ]
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | None) -> SchedulerState:
        if not raw:
            return cls()
        data = json.loads(raw)
        session = data.get("session")
        return cls(
            epoch=None if data.get("epoch") is None else float(data["epoch"]),
            session=Session(float(session[0]), int(session[1]), float(session[2]))
            if session
            else None,
            catch_up_day=data.get("catch_up_day"),
            catch_up_done=int(data.get("catch_up_done", 0)),
            gaps=tuple(GapTask(str(g), float(at)) for g, at in data.get("gaps", ())),
            last_feed=_opt_float(data.get("last_feed")),
            last_sync=_opt_float(data.get("last_sync")),
        )


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _latest(*times: float | None) -> float | None:
    return max((t for t in times if t is not None), default=None)


# -- planner input and output -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GroupView:
    id: str
    anchor: float
    """When the group became monitored (its ``baseline_at``): no backstop is owed before it."""
    last_backstop_at: float | None = None
    last_navigation_at: float | None = None
    quiet: bool = False
    """No posts in 7 days: first to be stretched when the budget runs short."""
    paused: bool = False
    listings: bool = False


@dataclass(frozen=True, slots=True)
class Snapshot:
    now: float
    profile: Profile
    groups: tuple[GroupView, ...] = ()
    state: SchedulerState = field(default_factory=SchedulerState)
    last_navigation_at: float | None = None
    last_feed_at: float | None = None
    last_read_end: float | None = None
    last_sync_at: float | None = None
    budget_day_remaining: float = 1.0
    backstop_only: bool = False
    paused: bool = False
    blocked: bool = False
    probe: ProbeRequest | None = None
    refresh_at: float | None = None
    recent_navigations: tuple[float, ...] = ()
    """Navigation times of the last 24 h, oldest first (from ``budget_events``)."""
    generation: int = 0
    """The control generation the snapshot's pause and profile come from."""


class ActionKind(enum.StrEnum):
    FEED = "feed"
    BACKSTOP = "backstop"
    SYNC = "sync"
    PROBE = "probe"


@dataclass(frozen=True, slots=True)
class Action:
    kind: ActionKind
    surface: Surface
    url: str
    group_id: str | None = None
    scroll_allowance: int = 0
    reason: str = "scheduled"
    catch_up: bool = False
    deep: bool = False
    """A feed read allowed more than one step (the catch-up, every ~4th read): it targets the
    last read that reached its target, to cover what the ordinary reads could not."""
    block_id: int | None = None
    min_dwell: float = 0.0


@dataclass(frozen=True, slots=True)
class Wait:
    until: float | None
    """``None``: until something outside the scheduler changes (a command, a human)."""
    state: CollectorState
    reason: str
    close_chrome: bool = True


@dataclass(frozen=True, slots=True)
class Decision:
    state: SchedulerState
    action: Action | None = None
    wait: Wait | None = None
    new_epoch: bool = False


_PRIORITY: Final = {
    ActionKind.PROBE: 0,
    ActionKind.FEED: 1,
    ActionKind.BACKSTOP: 2,
    ActionKind.SYNC: 3,
}


class Planner:
    """Pure: ``decide(snapshot)`` never touches the clock, the store or global randomness."""

    def __init__(
        self,
        settings: Settings,
        rhythm: Rhythm,
        jitter: Jitter,
        *,
        observe_mode: bool | None = None,
    ) -> None:
        self._settings = settings
        self._rhythm = rhythm
        self._jitter = jitter
        self._observe = settings.observe_mode if observe_mode is None else observe_mode
        self._reset = settings.downtime_reset_hours * HOUR
        self._min_gap = settings.min_navigation_gap_seconds
        self._revisit = settings.group_revisit_minutes * MINUTE
        self._loads: dict[tuple[Profile, int, bool], PlannedLoad] = {}

    @property
    def rhythm(self) -> Rhythm:
        return self._rhythm

    def load(self, profile: Profile, groups: int, backstop_only: bool) -> PlannedLoad:
        key = (profile, groups, backstop_only)
        if key not in self._loads:
            self._loads[key] = planned_load(
                self._settings, profile, groups, backstop_only=backstop_only
            )
        return self._loads[key]

    def interval(self, snap: Snapshot) -> float | None:
        """Seconds between two backstops of one group, before jitter and stretching."""
        active = sum(1 for g in snap.groups if not g.paused)
        hours = self.load(snap.profile, active, snap.backstop_only).backstop_interval_hours
        return None if hours is None else hours * HOUR

    def delay_bounds(self, snap: Snapshot) -> dict[str, float]:
        """Per group, the longest a post the feed misses may wait (seconds; ``inf``: paused)."""
        interval = self.interval(snap)
        stretch = snap.budget_day_remaining < STRETCH_BELOW
        bounds = {}
        for g in snap.groups:
            if g.paused or interval is None:
                bounds[g.id] = math.inf
                continue
            factor = QUIET_GROUP_STRETCH if stretch and g.quiet else 1.0
            bounds[g.id] = interval * factor * (1 + BACKSTOP_JITTER)
        return bounds

    # -- decide --------------------------------------------------------------------------------

    def decide(self, snap: Snapshot) -> Decision:
        now = snap.now
        state = self._prune(snap)
        snap = replace(
            snap,
            last_feed_at=_latest(snap.last_feed_at, state.last_feed),
            last_sync_at=_latest(snap.last_sync_at, state.last_sync),
        )
        day = self._rhythm.plan_at(now)
        phase = day.phase(now)
        if snap.paused:
            return Decision(state, wait=Wait(None, CollectorState.PAUSED, "paused"))
        if snap.blocked and snap.probe is None:
            return Decision(state, wait=Wait(None, CollectorState.BLOCKED, "blocked"))
        if phase is Phase.QUIET:
            return Decision(state, wait=Wait(day.quiet_end, CollectorState.QUIET, "quiet hours"))
        if phase is Phase.BREAK:
            return Decision(state, wait=Wait(day.resumes_at(now), CollectorState.BREAK, "break"))
        last_activity = _latest(snap.last_navigation_at, state.epoch)
        # The epoch counts as activity, so waiting for the first read never moves it on; and an
        # epoch that was never stored is started (and stored) now, never re-derived per call.
        new_epoch = (
            state.epoch is None or last_activity is None or now - last_activity > self._reset
        )
        if new_epoch:
            state = replace(state, epoch=now, session=None)
        epoch = state.epoch if state.epoch is not None else now
        spacing = (
            now if snap.last_navigation_at is None else snap.last_navigation_at + self._min_gap
        )
        candidates = self._candidates(snap, state, epoch, spacing)
        if not candidates:
            wait = Wait(day.next_change(now), CollectorState.OK, "nothing due", close_chrome=False)
            return Decision(state, wait=wait, new_epoch=new_epoch)
        at, action = min(candidates, key=lambda c: (c[0], _PRIORITY[c[1].kind]))
        if at <= now:
            # The epoch is the moment reading resumes, not the first look after the silence: a
            # gate that stayed closed for an hour must not leave backstops queued behind the
            # first read.
            last_nav = snap.last_navigation_at
            if (last_nav is None or last_nav < epoch) and epoch != now:
                state = replace(state, epoch=now, session=None)
                new_epoch = True
            return Decision(state, action=action, new_epoch=new_epoch)
        until = min(at, day.next_change(now))
        wait = Wait(until, CollectorState.OK, f"next {action.kind.value}", close_chrome=False)
        return Decision(state, wait=wait, new_epoch=new_epoch)

    def _candidates(
        self, snap: Snapshot, state: SchedulerState, epoch: float, spacing: float
    ) -> list[tuple[float, Action]]:
        if snap.blocked:
            probe = snap.probe
            assert probe is not None  # noqa: S101 - decide() returned when there is none
            return [(max(probe.not_before, spacing), self._probe_action(probe.block_id))]
        day = self._rhythm.plan_at(snap.now)
        phase = day.phase(snap.now)
        found: list[tuple[float, Action]] = []
        if not snap.backstop_only:
            found.append(self._feed(snap, state, epoch, spacing))
        if phase is Phase.FULL:
            backstop = self._backstop(snap, state, epoch, spacing)
            if backstop is not None:
                found.append(backstop)
        sync = self._sync(snap, phase, spacing)
        if sync is not None:
            found.append(sync)
        return found

    # -- feed ----------------------------------------------------------------------------------

    def feed_bounds(self, profile: Profile, phase: Phase) -> tuple[float, float]:
        knobs = knobs_for(self._settings, profile)
        if phase is Phase.SHOULDER:
            low = max(self._settings.shoulder_feed_min_minutes, knobs.feed_min_minutes)
            high = max(self._settings.shoulder_feed_max_minutes, knobs.feed_max_minutes)
            return low * MINUTE, high * MINUTE
        return knobs.feed_min_minutes * MINUTE, knobs.feed_max_minutes * MINUTE

    def _feed(
        self, snap: Snapshot, state: SchedulerState, epoch: float, spacing: float
    ) -> tuple[float, Action]:
        now = snap.now
        day = self._rhythm.plan_at(now)
        last = snap.last_feed_at
        if last is None or last < epoch:
            anchor = epoch
            due = epoch + self._jitter.uniform(0.0, REST_JITTER, "feed0", epoch)
        else:
            anchor = last
            low, high = self.feed_bounds(snap.profile, day.phase(now))
            due = last + self._jitter.uniform(low, high, "feed", last)
            rest = day.rest_before(now)
            if last < rest:
                due = max(due, rest + self._jitter.uniform(0.0, REST_JITTER, "rest", rest))
        steps, catch_up = self._feed_steps(state, anchor, now)
        action = Action(
            ActionKind.FEED,
            Surface.FEED,
            FEED_URL,
            scroll_allowance=steps,
            reason="catch_up" if catch_up else "scheduled",
            catch_up=catch_up,
            deep=steps > ORDINARY_STEPS,
        )
        return max(due, spacing), action

    def _feed_steps(self, state: SchedulerState, anchor: float, now: float) -> tuple[int, bool]:
        day = self._rhythm.plan_at(now)
        done = state.catch_up_count(day.day)
        in_window = day.catch_up_at <= now < day.catch_up_at + CATCH_UP_EXPIRES
        if in_window and done < len(day.catch_up_steps):
            return day.catch_up_steps[done], True
        deep = self._jitter.random("deep", anchor) < DEEP_SHARE
        return (DEEP_STEPS if deep else ORDINARY_STEPS), False

    # -- backstops -----------------------------------------------------------------------------

    def backstop_due(self, group: GroupView, epoch: float, interval: float) -> float:
        """When the group's next backstop is due: its own chain, or a jittered restart."""
        last = group.last_backstop_at
        if last is not None and last >= epoch:
            factor = self._jitter.uniform(
                1 - BACKSTOP_JITTER, 1 + BACKSTOP_JITTER, "backstop", group.id, last
            )
            return last + interval * factor
        anchor = max(self.restart_anchor(epoch), group.anchor)
        spread = self._jitter.uniform(*RESTART_SPREAD, "restart", group.id, anchor)
        return anchor + interval * spread

    def restart_anchor(self, epoch: float) -> float:
        """Backstops restart from the first full-cadence moment at or after the epoch.

        Reading usually resumes in the morning shoulder, where backstops are not allowed; had
        the re-drawn due times counted from there, every one falling inside the shoulder would
        be overdue at once when full cadence begins — a small burst every morning.
        """
        day = self._rhythm.plan_at(epoch)
        if day.phase(epoch) is Phase.SHOULDER and epoch < day.shoulder_end:
            return day.shoulder_end
        return epoch

    def _session_start(self, snap: Snapshot, state: SchedulerState, epoch: float) -> float | None:
        """When the next backstop of the open session may start (``None``: no open session)."""
        last_end = snap.last_read_end or 0.0
        if snap.backstop_only:
            start = last_end
        else:
            session = state.session
            if session is None or session.started_at < epoch:
                return None
            if session.backstops >= SESSION_BACKSTOPS:
                return None
            start = max(last_end, session.last_at)
            if snap.now - start > SESSION_IDLE:
                return None
        return start + self._jitter.uniform(*SESSION_GAP, "session_gap", start)

    def _backstop(
        self, snap: Snapshot, state: SchedulerState, epoch: float, spacing: float
    ) -> tuple[float, Action] | None:
        start = self._session_start(snap, state, epoch)
        interval = self.interval(snap)
        if start is None or interval is None:
            return None
        start = max(start, spacing)
        deadline = math.inf if snap.backstop_only else start + SESSION_IDLE
        groups = {g.id: g for g in snap.groups if not g.paused}
        # Earliest first; a gap task wins a tie. A gap task still inside its group's revisit
        # time must not hold back the scheduled backstops of every other group (a busy group
        # that gaps on every read would otherwise take all the backstops of the day).
        candidates: list[tuple[float, int, int, GroupView, str]] = []
        if snap.budget_day_remaining >= SKIP_GAPS_BELOW:
            for rank, task in enumerate(sorted(state.gaps, key=lambda t: t.at)):
                group = groups.get(task.group_id)
                if group is not None:
                    candidates.append((self._revisited(group, start), 0, rank, group, "gap"))
        stretch = snap.budget_day_remaining < STRETCH_BELOW
        for group in groups.values():
            own = interval * (QUIET_GROUP_STRETCH if stretch and group.quiet else 1.0)
            due = max(start, self.backstop_due(group, epoch, own))
            candidates.append((self._revisited(group, due), 1, 0, group, "scheduled"))
        if not candidates:
            return None
        best = min(candidates, key=lambda c: c[:3])
        at = self.admission_time(snap, best[0])
        if at > deadline:
            return None
        _, _, _, group, reason = best
        steps = OBSERVE_BACKSTOP_STEPS if self._observe else BACKSTOP_STEPS
        return at, Action(
            ActionKind.BACKSTOP,
            Surface.GROUP,
            group_url(group.id, listings=group.listings),
            group_id=group.id,
            scroll_allowance=steps,
            reason=reason,
        )

    def _revisited(self, group: GroupView, at: float) -> float:
        if group.last_navigation_at is None:
            return at
        return max(at, group.last_navigation_at + self._revisit)

    # -- smoothing -----------------------------------------------------------------------------

    def admission_time(self, snap: Snapshot, at: float) -> float:
        """The earliest moment from ``at`` on that a flexible read (backstop, sync) fits.

        It fits when, at the earliest moment the next feed read may come, every navigation
        window still has room for both: the flexible read yields, the feed read never meets a
        full cap. Without this the session rhythm alone (a feed read plus up to two backstops
        every 4.5-9.5 min) puts 7 navigations into a 15-minute window at p95 against a cap
        of 5, and the cap — releasing reads exactly as old ones expire — would set the rhythm.
        """
        for _ in range(len(snap.recent_navigations) + 1):
            blocked_until = self._blocked_until(snap, at)
            if blocked_until is None or math.isinf(blocked_until):
                return at if blocked_until is None else math.inf
            at = blocked_until
        return math.inf  # pragma: no cover - each round retires at least one event

    def _blocked_until(self, snap: Snapshot, at: float) -> float | None:
        """``None`` if a flexible read fits at ``at``; else when the blocking event expires.

        Two moments are checked in every navigation window: ``at`` itself (room for the read)
        and the earliest moment the next feed read may come (room for the read *and* it).
        """
        knobs = knobs_for(self._settings, snap.profile)
        points = [(at, 1)]
        if not snap.backstop_only:
            feed_at = at
            if snap.last_feed_at is not None:
                low, _ = self.feed_bounds(snap.profile, self._rhythm.phase(at))
                feed_at = max(at, snap.last_feed_at + low)
            points.append((feed_at, 2))
        events = snap.recent_navigations
        for kind, window, cap in knobs.windows():
            if kind is not BudgetKind.NAVIGATION:
                continue
            for point, reserve in points:
                first = bisect.bisect_right(events, point - window)
                excess = len(events) - first + reserve - cap
                if excess <= 0:
                    continue
                if first + excess - 1 >= len(events):
                    return math.inf  # the cap is smaller than the reserve itself
                return max(at, events[first + excess - 1] + window) + 1e-3
        return None

    # -- sync and probe ------------------------------------------------------------------------

    def _sync(self, snap: Snapshot, phase: Phase, spacing: float) -> tuple[float, Action] | None:
        if snap.refresh_at is not None and phase in {Phase.FULL, Phase.SHOULDER}:
            action = Action(
                ActionKind.SYNC,
                Surface.JOINS,
                JOINS_URL,
                scroll_allowance=SYNC_STEPS,
                reason="refresh",
            )
            return self.admission_time(snap, max(snap.refresh_at, spacing)), action
        if phase is not Phase.FULL:
            return None
        day = self._rhythm.plan_at(snap.now)
        if snap.last_sync_at is not None and snap.last_sync_at >= day.quiet_end:
            return None
        action = Action(
            ActionKind.SYNC, Surface.JOINS, JOINS_URL, scroll_allowance=SYNC_STEPS, reason="daily"
        )
        return self.admission_time(snap, max(day.sync_at, spacing)), action

    def _probe_action(self, block_id: int) -> Action:
        return Action(
            ActionKind.PROBE,
            Surface.FEED,
            FEED_URL,
            reason="resume_probe",
            block_id=block_id,
            min_dwell=PROBE_DWELL,
        )

    # -- state transitions ---------------------------------------------------------------------

    def _prune(self, snap: Snapshot) -> SchedulerState:
        """Drop gap tasks that are done (the group was backstopped since) or too old."""
        state = snap.state
        interval = self.interval(snap) or 0.0
        groups = {g.id: g for g in snap.groups}
        keep = []
        for task in state.gaps:
            group = groups.get(task.group_id)
            if group is None or group.paused or snap.now - task.at > max(interval, 6 * HOUR):
                continue
            if group.last_backstop_at is not None and group.last_backstop_at >= task.at:
                continue
            keep.append(task)
        return replace(state, gaps=tuple(keep)) if len(keep) != len(state.gaps) else state

    def after_grant(self, state: SchedulerState, action: Action, now: float) -> SchedulerState:
        """The state once ``action`` has been granted (its budget event is on disk)."""
        if action.kind is ActionKind.FEED:
            state = replace(state, session=Session(started_at=now, last_at=now), last_feed=now)
            if action.catch_up:
                day = self._rhythm.plan_at(now).day
                state = replace(
                    state,
                    catch_up_day=day.isoformat(),
                    catch_up_done=state.catch_up_count(day) + 1,
                )
            return state
        if action.kind is ActionKind.BACKSTOP:
            session = state.session
            if session is not None:
                session = replace(session, backstops=session.backstops + 1, last_at=now)
            gaps = tuple(g for g in state.gaps if g.group_id != action.group_id)
            return replace(state, session=session, gaps=gaps)
        if action.kind is ActionKind.PROBE:
            return replace(state, session=None, last_feed=now)
        return replace(state, last_sync=now)

    @staticmethod
    def with_gap(state: SchedulerState, groups_by_rate: Sequence[str], at: float) -> SchedulerState:
        """Queue gap backstops for the busiest groups of an uncovered interval (at most 3)."""
        queued = {g.group_id for g in state.gaps}
        fresh = [g for g in dict.fromkeys(groups_by_rate) if g not in queued][:GAP_BACKSTOPS]
        return replace(state, gaps=state.gaps + tuple(GapTask(g, at) for g in fresh))


def rhythm_result(rhythm: Rhythm, now: float) -> GateResult:
    """The rhythm gate: shut in quiet hours and breaks."""
    day = rhythm.plan_at(now)
    phase = day.phase(now)
    if phase is Phase.QUIET:
        return GateResult("rhythm", False, "quiet hours", CollectorState.QUIET, day.quiet_end)
    if phase is Phase.BREAK:
        return GateResult("rhythm", False, "break", CollectorState.BREAK, day.resumes_at(now))
    return GateResult("rhythm", ok=True)


def gap_targets(seen: Iterable[str], posting_rate: Mapping[str, float]) -> list[str]:
    """Groups seen in a gap's uncovered part, busiest first, at most ``GAP_BACKSTOPS``."""
    unique = list(dict.fromkeys(seen))
    unique.sort(key=lambda g: (-posting_rate.get(g, 0.0), g))
    return unique[:GAP_BACKSTOPS]


# -- the door ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Grant:
    """Permission for one step; its budget event is already fsynced."""

    action: Action
    profile: Profile
    knobs: ProfileKnobs
    selftest: bool = False
    """The launch this grant carries starts with the local self-test (an untested Chrome
    major): a Chrome launch of its own, admitted by the same gates and recorded with it."""
    generation: int = 0
    """The control generation the grant was made under (``ControlState.generation``): a
    change before its navigation discards it (``Scheduler.still_granted``)."""


@dataclass(frozen=True, slots=True)
class Hold:
    """No step now."""

    until: float | None
    state: CollectorState
    reasons: tuple[str, ...]
    close_chrome: bool
    gates: Mapping[str, bool] | None = None
    """The gates this hold checked, by name (``None``: the planner held, no gate was asked).
    The status to the brain reports them; nothing decides on them."""


@dataclass(frozen=True, slots=True)
class Cadence:
    """How often reading happens right now, for the status (the brain's delay promises)."""

    profile: Profile
    feed_max: float
    """Top of the feed interval in the current phase (the shoulder's, around quiet hours)."""
    backstop: float | None
    """The planner's backstop interval before jitter; ``None``: no backstops fit or no groups."""
    backstop_only: bool


class Scheduler:
    """Snapshot → plan → gates → write-ahead budget; the read loop asks it before every step."""

    def __init__(
        self,
        *,
        store: Store,
        settings: Settings,
        clock: Clock,
        planner: Planner,
        budget: Budget,
        gates: GateSet,
        ladder: Ladder,
        control: ControlCenter,
    ) -> None:
        ensure_fits(settings)
        self._store = store
        self._settings = settings
        self._clock = clock
        self._planner = planner
        self._budget = budget
        self._gates = gates
        self._ladder = ladder
        self._control = control
        self._state = SchedulerState()

    @property
    def state(self) -> SchedulerState:
        return self._state

    async def load(self) -> SchedulerState:
        self._state = SchedulerState.from_json(await self._store.kv_get(KV_SCHEDULER))
        return self._state

    async def _save(self, state: SchedulerState) -> None:
        await self._store.kv_set(KV_SCHEDULER, state.to_json())
        self._state = state

    async def local_profile(self, now: float | None = None) -> Profile:
        """The profile before the brain's pace cap: the login's ramp and the ladder's."""
        at = self._clock.time() if now is None else now
        raw = await self._store.kv_get("login_at")
        return local_profile(
            at,
            login_at=None if raw is None else float(raw),
            cautious_until=await self._ladder.cautious_until(),
            days_after_login=self._settings.cautious_days_after_login,
        )

    async def profile(self, now: float | None = None) -> Profile:
        return self._control.effective_profile(await self.local_profile(now))

    async def cadence(self) -> Cadence:
        """The current feed interval's top and backstop interval, from the planner itself."""
        snap = await self.snapshot()
        phase = self._planner.rhythm.phase(snap.now)
        _, top = self._planner.feed_bounds(snap.profile, phase)
        return Cadence(
            profile=snap.profile,
            feed_max=top,
            backstop=self._planner.interval(snap),
            backstop_only=snap.backstop_only,
        )

    async def rhythm_gate(self, context: GateContext) -> GateResult:  # noqa: ARG002 - one rule
        return rhythm_result(self._planner.rhythm, self._clock.time())

    async def snapshot(self) -> Snapshot:
        now = self._clock.time()
        store = self._store
        control = await self._control.load()  # the latest durable word, another process's too
        profile = self._control.effective_profile(await self.local_profile(now), control)
        views = []
        for row in await store.list_groups(monitored_only=True):
            last = await store.last_read(Surface.GROUP, group_id=row.id)
            views.append(
                GroupView(
                    id=row.id,
                    anchor=row.baseline_at,
                    last_backstop_at=last.started_at if last else None,
                    last_navigation_at=await store.last_budget_event_at(
                        BudgetKind.NAVIGATION, group_id=row.id
                    ),
                    quiet=FLAG_NO_RECENT_POSTS in row.flags,
                    paused=row.state is not GroupState.MEMBER or FLAG_PAUSED in row.flags,
                    listings=row.listings,
                )
            )
        feed = await store.last_read(Surface.FEED)
        reads = [feed, await store.last_read(Surface.GROUP), await store.last_read(Surface.JOINS)]
        ends = [r.ended_at for r in reads if r is not None and r.ended_at is not None]
        joins = reads[2]
        block = await self._ladder.current_block()
        return Snapshot(
            now=now,
            profile=profile,
            groups=tuple(views),
            state=self._state,
            last_navigation_at=await store.last_budget_event_at(BudgetKind.NAVIGATION),
            last_feed_at=feed.started_at if feed else None,
            last_read_end=max(ends, default=None),
            last_sync_at=joins.started_at if joins else None,
            budget_day_remaining=await self._budget.day_remaining(
                knobs_for(self._settings, profile)
            ),
            backstop_only=await store.kv_get(KV_BACKSTOP_ONLY) == "1",
            paused=self._control.is_paused(control),
            blocked=block is not None,
            probe=await self._ladder.probe_request(),
            refresh_at=control.refresh_at,
            recent_navigations=tuple(
                await store.budget_event_times(BudgetKind.NAVIGATION, since=now - DAY)
            ),
            generation=control.generation,
        )

    async def next(
        self,
        *,
        launch: bool = False,
        parked: bool = False,
        selftest: Callable[[], Awaitable[bool]] | None = None,
        launch_planned: bool = False,
    ) -> Grant | Hold:
        """The next navigation, granted and paid for — or why not, and until when.

        ``launch``: Chrome is not running, so a grant also starts it. The gates are checked as
        for a launch (the crash-loop gate counts the pending launch, the block gate lets the
        armed probe through) *before* the budget is paid, and the launch is recorded (fsynced)
        with the grant. ``launch_planned``: the runner's word that Chrome ended by its own
        planned close (or that this is the first launch of a planned process start); the
        crash-loop gate then leaves the launch out of its unplanned count, and it is recorded
        planned. ``parked``: Chrome is running between reads; a wait re-checks the gates and
        asks to close Chrome when any of them is shut (invariant 2).

        ``selftest`` is asked once a read is due, Chrome must start for it and every gate is open
        for that launch: whether the local self-test has to run first (an untested Chrome
        major). That is a second Chrome launch on the account's profile, so the gates are
        checked again with both launches pending, and both are recorded before either starts
        (``Grant.selftest``); the self-test never takes the read's one pending launch, and is
        planned exactly when the read's launch is (one scheduled launch). It may
        also halt the collector (an unreadable Chrome version): that second check's halt gate
        then refuses the launch. Asked only then, it never reads the version, writes a halt or
        alerts on a turn a gate refuses anyway (warm-up, away, on battery, an existing halt).
        """
        snap = await self.snapshot()
        decision = self._planner.decide(snap)
        if decision.new_epoch:
            # Before anything is spent: a lost epoch would turn every backstop overdue.
            await self._save(replace(self._state, epoch=decision.state.epoch, session=None))
        if decision.action is None:
            wait = decision.wait
            assert wait is not None  # noqa: S101 - a decision is an action or a wait
            hold = Hold(wait.until, wait.state, (wait.reason,), wait.close_chrome)
            return await self._parked_hold(hold) if parked else hold
        action = decision.action
        probe = action.kind is ActionKind.PROBE
        activity = Activity.LAUNCH if launch else Activity.NAVIGATE
        planned = launch and launch_planned
        report = await self._gates.evaluate(GateContext(activity, probe=probe, planned=planned))
        if not report.ok:
            return _held(report, self._clock.time())
        first = False
        if launch and selftest is not None:
            first = await selftest()
            # Every gate again: with the self-test's launch pending too, and after a halt the
            # question may have written (an unreadable version).
            context = GateContext(
                activity, probe=probe, launches=2 if first else 1, planned=planned
            )
            report = await self._gates.evaluate(context)
            if not report.ok:
                return _held(report, self._clock.time())
        knobs = knobs_for(self._settings, snap.profile)
        async with self._control.pinned() as terms:
            # The terms once more, held still until the budget event is on disk: an answer of
            # the brain (or the owner's pause) that landed after the plan or during the gates
            # must not be paid for under the old ones.
            closed = self._control.verdict(terms)
            if not closed.ok:
                return _held(GateReport((closed,)), self._clock.time())
            if terms.generation != snap.generation:
                log.info("the control state changed while planning: planning again")
                reason = "the control state changed; planning again"
                return Hold(self._clock.time(), CollectorState.OK, (reason,), False)
            verdict = await self._budget.acquire(
                BudgetKind.NAVIGATION, knobs, surface=action.surface, group_id=action.group_id
            )
        if not verdict.allowed:
            # Every other gate was open a moment ago; Chrome may stay parked on about:blank
            # until the window has room (closing it would only spend crash-loop headroom).
            return Hold(verdict.retry_at, CollectorState.OK, (verdict.reason,), False)
        if first:
            await self._record_launch("selftest", planned=planned)
        if launch:
            await self._record_launch(planned=planned)
        await self._save(self._planner.after_grant(decision.state, action, self._clock.time()))
        if action.kind is ActionKind.SYNC and action.reason == "refresh":
            await self._control.refresh_done()
        log.info("granted %s %s (%s)", action.kind.value, action.group_id or "", action.reason)
        return Grant(action, snap.profile, knobs, selftest=first, generation=snap.generation)

    async def _parked_hold(self, hold: Hold) -> Hold:
        """A wait while Chrome is parked: close it if any gate is shut now."""
        if hold.close_chrome:
            return hold
        report = await self._gates.evaluate(GateContext(Activity.NAVIGATE))
        if report.ok:
            return hold
        closed = _held(report, self._clock.time())
        return Hold(hold.until, closed.state, closed.reasons, close_chrome=True, gates=closed.gates)

    async def recheck(self, grant: Grant) -> Hold | None:
        """The gates once more, with nothing to pay: after Chrome started (the launch and the
        self-test take a while) and before a read's final look. ``None`` when all are open."""
        probe = grant.action.kind is ActionKind.PROBE
        report = await self._gates.evaluate(GateContext(Activity.NAVIGATE, probe=probe))
        return None if report.ok else _held(report, self._clock.time())

    async def still_granted(self, grant: Grant) -> Hold | None:
        """Right before the grant's navigation: ``None`` while the control state is still the
        one the grant was made under.

        Any change of the terms since — a pause of either side, the lease, the pace cap —
        discards the grant: its navigation stays counted (an over-count) and the next turn plans
        under the new terms. Chrome is closed when the change shut the control gate.
        """
        state = await self._control.load()
        if state.generation == grant.generation:
            return None
        now = self._clock.time()
        closed = self._control.verdict(state)
        if not closed.ok:
            return _held(GateReport((closed,)), now)
        reason = "the control state changed since the grant"
        return Hold(now, CollectorState.OK, (reason,), False)

    async def scroll_permit(
        self, grant: Grant, *, steps_done: int, read_id: int | None
    ) -> Hold | None:
        """``None`` when one more scroll step may happen now (its budget event is on disk).

        Paid under the terms in force now, not the grant's: the durable control row is read
        again and held still in this process until the event is on disk, so a pause stops the
        read here and a stricter pace cap applies from this step on.
        """
        if steps_done >= grant.action.scroll_allowance:
            return Hold(None, CollectorState.OK, ("scroll allowance used",), False)
        report = await self._gates.evaluate(
            GateContext(Activity.SCROLL, probe=grant.action.kind is ActionKind.PROBE)
        )
        if not report.ok:
            return _held(report, self._clock.time())
        local = await self.local_profile()
        async with self._control.pinned() as terms:
            closed = self._control.verdict(terms)
            if not closed.ok:
                return _held(GateReport((closed,)), self._clock.time())
            if terms.generation != grant.generation:
                log.info("the control state changed mid-read: this step is paid under it")
            profile = self._control.effective_profile(local, terms)
            verdict = await self._budget.acquire(
                BudgetKind.SCROLL,
                knobs_for(self._settings, profile),
                surface=grant.action.surface,
                group_id=grant.action.group_id,
                read_id=read_id,
            )
        if not verdict.allowed:
            return Hold(verdict.retry_at, CollectorState.OK, (verdict.reason,), False)
        return None

    async def _record_launch(self, detail: str | None = None, *, planned: bool) -> None:
        """A launch a grant carries, recorded (fsynced) before Chrome starts."""
        from fbcollector.store import ProcessKind  # noqa: PLC0415 - keep the import light

        await self._store.record_process_event(ProcessKind.CHROME_LAUNCH, detail, planned=planned)

    async def read_finished(
        self, grant: Grant, *, outcome: ReadOutcome, gap_groups: Sequence[str] = ()
    ) -> None:
        """Session bookkeeping after a read; a gap queues up to 3 gap backstops."""
        now = self._clock.time()
        state = self._state
        if grant.action.kind in {ActionKind.SYNC, ActionKind.PROBE}:
            pass  # neither opens nor extends a session
        elif outcome in {ReadOutcome.FAILED, ReadOutcome.ABORTED}:
            state = replace(state, session=None)
        elif state.session is not None:
            state = replace(state, session=replace(state.session, last_at=now))
        if outcome is ReadOutcome.GAP and gap_groups:
            state = Planner.with_gap(state, gap_groups, now)
        await self._save(state)


def _held(report: GateReport, now: float) -> Hold:
    until = report.retry_at
    return Hold(
        max(until, now) if until is not None else None,
        report.state,
        report.reasons,
        True,
        gates=report.as_status(),
    )


# -- simulation (the budget table's p95) ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SimulationResult:
    navigations: tuple[tuple[float, ActionKind], ...]
    scrolls: tuple[float, ...]
    start: float
    end: float

    def times(self, kind: BudgetKind) -> list[float]:
        if kind is BudgetKind.SCROLL:
            return list(self.scrolls)
        return [t for t, _ in self.navigations]

    def window_counts(self, kind: BudgetKind, window: float) -> list[int]:
        """At each event: how many events of ``kind`` the sliding window ending there holds."""
        times = sorted(self.times(kind))
        counts = []
        left = 0
        for right, t in enumerate(times):
            while times[left] <= t - window:
                left += 1
            counts.append(right - left + 1)
        return counts

    def percentile(self, kind: BudgetKind, window: float, q: float = 0.95) -> float:
        counts = sorted(self.window_counts(kind, window))
        if not counts:
            return 0.0
        return float(counts[min(len(counts) - 1, math.ceil(q * len(counts)) - 1)])

    def maximum(self, kind: BudgetKind, window: float) -> int:
        return max(self.window_counts(kind, window), default=0)

    def per_day(self, kind: BudgetKind) -> float:
        return len(self.times(kind)) / ((self.end - self.start) / DAY)


def simulate(
    settings: Settings,
    profile: Profile,
    groups: int,
    *,
    days: int = 14,
    seed: str = "simulation",
    start: float | None = None,
) -> SimulationResult:
    """Run the real planner on a virtual clock, unconstrained by caps; every read uses its full
    scroll allowance (an upper bound on the plan's scroll load)."""
    jitter = Jitter(seed)
    rhythm = Rhythm(RhythmConfig.from_settings(settings), jitter)
    planner = Planner(settings, rhythm, jitter)
    rng = random.Random(seed)
    t = start if start is not None else dt.datetime(2026, 10, 5, tzinfo=settings.zone).timestamp()
    begin, end = t, t + days * DAY
    state = SchedulerState()
    views = {str(1000 + i): GroupView(str(1000 + i), anchor=begin) for i in range(groups)}
    last_nav = last_feed = last_end = last_sync = None
    navs: list[tuple[float, ActionKind]] = []
    scrolls: list[float] = []
    while t < end:
        snap = Snapshot(
            now=t,
            profile=profile,
            groups=tuple(views.values()),
            state=state,
            last_navigation_at=last_nav,
            last_feed_at=last_feed,
            last_read_end=last_end,
            last_sync_at=last_sync,
            recent_navigations=tuple(n for n, _ in navs[bisect.bisect_left(navs, (t - DAY,)) :]),
        )
        decision = planner.decide(snap)
        state = decision.state
        if decision.action is None:
            until = decision.wait.until if decision.wait else None
            t = max(until if until is not None else t + MINUTE, t + 1.0)
            continue
        action = decision.action
        state = planner.after_grant(state, action, t)
        navs.append((t, action.kind))
        last_nav = t
        if action.kind is ActionKind.FEED:
            last_feed = t
        elif action.kind is ActionKind.SYNC:
            last_sync = t
        elif action.group_id is not None:
            views[action.group_id] = replace(
                views[action.group_id], last_backstop_at=t, last_navigation_at=t
            )
        dwell = rng.uniform(4.0, 10.0)
        scrolls.extend(
            t + dwell + step * rng.uniform(4.0, 12.0) for step in range(action.scroll_allowance)
        )
        t = t + dwell + action.scroll_allowance * 12.0 + rng.uniform(1.0, 5.0)
        last_end = t
    return SimulationResult(tuple(navs), tuple(scrolls), begin, end)
