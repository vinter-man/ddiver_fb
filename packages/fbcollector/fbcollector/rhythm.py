"""The shape of a collector day: quiet hours, shoulders, breaks, the catch-up and the sync time.

Every random choice here is a *deterministic* function of a per-installation secret seed and of
what it decides (the date, the group, the event it follows). Two things depend on that:

- A restart must not re-roll the day. If the quiet-hours edge or a break were drawn afresh at
  every start, a crash loop at 00:50 would keep drawing until one draw said "not quiet yet".
- A due time must not be re-drawn at every look. Drawing a fresh ±30 % jitter on each planning
  pass and acting as soon as one draw says "due" is taking the minimum of many draws, which
  quietly biases every interval towards its short end — exactly the regular, fast rhythm the
  jitter exists to avoid.

``random.Random`` seeded with a string hashes it with SHA-512, so the draws are stable across
processes and Python runs (unlike ``hash()``).

Local times are converted in the pinned zone and every duration after that is plain UTC seconds,
so days with a DST change are one hour shorter or longer and nothing else changes: the edges land
on the right wall-clock time, and a 45-minute jitter is 45 real minutes.
"""

from __future__ import annotations

import datetime as dt
import enum
import random
import secrets
import zoneinfo
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    from fbcollector.config import Settings

MINUTE: Final = 60.0
HOUR: Final = 3600.0
DAY: Final = 86400.0

CATCH_UP_WINDOW_MINUTES: Final = (30.0, 90.0)
"""The day's catch-up happens at a random point this many minutes after quiet hours end."""
CATCH_UP_STEPS: Final = 8
"""Scroll steps the catch-up may use in total, spread over 2-3 feed reads."""
CATCH_UP_READS: Final = (2, 3)
CATCH_UP_EXPIRES: Final = 3 * HOUR
"""A catch-up not started this long after its time is skipped: it belongs to the morning."""
MIN_QUIET: Final = 15 * MINUTE
BREAK_MARGIN: Final = 30 * MINUTE
SYNC_TAIL: Final = 30 * MINUTE

KV_SEED: Final = "scheduler_seed"


class Jitter:
    """Deterministic randomness keyed by what it decides (see the module docstring)."""

    def __init__(self, seed: str) -> None:
        if not seed:
            msg = "the jitter seed must not be empty"
            raise ValueError(msg)
        self._seed = seed

    def rng(self, *key: object) -> random.Random:
        return random.Random(f"{self._seed}|{'|'.join(_key_part(k) for k in key)}")

    def random(self, *key: object) -> float:
        """A number in ``[0, 1)``, the same every time for the same key."""
        return self.rng(*key).random()

    def uniform(self, low: float, high: float, *key: object) -> float:
        return low + (high - low) * self.random(*key)

    def randint(self, low: int, high: int, *key: object) -> int:
        return self.rng(*key).randint(low, high)


def _key_part(part: object) -> str:
    if isinstance(part, float):
        return repr(part)
    if isinstance(part, dt.date):
        return part.isoformat()
    return str(part)


class SeedStore(Protocol):
    async def kv_get(self, key: str) -> str | None: ...

    async def kv_set(self, key: str, value: str) -> None: ...


async def load_seed(store: SeedStore) -> str:
    """The installation's jitter seed: created once, kept in ``kv``, never logged."""
    seed = await store.kv_get(KV_SEED)
    if seed:
        return seed
    seed = secrets.token_hex(16)
    await store.kv_set(KV_SEED, seed)
    return seed


class Phase(enum.StrEnum):
    QUIET = "quiet"
    SHOULDER = "shoulder"  # the first hour after quiet hours and the last 1.5 h before them
    FULL = "full"
    BREAK = "break"


