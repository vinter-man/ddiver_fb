from __future__ import annotations

import datetime as dt
import zoneinfo

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from fbcollector.rhythm import (
    CATCH_UP_STEPS,
    KV_SEED,
    MINUTE,
    DayPlan,
    Jitter,
    Phase,
    Rhythm,
    RhythmConfig,
    Span,
    load_seed,
)

from .pacing_support import ZONE, local, make_settings

SOFIA = zoneinfo.ZoneInfo(ZONE)


def rhythm(seed: str = "seed", **overrides: object) -> Rhythm:
    config = RhythmConfig.from_settings(make_settings(**overrides))
    return Rhythm(config, Jitter(seed))


def wall(t: float, zone: zoneinfo.ZoneInfo = SOFIA) -> dt.datetime:
    return dt.datetime.fromtimestamp(t, zone)


def minutes_of_day(t: float) -> float:
    w = wall(t)
    return w.hour * 60 + w.minute + w.second / 60


class TestJitter:
    def test_same_key_same_value_across_instances(self) -> None:
        a, b = Jitter("s"), Jitter("s")
        assert a.random("feed", 12.5) == b.random("feed", 12.5)
        assert a.uniform(3, 9, dt.date(2026, 1, 2)) == b.uniform(3, 9, dt.date(2026, 1, 2))

    def test_keys_and_seeds_matter(self) -> None:
        j = Jitter("s")
        assert j.random("a") != j.random("b")
        assert Jitter("t").random("a") != j.random("a")
        # A float and its string are different keys only by representation, never by accident.
        assert j.random(1.5) == j.random(1.5)

    def test_bounds(self) -> None:
        j = Jitter("s")
        values = [j.uniform(10, 20, i) for i in range(200)]
        assert all(10 <= v < 20 for v in values)
        ints = {j.randint(2, 3, i) for i in range(50)}
        assert ints == {2, 3}

    def test_empty_seed_is_refused(self) -> None:
        with pytest.raises(ValueError, match="seed"):
            Jitter("")


class _KV:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def kv_get(self, key: str) -> str | None:
        return self.data.get(key)

    async def kv_set(self, key: str, value: str) -> None:
        self.data[key] = value


async def test_seed_is_created_once_and_kept() -> None:
    kv = _KV()
    first = await load_seed(kv)
    assert len(first) == 32
    assert kv.data[KV_SEED] == first
    assert await load_seed(kv) == first


class TestDayPlan:
    def test_a_weekday(self) -> None:
        r = rhythm()
        day = dt.date(2026, 10, 7)  # Wednesday
        plan = r.plan(day)
        assert 15 <= minutes_of_day(plan.quiet_start) <= 105  # 01:00 ± 45 min
        assert 375 <= minutes_of_day(plan.quiet_end) <= 465  # 07:00 ± 45 min
        assert wall(plan.quiet_start).date() == day
        assert plan.shoulder_end == pytest.approx(plan.quiet_end + 60 * MINUTE)
        assert plan.shoulder_start == pytest.approx(plan.next_quiet_start - 90 * MINUTE)
        assert plan.next_quiet_start == r.plan(day + dt.timedelta(days=1)).quiet_start

    def test_weekend_nights_start_and_end_later(self) -> None:
        r = rhythm(quiet_jitter_minutes=0)
        friday_night = r.plan(dt.date(2026, 10, 10))  # ends Saturday morning
        weekday_night = r.plan(dt.date(2026, 10, 8))
        assert minutes_of_day(friday_night.quiet_start) == pytest.approx(120)
        assert minutes_of_day(friday_night.quiet_end) == pytest.approx(480)
        assert minutes_of_day(weekday_night.quiet_start) == pytest.approx(60)
        assert minutes_of_day(r.plan(dt.date(2026, 10, 12)).quiet_end) == pytest.approx(420)

    def test_breaks_catch_up_and_sync(self) -> None:
        r = rhythm()
        for offset in range(30):
            plan = r.plan(dt.date(2026, 11, 1) + dt.timedelta(days=offset))
            assert len(plan.breaks) == 2
            previous_end = plan.shoulder_end
            for b in plan.breaks:
                assert 15 * MINUTE <= b.seconds <= 40 * MINUTE
                assert b.start >= previous_end
                previous_end = b.end
            assert previous_end <= plan.shoulder_start
            assert 30 * MINUTE <= plan.catch_up_at - plan.quiet_end <= 90 * MINUTE
            assert sum(plan.catch_up_steps) == CATCH_UP_STEPS
            assert len(plan.catch_up_steps) in {2, 3}
            assert min(plan.catch_up_steps) >= 2
            assert plan.shoulder_end <= plan.sync_at < plan.shoulder_start
            assert plan.phase(plan.sync_at) is Phase.FULL

    def test_the_same_seed_gives_the_same_day_after_a_restart(self) -> None:
        day = dt.date(2026, 12, 3)
        assert rhythm("x").plan(day) == rhythm("x").plan(day)
        assert rhythm("x").plan(day) != rhythm("y").plan(day)

    def test_no_breaks_when_disabled_or_no_room(self) -> None:
        assert rhythm(breaks_per_day=0).plan(dt.date(2026, 10, 7)).breaks == ()
        tight = rhythm(
            shoulder_after_minutes=240,
            shoulder_before_minutes=240,
            breaks_per_day=6,
            break_min_minutes=100,
            break_max_minutes=120,
        )
        plan = tight.plan(dt.date(2026, 10, 7))
        assert len(plan.breaks) < 6
        assert plan.full_seconds > 0

    def test_quiet_hours_across_midnight(self) -> None:
        r = rhythm(quiet_start=dt.time(23, 0), quiet_end=dt.time(6, 0), quiet_jitter_minutes=0)
        plan = r.plan(dt.date(2026, 10, 7))
        assert wall(plan.quiet_start).date() == dt.date(2026, 10, 6)
        assert wall(plan.quiet_start).hour == 23
        assert wall(plan.quiet_end).hour == 6
        assert r.config.quiet_hours == 7

    def test_config_arithmetic(self) -> None:
        config = RhythmConfig.from_settings(make_settings())
        assert config.quiet_hours == 6
        assert config.mean_break_hours == pytest.approx(2 * 27.5 / 60)


