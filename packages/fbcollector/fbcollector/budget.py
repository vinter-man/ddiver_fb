"""Hard caps, spacing and the planned load: what may be spent, and what the plan expects to spend.

Two layers, deliberately separate:

- **Enforcement** (``Budget``) reads nothing but ``budget_events``. Every cap is a count over a
  sliding UTC window of that table, the 45 s spacing is measured from the last *persisted*
  navigation and the per-group revisit rule from that group's last one, so all three survive a
  restart, a crash or a clock that stepped. ``acquire`` checks and records under one lock and
  returns only after the event is fsynced (invariant 6): a crash between the record and the
  step it pays for can only over-count.
- **The plan** (``planned_load``) is arithmetic over the scheduler's own parameters: feed
  cadence, backstop interval, quiet hours, shoulders and breaks. The design wants the planned
  load at or below 75 % of every cap, so that the scheduler — not the cap — shapes the rhythm
  and the headroom absorbs gaps, probes, sync and catch-up. When the configured backstop rate
  would not fit, the backstop interval is stretched until it does; a profile without a rate of
  its own (the cautious one since the owner's decision of 2026-09-25) gets exactly the room
  the caps leave, never closer than its floor (3 h): ``max(3 h, N / limit)``. A plan that
  cannot fit even so is refused before the collector starts.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from fbcollector.clock import Clock
from fbcollector.config import ProfileKnobs
from fbcollector.rhythm import HOUR, MINUTE, RhythmConfig
from fbwire.models import MAX_MONITORED, BudgetKind, BudgetWindow, Profile

if TYPE_CHECKING:
    from fbcollector.config import Settings
    from fbcollector.model import Surface

PLANNED_SHARE: Final = 0.75
FEED_STEPS_MEAN: Final = 1.3
"""Scroll steps a feed read takes on average (DESIGN: ≈ 1.3)."""
BACKSTOP_STEPS_MEAN: Final = 1.5
BACKSTOP_JITTER: Final = 0.3
MAX_BACKSTOP_INTERVAL_HOURS: Final = 24.0
"""A plan whose backstops would come less than once a day is refused, not run."""
RETRY_CAP: Final = 300.0
EPSILON: Final = 1e-9


class BudgetLedger(Protocol):
    """The part of the store the budget uses (``Store`` satisfies it)."""

    async def record_budget_event(
        self,
        kind: BudgetKind,
        *,
        surface: Surface | None = None,
        group_id: str | None = None,
        read_id: int | None = None,
    ) -> int: ...

    async def budget_used(
        self, kind: BudgetKind, window: float, *, now: float | None = None
    ) -> int: ...

    async def last_budget_event_at(
        self, kind: BudgetKind | None = None, *, group_id: str | None = None
    ) -> float | None: ...


@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    allowed: bool
    reason: str = ""
    retry_at: float | None = None
    """The earliest moment the same request could pass (a lower bound; re-check then)."""
    usage: tuple[BudgetWindow, ...] = ()


def window_label(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400 * 24}h"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}min"


class Budget:
    """The hard caps, the spacing rule and the revisit rule, over the persisted events."""

    def __init__(
        self,
        ledger: BudgetLedger,
        clock: Clock,
        *,
        min_gap: float = 45.0,
        revisit: float = 30 * MINUTE,
    ) -> None:
        self._ledger = ledger
        self._clock = clock
        self._min_gap = min_gap
        self._revisit = revisit
        self._lock = asyncio.Lock()

    async def usage(self, knobs: ProfileKnobs, *, now: float | None = None) -> list[BudgetWindow]:
        at = self._clock.time() if now is None else now
        return [
            BudgetWindow(
                kind=kind,
                window_seconds=seconds,
                cap=cap,
                used=await self._ledger.budget_used(kind, seconds, now=at),
            )
            for kind, seconds, cap in knobs.windows()
        ]

    async def check(
        self, kind: BudgetKind, knobs: ProfileKnobs, *, group_id: str | None = None
    ) -> BudgetVerdict:
        """Would one more step of ``kind`` stay inside every cap and spacing rule right now?"""
        now = self._clock.time()
        usage = tuple(await self.usage(knobs, now=now))
        for window in usage:
            if window.kind is kind and window.used + 1 > window.cap:
                retry = min(window.window_seconds / 12, RETRY_CAP)
                return BudgetVerdict(
                    allowed=False,
                    reason=(
                        f"{kind.value} cap {window.cap}/{window_label(window.window_seconds)}"
                        " reached"
                    ),
                    retry_at=now + retry,
                    usage=usage,
                )
        if kind is BudgetKind.NAVIGATION:
            spacing = await self._spacing(now, group_id)
            if spacing is not None:
                reason, retry_at = spacing
                return BudgetVerdict(False, reason, retry_at, usage)
        return BudgetVerdict(allowed=True, usage=usage)

    async def _spacing(self, now: float, group_id: str | None) -> tuple[str, float] | None:
        last = await self._ledger.last_budget_event_at(BudgetKind.NAVIGATION)
        # A last event in the future (the wall clock stepped back) still counts: wait it out.
        if last is not None and now < last + self._min_gap:
            return (
                f"less than {self._min_gap:.0f} s since the last navigation",
                last + self._min_gap,
            )
        if group_id is not None:
            last_group = await self._ledger.last_budget_event_at(
                BudgetKind.NAVIGATION, group_id=group_id
            )
            if last_group is not None and now < last_group + self._revisit:
                return (
                    f"group revisited within {self._revisit / MINUTE:.0f} min",
                    last_group + self._revisit,
                )
        return None

    async def acquire(
        self,
        kind: BudgetKind,
        knobs: ProfileKnobs,
        *,
        surface: Surface | None = None,
        group_id: str | None = None,
        read_id: int | None = None,
    ) -> BudgetVerdict:
        """Check and, if allowed, record the step (fsynced) before the caller issues it."""
        async with self._lock:
            verdict = await self.check(kind, knobs, group_id=group_id)
            if verdict.allowed:
                await self._ledger.record_budget_event(
                    kind, surface=surface, group_id=group_id, read_id=read_id
                )
        return verdict

    async def day_remaining(self, knobs: ProfileKnobs) -> float:
        """The smallest remaining share of any 24 h cap (1.0 = untouched, 0.0 = spent)."""
        shares = [
            w.remaining / w.cap if w.cap else 0.0
            for w in await self.usage(knobs)
            if w.window_seconds >= 86400
        ]
        return min(shares, default=1.0)


# -- the plan ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DayShape:
    """How a day splits into hours of each cadence (means; the jitter moves edges, not sizes)."""

    active_hours: float
    shoulder_hours: float
    break_hours: float

    @property
    def full_hours(self) -> float:
        return max(0.0, self.active_hours - self.shoulder_hours - self.break_hours)

    @classmethod
    def from_rhythm(cls, rhythm: RhythmConfig) -> DayShape:
        active = 24.0 - rhythm.quiet_hours
        shoulder = min(
            active, (rhythm.shoulder_after_minutes + rhythm.shoulder_before_minutes) / 60
        )
        return cls(
            active_hours=active,
            shoulder_hours=shoulder,
            break_hours=min(active - shoulder, rhythm.mean_break_hours),
        )


@dataclass(frozen=True, slots=True)
class Cadence:
    """Feed cadence in minutes, full and shoulder; the knobs the plan is computed from."""

    feed_min: float
    feed_max: float
    shoulder_min: float
    shoulder_max: float

    @property
    def feed_per_hour(self) -> float:
        return 60 / ((self.feed_min + self.feed_max) / 2)

    @property
    def shoulder_per_hour(self) -> float:
        return 60 / ((self.shoulder_min + self.shoulder_max) / 2)

    @classmethod
    def of(cls, knobs: ProfileKnobs, settings: Settings) -> Cadence:
        return cls(
            feed_min=knobs.feed_min_minutes,
            feed_max=knobs.feed_max_minutes,
            shoulder_min=max(settings.shoulder_feed_min_minutes, knobs.feed_min_minutes),
            shoulder_max=max(settings.shoulder_feed_max_minutes, knobs.feed_max_minutes),
        )


@dataclass(frozen=True, slots=True)
class Utilisation:
    kind: BudgetKind
    window_seconds: int
    cap: int
    planned: float

    @property
    def share(self) -> float:
        return self.planned / self.cap if self.cap else math.inf

    @property
    def label(self) -> str:
        return f"{self.kind.value}/{window_label(self.window_seconds)}"


@dataclass(frozen=True, slots=True)
class PlannedLoad:
    """The mean load of a profile with ``groups`` monitored groups (DESIGN's budget table)."""

    profile: Profile
    groups: int
    backstop_only: bool
    shape: DayShape
    feed_per_hour: float
    shoulder_feed_per_hour: float
    backstop_per_hour: float
    backstop_interval_hours: float | None
    backstop_rate_limit: float
    """Backstop navigations per hour the budget leaves room for (≤ the configured rate)."""
    stretched: bool
    """The configured backstop interval did not fit and was stretched to ``backstop_rate_limit``
    (never for a profile without a configured rate: its interval *is* the budget's)."""

    @property
    def nav_per_hour(self) -> float:
        return self.feed_per_hour + self.backstop_per_hour

    @property
    def nav_per_day(self) -> float:
        return (
            self.shape.full_hours * self.nav_per_hour
            + self.shape.shoulder_hours * self.shoulder_feed_per_hour
        )

    @property
    def scroll_per_hour(self) -> float:
        return self.feed_per_hour * FEED_STEPS_MEAN + self.backstop_per_hour * BACKSTOP_STEPS_MEAN

    @property
    def scroll_per_day(self) -> float:
        return (
            self.shape.full_hours * self.scroll_per_hour
            + self.shape.shoulder_hours * self.shoulder_feed_per_hour * FEED_STEPS_MEAN
        )

    def planned(self, kind: BudgetKind, window_seconds: int) -> float:
        """The planned mean count in a window (full cadence for windows up to an hour)."""
        if window_seconds >= 86400:
            per_day = self.nav_per_day if kind is BudgetKind.NAVIGATION else self.scroll_per_day
            return per_day * window_seconds / 86400
        per_hour = self.nav_per_hour if kind is BudgetKind.NAVIGATION else self.scroll_per_hour
        return per_hour * window_seconds / 3600

    def utilisation(self, knobs: ProfileKnobs) -> list[Utilisation]:
        return [
            Utilisation(kind, seconds, cap, self.planned(kind, seconds))
            for kind, seconds, cap in knobs.windows()
        ]

    def problems(self, knobs: ProfileKnobs, share: float = PLANNED_SHARE) -> list[str]:
        """Why this plan does not fit under ``share`` of every cap (empty: it fits)."""
        problems = [
            f"{self.profile.value}: planned {u.planned:.1f} {u.label} is over "
            f"{share:.0%} of the cap {u.cap}"
            for u in self.utilisation(knobs)
            if u.planned > share * u.cap + EPSILON
        ]
        if self.groups and not self.backstop_only and self.backstop_interval_hours is None:
            problems.append(f"{self.profile.value}: the feed alone leaves no room for backstops")
        elif (
            self.backstop_interval_hours is not None
            and self.backstop_interval_hours > MAX_BACKSTOP_INTERVAL_HOURS
        ):
            problems.append(
                f"{self.profile.value}: backstops every {self.backstop_interval_hours:.1f} h "
                f"for {self.groups} groups (over {MAX_BACKSTOP_INTERVAL_HOURS:.0f} h)"
            )
        return problems


def backstop_rate_limit(
    knobs: ProfileKnobs,
    shape: DayShape,
    cadence: Cadence,
    *,
    backstop_only: bool = False,
    share: float = PLANNED_SHARE,
) -> float:
    """The most backstop navigations per full-cadence hour that keep the plan under ``share``.

    Every cap gives one bound; the tightest wins, and a configured rate is one more bound (a
    profile without one takes all the room). ``backstop_only`` (the feed is not chronological)
    drops the feed and its shoulder reads and lets backstops use the room.
    """
    feed = 0.0 if backstop_only else cadence.feed_per_hour
    shoulder = 0.0 if backstop_only else cadence.shoulder_per_hour
    full = max(shape.full_hours, EPSILON)
    bounds = [
        share * knobs.nav_per_hour - feed,
        share * knobs.nav_per_15min * 4 - feed,
        (share * knobs.nav_per_day - shoulder * shape.shoulder_hours) / full - feed,
        (share * knobs.scroll_per_hour - feed * FEED_STEPS_MEAN) / BACKSTOP_STEPS_MEAN,
        (
            (share * knobs.scroll_per_day - shoulder * shape.shoulder_hours * FEED_STEPS_MEAN)
            / full
            - feed * FEED_STEPS_MEAN
        )
        / BACKSTOP_STEPS_MEAN,
    ]
    rate = knobs.backstop_groups_per_hour
    if rate is None:
        return min(bounds)
    configured = rate + (cadence.feed_per_hour if backstop_only else 0.0)
    return min(configured, *bounds)


def planned_load(
    settings: Settings,
    profile: Profile,
    groups: int,
    *,
    backstop_only: bool = False,
    rhythm: RhythmConfig | None = None,
) -> PlannedLoad:
    """The mean load the scheduler plans for ``groups`` monitored groups in ``profile``."""
    if not 0 <= groups <= MAX_MONITORED:
        msg = f"monitored groups must be between 0 and {MAX_MONITORED}"
        raise ValueError(msg)
    knobs = knobs_for(settings, profile)
    shape = DayShape.from_rhythm(rhythm or RhythmConfig.from_settings(settings))
    cadence = Cadence.of(knobs, settings)
    limit = backstop_rate_limit(knobs, shape, cadence, backstop_only=backstop_only)
    interval: float | None = None
    per_hour = 0.0
    if groups and limit > EPSILON:
        interval = max(knobs.backstop_floor_hours, groups / limit)
        per_hour = groups / interval
    configured = (
        knobs.backstop_interval_hours(groups)
        if groups and knobs.backstop_groups_per_hour is not None
        else None
    )
    return PlannedLoad(
        profile=profile,
        groups=groups,
        backstop_only=backstop_only,
        shape=shape,
        feed_per_hour=0.0 if backstop_only else cadence.feed_per_hour,
        shoulder_feed_per_hour=0.0 if backstop_only else cadence.shoulder_per_hour,
        backstop_per_hour=per_hour,
        backstop_interval_hours=interval,
        backstop_rate_limit=limit,
        stretched=(
            interval is not None and configured is not None and interval > configured + EPSILON
        ),
    )


def knobs_for(settings: Settings, profile: Profile) -> ProfileKnobs:
    return settings.cautious if profile is Profile.CAUTIOUS else settings.normal


class PlanDoesNotFitError(Exception):
    """The configured pacing cannot stay under 75 % of its own caps; refusing to run it."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems))


def ensure_fits(settings: Settings, groups: int = MAX_MONITORED) -> list[PlannedLoad]:
    """Refuse a configuration whose plan does not fit, for every profile and for ``groups``.

    Checked for the hard maximum of monitored groups by default, so a brain that later sends
    25 groups cannot push a running collector into a plan that was never validated.
    """
    loads = []
    problems: list[str] = []
    for profile in Profile:
        for count in sorted({0, groups}):
            for backstop_only in (False, True):
                load = planned_load(settings, profile, count, backstop_only=backstop_only)
                loads.append(load)
                problems += load.problems(knobs_for(settings, profile))
    if problems:
        raise PlanDoesNotFitError(list(dict.fromkeys(problems)))
    return loads


@dataclass(frozen=True, slots=True)
class TableRow:
    """One row of the design's budget table, with the simulated p95 next to the mean."""

    load: PlannedLoad
    p95_nav_per_hour: float | None = None
    p95_nav_per_15min: float | None = None
    p95_nav_per_day: float | None = None
    p95_scroll_per_hour: float | None = None
    p95_scroll_per_day: float | None = None


def format_table(rows: Sequence[TableRow]) -> str:
    """The budget table as the design shows it (Markdown)."""
    head = (
        "| Profile | N | Backstop interval | Navigations/h (mean, p95) | Navigations/24 h "
        "(mean, p95) | Scroll steps/24 h (mean, p95) |\n|---|---|---|---|---|---|"
    )
    lines = [head]
    for row in rows:
        load = row.load
        interval = f"{load.backstop_interval_hours:.1f} h" if load.backstop_interval_hours else "—"
        if load.stretched:
            interval += " (stretched to fit)"
        lines.append(
            f"| {load.profile.value} | {load.groups} | {interval} | "
            f"{load.nav_per_hour:.1f}, {_opt(row.p95_nav_per_hour)} | "
            f"{load.nav_per_day:.0f}, {_opt(row.p95_nav_per_day)} | "
            f"{load.scroll_per_day:.0f}, {_opt(row.p95_scroll_per_day)} |"
        )
    return "\n".join(lines)


def _opt(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}"


def interval_seconds(load: PlannedLoad) -> float | None:
    return None if load.backstop_interval_hours is None else load.backstop_interval_hours * HOUR
