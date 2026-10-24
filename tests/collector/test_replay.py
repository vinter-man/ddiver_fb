"""``fbcollector replay``: what a parser fix recovers from the kept payloads, and what it must not.

The usual Facebook rename leaves no deferred row and no quarantine file behind — the old parser
simply did not find the story. Replay has to recover those from the steps a live read judged
clean, and never from a warning page or a failed probe.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from fbcollector.clock import FakeClock
from fbcollector.detectors import FEED_URL
from fbcollector.model import Coverage, GroupMeta, GroupState, ReadOutcome, Sighting, Surface
from fbcollector.raw import save_step, step_name
from fbcollector.reader import ReaderConfig
from fbcollector.replay import replay
from fbcollector.store import Store
from fbwire.models import Item

from .reader_support import G1, G2, T0, feed_body, feed_document, nav, post, seed, story


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    clock = FakeClock(start=T0 - 7200)
    async with Store(tmp_path / "state.sqlite", clock=clock) as opened:
        await seed(opened, clock, G1, G2)
        clock.advance(7200)
        yield opened


def config(tmp_path: Path) -> ReaderConfig:
    return ReaderConfig("home-1", tmp_path / "raw", tmp_path / "shots")


async def read_with(
    store: Store, raw: Path, verdicts: list[str], *nodes: dict[str, Any], kind: str = "feed"
) -> int:
    """A finished read whose steps were judged ``verdicts``; step 0 holds ``nodes``."""
    read_id = await store.start_read(Surface.FEED)
    for index in range(len(verdicts)):
        step = (
            nav(FEED_URL, feed_document(), feed_body(*nodes)) if index == 0 else nav(FEED_URL, None)
        )
        save_step(
            raw, read_id=read_id, index=index, surface=Surface.FEED, group_id=None, at=T0, step=step
        )
    await store.finish_read(
        read_id,
        coverage=Coverage(),
        steps=0,
        outcome=ReadOutcome.COMPLETE,
        stats={"verdicts": verdicts, "kind": kind},
    )
    return read_id


async def queued(store: Store) -> list[str]:
    return [Item.model_validate_json(r.payload).post_id for r in await store.next_batch("home-1")]


async def test_a_story_the_old_parser_missed_is_recovered_from_a_clean_step(
    store: Store, tmp_path: Path
) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    await read_with(store, raw, ["ok", "ok"], story(post(1), G1, T0 - 60))
    result = await replay(store, raw, since=T0 - 3600, config=config(tmp_path), now=T0 + 60)
    assert result.queued == 1
    assert await queued(store) == [post(1)]
    again = await replay(store, raw, since=T0 - 3600, config=config(tmp_path), now=T0 + 60)
    assert again.queued == 0  # never re-sends


@pytest.mark.parametrize(
    ("verdicts", "kind"),
    [
        (["unknown"], "feed"),  # an unrecognised page: nothing from it was ever stored
        (["blocked"], "feed"),
        (["ok", "blocked"], "probe"),  # a probe whose final look failed stored nothing
    ],
)
async def test_nothing_is_replayed_from_a_warning_page_or_a_failed_probe(
    store: Store, tmp_path: Path, verdicts: list[str], kind: str
) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    await read_with(store, raw, verdicts, story(post(2), G1, T0 - 60), kind=kind)
    result = await replay(store, raw, since=T0 - 3600, config=config(tmp_path), now=T0 + 60)
    assert result.queued == 0
    assert not await store.is_seen(G1, post(2))


async def test_a_step_without_its_read_row_is_not_replayed(store: Store, tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    step = nav(FEED_URL, feed_document(), feed_body(story(post(3), G1, T0 - 60)))
    save_step(raw, read_id=99, index=0, surface=Surface.FEED, group_id=None, at=T0, step=step)
    result = await replay(store, raw, since=T0 - 3600, config=config(tmp_path), now=T0 + 60)
    assert result.queued == 0


async def test_a_lost_groups_story_waits_as_in_a_live_read(store: Store, tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    await store.upsert_group(GroupMeta(id=G2), state=GroupState.LOST)
    await read_with(store, raw, ["ok"], story(post(4), G2, T0 - 60), story(post(5), G1, T0 - 50))
    result = await replay(store, raw, since=T0 - 3600, config=config(tmp_path), now=T0 + 60)
    assert result.queued == 1
    assert not await store.is_seen(G2, post(4))


async def test_a_post_that_did_not_fit_the_wire_model_is_retried_after_a_fix(
    store: Store, tmp_path: Path
) -> None:
    """Replay skipped every seen post, and an invalid one was seen."""
    raw = tmp_path / "raw"
    raw.mkdir()
    await store.upsert_group(GroupMeta(id=G1, name="N" * 2000))  # longer than the wire allows
    read_id = await read_with(store, raw, ["ok"], story(post(6), G1, T0 - 60))
    first = await replay(store, raw, since=T0 - 3600, config=config(tmp_path), now=T0 + 60)
    assert (first.queued, first.still_invalid) == (0, 1)
    assert not await store.is_seen(G1, post(6))
    row = await store.get_quarantined(G1, post(6))
    assert row is not None and row.errors == ("group_name:string_too_long",)
    assert row.raw_path == str(raw / step_name(read_id, 0, T0))  # its payload, for later
    await store.upsert_group(GroupMeta(id=G1, name="Flats"))  # the fix
    fixed = await replay(store, raw, since=T0 - 3600, config=config(tmp_path), now=T0 + 120)
    assert (fixed.queued, fixed.still_invalid) == (1, 0)
    assert await queued(store) == [post(6)]
    assert await store.get_quarantined(G1, post(6)) is None
    again = await replay(store, raw, since=T0 - 3600, config=config(tmp_path), now=T0 + 180)
    assert again.queued == 0  # never re-sends


async def test_a_quarantined_post_is_retried_from_a_payload_older_than_since(
    store: Store, tmp_path: Path
) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    await store.upsert_group(GroupMeta(id=G1, name="N" * 2000))
    await read_with(store, raw, ["ok"], story(post(7), G1, T0 - 60), story(post(8), G1, T0 - 50))
    await replay(store, raw, since=T0 - 3600, config=config(tmp_path), now=T0 + 60)
    assert {r.post_id for r in await store.list_quarantined()} == {post(7), post(8)}
    lost = Sighting(post_id=post(9), group_id=G1, surface=Surface.FEED, read_id=77)
    await store.quarantine(lost, errors=["url:value_error"], raw_path=str(raw / "pruned.gz"))
    unsaved = Sighting(post_id=post(10), group_id=G1, surface=Surface.FEED, read_id=78)
    await store.quarantine(unsaved, errors=["url:value_error"])  # its payload was never saved
    await store.upsert_group(GroupMeta(id=G1, name="Flats"))
    # --since after the step: only the quarantine names its payload, and its posts only.
    later = await replay(store, raw, since=T0 + 30, config=config(tmp_path), now=T0 + 120)
    assert (later.steps, later.queued, later.still_invalid) == (1, 2, 0)
    assert sorted(await queued(store)) == [post(7), post(8)]
    # A payload already pruned, or never saved, is passed over; its post stays quarantined.
    assert [r.post_id for r in await store.list_quarantined()] == [post(9), post(10)]
