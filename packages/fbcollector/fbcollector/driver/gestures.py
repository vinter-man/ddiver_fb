"""Human-sized input: one mouse path per read, and wheel gestures for scroll steps.

Only two kinds of input exist (invariant 1): ``mouse.move`` to a point that reacts to nothing,
and mouse-wheel events. Both are planned here as plain numbers from an injected ``Random``, so
their shape is tested without a browser and the local test page checks what Chrome delivered.

- **The mouse** goes once per read, along a slightly bent multi-point path, to a random point in
  the empty margin between the feed column and the right rail (or the window edge), at a random
  height in the middle of the window. The driver hit-tests the point before moving there and
  skips the move rather than hover a link or an avatar.
- **A scroll step** is 3–8 wheel events of 100–120 px, 16–60 ms apart, so step lengths vary;
  about one step in eight ends with a small scroll back up, as a reader who went too far does.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Final

from fbcollector.driver.cdp import Box, Layout

WHEEL_EVENTS: Final = (3, 8)
WHEEL_DELTA: Final = (100.0, 120.0)
WHEEL_PAUSE_MS: Final = (16.0, 60.0)
BACK_UP_SHARE: Final = 0.125
BACK_UP_EVENTS: Final = (1, 2)
PATH_POINTS: Final = (3, 6)
MARGIN_MIN_WIDTH: Final = 24.0
MARGIN_INSET: Final = 8.0
DEFAULT_MARGIN: Final = 48.0
"""Without a recognisable layout: this far from the window's right edge (inside the scrollbar
gutter of the document, never over content)."""


@dataclass(frozen=True, slots=True)
class WheelEvent:
    delta_y: float
    pause_ms: float
    """Wait after this event."""


def wheel_gesture(rng: random.Random) -> list[WheelEvent]:
    count = rng.randint(*WHEEL_EVENTS)
    events = [
        WheelEvent(round(rng.uniform(*WHEEL_DELTA), 1), round(rng.uniform(*WHEEL_PAUSE_MS), 1))
        for _ in range(count)
    ]
    if rng.random() < BACK_UP_SHARE:
        events.extend(
            WheelEvent(-round(rng.uniform(*WHEEL_DELTA), 1), round(rng.uniform(*WHEEL_PAUSE_MS), 1))
            for _ in range(rng.randint(*BACK_UP_EVENTS))
        )
    return events


def margin_span(layout: Layout) -> tuple[float, float] | None:
    """The horizontal gap right of the feed column, inset from both sides; ``None`` if too thin."""
    if layout.width <= 0 or layout.height <= 0:
        return None
    feed: Box | None = layout.feed
    if feed is None:
        if layout.width < 2 * DEFAULT_MARGIN:
            return None
        return layout.width - DEFAULT_MARGIN, layout.width - MARGIN_INSET
    left = feed.right + MARGIN_INSET
    edge = layout.rail.left if layout.rail is not None and layout.rail.left > feed.right else None
    # Clamped to the window before the width check: in a window narrower than the feed column
    # the span would otherwise come out inverted, and points drawn from it land on the feed.
    right = min((edge if edge is not None else layout.width), layout.width) - MARGIN_INSET
    if right - left < MARGIN_MIN_WIDTH - 2 * MARGIN_INSET:
        return None
    return left, right


def margin_candidates(
    rng: random.Random, layout: Layout, count: int = 4
) -> list[tuple[float, float]]:
    span = margin_span(layout)
    if span is None:
        return []
    top, bottom = layout.height * 0.3, layout.height * 0.7
    return [
        (round(rng.uniform(*span), 1), round(rng.uniform(top, bottom), 1)) for _ in range(count)
    ]


def mouse_path(
    rng: random.Random,
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    x_bounds: tuple[float, float] | None = None,
) -> list[tuple[float, float]]:
    """Waypoints from ``start`` to ``end`` (inclusive of ``end``) along a gentle bend.

    ``x_bounds`` keeps every waypoint inside the margin column, so the bend never carries the
    pointer over the feed or the rail on its way.
    """
    points = rng.randint(*PATH_POINTS)
    (x0, y0), (x1, y1) = start, end
    length = math.hypot(x1 - x0, y1 - y0)
    bend = rng.uniform(-0.12, 0.12) * length
    nx, ny = (-(y1 - y0) / length, (x1 - x0) / length) if length else (0.0, 0.0)
    path = []
    for i in range(1, points + 1):
        t = i / points
        ease = t * t * (3 - 2 * t)  # slow start and end, as a hand moves
        wobble = math.sin(math.pi * t) * bend
        x = x0 + (x1 - x0) * ease + nx * wobble
        if x_bounds is not None:
            x = min(max(x, x_bounds[0]), x_bounds[1])
        path.append((round(x, 1), round(y0 + (y1 - y0) * ease + ny * wobble, 1)))
    path[-1] = end
    return path
