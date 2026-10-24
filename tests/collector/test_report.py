"""The phase-0 report, from a database built through the store's own API."""

from __future__ import annotations

import datetime as dt
import zoneinfo
from pathlib import Path

from fbcollector.clock import FakeClock
from fbcollector.model import Coverage, GroupMeta, PageState, ReadOutcome, Sighting, Surface
from fbcollector.report import (
    feed_miss,
    field_fill,
    generate,
    load,
    percentile,
    reading_hours,
    render,
)
from fbcollector.store import Store
from fbwire.models import BudgetKind, Item, MonitoredGroup, Via

ZONE = zoneinfo.ZoneInfo("Europe/Sofia")
T0 = 1_790_000_000.0
G1, G2, G3 = "400000000000001", "400000000000002", "400000000000003"


def item(group: str, post: str, created: float, now: float, via: Via) -> Item:
    return Item(
        collector_id="home-1",
        seq=0,
        group_id=group,
        post_id=post,
        url=f"https://www.facebook.com/groups/{group}/posts/{post}/",
        group_name="g",
        group_url=f"https://www.facebook.com/groups/{group}/",
        created_at=int(created),
        first_seen_at=int(now),
        via=via,
    )


STATS = {
    "health": {
        "found": 10,
        "accepted": 9,
        "units": 12,
        "missing": {"no_created_at": 1},
        "dropped": {"no_post_id": 0},
        "friendly_names": {"CometNewsFeedPaginationQuery": 2, "SomeOtherQuery": 5},
        "story_bodies": 2,
        "batch_stories": 11,
    },
    "texts": {"n": 9, "ellipsis": 1},
    "dom": {"see_more": 2},
    "group_paths": {"to": [9, 9], "target_group": [3, 2]},
    "pagination": {"scrolls": 2, "fired": 1},
    "resources": {"chrome_rss_mb": 700.0},
}


async def build_db(tmp_path: Path) -> Path:
    clock = FakeClock(start=T0 - 86400)
    path = tmp_path / "state.sqlite"
    async with Store(path, clock=clock) as store:
        for gid, name in ((G1, "Flats"), (G2, "Sale"), (G3, "Unfollowed")):
            await store.upsert_group(
                GroupMeta(id=gid, name=name, subscribe_status="IS_SUBSCRIBED"), synced=True
            )
        await store.set_monitored(
            [MonitoredGroup(group_id=G1), MonitoredGroup(group_id=G2, listings=True)]
        )
        await store.set_group_flags(G3, {"not_followed"})
        clock.advance(86400)
        # A complete feed read covering [T0 - 3600, T0], which saw post 1 but not post 2.
        read = await store.start_read(Surface.FEED)
        await store.record_budget_event(BudgetKind.NAVIGATION, surface=Surface.FEED)
        await store.record_budget_event(BudgetKind.SCROLL, surface=Surface.FEED)
        feed = Sighting("1", G1, Surface.FEED, read_id=read, created_at=int(T0 - 600))
        await store.mark_seen(
            feed, via=Via.FEED, item=item(G1, "1", T0 - 600, T0, Via.FEED), outcome="queued"
        )
        # History from before the group's baseline: seen, never an item, never a measure.
        history = Sighting("9", G1, Surface.FEED, read_id=read, created_at=int(T0 - 4 * 86400))
        await store.mark_seen(history, via=Via.FEED, outcome="before_baseline")
        await store.finish_read(
            read,
            coverage=Coverage(newest=int(T0), oldest=int(T0 - 3600), target_reached=True),
            steps=2,
            outcome=ReadOutcome.COMPLETE,
            page_state=PageState.OK,
            stats=STATS,
        )
        clock.advance(3 * 3600)
        backstop = await store.start_read(Surface.GROUP, group_id=G1)
        for post, group in (("2", G1), ("3", G3), ("4", G2)):
            sighting = Sighting(
                post, group, Surface.GROUP, read_id=backstop, created_at=int(T0 - 1200)
            )
            await store.mark_seen(
                sighting,
                via=Via.BACKSTOP,
                item=None
                if group == G3
                else item(group, post, T0 - 1200, clock.time(), Via.BACKSTOP),
                outcome="not_monitored" if group == G3 else "queued",
            )
        await store.finish_read(
            backstop,
            coverage=Coverage(),
            steps=1,
            outcome=ReadOutcome.COMPLETE,
            page_state=PageState.OK,
            stats={"pagination": {"scrolls": 1, "fired": 1}},
        )
        failed = await store.start_read(Surface.FEED)
        await store.finish_read(
            failed,
            coverage=Coverage(),
            steps=0,
            outcome=ReadOutcome.FAILED,
            page_state=PageState.UNKNOWN,
        )
        block = await store.open_block(
            state=PageState.BLOCKED, reason="url:checkpoint", until=T0 + 99999, rung=0
        )
        await store.close_block(block.id, reason="resumed after a clean probe")
    return path


async def test_measures(tmp_path: Path) -> None:
    data = load(await build_db(tmp_path))
    misses = {m.group_id: m for m in feed_miss(data)}
    assert misses[G1].covered == 2
    assert misses[G1].missed == 1  # post 2 was inside the feed read's window and not in it
    assert G3 not in misses  # not followed: never a feed miss
    data.groups[G1]["flags"] = '["feed_absent"]'  # followed, but snoozed in the feed
    assert G1 not in {m.group_id for m in feed_miss(data)}
    data.groups[G1]["flags"] = "[]"
    assert misses[G2].listings
    fill = field_fill(data)
    assert fill["created_at"] == 0.9
    assert fill["accepted"] == 0.9
    assert reading_hours(data, ZONE) == 2
    assert percentile([], 0.5) is None
    assert percentile([3, 1, 2], 0.5) == 2