class TestPhases:
    def test_phase_walk_through_a_day(self) -> None:
        r = rhythm()
        plan = r.plan(dt.date(2026, 10, 7))
        assert plan.phase(plan.quiet_start + 60) is Phase.QUIET
        assert plan.phase(plan.quiet_end + 60) is Phase.SHOULDER
        assert plan.phase(plan.shoulder_end + 1) in {Phase.FULL, Phase.BREAK}
        first_break = plan.breaks[0]
        assert plan.phase(first_break.start + 1) is Phase.BREAK
        assert plan.resumes_at(first_break.start + 1) == first_break.end
        assert plan.resumes_at(plan.quiet_start + 1) == plan.quiet_end
        assert plan.resumes_at(first_break.end + 5) == first_break.end + 5
        assert plan.phase(plan.next_quiet_start - 60) is Phase.SHOULDER
        assert plan.next_change(plan.quiet_end + 1) == plan.shoulder_end
        assert plan.next_change(plan.next_quiet_start - 1) == plan.next_quiet_start
        assert plan.rest_before(first_break.end + 10) == first_break.end
        assert plan.rest_before(plan.quiet_end + 10) == plan.quiet_end
        assert r.phase(plan.quiet_end + 1) is Phase.SHOULDER

    def test_span_membership(self) -> None:
        span = Span(10.0, 20.0)
        assert 10.0 in span
        assert 20.0 not in span
        assert "15" not in span
        assert span.seconds == 10
        plan = rhythm().plan(dt.date(2026, 10, 7))
        assert "x" not in plan

    def test_plan_at_finds_the_containing_day(self) -> None:
        r = rhythm()
        start = local(2026, 10, 5)
        t = start
        while t < start + 10 * 86400:
            plan = r.plan_at(t)
            assert plan.quiet_start <= t < plan.next_quiet_start
            t += 1234.5

    def test_plan_cache_is_bounded(self) -> None:
        r = rhythm()
        for offset in range(40):
            r.plan(dt.date(2026, 1, 1) + dt.timedelta(days=offset))
        assert len(r._plans) <= 16


