from __future__ import annotations

import asyncio
import functools
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fbcollector.budget import (
    MAX_BACKSTOP_INTERVAL_HOURS,
    PLANNED_SHARE,
    Budget,
    DayShape,
    PlanDoesNotFitError,
    TableRow,
    ensure_fits,
    format_table,
    interval_seconds,
    knobs_for,
    planned_load,
    window_label,
)
from fbcollector.clock import FakeClock
from fbcollector.config import CAUTIOUS, NORMAL
from fbcollector.model import Surface
from fbcollector.rhythm import RhythmConfig
from fbcollector.scheduler import ActionKind, simulate
from fbcollector.store import Store
from fbwire.models import BudgetKind, Profile

from .pacing_support import make_settings

NAV = BudgetKind.NAVIGATION
SCROLL = BudgetKind.SCROLL


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
def budget(store: Store, clock: FakeClock) -> Budget:
    return Budget(store, clock, min_gap=45, revisit=1800)


class TestCaps:
    async def test_the_15_minute_sub_cap(self, budget: Budget, clock: FakeClock) -> None:
        for _ in range(NORMAL.nav_per_15min):
            assert (await budget.acquire(NAV, NORMAL, surface=Surface.FEED)).allowed
            clock.advance(46)
        denied = await budget.acquire(NAV, NORMAL, surface=Surface.FEED)
        assert not denied.allowed
        assert "navigation cap 5/15min" in denied.reason
        assert denied.retry_at is not None and denied.retry_at > clock.time()
        assert any(w.window_seconds == 900 and w.used == 5 for w in denied.usage)
        # the first event leaves the sliding window 15 minutes after it was recorded
        clock.advance(900 - 4 * 46)
        assert (await budget.check(NAV, NORMAL)).allowed

    async def test_hour_and_day_caps(self, store: Store, budget: Budget, clock: FakeClock) -> None:
        knobs = NORMAL.model_copy(update={"nav_per_15min": 100})
        for _ in range(knobs.nav_per_hour):
            await store.record_budget_event(NAV)
            clock.advance(60)
        verdict = await budget.check(NAV, knobs)
        assert not verdict.allowed
        assert "20/1h" in verdict.reason
        for _ in range(knobs.nav_per_day // knobs.nav_per_hour - 1):
            clock.advance(3600)
            for _ in range(knobs.nav_per_hour):
                await store.record_budget_event(NAV)
        clock.advance(3601)
        assert await store.budget_used(NAV, 86400) == knobs.nav_per_day
        verdict = await budget.check(NAV, knobs)
        assert "320/24h" in verdict.reason

    async def test_scroll_caps_have_no_spacing(self, budget: Budget) -> None:
        for _ in range(CAUTIOUS.scroll_per_hour):
            assert (await budget.acquire(SCROLL, CAUTIOUS, read_id=1)).allowed
        verdict = await budget.acquire(SCROLL, CAUTIOUS, read_id=1)
        assert not verdict.allowed
        assert "scroll cap 30/1h" in verdict.reason

    async def test_a_zero_cap_never_allows(self, budget: Budget) -> None:
        knobs = CAUTIOUS.model_copy(update={"scroll_per_hour": 0})
        assert not (await budget.check(SCROLL, knobs)).allowed

    async def test_acquire_records_only_what_it_allows(
        self, store: Store, budget: Budget, clock: FakeClock
    ) -> None:
        assert (await budget.acquire(NAV, NORMAL, surface=Surface.GROUP, group_id="7")).allowed
        assert not (await budget.acquire(NAV, NORMAL)).allowed  # spacing
        assert await store.budget_used(NAV, 3600) == 1
        assert await store.budget_event_times(NAV, since=0) == [clock.time()]

    async def test_concurrent_acquires_never_overshoot(self, store: Store, budget: Budget) -> None:
        knobs = CAUTIOUS.model_copy(update={"scroll_per_hour": 7})
        results = await asyncio.gather(*(budget.acquire(SCROLL, knobs) for _ in range(20)))
        assert sum(r.allowed for r in results) == 7
        assert await store.budget_used(SCROLL, 3600) == 7

    async def test_day_remaining(self, store: Store, budget: Budget) -> None:
        assert await budget.day_remaining(NORMAL) == 1.0
        for _ in range(160):
            await store.record_budget_event(NAV)
        assert await budget.day_remaining(NORMAL) == pytest.approx(0.5)
        zero = NORMAL.model_copy(update={"scroll_per_day": 0})
        assert await budget.day_remaining(zero) == 0.0

    def test_window_labels(self) -> None:
        assert window_label(900) == "15min"
        assert window_label(3600) == "1h"
        assert window_label(86400) == "24h"
        assert window_label(7200) == "2h"


class TestSpacing:
    async def test_45_seconds_from_the_last_persisted_navigation(
        self, tmp_path: Path, clock: FakeClock
    ) -> None:
        path = tmp_path / "state.sqlite"
        async with Store(path, clock=clock) as first:
            assert (await Budget(first, clock).acquire(NAV, NORMAL)).allowed
        clock.advance(30)
        # a restart: new store, new budget; the rule is read back from the table
        async with Store(path, clock=clock) as second:
            budget = Budget(second, clock, min_gap=45)
            verdict = await budget.check(NAV, NORMAL)
            assert not verdict.allowed
            assert verdict.retry_at == pytest.approx(clock.time() + 15)
            clock.advance(15)
            assert (await budget.check(NAV, NORMAL)).allowed

    async def test_a_clock_stepped_back_waits_the_step_out(
        self, budget: Budget, clock: FakeClock
    ) -> None:
        await budget.acquire(NAV, NORMAL)
        clock.jump_wall(-600)
        verdict = await budget.check(NAV, NORMAL)
        assert not verdict.allowed
        assert verdict.retry_at == pytest.approx(clock.time() + 645)

    async def test_a_group_is_not_revisited_within_30_minutes(
        self, budget: Budget, clock: FakeClock
    ) -> None:
        assert (await budget.acquire(NAV, NORMAL, group_id="1")).allowed
        clock.advance(600)
        other = await budget.acquire(NAV, NORMAL, group_id="2")
        assert other.allowed
        clock.advance(60)
        again = await budget.check(NAV, NORMAL, group_id="1")
        assert not again.allowed
        assert "revisited" in again.reason
        clock.advance(1800 - 660)
        assert (await budget.check(NAV, NORMAL, group_id="1")).allowed


class TestPlannedLoad:
    @pytest.mark.parametrize(("groups", "interval"), [(10, 1.6), (15, 2.3), (20, 3.1), (25, 3.9)])
    def test_normal_matches_the_design_table(self, groups: int, interval: float) -> None:
        load = planned_load(make_settings(), Profile.NORMAL, groups)
        assert load.backstop_interval_hours is not None
        assert round(load.backstop_interval_hours, 1) == interval
        assert load.nav_per_hour == pytest.approx(15.0, abs=0.05)
        assert load.nav_per_day == pytest.approx(226, abs=1)
        assert load.scroll_per_day == pytest.approx(312, abs=1)
        assert load.feed_per_hour == pytest.approx(8.57, abs=0.01)
        assert not load.stretched
        assert load.problems(NORMAL) == []

    def test_the_day_shape_of_the_design(self) -> None:
        shape = DayShape.from_rhythm(RhythmConfig.from_settings(make_settings()))
        assert shape.active_hours == 18
        assert shape.shoulder_hours == 2.5
        assert shape.full_hours == pytest.approx(14.58, abs=0.01)

    def test_cautious_feed_cadence(self) -> None:
        """Owner decision of 2026-09-25: the cautious feed comes every U(10, 18) min."""
        load = planned_load(make_settings(), Profile.CAUTIOUS, 4)
        assert (CAUTIOUS.feed_min_minutes, CAUTIOUS.feed_max_minutes) == (10, 18)
        assert load.feed_per_hour == pytest.approx(60 / 14)

    def test_cautious_backstops_take_the_room_the_caps_leave(self) -> None:
        """``max(3 h, the smallest interval under 75 % of every cap)``: with the feed at
        60/14 an hour, the hourly navigation cap leaves 7.5 - 4.29 = 3.21 backstops an hour."""
        settings = make_settings()
        limit = PLANNED_SHARE * CAUTIOUS.nav_per_hour - 60 / 14
        for groups in range(1, 26):
            load = planned_load(settings, Profile.CAUTIOUS, groups)
            assert load.backstop_rate_limit == pytest.approx(limit)
            assert load.backstop_interval_hours == pytest.approx(max(3.0, groups / limit))
            assert not load.stretched  # no rate of its own: the budget's interval is the rule
            assert load.problems(CAUTIOUS) == []
            assert max(u.share for u in load.utilisation(CAUTIOUS)) <= PLANNED_SHARE + 1e-9
        # the one cap that binds: the hourly navigations, at 75 % exactly from N = 10 on
        busy = planned_load(settings, Profile.CAUTIOUS, 25)
        assert busy.nav_per_hour == pytest.approx(PLANNED_SHARE * CAUTIOUS.nav_per_hour)

    @pytest.mark.parametrize(("groups", "hours"), [(1, 3.0), (4, 3.0), (9, 3.0), (10, 3.11)])
    def test_few_cautious_groups_are_backstopped_every_3_hours(
        self, groups: int, hours: float
    ) -> None:
        load = planned_load(make_settings(), Profile.CAUTIOUS, groups)
        assert load.backstop_interval_hours == pytest.approx(hours, abs=0.005)

    def test_a_configured_cautious_rate_is_still_a_ceiling(self) -> None:
        settings = make_settings(cautious={"backstop_groups_per_hour": 1.0})
        load = planned_load(settings, Profile.CAUTIOUS, 10)
        assert load.backstop_rate_limit == 1.0
        assert load.backstop_interval_hours == 10.0
        assert not load.stretched  # the rate fits: nothing was stretched

    def test_a_rate_that_does_not_fit_is_stretched(self) -> None:
        settings = make_settings(normal={"nav_per_hour": 16})
        load = planned_load(settings, Profile.NORMAL, 25)
        assert load.stretched
        assert "(stretched to fit)" in format_table([TableRow(load)])

    def test_small_n_uses_the_floor(self) -> None:
        load = planned_load(make_settings(), Profile.NORMAL, 3)
        assert load.backstop_interval_hours == 1.5
        assert load.backstop_per_hour == pytest.approx(2.0)
        assert interval_seconds(load) == 5400

    def test_no_groups_no_backstops(self) -> None:
        load = planned_load(make_settings(), Profile.NORMAL, 0)
        assert load.backstop_interval_hours is None
        assert interval_seconds(load) is None
        assert load.nav_per_hour == pytest.approx(8.57, abs=0.01)
        assert load.problems(NORMAL) == []

    def test_backstop_only_gives_the_feed_budget_to_backstops(self) -> None:
        settings = make_settings()
        normal = planned_load(settings, Profile.NORMAL, 15)
        only = planned_load(settings, Profile.NORMAL, 15, backstop_only=True)
        assert only.feed_per_hour == 0
        assert only.backstop_interval_hours is not None
        assert normal.backstop_interval_hours is not None
        assert only.backstop_interval_hours < normal.backstop_interval_hours
        assert only.nav_per_hour <= normal.nav_per_hour + 1e-9
        assert only.problems(NORMAL) == []

    def test_group_count_is_bounded(self) -> None:
        with pytest.raises(ValueError, match="between 0 and 25"):
            planned_load(make_settings(), Profile.NORMAL, 26)


class TestRefusal:
    def test_the_design_defaults_fit(self) -> None:
        loads = ensure_fits(make_settings())
        assert {load.profile for load in loads} == set(Profile)

    def test_a_cap_too_low_for_the_feed_is_refused(self) -> None:
        settings = make_settings(cautious={"nav_per_hour": 5})
        with pytest.raises(PlanDoesNotFitError) as info:
            ensure_fits(settings)
        text = str(info.value)
        assert "cautious" in text
        assert "navigation/1h" in text
        assert "no room for backstops" in text

    def test_backstops_less_than_daily_are_refused(self) -> None:
        settings = make_settings(cautious={"nav_per_hour": 7})
        load = planned_load(settings, Profile.CAUTIOUS, 25)
        assert load.backstop_interval_hours is not None
        assert load.backstop_interval_hours > MAX_BACKSTOP_INTERVAL_HOURS
        with pytest.raises(PlanDoesNotFitError, match="over 24 h"):
            ensure_fits(settings)


# -- the design's budget table, regenerated ------------------------------------------------


@functools.cache
def _row(profile: Profile, groups: int) -> tuple[TableRow, dict[str, float]]:
    settings = make_settings()
    load = planned_load(settings, profile, groups)
    sim = simulate(settings, profile, groups, days=12, seed=f"table-{profile.value}-{groups}")
    row = TableRow(
        load=load,
        p95_nav_per_hour=sim.percentile(NAV, 3600),
        p95_nav_per_15min=sim.percentile(NAV, 900),
        p95_nav_per_day=sim.percentile(NAV, 86400),
        p95_scroll_per_hour=sim.percentile(SCROLL, 3600),
        p95_scroll_per_day=sim.percentile(SCROLL, 86400),
    )
    backstops = sum(1 for _, kind in sim.navigations if kind is ActionKind.BACKSTOP)
    measured = {
        "backstops_per_full_hour": backstops / (load.shape.full_hours * 12),
        "nav_per_day": sim.per_day(NAV),
    }
    return row, measured


@pytest.mark.parametrize("groups", [4, 10, 15, 20, 25])
@pytest.mark.parametrize("profile", list(Profile))
def test_budget_table_fits_under_its_caps(profile: Profile, groups: int) -> None:
    """The design's table from the scheduler's own parameters: mean ≤ 75 % of every cap (the
    plan), and the simulated p95 under jitter never above a cap (the cap rarely binds)."""
    knobs = knobs_for(make_settings(), profile)
    row, measured = _row(profile, groups)
    load = row.load
    assert load.problems(knobs) == []
    caps = {(kind, window): cap for kind, window, cap in knobs.windows()}
    assert row.p95_nav_per_15min is not None and row.p95_nav_per_15min <= caps[(NAV, 900)]
    assert row.p95_nav_per_hour is not None and row.p95_nav_per_hour <= caps[(NAV, 3600)]
    assert row.p95_nav_per_day is not None and row.p95_nav_per_day <= caps[(NAV, 86400)]
    assert row.p95_scroll_per_hour is not None and row.p95_scroll_per_hour <= caps[(SCROLL, 3600)]
    assert row.p95_scroll_per_day is not None and row.p95_scroll_per_day <= caps[(SCROLL, 86400)]
    # The smoothing that keeps p95 under the caps must not starve the backstops.
    assert measured["backstops_per_full_hour"] == pytest.approx(load.backstop_per_hour, rel=0.2)
    assert measured["nav_per_day"] == pytest.approx(load.nav_per_day, rel=0.1)


def test_budget_table_renders() -> None:
    rows = [_row(profile, n)[0] for profile in Profile for n in (4, 10, 25)]
    table = format_table(rows)
    assert "| normal | 10 | 1.6 h | 15.0," in table
    assert "| cautious | 4 | 3.0 h | 5.6," in table
    assert "| cautious | 25 | 7.8 h | 7.5," in table
    assert "stretched to fit" not in table  # the design's defaults fit as they are
    assert len(table.splitlines()) == len(rows) + 2
    assert format_table([TableRow(planned_load(make_settings(), Profile.NORMAL, 0))]).endswith(
        "| normal | 0 | — | 8.6, — | 132, — | 172, — |"
    )


DESIGN = Path(__file__).resolve().parents[2] / "DESIGN.md"


def _design_cautious_rows() -> dict[int, list[float]]:
    """The cautious table's rows in DESIGN.md: N -> the numbers of its other columns."""
    text = DESIGN.read_text(encoding="utf-8")
    start = text.index("Cautious profile planned load")
    rows: dict[int, list[float]] = {}
    for line in text[start:].splitlines()[1:]:
        if rows and not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not line.startswith("|") or not cells[0].isdigit():
            continue
        rows[int(cells[0])] = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", " ".join(cells[1:]))]
    return rows


def test_the_design_documents_the_cautious_table_the_planner_makes() -> None:
    """DESIGN.md's cautious table is this planner's (means) and simulation's (p95), rounded as
    it prints them: a change to the pacing has to change the document too."""
    rows = _design_cautious_rows()
    assert sorted(rows) == [4, 10, 15, 20, 25]
    for groups, numbers in rows.items():
        row, _ = _row(Profile.CAUTIOUS, groups)
        load = row.load
        assert load.backstop_interval_hours is not None
        expected = [
            round(load.backstop_interval_hours, 1),
            round(load.nav_per_hour, 1),
            row.p95_nav_per_hour,
            round(load.nav_per_day),
            row.p95_nav_per_day,
            round(load.scroll_per_day),
            row.p95_scroll_per_day,
        ]
        assert numbers == pytest.approx(expected), groups