async def test_only_delivered_posts_are_measured(tmp_path: Path) -> None:
    from fbcollector.report import late_rate, latencies, posts

    data = load(await build_db(tmp_path))
    assert posts(data) == {G1: 2, G2: 1}  # not the history, not the unmonitored group
    assert late_rate(data) == (2, 3)  # both backstop finds were 3 h 20 min old
    assert sorted(latencies(data)["feed"]) == [600.0]  # not 4 days


async def test_posts_created_while_nothing_was_read_are_not_measured(tmp_path: Path) -> None:
    """Latency and the late rate count a post only if it was seen, or a feed or group read
    began, within 30 min of its creation: those reads here start at T0 and T0 + 3 h."""
    from fbcollector.report import late_rate, latencies, measured

    path = await build_db(tmp_path)
    async with Store(path, clock=FakeClock(start=T0 + 4 * 3600 + 300)) as store:
        joins = await store.start_read(Surface.JOINS)  # reads no posts: not a sign of reading
        await store.finish_read(
            joins,
            coverage=Coverage(),
            steps=1,
            outcome=ReadOutcome.COMPLETE,
            page_state=PageState.OK,
        )
    data = load(path)
    assert measured(data)[1] == 0
    data.delivered += [
        (G1, "5", int(T0 - 2 * 3600), T0, "feed"),  # before reading began
        (G1, "6", int(T0 + 3600), T0 + 3 * 3600, "backstop"),  # in a 3 h rest
        (G1, "7", int(T0 + 3 * 3600 - 1800), T0 + 3 * 3600 + 60, "backstop"),  # read in 30 min
        (G1, "8", int(T0 + 3 * 3600 - 1801), T0 + 3 * 3600 + 60, "backstop"),  # a second more
        (G1, "9", int(T0 + 4 * 3600), T0 + 5 * 3600, "backstop"),  # a joins sync 5 min after
        (G1, "10", int(T0 + 4 * 3600), T0 + 4 * 3600 + 600, "feed"),  # seen by a running read
    ]
    assert measured(data)[1] == 4
    assert late_rate(data) == (2, 5)  # posts 2 and 4 (3 h 20 min), not 1, 7 or 10
    assert sorted(latencies(data)["backstop"]) == [1860.0, 12000.0, 12000.0]
    assert sorted(latencies(data)["feed"]) == [600.0, 600.0]
    text = render(data, zone=ZONE, now=T0 + 5 * 3600)
    assert "Not measured: 4 posts neither seen nor a feed or group read began within 30" in text
    assert "- backstop: 3 posts, p50 200.0 min" in text
    # A report since T0 + 1 h still sees the read at T0 that followed posts 2 and 4.
    since = generate(path, zone=ZONE, now=T0 + 5 * 3600, since=T0 + 3600)
    assert "Posts first seen > 2 h after creation: 2 of 2 (100.0 %)" in since


async def test_rows_from_before_the_outcome_column_are_judged_by_baseline(
    tmp_path: Path,
) -> None:
    import sqlite3

    from fbcollector.report import posts

    path = await build_db(tmp_path)
    db = sqlite3.connect(path)
    db.execute("UPDATE seen SET outcome=NULL")
    db.commit()
    db.close()
    assert posts(load(path)) == {G1: 2, G2: 1}


async def test_rendered_report(tmp_path: Path) -> None:
    path = await build_db(tmp_path)
    text = generate(path, zone=ZONE, now=T0 + 5 * 3600)
    assert text.startswith("# Phase-0 report")
    assert "Posts: **3** (needed ≥ 300)" in text
    assert "keep observing" in text
    assert f"| {G1} | no | 2 | 1 | 50.0% ⚠️ |" in text
    assert "created_at: 90.0% ⚠️" in text
    assert "Ending in an ellipsis: 1 (11.1 %)" in text
    assert "| `to` | 9 | 100.0 % |" in text
    assert "pagination fired on 2 of 3 scroll steps" in text
    assert "block #1" in text
    assert "unrecognised pages: 1" in text
    assert "p50 700 MiB" in text
    assert "stories per GraphQL response: 5.5" in text  # 11 stories in 2 story-bearing bodies
    assert "1 navigations, 1 scroll steps" in text
    since = generate(path, zone=ZONE, now=T0 + 5 * 3600, since=T0 + 3600)
    assert "Posts: **2**" in since


async def test_the_diagnostics_alone_print_no_group_id(tmp_path: Path) -> None:
    """``diagnostics_only`` (``report --diagnostics-only``, the operator door's
    ``diagnostics``) is the report's last section alone, without the sections that print
    group ids."""
    path = await build_db(tmp_path)
    full = generate(path, zone=ZONE, now=T0 + 5 * 3600)
    alone = generate(path, zone=ZONE, now=T0 + 5 * 3600, diagnostics_only=True)
    assert G1 in full
    assert G2 in full
    assert alone.startswith("# Parser diagnostics\n")
    assert [g for g in (G1, G2, G3) if g in alone] == []
    section = "## Parser diagnostics (structure only)"
    assert alone.split(section, 1)[1] == full.split(section, 1)[1]
    assert "## Account health" not in alone


def test_an_empty_report() -> None:
    from fbcollector.report import Data

    text = render(Data(), zone=ZONE, now=T0)
    assert "No feed reads yet." in text
    assert "No warning, logout or checkpoint." in text
    assert "Reading period: -" in text
    assert dt.datetime.fromtimestamp(T0, ZONE).strftime("%Y-%m-%d") in text