class TestDst:
    @pytest.mark.parametrize(
        ("day", "hours"),
        [
            (dt.date(2026, 3, 29), 5),  # Sofia springs forward 03:00 -> 04:00 inside quiet hours
            (dt.date(2026, 10, 25), 7),  # and falls back 04:00 -> 03:00
        ],
    )
    def test_quiet_hours_keep_their_wall_clock_edges(self, day: dt.date, hours: int) -> None:
        r = rhythm(quiet_jitter_minutes=0, quiet_weekend_shift_minutes=0)
        plan = r.plan(day)
        assert wall(plan.quiet_start).hour == 1
        assert wall(plan.quiet_end).hour == 7
        assert plan.quiet_end - plan.quiet_start == pytest.approx(hours * 3600)
        # every instant of that night is quiet, and the day after is not
        t = plan.quiet_start
        while t < plan.quiet_end:
            assert r.phase(t) is Phase.QUIET
            t += 300
        assert r.phase(plan.quiet_end + 1) is Phase.SHOULDER

    @settings(max_examples=150, deadline=None)
    @given(
        day=st.dates(min_value=dt.date(2026, 1, 1), max_value=dt.date(2029, 12, 31)),
        zone=st.sampled_from(["Europe/Sofia", "America/New_York", "Europe/London", "UTC"]),
        seed=st.text(min_size=1, max_size=8),
    )
    # Spring-forward Sundays, where the night loses its hour, first of all (the reviewer's
    # counterexample was 2029-03-25 in Sofia: 3.99 real hours).
    @example(day=dt.date(2029, 3, 25), zone="Europe/Sofia", seed="\x81j\U0001209e\xe3\xa81\xf5")
    @example(day=dt.date(2026, 3, 29), zone="Europe/Sofia", seed="a")
    @example(day=dt.date(2026, 3, 29), zone="Europe/London", seed="a")
    @example(day=dt.date(2026, 3, 8), zone="America/New_York", seed="a")
    @example(day=dt.date(2026, 10, 25), zone="Europe/Sofia", seed="a")
    @example(day=dt.date(2026, 11, 1), zone="America/New_York", seed="a")
    def test_every_day_is_well_formed_in_any_zone(self, day: dt.date, zone: str, seed: str) -> None:
        config = RhythmConfig.from_settings(make_settings(timezone=zone))
        r = Rhythm(config, Jitter(seed))
        plan: DayPlan = r.plan(day)
        z = zoneinfo.ZoneInfo(zone)
        start = wall(plan.quiet_start, z)
        end = wall(plan.quiet_end, z)
        # 01:00/07:00 ± 45 min, plus an hour at weekends (a DST jump can add or take one)
        assert 0 <= start.hour <= 3
        assert 6 <= end.hour <= 9
        # The night is 6 h ± 90 min on the wall clock (an edge that falls into a DST gap moves
        # out of it, by up to the jump); its real length is that minus the hour a spring-forward
        # night loses, plus the hour an autumn night gains: quiet hours keep their wall-clock
        # edges, as a sleeper's alarm does.
        on_the_wall = (end.replace(tzinfo=None) - start.replace(tzinfo=None)).total_seconds()
        shift = (end.utcoffset() - start.utcoffset()).total_seconds()  # type: ignore[operator]
        assert 4.5 * 3600 - abs(shift) <= on_the_wall <= 7.5 * 3600 + abs(shift)
        real = plan.quiet_end - plan.quiet_start
        assert real == pytest.approx(on_the_wall - shift)
        assert 3.5 * 3600 <= real <= 8.5 * 3600
        assert plan.quiet_end < plan.shoulder_end <= plan.shoulder_start < plan.next_quiet_start
        assert r.plan_at(plan.quiet_start) == plan
        assert r.plan_at(plan.next_quiet_start - 1) == plan


class TestDstEdges:
    """A quiet-hours edge that falls into a DST gap or overlap resolves to the longer night."""

    NEW_YORK = zoneinfo.ZoneInfo("America/New_York")

    def r(self, start: dt.time, end: dt.time) -> Rhythm:
        config = RhythmConfig(
            zone=self.NEW_YORK,
            quiet_start=start,
            quiet_end=end,
            quiet_jitter_minutes=0,
            weekend_shift_minutes=0,
        )
        return Rhythm(config, Jitter("edges"))

    def test_a_start_in_the_spring_gap_moves_earlier(self) -> None:
        # 2026-03-08 02:30 does not exist in New York (02:00 -> 03:00)
        night = self.r(dt.time(2, 30), dt.time(7, 0)).quiet(dt.date(2026, 3, 8))
        assert dt.datetime.fromtimestamp(night.start, dt.UTC).hour == 6  # 01:30 EST
        assert wall(night.start, self.NEW_YORK).strftime("%H:%M %Z") == "01:30 EST"

    def test_an_end_in_the_autumn_overlap_takes_the_later_reading(self) -> None:
        # 2026-11-01 01:30 happens twice in New York; the second one is an hour later
        night = self.r(dt.time(0, 30), dt.time(1, 30)).quiet(dt.date(2026, 11, 1))
        assert wall(night.end, self.NEW_YORK).strftime("%H:%M %Z") == "01:30 EST"
        assert night.end - night.start == pytest.approx(2 * 3600)