@dataclass(frozen=True, slots=True)
class Span:
    start: float
    end: float

    def __contains__(self, t: object) -> bool:
        return isinstance(t, int | float) and self.start <= t < self.end

    @property
    def seconds(self) -> float:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class DayPlan:
    """One day, from the start of its quiet hours to the start of the next night's.

    ``day`` is the local date on whose morning the quiet hours end; the plan covers
    ``[quiet_start, next_quiet_start)``.
    """

    day: dt.date
    quiet_start: float
    quiet_end: float
    shoulder_end: float
    shoulder_start: float
    next_quiet_start: float
    breaks: tuple[Span, ...]
    catch_up_at: float
    catch_up_steps: tuple[int, ...]
    sync_at: float

    def __contains__(self, t: object) -> bool:
        return isinstance(t, int | float) and self.quiet_start <= t < self.next_quiet_start

    def phase(self, t: float) -> Phase:
        if t < self.quiet_end:
            return Phase.QUIET
        if any(t in b for b in self.breaks):
            return Phase.BREAK
        if t < self.shoulder_end or t >= self.shoulder_start:
            return Phase.SHOULDER
        return Phase.FULL

    def resumes_at(self, t: float) -> float:
        """When the quiet hours or break around ``t`` end (``t`` itself when neither applies)."""
        if t < self.quiet_end:
            return self.quiet_end
        for b in self.breaks:
            if t in b:
                return b.end
        return t

    def next_change(self, t: float) -> float:
        """The next phase boundary after ``t``: a planner re-plans there at the latest."""
        edges = [self.quiet_end, self.shoulder_end, self.shoulder_start, self.next_quiet_start]
        for b in self.breaks:
            edges += [b.start, b.end]
        return min((e for e in edges if e > t), default=self.next_quiet_start)

    def rest_before(self, t: float) -> float:
        """The end of the latest quiet hours or break that finished at or before ``t``."""
        ends = [self.quiet_end, *(b.end for b in self.breaks)]
        return max((e for e in ends if e <= t), default=self.quiet_end)

    @property
    def full_seconds(self) -> float:
        return max(0.0, self.shoulder_start - self.shoulder_end) - sum(
            b.seconds for b in self.breaks
        )


@dataclass(frozen=True, slots=True)
class RhythmConfig:
    zone: zoneinfo.ZoneInfo
    quiet_start: dt.time = dt.time(1, 0)
    quiet_end: dt.time = dt.time(7, 0)
    quiet_jitter_minutes: float = 45
    weekend_shift_minutes: float = 60
    shoulder_after_minutes: float = 60
    shoulder_before_minutes: float = 90
    breaks_per_day: int = 2
    break_min_minutes: float = 15
    break_max_minutes: float = 40

    @classmethod
    def from_settings(cls, settings: Settings) -> RhythmConfig:
        return cls(
            zone=settings.zone,
            quiet_start=settings.quiet_start,
            quiet_end=settings.quiet_end,
            quiet_jitter_minutes=settings.quiet_jitter_minutes,
            weekend_shift_minutes=settings.quiet_weekend_shift_minutes,
            shoulder_after_minutes=settings.shoulder_after_minutes,
            shoulder_before_minutes=settings.shoulder_before_minutes,
            breaks_per_day=settings.breaks_per_day,
            break_min_minutes=settings.break_min_minutes,
            break_max_minutes=settings.break_max_minutes,
        )

    @property
    def quiet_hours(self) -> float:
        """The nominal length of the night in hours (jitter moves both edges, not the length)."""
        start = self.quiet_start.hour * 60 + self.quiet_start.minute
        end = self.quiet_end.hour * 60 + self.quiet_end.minute
        return ((end - start) % (24 * 60)) / 60 or 24.0

    @property
    def mean_break_hours(self) -> float:
        return self.breaks_per_day * (self.break_min_minutes + self.break_max_minutes) / 2 / 60


