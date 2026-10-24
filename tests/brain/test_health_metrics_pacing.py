"""Health (bot + ingest, nothing about the collector), metrics names, and the delay bounds."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from ddiver_fb.health import BrainBeat, check, read_beat, write_beat
from ddiver_fb.metrics import FbMetricsSource
from ddiver_fb.pacing import (
    BACKSTOP_COUNTER,
    FEED_COUNTER,
    READ_MINUTES,
    backstop_delay,
    daytime_seconds,
    feed_delay,
    is_daytime,
    p90,
    shoulder_delay,
)
from ddiver_fb.source import SourceMetrics
from fbcollector.budget import BACKSTOP_JITTER, knobs_for, planned_load
from fbwire import (
    MAX_MONITORED,
    BudgetKind,
    BudgetWindow,
    CollectorState,
    ItemOutcome,
    Profile,
)
from tests.brain.helpers import T0, status
from tests.collector.pacing_support import make_settings


def beat(**kw) -> BrainBeat:
    values: dict[str, Any] = {"ts": T0.isoformat(), "bot_connected": True, "ingest_up": True}
    values.update(kw)
    return BrainBeat(**values)


class TestHealth:
    def test_healthy(self, tmp_path: Path):
        path = tmp_path / "health.json"
        write_beat(path, beat(bot_ping_ok_at=T0.isoformat(), queue=3))
        report = check(path, now=T0 + dt.timedelta(seconds=30))
        assert report.healthy and report.reason == "ok (30s)"
        assert read_beat(path) == beat(bot_ping_ok_at=T0.isoformat(), queue=3)

    @pytest.mark.parametrize(
        ("the_beat", "age", "reason"),
        [
            (beat(), 121, "heartbeat is 121s old"),
            (beat(), -5, "heartbeat is -5s old"),
            (beat(ingest_up=False), 0, "the ingest server is not serving"),
            (beat(bot_connected=False), 0, "the bot is not connected to Telegram"),
            (
                beat(bot_ping_ok_at=(T0 - dt.timedelta(minutes=10)).isoformat()),
                0,
                "the bot has not answered a ping for a while",
            ),
        ],
    )
    def test_unhealthy(self, tmp_path: Path, the_beat: BrainBeat, age: int, reason: str):
        path = tmp_path / "health.json"
        write_beat(path, the_beat)
        report = check(path, now=T0 + dt.timedelta(seconds=age))
        assert not report.healthy and report.reason == reason

    @pytest.mark.parametrize(
        "content", [None, "not json", "[]", '{"ts": 5}', '{"ts": "yesterday"}']
    )
    def test_no_usable_file(self, tmp_path: Path, content: str | None):
        path = tmp_path / "health.json"
        if content is not None:
            path.write_text(content)
        assert check(path, now=T0).reason == "no heartbeat file"

    def test_a_naive_timestamp_is_utc_and_odd_fields_are_defaults(self, tmp_path: Path):
        path = tmp_path / "health.json"
        path.write_text(
            json.dumps({"ts": "2026-09-22T12:00:00", "bot_connected": "yes", "queue": "many"})
        )
        loaded = read_beat(path)
        assert loaded is not None and not loaded.bot_connected and loaded.queue == 0
        assert check(path, now=T0).reason == "the ingest server is not serving"

    def test_the_collector_is_not_part_of_it(self):
        assert set(BrainBeat.__dataclass_fields__) == {
            "ts",
            "bot_connected",
            "ingest_up",
            "bot_ping_ok_at",
            "queue",
        }


class TestMetrics:
    def source(self, **kw) -> FbMetricsSource:
        items = SourceMetrics()
        items.count(ItemOutcome.STORED, 7)
        values: dict[str, Any] = {
            "items": items,
            "status_age": lambda: 42.0,
            "group_ages": lambda: {2: 60.0, 1: 30.5},
            "bot_connected": lambda: True,
            "ingest_up": lambda: False,
        }
        values.update(kw)
        return FbMetricsSource(**values)

    def test_the_design_names_and_one_help_line_per_family(self):
        text = self.source().render()
        assert 'fbb_ingest_items_total{result="stored"} 7' in text
        assert 'fbb_ingest_items_total{result="rejected"} 0' in text
        assert "fbb_collector_status_age_seconds 42" in text
        assert 'fbb_group_last_item_age_seconds{group="1"} 30.5' in text
        assert "fbb_bot_connected 1" in text and "fbb_ingest_up 0" in text
        assert text.count("# HELP fbb_ingest_items_total") == 1
        assert text.count("# TYPE fbb_group_last_item_age_seconds") == 1
        assert text.index('group="1"') < text.index('group="2"')

    def test_no_listener_series_for_an_app_without_a_listener(self):
        text = self.source().render()
        assert "ddiver_listener_connected" not in text and "ddiver_up 1" in text

    def test_the_holders_last_report_is_re_exported_as_fbc_series(self):
        """Round 2 F11: DESIGN's collector series existed nowhere."""
        report = status(
            CollectorState.QUIET,
            gates={"home": True, "rhythm": False},
            budget=(BudgetWindow(kind=BudgetKind.NAVIGATION, window_seconds=3600, cap=10, used=3),),
            counters={"outbox_depth": 5, "outbox_dropped": 2, "uplink_refused_items": 4},
        )
        text = self.source(collector=lambda: (CollectorState.OFFLINE, report)).render()
        assert 'fbc_state{state="offline"} 1' in text and 'fbc_state{state="quiet"} 0' in text
        assert 'fbc_gate_closed{gate="rhythm"} 1' in text
        assert 'fbc_gate_closed{gate="home"} 0' in text
        assert 'fbc_budget_used{kind="navigation",window="3600"} 3' in text
        assert 'fbc_budget_remaining{kind="navigation",window="3600"} 7' in text
        assert "fbc_outbox_depth 5" in text and "fbc_outbox_dropped_total 2" in text
        assert 'fbc_uplink_refused_total{endpoint="items"} 4' in text
        assert text.count("# HELP fbc_state") == 1
        assert "fbc_" not in self.source().render()  # nothing reported yet: no series

    def test_no_status_yet_means_no_age_series(self):
        assert (
            "fbb_collector_status_age_seconds" not in self.source(status_age=lambda: None).render()
        )


