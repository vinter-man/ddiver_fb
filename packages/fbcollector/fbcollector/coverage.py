"""How far back a read has looked, and when it may stop (DESIGN, Seen set, coverage and stop rules).

A read scrolls only as far as it must. What "far enough" means:

- **Feed**: its target ``T_feed`` is the start of the last feed read that covered its span (see
  ``targets`` below). It stops once the last batch's *2nd-oldest* regular story is older than
  ``T_feed − 30 min`` **and** three regular stories in a row were already seen before this read
  — the feed has scrolled back into territory an earlier read covered. The run of seen stories
  may be followed by older unseen ones (posts from before any earlier coverage); they do not
  send the read further back.
- **Backstop**: its own target ``B_g`` per group; it stops once the last batch's 2nd-oldest
  regular story is older than ``B_g − 30 min``, whatever the feed saw.

Only *regular* stories count: pinned and highlighted units are out of order by nature, and each
pagination batch opens with one out-of-order anchor story (fbscrape). Using the 2nd-oldest of a
batch rather than the oldest is the same caution against one stray old story ending a read early.

A read that runs out of steps short of its target has a **gap**: the interval it could not
reach. That is so when its last batch still holds unseen stories newer than the target, and also
when its coverage never got below the target (the last batch was all seen by a recent backstop,
or undated) — unless the list itself ended (pagination did not fire), which is not a gap. The
groups of the unseen stories newer than the target get gap backstops (at most three, busiest
first — the scheduler's rule); a backstop's gap is its own group's.

Targets come in two kinds. ``B_g`` and the target of the deep feed reads (the day's catch-up and
every ~4th read) are the start of the last read that *reached* its target (DESIGN): a read that
ran out of steps leaves the target where it was, so the next one scrolls on toward it and the
uncovered interval is not dropped. Ordinary feed reads (0-1 steps) move on after a gap read too
(``feed_target``), or they would chase the same unreachable interval forever; their gap goes to
the gap backstops and to the next deep read.

Watermarks are clamped to ``now + 5 min``: a story stamped in the future must not become a
target that no later story can reach.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Final

from fbcollector.model import Coverage, ParsedStory, Surface

MARGIN: Final = 30 * 60.0
WATERMARK_SLACK: Final = 5 * 60.0
SEEN_STREAK: Final = 3
BOOTSTRAP_LOOKBACK: Final = 2 * 3600.0
"""Before any read covered its span, the target is at most this far back (see ``targets``)."""


def second_oldest(times: Sequence[int]) -> int | None:
    """The 2nd-oldest of a batch's creation times (the only one if there is just one)."""
    if not times:
        return None
    ordered = sorted(times)
    return ordered[1] if len(ordered) > 1 else ordered[0]


@dataclass
class CoverageTracker:
    """Feeds the stories of each step in; answers "reached?", "gap?" and the coverage."""

    surface: Surface
    target: float
    now: float
    bootstrap: bool = False
    """No read has covered its span yet: the seen-streak half of the feed rule cannot hold."""
    _batches: dict[int, list[int]] = field(default_factory=dict)
    _unseen: dict[int, list[tuple[int, str | None]]] = field(default_factory=dict)
    _streak: int = 0
    _best_streak: int = 0
    _newest: int | None = None
    regular: int = 0
    """Regular stories seen, dated or not."""

    def add(self, stories: Iterable[ParsedStory], seen_before: AbstractSet[str]) -> None:
        """``seen_before``: post ids that were in the seen set before this read began."""
        for story in sorted(stories, key=lambda s: s.position):
            if not story.regular:
                continue
            self.regular += 1
            seen = story.post_id in seen_before
            self._streak = self._streak + 1 if seen else 0
            self._best_streak = max(self._best_streak, self._streak)
            if story.created_at is None:
                continue
            created = min(story.created_at, int(self.now + WATERMARK_SLACK))
            self._batches.setdefault(story.batch, []).append(created)
            self._newest = created if self._newest is None else max(self._newest, created)
            if not seen:
                self._unseen.setdefault(story.batch, []).append((created, story.group_id))

    @property
    def last_batch(self) -> int | None:
        return max(self._batches) if self._batches else None

    @property
    def oldest(self) -> int | None:
        batch = self.last_batch
        return None if batch is None else second_oldest(self._batches[batch])

    @property
    def streak(self) -> int:
        """The longest run of already-seen regular stories so far (position order)."""
        return self._best_streak

    @property
    def reached(self) -> bool:
        oldest = self.oldest
        if oldest is None or oldest >= self.target - MARGIN:
            return False
        if self.surface is Surface.FEED and not self.bootstrap:
            return self._best_streak >= SEEN_STREAK
        return True

    def gap_groups(self) -> list[str]:
        """Groups of unseen regular stories newer than the target: those of the last batch
        (the nearest to the uncovered interval), else those of the whole read."""
        batch = self.last_batch
        if batch is None:
            return []
        found = [g for t, g in self._unseen.get(batch, []) if g is not None and t >= self.target]
        if not found:
            found = [
                g
                for unseen in self._unseen.values()
                for t, g in unseen
                if g is not None and t >= self.target
            ]
        return list(dict.fromkeys(found))

    def short_of_target(self) -> bool:
        """The read's coverage never got below its target (a capped read: a gap)."""
        oldest = self.oldest
        if oldest is None:
            return self.regular > 0  # undated stories: how far it got is unknown
        return oldest >= self.target

    @property
    def gap(self) -> bool:
        batch = self.last_batch
        return batch is not None and any(t >= self.target for t, _ in self._unseen.get(batch, []))

    def coverage(self) -> Coverage:
        return Coverage(
            newest=self._newest,
            oldest=self.oldest,
            target=self.target,
            target_reached=self.reached,
        )


def feed_target(
    *, last_covered_start: float | None, baselines: Sequence[float], now: float
) -> tuple[float, bool]:
    """``(T_feed, bootstrap)``.

    For an ordinary read ``last_covered_start`` is the start of the latest feed read that
    finished its stepping — reached its target *or* recorded a gap. DESIGN counts only reads
    that reached their target; a gap read hands its uncovered interval to the gap backstops, and
    if it did not move the target on, every later ordinary read (0-1 steps) would chase the same
    unreachable interval and record the same gap forever. A deep read (the catch-up, every ~4th
    read) passes the start of the latest read that *reached* its target instead: it exists to
    cover what the ordinary reads could not. Before any such read: the earliest monitored
    baseline, but no further back than two hours (``bootstrap``: nothing has been seen to stream
    into).
    """
    if last_covered_start is not None:
        return last_covered_start, False
    earliest = min(baselines, default=now)
    return max(earliest, now - BOOTSTRAP_LOOKBACK), True


def backstop_target(
    *, last_covered_start: float | None, baseline: float, now: float, novelty: float
) -> float:
    """``B_g``: the start of the group's last backstop that *reached* its target, else its
    baseline — never older than the baseline (a re-monitored group's older posts can never
    become items) or the novelty window. A capped backstop leaves ``B_g`` where it was, so the
    part of the group it could not reach is scrolled toward again, never skipped."""
    start = last_covered_start if last_covered_start is not None else baseline
    return max(start, baseline, now - novelty)