class Rhythm:
    """Day plans in the pinned zone, cached per date."""

    _CACHE: Final = 16

    def __init__(self, config: RhythmConfig, jitter: Jitter) -> None:
        self.config = config
        self._jitter = jitter
        self._plans: dict[dt.date, DayPlan] = {}

    def local_date(self, t: float) -> dt.date:
        return dt.datetime.fromtimestamp(t, self.config.zone).date()

    def plan_at(self, t: float) -> DayPlan:
        """The day plan whose span contains ``t``."""
        today = self.local_date(t)
        for offset in (0, -1, 1, -2, 2):
            plan = self.plan(today + dt.timedelta(days=offset))
            if t in plan:
                return plan
        msg = f"no day plan contains {t}"  # pragma: no cover - quiet starts are ordered
        raise LookupError(msg)  # pragma: no cover

    def phase(self, t: float) -> Phase:
        return self.plan_at(t).phase(t)

    def plan(self, day: dt.date) -> DayPlan:
        cached = self._plans.get(day)
        if cached is not None:
            return cached
        plan = self._build(day)
        if len(self._plans) >= self._CACHE:
            self._plans.pop(next(iter(self._plans)))
        self._plans[day] = plan
        return plan

    def quiet(self, day: dt.date) -> Span:
        """The quiet hours that end on the morning of ``day``.

        Edges are computed on the wall clock (the weekend shift and the jitter move the local
        time, not a count of seconds), then converted. A wall time that falls into a DST gap or
        overlap has two readings; the start takes the earlier and the end the later, so a DST
        night can only make quiet hours longer, never shorter.
        """
        cfg = self.config
        start_day = day if cfg.quiet_start < cfg.quiet_end else day - dt.timedelta(days=1)
        shift = cfg.weekend_shift_minutes if day.weekday() >= 5 else 0.0
        jitter = cfg.quiet_jitter_minutes
        start_wall = dt.datetime.combine(start_day, cfg.quiet_start) + dt.timedelta(
            minutes=shift + self._jitter.uniform(-jitter, jitter, "quiet_start", day)
        )
        end_wall = dt.datetime.combine(day, cfg.quiet_end) + dt.timedelta(
            minutes=shift + self._jitter.uniform(-jitter, jitter, "quiet_end", day)
        )
        start = self._instant(start_wall, earliest=True)
        end = self._instant(end_wall, earliest=False)
        return Span(start, max(end, start + MIN_QUIET))

    def _instant(self, wall: dt.datetime, *, earliest: bool) -> float:
        readings = [wall.replace(tzinfo=self.config.zone, fold=fold).timestamp() for fold in (0, 1)]
        return min(readings) if earliest else max(readings)

    def _build(self, day: dt.date) -> DayPlan:
        cfg = self.config
        night = self.quiet(day)
        next_start = self.quiet(day + dt.timedelta(days=1)).start
        middle = (night.end + next_start) / 2
        shoulder_end = min(night.end + cfg.shoulder_after_minutes * MINUTE, middle)
        shoulder_start = max(next_start - cfg.shoulder_before_minutes * MINUTE, shoulder_end)
        breaks = self._breaks(day, Span(shoulder_end, shoulder_start))
        catch_low, catch_high = CATCH_UP_WINDOW_MINUTES
        catch_up_at = night.end + self._jitter.uniform(
            catch_low * MINUTE, catch_high * MINUTE, "catch_up", day
        )
        return DayPlan(
            day=day,
            quiet_start=night.start,
            quiet_end=night.end,
            shoulder_end=shoulder_end,
            shoulder_start=shoulder_start,
            next_quiet_start=next_start,
            breaks=breaks,
            catch_up_at=min(catch_up_at, next_start),
            catch_up_steps=self._catch_up_steps(day),
            sync_at=self._sync_at(day, Span(shoulder_end, shoulder_start), breaks),
        )

    def _breaks(self, day: dt.date, full: Span) -> tuple[Span, ...]:
        cfg = self.config
        longest = cfg.break_max_minutes * MINUTE
        count = cfg.breaks_per_day
        while count and full.seconds < count * (longest + BREAK_MARGIN):
            count -= 1
        if not count:
            return ()
        segment = full.seconds / count
        spans = []
        for index in range(count):
            length = self._jitter.uniform(
                cfg.break_min_minutes * MINUTE, longest, "break_length", day, index
            )
            offset = self._jitter.uniform(0.0, segment - length, "break_start", day, index)
            start = full.start + index * segment + offset
            spans.append(Span(start, start + length))
        return tuple(spans)

    def _catch_up_steps(self, day: dt.date) -> tuple[int, ...]:
        reads = self._jitter.randint(*CATCH_UP_READS, "catch_up_reads", day)
        rng = self._jitter.rng("catch_up_split", day)
        # A random composition of CATCH_UP_STEPS into ``reads`` parts of at least 2 each.
        spare = CATCH_UP_STEPS - 2 * reads
        cuts = sorted(rng.randint(0, spare) for _ in range(reads - 1))
        bounds = [0, *cuts, spare]
        return tuple(2 + bounds[i + 1] - bounds[i] for i in range(reads))

    def _sync_at(self, day: dt.date, full: Span, breaks: tuple[Span, ...]) -> float:
        latest = max(full.start, full.end - SYNC_TAIL)
        at = self._jitter.uniform(full.start, latest, "sync", day)
        for b in breaks:
            if at in b:
                return b.end + MINUTE
        return at