class TestPacing:
    def test_feed_bounds(self):
        assert feed_delay(Profile.NORMAL) == dt.timedelta(minutes=10.5)
        assert feed_delay(Profile.CAUTIOUS) == dt.timedelta(minutes=19)
        assert feed_delay(Profile.NORMAL, {FEED_COUNTER: 600}) == dt.timedelta(minutes=11)

    @pytest.mark.parametrize(
        ("profile", "groups", "hours"),
        [
            (Profile.NORMAL, 1, 1.5),
            (Profile.NORMAL, 15, 15 / 6.4),
            (Profile.NORMAL, 25, 25 / 6.4),
            (Profile.CAUTIOUS, 5, 3.0),
            (Profile.CAUTIOUS, 4, 3.0),
            (Profile.CAUTIOUS, 25, 25 / 3.2),
            (Profile.NORMAL, 0, 1.5),
        ],
    )
    def test_backstop_bounds_follow_the_collectors_plan(self, profile, groups, hours):
        expected = dt.timedelta(hours=hours * 1.3, minutes=1)
        assert backstop_delay(profile, groups) == expected

    @pytest.mark.parametrize("profile", list(Profile))
    def test_the_bound_shown_is_never_below_the_collectors_own_interval(self, profile):
        """C1-07: the cautious figure was 36 % short for N >= 10.

        Asked of the collector's own planner with its default settings, for every monitored
        count it accepts, so a change to its pacing cannot quietly make the brain's promise false.
        """
        settings = make_settings()
        for groups in range(1, MAX_MONITORED + 1):
            planned = planned_load(settings, profile, groups).backstop_interval_hours
            assert planned is not None, (profile, groups)
            longest = dt.timedelta(hours=planned * (1 + BACKSTOP_JITTER))
            shown = backstop_delay(profile, groups) - dt.timedelta(minutes=READ_MINUTES)
            assert shown >= longest, (profile, groups)

    @pytest.mark.parametrize("profile", list(Profile))
    def test_the_feed_bounds_cover_the_collectors_cadence(self, profile):
        settings = make_settings()
        top = knobs_for(settings, profile).feed_max_minutes
        assert feed_delay(profile) >= dt.timedelta(minutes=top + READ_MINUTES)
        shoulder = settings.shoulder_feed_max_minutes
        assert shoulder_delay() >= dt.timedelta(minutes=shoulder + READ_MINUTES)

    def test_the_shoulder_bound(self):
        assert shoulder_delay() == dt.timedelta(minutes=26)

    def test_a_collector_that_stretched_its_backstops_says_so(self):
        got = backstop_delay(Profile.NORMAL, 5, {BACKSTOP_COUNTER: 7200})
        assert got == dt.timedelta(hours=2.6, minutes=1)

    def test_daytime(self):
        noon_kyiv = dt.datetime(2026, 9, 22, 9, 0, tzinfo=dt.UTC)
        assert is_daytime(noon_kyiv, "Europe/Kyiv", 8, 23)
        assert not is_daytime(noon_kyiv, "America/New_York", 8, 23)  # 05:00 there
        assert is_daytime(noon_kyiv, "Not/AZone", 8, 23)  # an unknown zone reads as UTC

    def test_daytime_seconds(self):
        def at(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
            return dt.datetime(year, month, day, hour, minute, tzinfo=dt.UTC).timestamp()

        kyiv = "Europe/Kyiv"  # UTC+3 until 2026-10-25 04:00 local, UTC+2 after
        # 2026-10-11 06:59 (09:59 there): the rideshare group's 13.6 h since 20:26 the evening
        # before are 2.6 h of the evening and 2 h of the morning.
        since = at(2026, 10, 10, 17, 26)
        assert daytime_seconds(since, at(2026, 10, 11, 6, 59), kyiv, 8, 23) == 4.55 * 3600
        assert daytime_seconds(at(2026, 10, 11, 23), at(2026, 10, 12, 1), kyiv, 8, 23) == 0
        assert daytime_seconds(at(2026, 10, 11, 9), at(2026, 10, 11, 9), kyiv, 8, 23) == 0
        assert daytime_seconds(at(2026, 10, 11, 9), at(2026, 10, 11, 8), kyiv, 8, 23) == 0
        three_days = daytime_seconds(at(2026, 10, 11, 12), at(2026, 10, 14, 12), kyiv, 8, 23)
        assert three_days == 3 * 15 * 3600
        # The clock change: 08:00-23:00 there is still 15 h, whatever UTC says.
        change = daytime_seconds(at(2026, 10, 24, 21), at(2026, 10, 25, 21, 30), kyiv, 8, 23)
        assert change == 15 * 3600
        assert daytime_seconds(at(2026, 10, 25, 21), at(2026, 10, 25, 22), kyiv, 8, 23) == 0
        assert daytime_seconds(at(2026, 10, 11, 0), at(2026, 10, 12, 0), kyiv, 0, 24) == 86400
        assert daytime_seconds(at(2026, 10, 11, 0), at(2026, 10, 12, 0), kyiv, 9, 9) == 0
        noon = at(2026, 10, 11, 12)
        assert daytime_seconds(noon, noon + 60, "Not/AZone", 8, 23) == 60  # read as UTC

    def test_p90(self):
        assert p90([5.0]) == 5.0
        assert p90([float(n) for n in range(1, 11)]) == 9.0
        assert p90([3.0, 1.0, 2.0]) == 3.0
