"""``fbcollector replay --since``: re-parse saved payloads after a parser fix.

Every step's payload is kept (48 h, longer in observe mode) for exactly this. Once the parser is
fixed, replay reads the payloads again with the same parser and the same item rules as a live
read, and queues the stories that now parse and are still inside the novelty window:

- stories of a step the detectors judged clean (OK, or a flag such as not followed), as the
  read's ``stats.verdicts`` recorded them step by step. The usual Facebook rename leaves no
  trace but a count: a story the old parser did not find, or dropped for want of a post id, has
  no deferred row and no quarantine file, and it is exactly what the fix should recover. Steps
  of an unrecognised, blocked or logged-out page are never replayed (nothing from them was ever
  stored), nor any step of a resume probe that failed, nor a read whose row is gone;
- stories that were *deferred* (no text, no time, no group), whatever their step;
- stories whose reading raised and were quarantined with their raw JSON;
- posts whose item did not fit the wire model (the ``quarantine`` table), from the step payload
  their row names — also one older than ``--since``, while it is kept. One that still does not
  fit stays quarantined, one attempt more.

Stories already in the seen set are left alone: replay never re-sends a post.

It needs no browser and sends nothing to Facebook.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from fbcollector.model import (
    DomFacts,
    GraphqlBody,
    PageState,
    ParsedStory,
    Sighting,
    Step,
    Surface,
)
from fbcollector.parser import ReadParser
from fbcollector.raw import RawStep, iter_quarantined, iter_steps, load_step, step_name
from fbcollector.reader import ReaderConfig, build_item
from fbcollector.store import INVALID, Store
from fbwire.models import Via

log = logging.getLogger(__name__)

STORED_STATES: Final = frozenset(
    state.value
    for state in (
        PageState.OK,
        PageState.FEED_NOT_CHRONO,
        PageState.NOT_FOLLOWED,
        PageState.SORT_IGNORED,
    )
)
"""Step verdicts whose stories a live read stores (DESIGN A4: nothing from a warning page)."""


@dataclass
class ReplayResult:
    steps: int = 0
    stories: int = 0
    queued: int = 0
    still_deferred: int = 0
    still_invalid: int = 0
    """Quarantined posts whose item still does not fit the wire model."""
    skipped: int = 0


def _via(surface: Surface, listings: bool) -> Via:
    if surface is Surface.GROUP:
        return Via.BACKSTOP_LISTINGS if listings else Via.BACKSTOP
    return Via.FEED


def _wrap(node: bytes) -> Step:
    """A quarantined story as a one-edge feed body, so the ordinary parser reads it."""
    frame = {"data": {"viewer": {"news_feed": {"edges": [{"node": json.loads(node)}]}}}}
    body = GraphqlBody(friendly_name="replay", body=json.dumps(frame).encode())
    return Step(final_url="replay", status=None, graphql=(body,), dom=DomFacts())


async def replay(
    store: Store, raw_dir: Path, *, since: float, config: ReaderConfig, now: float
) -> ReplayResult:
    result = ReplayResult()
    reads: dict[int, list[RawStep]] = defaultdict(list)
    read_files: set[tuple[int, int]] = set()
    for saved in iter_steps(raw_dir, since=since):
        reads[saved.read_id].append(saved)
        read_files.add((saved.read_id, saved.index))
    for read_id, steps in reads.items():
        first = steps[0]
        clean = await _clean_steps(store, read_id)
        parser = ReadParser(first.surface, group_id=first.group_id)
        for saved in sorted(steps, key=lambda s: s.index):
            result.steps += 1
            stories = parser.parse(saved.step).stories
            await _consider(
                store,
                stories,
                first,
                config=config,
                now=now,
                result=result,
                clean=saved.index in clean,
                raw_path=str(raw_dir / step_name(saved.read_id, saved.index, saved.at)),
            )
    for quarantined in iter_quarantined(raw_dir, since=since):
        parser = ReadParser(quarantined.surface, group_id=quarantined.group_id)
        try:
            step = _wrap(quarantined.node)
        except ValueError:
            continue
        origin = RawStep(
            quarantined.read_id, 0, quarantined.surface, quarantined.group_id, 0.0, step
        )
        stories = parser.parse(step).stories
        await _consider(
            store, stories, origin, config=config, now=now, result=result, quarantined=True
        )
    await _retry_quarantine(store, raw_dir, read_files, config=config, now=now, result=result)
    return result


async def _retry_quarantine(
    store: Store,
    raw_dir: Path,
    done: set[tuple[int, int]],
    *,
    config: ReaderConfig,
    now: float,
    result: ReplayResult,
) -> None:
    """Quarantined posts whose step payload the pass above did not read (older than
    ``--since``): their own step, while it is kept, and only their own story from it."""
    waiting: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for row in await store.list_quarantined():
        if row.raw_path:
            waiting[Path(row.raw_path).name].add(row.key)
    for name, keys in sorted(waiting.items()):
        try:
            saved = load_step(raw_dir / name)  # by name: the row's path may be another root's
        except (OSError, ValueError, KeyError, EOFError):
            continue
        if (saved.read_id, saved.index) in done:
            continue
        result.steps += 1
        parser = ReadParser(saved.surface, group_id=saved.group_id)
        stories = [s for s in parser.parse(saved.step).stories if (s.group_id, s.post_id) in keys]
        path = str(raw_dir / name)
        await _consider(store, stories, saved, config=config, now=now, result=result, raw_path=path)


async def _clean_steps(store: Store, read_id: int) -> frozenset[int]:
    """Indexes of the read's steps whose stories a live read would have stored."""
    record = await store.get_read(read_id)
    if record is None:
        return frozenset()
    verdicts = [str(v) for v in record.stats.get("verdicts") or ()]
    clean = {i for i, v in enumerate(verdicts) if v in STORED_STATES}
    if record.stats.get("kind") == "probe" and len(clean) != len(verdicts):
        return frozenset()  # a probe stores nothing unless its final look was clean
    return frozenset(clean)


async def _consider(
    store: Store,
    stories: Iterable[ParsedStory],
    origin: RawStep,
    *,
    config: ReaderConfig,
    now: float,
    result: ReplayResult,
    quarantined: bool = False,
    clean: bool = False,
    raw_path: str | None = None,
) -> None:
    """Queue what now parses: stories of clean steps, deferred ones, quarantined ones (their
    reading raised, or their item did not fit the wire model)."""
    for story in stories:
        result.stories += 1
        waiting = await store.get_deferred(story.post_id)
        if story.group_id is None or story.deferred_reason is not None:
            result.still_deferred += waiting is not None
            continue
        invalid = await store.get_quarantined(story.group_id, story.post_id)
        pending = waiting is not None or invalid is not None or quarantined or clean
        if not pending or await store.is_seen(story.group_id, story.post_id):
            result.skipped += 1
            continue
        group = await store.get_group(story.group_id)
        via = _via(origin.surface, bool(group and group.listings))
        built = build_item(config, story, group, via=via, now=now)
        if built.outcome == "not_member":
            result.skipped += 1  # as a live read: kept for when the group is a member again
            continue
        sighting = Sighting.of(story, origin.surface, origin.read_id)
        if built.outcome == INVALID:
            await store.quarantine(sighting, errors=built.errors, raw_path=raw_path)
            result.still_invalid += 1
            continue
        marked = await store.mark_seen(sighting, via=via, item=built.item, outcome=built.outcome)
        if marked.seq is not None:
            result.queued += 1
            log.info("replay queued post %s/%s", story.group_id, story.post_id)
