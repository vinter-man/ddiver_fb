"""How long a post can take to reach a user, and when the collector's "day" is.

The brain does not pace anything — the collector does, at home, under its own hard caps — but it
has to tell users honestly how late a notification can be ("up to X h", DESIGN "Scheduler").
The numbers below are the collector's design defaults (DESIGN.md, the cadence table; the
collector's ``config.py`` profiles), so the brain can answer before the collector ever reported.
A collector that computes its own effective intervals (because the budget is short and it
stretched them) can send them in ``Status.counters`` under :data:`FEED_COUNTER` and
:data:`BACKSTOP_COUNTER`, and those win.

The answer is an upper bound, deliberately: the feed's interval top plus a minute of read time,
and the backstop's interval with its +30 % jitter. A user told "up to 10 min" who gets a post
in 4 is pleased; one told "about 4" who waits 10 files a bug.
"""

from __future__ import annotations

import datetime as dt
import math
import zoneinfo
from collections.abc import Mapping
from typing import Final

from fbwire import Profile

FEED_MAX_MINUTES: Final[Mapping[Profile, float]] = {
    Profile.CAUTIOUS: 18.0,
    Profile.NORMAL: 9.5,
}
"""Top of the feed interval: U(10, 18) min cautious (the owner's decision of 2026-09-25),
U(4.5, 9.5) min normal."""

BACKSTOP: Final[Mapping[Profile, tuple[float, float]]] = {
    Profile.CAUTIOUS: (3.0, 3.2),
    Profile.NORMAL: (1.5, 6.4),
}
"""``(floor hours, groups per hour)``: the backstop interval is ``max(floor, N / rate)`` h.

The cautious profile has no rate of its own since 2026-09-25: its backstops take the room the
caps leave at 75 % (``fbcollector.budget.planned_load``), which with the collector's defaults
is 7.5 - 60/14 = 3.21 groups/h (N = 4: 3 h, N = 25: 7.8 h). 3.2 keeps the figure users are
shown an upper bound. Normal is the design's and the planner's alike. A collector that sends
:data:`BACKSTOP_COUNTER` is believed instead."""

SHOULDER_FEED_MAX_MINUTES: Final = 25.0
"""Top of the feed interval in the hour after quiet hours and the 1.5 h before: U(15, 25) min."""

JITTER: Final = 0.30
READ_MINUTES: Final = 1.0
"""A read takes about this long between its start and the post reaching the brain."""

FEED_COUNTER: Final = "feed_interval_max_seconds"
BACKSTOP_COUNTER: Final = "backstop_interval_seconds"


def feed_delay(profile: Profile, counters: Mapping[str, int] | None = None) -> dt.timedelta:
    """Longest wait for a post the Groups feed shows."""
    override = (counters or {}).get(FEED_COUNTER)
    if override:
        return dt.timedelta(seconds=override) + dt.timedelta(minutes=READ_MINUTES)
    return dt.timedelta(minutes=FEED_MAX_MINUTES[profile] + READ_MINUTES)


def shoulder_delay() -> dt.timedelta:
    """Longest wait for a feed post around quiet hours, when the feed is read more slowly."""
    return dt.timedelta(minutes=SHOULDER_FEED_MAX_MINUTES + READ_MINUTES)


def backstop_delay(
    profile: Profile, monitored: int, counters: Mapping[str, int] | None = None
) -> dt.timedelta:
    """Longest wait for a post only the per-group backstop finds (``monitored`` groups)."""
    override = (counters or {}).get(BACKSTOP_COUNTER)
    if override:
        hours = override / 3600
    else:
        floor, rate = BACKSTOP[profile]
        hours = max(floor, max(monitored, 1) / rate)
    return dt.timedelta(hours=hours * (1 + JITTER)) + dt.timedelta(minutes=READ_MINUTES)


def _zone(tz: str) -> zoneinfo.ZoneInfo:
    """``tz``, or UTC for a zone this machine does not know."""
    try:
        return zoneinfo.ZoneInfo(tz)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return zoneinfo.ZoneInfo("UTC")


def is_daytime(moment: dt.datetime, tz: str, start_hour: int, end_hour: int) -> bool:
    """Whether ``moment`` falls in ``[start_hour, end_hour)`` local time in ``tz``."""
    local = moment.astimezone(_zone(tz))
    return start_hour <= local.hour < end_hour


def daytime_seconds(start: float, end: float, tz: str, start_hour: int, end_hour: int) -> float:
    """How much of ``[start, end)`` (epoch seconds) falls in the day of :func:`is_daytime`.

    The day's edges are wall-clock hours in ``tz``, so on the day of a clock change it is still
    ``start_hour`` to ``end_hour`` there, one hour longer or shorter.
    """
    if end <= start or end_hour <= start_hour:
        return 0.0
    zone = _zone(tz)
    day = dt.datetime.fromtimestamp(start, zone).date()
    last = dt.datetime.fromtimestamp(end, zone).date()
    total = 0.0
    while day <= last:
        midnight = dt.datetime.combine(day, dt.time(0), zone)
        # Same-zone arithmetic is wall-clock arithmetic: 08:00 is 08:00 on a clock-change day.
        opens = (midnight + dt.timedelta(hours=start_hour)).timestamp()
        closes = (midnight + dt.timedelta(hours=end_hour)).timestamp()
        total += max(0.0, min(end, closes) - max(start, opens))
        day += dt.timedelta(days=1)
    return total


def p90(values: list[float]) -> float:
    """Nearest-rank 90th percentile of a non-empty list."""
    ordered = sorted(values)
    rank = max(1, math.ceil(0.9 * len(ordered)))
    return ordered[rank - 1]
