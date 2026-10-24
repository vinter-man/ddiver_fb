"""The read loop over the real parser, detectors, ladder and store, with a scripted driver."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from fbcollector.alerts import AlertResult
from fbcollector.clock import FakeClock
from fbcollector.detectors import FEED_URL, group_url
from fbcollector.driver.policy import UrlNotAllowedError
from fbcollector.events import CollectorEvents
from fbcollector.ladder import Ladder
from fbcollector.model import GroupMeta, GroupState, PageState, ReadOutcome, Step, Surface
from fbcollector.raw import iter_steps
from fbcollector.reader import (
    CANCEL_GRACE,
    DAY,
    FLAG_NOT_FOLLOWED,
    FLAG_SORT_IGNORED,
    KV_PANEL_TOLD,
    KV_PARSER_BROKEN,
    KV_PARSER_HISTORY,
    NOTICE_ONCE,
    PARK_DEADLINE,
    READ_DEADLINE,
    SLEEP_ABORT,
    STEP_DEADLINE,
    STOP_GRACE,
    Abort,
    Reader,
    ReaderConfig,
    ReadResult,
    await_chain,
    page_facts,
    panel_notice_digest,
    panel_notice_key,
    panel_notice_text,
)
from fbcollector.scheduler import (
    FLAG_NO_RECENT_POSTS,
    FLAG_PAUSED,
    KV_BACKSTOP_ONLY,
    ActionKind,
    Hold,
)
from fbcollector.store import Store
from fbwire.models import CollectorState, Item, Via

from .reader_support import (
    G1,
    G2,
    G3,
    T0,
    FakeDriver,
    Recorder,
    StubScheduler,
    feed_body,
    feed_document,
    grant,
    group_body,
    group_document,
    joins_body,
    joins_document,
    nav,
    post,
    scroll,
    seed,
    story,
)

GROUP1 = group_url(G1)


@dataclass
class World:
    clock: FakeClock
    store: Store
    driver: FakeDriver
    scheduler: StubScheduler
    ladder: Ladder
    events: Recorder
    reader: Reader
    tmp: Path

    async def items(self) -> list[Item]:
        rows = await self.store.next_batch("home-1")
        return [Item.model_validate_json(r.payload) for r in rows]


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    clock = FakeClock(start=T0 - 7200)
    (tmp_path / "raw").mkdir()
    (tmp_path / "shots").mkdir()
    async with Store(tmp_path / "state.sqlite", clock=clock) as store:
        await seed(store, clock, G1, G2)
        await store.upsert_group(GroupMeta(id=G3, name="Unmonitored"), synced=True)
        clock.advance(7200 + 600)  # now = T0 + 10 min
        events = Recorder()
        ladder = Ladder(store, clock, events=events)
        driver = FakeDriver()
        scheduler = StubScheduler()
        reader = Reader(
            store=store,
            driver=driver,
            scheduler=scheduler,
            ladder=ladder,
            events=events,
            clock=clock,
            config=ReaderConfig("home-1", tmp_path / "raw", tmp_path / "shots"),
        )
        yield World(clock, store, driver, scheduler, ladder, events, reader, tmp_path)


def feed_nav(*nodes: dict[str, Any]) -> Step:
    return nav(FEED_URL, feed_document(), feed_body(*nodes))


# -- feed reads -------------------------------------------------------------------------------


async def test_feed_read_queues_new_posts_and_defers_incomplete_ones(world: World) -> None:
    world.driver.navigations.append(
        nav(
            FEED_URL,
            feed_document(),
            feed_body(
                story(post(1), G1, T0),
                story(post(2), G2, T0 - 300),
                story(post(3), G1, T0 - 600, text=None),  # a message section without text
                story(post(4), G3, T0 - 700),  # a known group that is not monitored
                story(post(5), "400000000000099", T0 - 800),  # a group we do not know
            ),
        )
    )
    result = await world.reader.read(grant(ActionKind.FEED, steps=0))
    # The first read ever cannot scroll back to its bootstrap target with no steps: a gap.
    assert result.outcome is ReadOutcome.GAP
    assert result.page_state is PageState.OK
    assert not result.close_chrome
    assert result.new_items == 2
    items = await world.items()
    assert [(i.group_id, i.post_id) for i in items] == [(G1, post(1)), (G2, post(2))]
    first = items[0]
    assert first.via is Via.FEED
    assert first.text == "hello"
    assert first.url == f"https://www.facebook.com/groups/{G1}/posts/{post(1)}/"
    assert first.first_seen_at == int(T0 + 600)
    assert first.author.name == "Ann"
    assert not first.late
    assert await world.store.is_seen(G3, post(4))  # seen, never sent
    assert await world.store.is_seen("400000000000099", post(5))
    cur = await world.store.db.execute("SELECT post_id, outcome FROM seen ORDER BY post_id")
    outcomes = {str(r[0]): r[1] for r in await cur.fetchall()}
    assert outcomes == {
        post(1): "queued",
        post(2): "queued",
        post(4): "not_monitored",
        post(5): "unknown_group",
    }
    deferred = await world.store.get_deferred(post(3))
    assert deferred is not None
    assert deferred.raw_path is not None and deferred.raw_path.endswith(".step.json.gz")
    assert world.driver.calls[-1] == "park"
    assert world.scheduler.finished == [(ReadOutcome.GAP, (G1, G2))]
    record = await world.store.last_read(Surface.FEED)
    assert record is not None
    counts = record.stats["counts"]
    assert counts["queued"] == 2
    assert counts["not_monitored"] == 1
    assert counts["unknown_group"] == 1
    assert counts["deferred"] == 1
    assert record.stats["verdicts"] == ["ok", "ok"]
    assert record.stats["group_paths"]
    saved = list(iter_steps(world.tmp / "raw", since=0))
    assert [s.index for s in saved] == [0, 1]
    assert saved[0].step.document == feed_document()


async def test_second_read_sees_posts_again_and_stops_on_the_seen_streak(world: World) -> None:
    nodes = [story(post(n), G1, T0 - 600 * n) for n in range(1, 6)]
    world.driver.navigations.append(feed_nav(*nodes))
    await world.reader.read(grant(ActionKind.FEED, steps=0))
    world.clock.advance(600)
    old = [story(post(n), G1, T0 - 3600 * n) for n in range(6, 9)]
    fresh = [story(post(20), G1, T0 + 900), story(post(21), G1, T0 + 800)]
    world.driver.navigations.append(feed_nav(*fresh, nodes[0]))
    # Three seen stories in a row, then older ones nobody saw: the read stops here anyway.
    world.driver.scrolls.append(scroll(FEED_URL, feed_body(*nodes[1:], *old)))
    result = await world.reader.read(grant(ActionKind.FEED, steps=3))
    assert result.outcome is ReadOutcome.COMPLETE
    assert world.driver.calls.count("scroll") == 1  # reached after one step, not three
    record = await world.store.last_read(Surface.FEED)
    assert record is not None
    assert record.coverage.target_reached
    assert record.stats["counts"]["seen_again"] >= 3
    assert {post(20), post(21)} <= {i.post_id for i in await world.items()}


async def test_a_capped_read_still_seeing_new_posts_records_a_gap(world: World) -> None:
    # A first read establishes T_feed at its start.
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0 - 60)))
    await world.reader.read(grant(ActionKind.FEED, steps=0))
    world.clock.advance(3600)
    fresh = [story(post(10 + n), G2 if n % 2 else G1, T0 + 3000 - 60 * n) for n in range(6)]
    world.driver.navigations.append(feed_nav(*fresh[:3]))
    world.driver.scrolls.append(scroll(FEED_URL, feed_body(*fresh[3:])))
    result = await world.reader.read(grant(ActionKind.FEED, steps=1))
    assert result.outcome is ReadOutcome.GAP
    outcome, groups = world.scheduler.finished[-1]
    assert outcome is ReadOutcome.GAP
    assert set(groups) <= {G1, G2}
    assert groups


async def test_budget_hold_caps_and_a_gate_hold_aborts(world: World) -> None:
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0 + 500)))
    world.scheduler.holds.append(Hold(T0 + 900, CollectorState.OK, ("scroll cap",), False))
    capped = await world.reader.read(grant(ActionKind.FEED, steps=3))
    assert capped.outcome in {ReadOutcome.GAP, ReadOutcome.COMPLETE}
    assert "scroll" not in world.driver.calls
    world.driver.navigations.append(feed_nav(story(post(2), G1, T0 + 500)))
    world.scheduler.holds.append(Hold(None, CollectorState.NOT_HOME, ("sleep: slept",), True))
    aborted = await world.reader.read(grant(ActionKind.FEED, steps=3))
    assert aborted.outcome is ReadOutcome.ABORTED
    assert (aborted.close_chrome, aborted.planned_close) == (True, True)  # a gate: planned
    assert world.driver.calls[-1] != "park"  # the loop closes Chrome instead


async def test_a_gate_closed_before_the_final_look_aborts_the_read(world: World) -> None:
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0 + 500)))
    world.scheduler.rechecks.append(Hold(None, CollectorState.NOT_HOME, ("sleep: slept",), True))
    result = await world.reader.read(grant(ActionKind.FEED, steps=0))
    assert result.outcome is ReadOutcome.ABORTED
    assert (result.close_chrome, result.planned_close) == (True, True)
    assert not any(c.startswith("dwell") for c in world.driver.calls)
    assert "park" not in world.driver.calls  # the loop closes Chrome instead
    assert await world.store.is_seen(G1, post(1))  # the navigation itself was clean


@pytest.mark.parametrize("closed", [True, False], ids=["paused", "another_pace"])
async def test_a_grant_whose_terms_changed_before_its_navigation_sends_nothing(
    world: World, closed: bool
) -> None:
    """The control state changed between the grant and the navigation
    (after the read prepared itself): the grant is discarded before anything is sent."""
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0 + 500)))
    if closed:
        hold = Hold(None, CollectorState.PAUSED, ("control: paused by the owner",), True)
    else:
        hold = Hold(T0, CollectorState.OK, ("the control state changed since the grant",), False)
    world.scheduler.confirms.append(hold)
    result = await world.reader.read(grant(ActionKind.FEED, steps=3))
    assert result.outcome is ReadOutcome.ABORTED
    assert result.reason == hold.reasons[0]
    assert result.close_chrome is closed
    assert result.planned_close  # the terms changed: a stop, not a failure
    assert not any(c.startswith(("navigate", "scroll", "dwell")) for c in world.driver.calls)
    assert world.driver.calls == ([] if closed else ["park"])
    assert world.scheduler.finished == [(ReadOutcome.ABORTED, ())]


async def test_a_post_that_does_not_fit_the_wire_model_is_quarantined_not_seen(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """Such a post was marked seen with no outbox row — lost for good.
    Now it waits unseen, is tried again at each sighting, and the owner is told once."""
    from fbcollector.events import KV_QUARANTINE

    caplog.set_level("DEBUG")
    # A group name longer than the wire allows: the reduced retry cannot drop it.
    await world.store.upsert_group(GroupMeta(id=G1, name="N" * 2000))
    text = "a flat for rent, call 0888"
    world.driver.navigations.append(
        feed_nav(story(post(1), G1, T0, text=text), story(post(2), G2, T0 - 60))
    )
    await world.reader.read(grant(ActionKind.FEED, steps=0))
    assert not await world.store.is_seen(G1, post(1))
    assert await world.store.is_seen(G2, post(2))  # its neighbour is an item as usual
    assert [i.post_id for i in await world.items()] == [post(2)]
    row = await world.store.get_quarantined(G1, post(1))
    assert row is not None
    assert (row.errors, row.attempts) == (("group_name:string_too_long",), 1)
    assert row.raw_path is not None  # the step's payload, for replay
    assert await asyncio.to_thread(Path(row.raw_path).exists)
    events = [kw["detail"] for k, kw in world.events.events if k == "items_invalid"]
    assert events == ["1 post(s) did not fit the wire model: group_name:string_too_long"]
    logged = " ".join(r.getMessage() for r in caplog.records if r.levelno > logging.DEBUG)
    assert "does not fit the wire model (group_name:string_too_long)" in logged
    assert text not in logged  # invariant 8: no post text above DEBUG
    # Seen again: tried again, still refused, one attempt more — and no second event.
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0, text=text)))
    await world.reader.read(grant(ActionKind.FEED, steps=0))
    row = await world.store.get_quarantined(G1, post(1))
    assert row is not None and row.attempts == 2
    assert len([k for k, _ in world.events.events if k == "items_invalid"]) == 1
    # The name is fixed (an upgrade, a sync): the next sighting makes it an item.
    await world.store.upsert_group(GroupMeta(id=G1, name="Flats"))
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0, text=text)))
    await world.reader.read(grant(ActionKind.FEED, steps=0))
    assert await world.store.is_seen(G1, post(1))
    assert await world.store.get_quarantined(G1, post(1)) is None
    assert {i.post_id for i in await world.items()} == {post(1), post(2)}
    assert await world.store.kv_get(KV_QUARANTINE) is not None  # re-armed by the next check
    from fbcollector.events import quarantine_edge

    assert await quarantine_edge(world.store) is None
    assert await world.store.kv_get(KV_QUARANTINE) is None


async def test_a_quarantined_post_counts_as_read_before_for_the_stop_rule(world: World) -> None:
    """The feed stops once it is back in covered territory: three posts in a row read before.
    A quarantined post was read before too; were it taken for new, one bad post would keep
    every read of the next week from its target."""
    await world.store.upsert_group(GroupMeta(id=G2, name="N" * 2000))
    nodes = (
        story(post(1), G1, T0 - 2940),  # the batch's anchor: out of order, not counted
        story(post(2), G1, T0 - 3000),
        story(post(3), G2, T0 - 3060),  # quarantined
        story(post(4), G1, T0 - 3120),
    )
    world.driver.navigations.append(feed_nav(*nodes))
    await world.reader.read(grant(ActionKind.FEED, steps=0))
    assert await world.store.get_quarantined(G2, post(3)) is not None
    world.driver.navigations.append(feed_nav(*nodes))
    again = await world.reader.read(grant(ActionKind.FEED, steps=0))
    assert again.read_id is not None
    record = await world.store.get_read(again.read_id)
    assert record is not None and record.coverage.target_reached


async def test_shutdown_between_steps_aborts_but_parks(world: World) -> None:
    import asyncio

    stop = asyncio.Event()
    stop.set()
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0 + 500)))
    result = await world.reader.read(grant(ActionKind.FEED, steps=3), stop=stop)
    assert result.outcome is ReadOutcome.ABORTED
    assert not result.close_chrome
    assert world.driver.calls[-1] == "park"


async def test_novelty_window_and_baseline(world: World) -> None:
    world.driver.navigations.append(
        feed_nav(
            story(post(1), G1, T0 - 3 * 3600),  # before the group's baseline (T0 - 2 h)
            story(post(2), G1, T0 - 60),
            story(post(3), G2, T0 - 60, actors=[{"id": "pfbid0abc", "name": "X"}]),
        )
    )
    await world.reader.read(grant(ActionKind.FEED, steps=0))
    items = {i.post_id: i for i in await world.items()}
    assert set(items) == {post(2), post(3)}
    assert items[post(3)].author.id is None  # a non-numeric id is dropped by the parser
    record = await world.store.last_read(Surface.FEED)
    assert record is not None
    assert record.stats["counts"]["before_baseline"] == 1


async def test_posts_of_a_group_that_is_lost_for_now_are_not_consumed(world: World) -> None:
    from fbcollector.sync import apply_sync

    await world.store.upsert_group(GroupMeta(id=G1), state=GroupState.LOST)
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0 + 300)))
    await world.reader.read(grant(ActionKind.FEED, steps=0))
    assert not await world.store.is_seen(G1, post(1))
    assert await world.items() == []
    record = await world.store.last_read(Surface.FEED)
    assert record is not None and record.stats["counts"]["not_member"] == 1
    # The lost verdict was a glitch: the next complete sync lists the group again.
    from fbcollector.joins import JoinedGroups
    from fbcollector.parser import PageInfo

    listed = JoinedGroups(tuple(GroupMeta(id=g, name=g, join_state="MEMBER") for g in (G1, G2, G3)))
    sync = await apply_sync(
        world.store, listed, header_count=3, page_info=PageInfo("joined_groups", False, None)
    )
    assert sync.relisted == (G1,)
    world.clock.advance(600)
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0 + 300)))
    await world.reader.read(grant(ActionKind.FEED, steps=0))
    assert [i.post_id for i in await world.items()] == [post(1)]


async def test_stale_and_late_posts(world: World, tmp_path: Path) -> None:
    world.clock.advance(9 * 86400)
    world.driver.navigations.append(
        feed_nav(
            story(post(1), G1, T0 + 86400),  # 8 days old: past the 7-day window
            story(post(2), G1, T0 + 9 * 86400 - 3 * 3600),  # 3 h old: late
        )
    )
    await world.reader.read(grant(ActionKind.FEED, steps=0))
    items = await world.items()
    assert [i.post_id for i in items] == [post(2)]
    assert items[0].late
    record = await world.store.last_read(Surface.FEED)
    assert record is not None
    assert record.stats["stale_dropped"] == {G1: 1}


async def test_a_filling_outbox_is_one_event_not_one_per_post(world: World) -> None:
    from dataclasses import replace as dc_replace

    world.reader._config = dc_replace(world.reader._config, outbox_cap=5)
    fresh = [story(post(n), G1, T0 + 500 - n) for n in range(1, 11)]
    world.driver.navigations.append(feed_nav(*fresh))
    await world.reader.read(grant(ActionKind.FEED, steps=0))
    assert world.events.kinds().count("outbox_high") == 1
    world.driver.navigations.append(feed_nav(story(post(20), G1, T0 + 550)))
    await world.reader.read(grant(ActionKind.FEED, steps=0))
    assert world.events.kinds().count("outbox_high") == 1  # still high: no new event


async def test_an_item_that_does_not_fit_the_wire_drops_its_author(world: World) -> None:
    bad_author = [{"__typename": "User", "id": "100000000000077", "name": "N" * 2000}]
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0, actors=bad_author)))
    await world.reader.read(grant(ActionKind.FEED, steps=0))
    items = await world.items()
    assert len(items) == 1
    assert items[0].author.name is None


# -- verdicts ---------------------------------------------------------------------------------


async def test_a_checkpoint_blocks_before_the_screenshot_and_stores_nothing(world: World) -> None:
    order: list[str] = []

    async def on_shot() -> None:
        block = await world.store.current_block()
        order.append("block on disk" if block is not None else "no block yet")

    world.driver.on_screenshot = on_shot
    checkpoint = "https://www.facebook.com/checkpoint/1501092823525282/?next=https%3A%2F%2Fx"
    world.driver.navigations.append(nav(checkpoint, feed_document(), feed_body(story(post(1)))))
    result = await world.reader.read(grant(ActionKind.FEED))
    assert result.outcome is ReadOutcome.FAILED
    assert result.page_state is PageState.BLOCKED
    assert (result.close_chrome, result.planned_close) == (True, False)  # a failure: unplanned
    assert order == ["block on disk"]
    block = await world.store.current_block()
    assert block is not None
    assert block.screenshot is not None and block.screenshot.endswith("blocked.jpg.gz")
    assert not await world.store.is_seen(G1, post(1))
    assert "park" not in world.driver.calls
    assert "block_opened" in world.events.kinds()


async def test_an_unrecognised_page_backs_off_and_closes_chrome(world: World) -> None:
    world.driver.navigations.append(
        nav(FEED_URL, feed_document(lang="de"), feed_body(story(post(1))), html_lang="de")
    )
    result = await world.reader.read(grant(ActionKind.FEED))
    assert result.page_state is PageState.UNKNOWN
    assert (result.close_chrome, result.planned_close) == (True, False)
    assert (await world.ladder.state()).backoff_until is not None
    assert await world.store.current_block() is None
    assert not await world.store.is_seen(G1, post(1))


async def test_a_late_dialog_on_the_final_look_is_a_block(world: World) -> None:
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0)))
    world.driver.dwells.append(
        nav(
            FEED_URL,
            None,
            status=None,
            dialog_texts=("We suspect automated behavior on your account",),
        )
    )
    result = await world.reader.read(grant(ActionKind.FEED, steps=0))
    assert result.page_state is PageState.BLOCKED
    assert (result.close_chrome, result.planned_close) == (True, False)
    assert await world.store.is_seen(G1, post(1))  # the navigation's posts were fine


async def test_a_driver_failure_is_an_unknown_page(world: World) -> None:
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0 + 500)))
    world.driver.scrolls.append(RuntimeError("Target page, context or browser has been closed"))
    result = await world.reader.read(grant(ActionKind.FEED, steps=2))
    assert result.page_state is PageState.UNKNOWN
    assert (result.close_chrome, result.planned_close) == (True, False)  # the browser failed
    assert (await world.ladder.state()).unknown_streak == 1


def bounded_reader(
    world: World,
    *,
    deadline: float = READ_DEADLINE,
    step_deadline: float = STEP_DEADLINE,
    cancel_grace: float = CANCEL_GRACE,
    stop_grace: float = STOP_GRACE,
    park_deadline: float = PARK_DEADLINE,
) -> Reader:
    """The world's reader with short deadlines (seconds of the event loop's real time)."""
    config = ReaderConfig(
        "home-1",
        world.tmp / "raw",
        world.tmp / "shots",
        deadline=deadline,
        step_deadline=step_deadline,
        cancel_grace=cancel_grace,
        stop_grace=stop_grace,
        park_deadline=park_deadline,
    )
    return Reader(
        store=world.store,
        driver=world.driver,
        scheduler=world.scheduler,
        ladder=world.ladder,
        events=world.events,
        clock=world.clock,
        config=config,
    )


async def test_a_hung_read_fails_at_its_deadline_and_logs_where_it_waited(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """2026-10-07: a browser call never answered and the read held the loop for eight hours."""
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0 + 500)))
    never = asyncio.Event()

    async def stuck_scroll() -> Step:
        await never.wait()
        raise AssertionError("the event is never set")

    world.driver.scroll_step = stuck_scroll  # type: ignore[method-assign]
    reader = bounded_reader(world, deadline=0.2, step_deadline=0.1)
    with caplog.at_level(logging.WARNING, logger="fbcollector.reader"):
        result = await reader.read(grant(ActionKind.FEED, steps=2))
    assert result.outcome is ReadOutcome.FAILED
    assert result.page_state is PageState.UNKNOWN
    assert (result.close_chrome, result.planned_close) == (True, False)  # Chrome is closed
    assert result.reason == "driver failure: ReadHung"
    assert (await world.ladder.state()).unknown_streak == 1  # like any browser failure
    hung = [r.getMessage() for r in caplog.records if "hung for" in r.getMessage()]
    assert len(hung) == 1
    assert "_steps > test_reader.py" in hung[0]
    assert "stuck_scroll" in hung[0]
    assert not any("left behind" in r.getMessage() for r in caplog.records)  # it unwound
    assert await world.store.is_seen(G1, post(1))  # what the navigation read is kept


async def test_a_read_hung_while_chrome_closes_for_sleep_is_a_sleep_abort(world: World) -> None:
    """The sleep watcher closes Chrome under the read; a call the closing Chrome never answers
    ends at the deadline as the sleep's abort, not as a strike on the ladder."""
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0 + 500)))
    abort = Abort()

    async def stuck_scroll() -> Step:
        abort.stop(SLEEP_ABORT)
        await asyncio.Event().wait()
        raise AssertionError("never set")

    world.driver.scroll_step = stuck_scroll  # type: ignore[method-assign]
    reader = bounded_reader(world, deadline=0.2, step_deadline=0.1)
    result = await reader.read(grant(ActionKind.FEED, steps=2), abort=abort)
    assert result.outcome is ReadOutcome.ABORTED
    assert result.planned_close
    assert (await world.ladder.state()).unknown_streak == 0


async def test_a_shutdown_cuts_a_step_that_does_not_end_without_a_strike(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """Launchd's stop allows 90 s; a read waits ``stop_grace`` for its step, then is cut."""
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0 + 500)))
    stop = asyncio.Event()

    async def stuck_scroll() -> Step:
        stop.set()
        await asyncio.Event().wait()
        raise AssertionError("never set")

    world.driver.scroll_step = stuck_scroll  # type: ignore[method-assign]
    reader = bounded_reader(world, deadline=30.0, stop_grace=0.05)
    with caplog.at_level(logging.WARNING, logger="fbcollector.reader"):
        result = await asyncio.wait_for(reader.read(grant(ActionKind.FEED, steps=2), stop=stop), 5)
    assert result.outcome is ReadOutcome.ABORTED
    assert (result.close_chrome, result.planned_close) == (True, True)
    assert result.reason == "shutting down"
    assert (await world.ladder.state()).unknown_streak == 0
    assert any("cut by the shutdown" in r.getMessage() for r in caplog.records)


async def test_a_shutdown_lets_a_step_under_way_finish(world: World) -> None:
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0 + 500)))
    stop = asyncio.Event()
    calls = 0

    async def slow_scroll() -> Step:
        nonlocal calls
        calls += 1
        stop.set()
        await asyncio.sleep(0.05)
        return scroll(FEED_URL, fired=False)

    world.driver.scroll_step = slow_scroll  # type: ignore[method-assign]
    reader = bounded_reader(world, deadline=30.0, stop_grace=5.0)
    result = await reader.read(grant(ActionKind.FEED, steps=3), stop=stop)
    assert result.outcome is ReadOutcome.ABORTED
    assert result.reason == "shutting down"  # the read's own look at stop, between steps
    assert not result.close_chrome
    assert calls == 1


async def test_a_hung_read_that_ignores_its_cancel_is_left_behind(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """A Playwright call unwinds by waiting for its driver's answer: if that never comes, the
    read is left behind and the loop goes on."""
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0 + 500)))
    release = asyncio.Event()

    async def stubborn_scroll() -> Step:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()  # the driver's answer to the abort, which does not come
            raise
        raise AssertionError("never set")

    world.driver.scroll_step = stubborn_scroll  # type: ignore[method-assign]
    reader = bounded_reader(world, deadline=0.1, step_deadline=0.05, cancel_grace=0.05)
    with caplog.at_level(logging.WARNING, logger="fbcollector.reader"):
        result = await asyncio.wait_for(reader.read(grant(ActionKind.FEED, steps=1)), 5)
    assert result.reason == "driver failure: ReadHung"
    assert any("left behind" in r.getMessage() for r in caplog.records)
    release.set()  # let the abandoned task end before the loop closes
    await asyncio.sleep(0.01)


async def test_a_park_that_hangs_closes_chrome(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0 + 500)))

    async def stuck_park() -> None:
        await asyncio.Event().wait()

    world.driver.park = stuck_park  # type: ignore[method-assign]
    reader = bounded_reader(world, park_deadline=0.05)
    with caplog.at_level(logging.WARNING, logger="fbcollector.reader"):
        result = await asyncio.wait_for(reader.read(grant(ActionKind.FEED, steps=0)), 5)
    assert result.outcome is ReadOutcome.COMPLETE  # the read itself was whole
    assert (result.close_chrome, result.planned_close) == (True, False)
    assert result.reason == "parking hung"
    assert any("parking the tab did not end" in r.getMessage() for r in caplog.records)


def test_a_read_deadline_grows_with_its_steps_and_dwell() -> None:
    config = ReaderConfig("home-1", Path("raw"), Path("shots"))
    assert config.read_deadline(0, 0.0) == 8 * 60
    assert config.read_deadline(8, 15.0) == 8 * 60 + 8 * 120 + 15


async def test_await_chain_names_each_coroutine_and_stops_at_a_future() -> None:
    gate = asyncio.Event()

    async def inner() -> None:
        await gate.wait()

    async def outer() -> None:
        await inner()

    task = asyncio.create_task(outer())
    await asyncio.sleep(0)
    chain = await_chain(task.get_coro())
    assert [link.split(" ")[1] for link in chain] == ["outer", "inner", "wait"]
    assert chain[0].startswith("test_reader.py:")
    gate.set()
    await task
    assert await_chain(task.get_coro()) == []  # a finished coroutine has no frame
    assert await_chain(asyncio.get_running_loop().create_future()) == []
    assert await_chain(None) == []


async def test_a_refused_url_is_a_bug_not_a_warning(world: World) -> None:
    world.driver.navigate_error = UrlNotAllowedError("refused")
    result = await world.reader.read(grant(ActionKind.FEED))
    assert result.outcome is ReadOutcome.FAILED
    assert result.reason == "url_not_allowed"
    assert (await world.ladder.state()).unknown_streak == 0


async def test_feed_not_chronological_switches_to_backstop_only(world: World) -> None:
    world.driver.navigations.append(
        nav(FEED_URL, feed_document(chrono=False), feed_body(story(post(1), G1, T0)))
    )
    result = await world.reader.read(grant(ActionKind.FEED))
    assert result.page_state is PageState.FEED_NOT_CHRONO
    assert not result.close_chrome
    assert await world.store.kv_get(KV_BACKSTOP_ONLY) == "1"
    assert world.events.kinds() == ["feed_not_chrono"]
    assert await world.store.is_seen(G1, post(1))  # the posts themselves are real
    world.driver.navigations.append(
        nav(FEED_URL, feed_document(chrono=False), feed_body(story(post(2), G1, T0)))
    )
    await world.reader.read(grant(ActionKind.FEED))
    assert world.events.kinds() == ["feed_not_chrono"]  # announced once


# -- backstops --------------------------------------------------------------------------------


async def test_backstop_refreshes_group_metadata_and_flags(world: World) -> None:
    world.driver.navigations.append(
        nav(GROUP1, group_document(subscribe="CAN_SUBSCRIBE"), group_body(story(post(1), G1, T0)))
    )
    result = await world.reader.read(grant(ActionKind.BACKSTOP, group=G1, steps=0))
    assert result.page_state is PageState.NOT_FOLLOWED
    assert result.outcome is ReadOutcome.COMPLETE
    row = await world.store.get_group(G1)
    assert row is not None
    assert row.subscribe_status == "CAN_SUBSCRIBE"
    assert row.privacy.value == "private"
    assert FLAG_NOT_FOLLOWED in row.flags
    items = await world.items()
    assert items[0].via is Via.BACKSTOP
    assert items[0].group_privacy.value == "private"
    assert world.events.kinds() == ["not_followed"]

    world.driver.navigations.append(
        nav(GROUP1, group_document(subscribe="CAN_SUBSCRIBE"), group_body(story(post(2), G1, T0)))
    )
    await world.reader.read(grant(ActionKind.BACKSTOP, group=G1, steps=0))
    assert world.events.kinds() == ["not_followed"]  # once, not every backstop

    world.driver.navigations.append(
        nav(GROUP1, group_document(sorting="TOP_POSTS"), group_body(story(post(3), G1, T0)))
    )
    result = await world.reader.read(grant(ActionKind.BACKSTOP, group=G1, steps=0))
    assert result.page_state is PageState.SORT_IGNORED
    row = await world.store.get_group(G1)
    assert row is not None
    assert FLAG_NOT_FOLLOWED not in row.flags  # followed again
    assert FLAG_SORT_IGNORED in row.flags

    world.driver.navigations.append(
        nav(GROUP1, group_document(), group_body(story(post(4), G1, T0)))
    )
    result = await world.reader.read(grant(ActionKind.BACKSTOP, group=G1, steps=0))
    assert result.page_state is PageState.OK
    row = await world.store.get_group(G1)
    assert row is not None
    assert not row.flags & {FLAG_NOT_FOLLOWED, FLAG_SORT_IGNORED}
    assert FLAG_NO_RECENT_POSTS not in row.flags


async def test_a_followed_group_missing_from_the_feed_for_two_days_is_flagged(
    world: World,
) -> None:
    """DESIGN's second NOT_FOLLOWED trigger: followed, absent from the feed, posts found."""
    from fbcollector.reader import FLAG_FEED_ABSENT

    world.driver.navigations.append(feed_nav(story(post(1), G2, T0 + 300)))
    await world.reader.read(grant(ActionKind.FEED, steps=0))
    world.clock.advance(20 * 3600)
    # Only a day of feed reads so far: too early to say.
    world.driver.navigations.append(
        nav(GROUP1, group_document(), group_body(story(post(2), G1, T0 + 20 * 3600)))
    )
    await world.reader.read(grant(ActionKind.BACKSTOP, group=G1, steps=0))
    assert "not_followed" not in world.events.kinds()
    world.clock.advance(29 * 3600)
    now = world.clock.time()
    world.driver.navigations.append(feed_nav(story(post(3), G2, now - 60)))
    await world.reader.read(grant(ActionKind.FEED, steps=0))
    world.driver.navigations.append(
        nav(GROUP1, group_document(), group_body(story(post(4), G1, now - 3 * 3600)))
    )
    await world.reader.read(grant(ActionKind.BACKSTOP, group=G1, steps=0))
    assert world.events.kinds().count("not_followed") == 1
    row = await world.store.get_group(G1)
    assert row is not None and FLAG_FEED_ABSENT in row.flags
    world.driver.navigations.append(
        nav(GROUP1, group_document(), group_body(story(post(5), G1, now - 2 * 3600)))
    )
    await world.reader.read(grant(ActionKind.BACKSTOP, group=G1, steps=0))
    assert world.events.kinds().count("not_followed") == 1  # told once
    # The feed shows the group again: the flag goes.
    world.driver.navigations.append(feed_nav(story(post(6), G1, now + 60)))
    await world.reader.read(grant(ActionKind.FEED, steps=0))
    row = await world.store.get_group(G1)
    assert row is not None and FLAG_FEED_ABSENT not in row.flags


async def test_an_empty_listings_page_is_unrecognised(world: World) -> None:
    from fbwire.models import MonitoredGroup

    await world.store.set_monitored([MonitoredGroup(group_id=G1, listings=True)])
    listing_url = group_url(G1, listings=True)
    world.driver.navigations.append(
        nav(listing_url, group_document(sorting="CHRONOLOGICAL_LISTINGS"))
    )
    result = await world.reader.read(grant(ActionKind.BACKSTOP, group=G1, steps=0))
    assert result.page_state is PageState.UNKNOWN  # no stories and no empty marker


async def test_a_listings_backstop_reads_listings_end_to_end(world: World) -> None:
    from fbwire.models import MonitoredGroup

    await world.store.set_monitored([MonitoredGroup(group_id=G1, listings=True)])
    world.clock.advance(60)  # past the new baseline
    listing_url = group_url(G1, listings=True)
    listing = story(
        post(1),
        G1,
        world.clock.time() - 30,
        marketplace_listing_title="2-room flat",
        formatted_price={"text": "650 EUR"},
    )
    world.driver.navigations.append(
        nav(
            listing_url,
            group_document(sorting="CHRONOLOGICAL_LISTINGS"),
            group_body(listing, story(post(2), G1, T0 - 9 * 86400)),
        )
    )
    backstop = grant(ActionKind.BACKSTOP, group=G1, steps=0)
    listings_grant = type(backstop)(
        dataclasses.replace(backstop.action, url=listing_url), backstop.profile, backstop.knobs
    )
    result = await world.reader.read(listings_grant)
    assert result.page_state is PageState.OK  # CHRONOLOGICAL_LISTINGS is the sort asked for
    assert "navigate " + listing_url in world.driver.calls
    items = await world.items()
    assert [(i.post_id, i.via) for i in items] == [(post(1), Via.BACKSTOP_LISTINGS)]
    row = await world.store.get_group(G1)
    assert row is not None and FLAG_NO_RECENT_POSTS not in row.flags


async def test_a_group_with_no_recent_posts_is_flagged_quiet(world: World) -> None:
    world.clock.advance(10 * 86400)
    world.driver.navigations.append(
        nav(GROUP1, group_document(), group_body(story(post(1), G1, T0)))
    )
    await world.reader.read(grant(ActionKind.BACKSTOP, group=G1, steps=0))
    row = await world.store.get_group(G1)
    assert row is not None
    assert FLAG_NO_RECENT_POSTS in row.flags


async def test_group_lost_pauses_it_and_two_in_a_day_block(world: World) -> None:
    world.driver.navigations.append(nav(GROUP1, group_document(join_state="NOT_MEMBER")))
    result = await world.reader.read(grant(ActionKind.BACKSTOP, group=G1))
    assert result.page_state is PageState.GROUP_LOST
    assert not result.close_chrome
    row = await world.store.get_group(G1)
    assert row is not None
    assert row.state is GroupState.LOST
    assert FLAG_PAUSED in row.flags
    assert world.events.kinds() == ["group_lost"]

    world.driver.navigations.append(nav(group_url(G2), group_document(G2, join_state="NOT_MEMBER")))
    second = await world.reader.read(grant(ActionKind.BACKSTOP, group=G2))
    assert (second.close_chrome, second.planned_close) == (True, False)
    assert (await world.store.current_block()) is not None
    row2 = await world.store.get_group(G2)
    assert row2 is not None
    assert row2.state is GroupState.LOST


async def test_backstop_scrolls_until_its_target(world: World) -> None:
    older = [story(post(10 + n), G1, T0 - 9000 - 600 * n) for n in range(3)]
    world.driver.navigations.append(
        nav(
            GROUP1,
            group_document(),
            group_body(story(post(1), G1, T0 - 60), story(post(2), G1, T0 - 120)),
        )
    )
    world.driver.scrolls.append(scroll(GROUP1, group_body(*older)))
    world.driver.scrolls.append(scroll(GROUP1, group_body(story(post(30), G1, T0 - 99999))))
    result = await world.reader.read(grant(ActionKind.BACKSTOP, group=G1, steps=6))
    assert result.outcome is ReadOutcome.COMPLETE
    assert world.driver.calls.count("scroll") == 1
    record = await world.store.last_read(Surface.GROUP, group_id=G1)
    assert record is not None
    assert record.coverage.target_reached
    assert record.stats["pagination"] == {"scrolls": 1, "fired": 1}


async def test_a_capped_backstop_keeps_its_target_for_the_next_one(world: World) -> None:
    """The part of a busy group a capped backstop could not reach is scrolled toward again."""
    busy = [story(post(100 + n), G1, T0 - 60 * n) for n in range(1, 16)]
    world.driver.navigations.append(nav(GROUP1, group_document(), group_body(*busy[:5])))
    world.driver.scrolls.append(scroll(GROUP1, group_body(*busy[5:10])))
    world.driver.scrolls.append(scroll(GROUP1, group_body(*busy[10:])))
    first = await world.reader.read(grant(ActionKind.BACKSTOP, group=G1, steps=2))
    assert first.outcome is ReadOutcome.GAP
    assert world.scheduler.finished[-1] == (ReadOutcome.GAP, (G1,))
    capped = await world.store.last_read(Surface.GROUP, group_id=G1)
    assert capped is not None and not capped.coverage.target_reached
    assert capped.coverage.target == T0 - 7200  # the group's baseline
    world.clock.advance(2400)
    world.driver.navigations.append(
        nav(GROUP1, group_document(), group_body(story(post(200), G1, T0 + 2000)))
    )
    await world.reader.read(grant(ActionKind.BACKSTOP, group=G1, steps=0))
    after = await world.store.last_read(Surface.GROUP, group_id=G1)
    assert after is not None
    assert after.coverage.target == T0 - 7200  # not the capped read's start (T0 + 600)


async def test_a_capped_backstop_over_posts_the_feed_saw_is_still_a_gap(world: World) -> None:
    six = [story(post(n), G1, T0 - 50 * n) for n in range(1, 7)]
    world.driver.navigations.append(feed_nav(*six))
    await world.reader.read(grant(ActionKind.FEED, steps=0))
    world.driver.navigations.append(nav(GROUP1, group_document(), group_body(*six[:3])))
    world.driver.scrolls.append(scroll(GROUP1, group_body(*six[3:])))
    result = await world.reader.read(grant(ActionKind.BACKSTOP, group=G1, steps=1))
    record = await world.store.last_read(Surface.GROUP, group_id=G1)
    assert record is not None
    assert record.coverage.oldest is not None and record.coverage.target is not None
    assert record.coverage.oldest > record.coverage.target  # it never got down there
    assert result.outcome is ReadOutcome.GAP
    assert world.scheduler.finished[-1] == (ReadOutcome.GAP, (G1,))


async def test_a_list_that_ends_is_not_a_gap(world: World) -> None:
    """A small group whose whole list is newer than the target: the last scroll loads nothing."""
    three = [story(post(n), G1, T0 - 50 * n) for n in range(1, 4)]
    world.driver.navigations.append(nav(GROUP1, group_document(), group_body(*three)))
    first = await world.reader.read(grant(ActionKind.BACKSTOP, group=G1, steps=2))
    assert first.outcome is ReadOutcome.GAP  # unseen posts newer than the target
    world.clock.advance(3600)
    world.driver.navigations.append(nav(GROUP1, group_document(), group_body(*three)))
    again = await world.reader.read(grant(ActionKind.BACKSTOP, group=G1, steps=2))
    assert again.outcome is ReadOutcome.COMPLETE  # all seen, and the list ended


async def test_the_catch_up_targets_the_last_read_that_reached_its_target(world: World) -> None:
    # A first feed read reaches its (bootstrap) target.
    older = [story(post(n), G1, T0 - 9000 - 60 * n) for n in range(1, 4)]
    world.driver.navigations.append(feed_nav(story(post(10), G1, T0 + 300), *older))
    await world.reader.read(grant(ActionKind.FEED, steps=0))
    reached = await world.store.last_read(Surface.FEED)
    assert reached is not None and reached.coverage.target_reached
    # An ordinary read an hour later cannot get back down: a gap, and T_feed moves on for it.
    world.clock.advance(3600)
    fresh = [story(post(20 + n), G2, T0 + 3900 - 60 * n) for n in range(3)]
    world.driver.navigations.append(feed_nav(*fresh))
    gap = await world.reader.read(grant(ActionKind.FEED, steps=0))
    assert gap.outcome is ReadOutcome.GAP
    gap_read = await world.store.last_read(Surface.FEED)
    assert gap_read is not None
    world.clock.advance(2700)
    world.driver.navigations.append(feed_nav(story(post(30), G2, T0 + 6500)))
    await world.reader.read(grant(ActionKind.FEED, steps=0))
    ordinary = await world.store.last_read(Surface.FEED)
    assert ordinary is not None and ordinary.coverage.target == gap_read.started_at
    world.driver.navigations.append(feed_nav(story(post(31), G2, T0 + 6600)))
    await world.reader.read(grant(ActionKind.FEED, steps=0, catch_up=True, deep=True))
    catch_up = await world.store.last_read(Surface.FEED)
    assert catch_up is not None
    assert catch_up.coverage.target == reached.started_at  # the uncovered hour is chased


# -- the resume probe -------------------------------------------------------------------------


async def probe_world(world: World) -> int:
    block = await world.ladder.trip(PageState.BLOCKED, reason="test")
    world.clock.advance(7 * 3600)
    from fbcollector.ladder import ResumeResult

    assert await world.ladder.resume(block.id, "nonce-000001") is ResumeResult.ACCEPTED
    return block.id


async def test_a_clean_probe_lifts_the_block_only_after_the_final_look(world: World) -> None:
    block_id = await probe_world(world)
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0 + 7 * 3600)))
    probe = grant(ActionKind.PROBE, steps=0, block_id=block_id, min_dwell=15.0)
    result = await world.reader.read(probe)
    assert result.outcome is ReadOutcome.COMPLETE
    assert await world.store.current_block() is None
    assert "dwell 15" in world.driver.calls
    assert await world.store.is_seen(G1, post(1))
    assert "block_closed" in world.events.kinds()


async def test_a_feed_that_is_not_chronological_is_a_clean_probe(world: World) -> None:
    """Backstop-only mode may last; a probe that failed on it would climb every block."""
    block_id = await probe_world(world)
    world.driver.navigations.append(
        nav(FEED_URL, feed_document(chrono=False), feed_body(story(post(1), G1, T0 + 7 * 3600)))
    )
    probe = grant(ActionKind.PROBE, steps=0, block_id=block_id, min_dwell=15.0)
    result = await world.reader.read(probe)
    assert result.page_state is PageState.FEED_NOT_CHRONO
    assert await world.store.current_block() is None  # lifted, no new rung
    latest = await world.store.latest_block()
    assert latest is not None and latest.id == block_id
    assert "dwell 15" in world.driver.calls  # judged on the final look
    assert await world.store.kv_get(KV_BACKSTOP_ONLY) == "1"
    assert await world.store.is_seen(G1, post(1))
    assert world.events.kinds() == ["block_opened", "block_closed", "feed_not_chrono"]


async def test_a_late_dialog_fails_the_probe(world: World) -> None:
    block_id = await probe_world(world)
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0 + 7 * 3600)))
    world.driver.dwells.append(
        nav(FEED_URL, None, status=None, dialog_texts=("You're Temporarily Blocked",))
    )
    probe = grant(ActionKind.PROBE, steps=0, block_id=block_id, min_dwell=15.0)
    result = await world.reader.read(probe)
    assert (result.close_chrome, result.planned_close) == (True, False)
    block = await world.store.current_block()
    assert block is not None
    assert block.id != block_id
    assert block.rung == 1
    assert not await world.store.is_seen(G1, post(1))


# -- group sync -------------------------------------------------------------------------------


async def test_a_complete_sync_marks_missing_groups_unverified(world: World) -> None:
    joins = "https://www.facebook.com/groups/joins/"
    world.driver.navigations.append(
        nav(
            joins,
            joins_document(),
            joins_body((G1, "One"), ("400000000000005", "Five")),
            joined_header_count=2,
        )
    )
    result = await world.reader.read(grant(ActionKind.SYNC, steps=6))
    assert result.sync is not None
    assert result.sync.complete
    assert result.sync.added == ("400000000000005",)
    assert set(result.sync.unverified) == {G2, G3}
    assert world.driver.calls.count("scroll") == 0  # has_next_page was false
    g2 = await world.store.get_group(G2)
    assert g2 is not None
    assert g2.state is GroupState.UNVERIFIED
    assert any(k.startswith("unverified:") for k, _ in world.events.notes)
    record = await world.store.last_read(Surface.JOINS)
    assert record is not None
    assert record.stats["sync"]["complete"] is True


async def test_an_incomplete_sync_decides_nothing_about_missing_groups(world: World) -> None:
    joins = "https://www.facebook.com/groups/joins/"
    new = "400000000000005"
    world.driver.navigations.append(
        nav(
            joins,
            joins_document(),
            joins_body((G1, "One"), (new, "Maybe a suggestion"), has_next=True),
            joined_header_count=3,
        )
    )
    world.driver.scrolls.append(scroll(joins, joins_body((G1, "One"), has_next=True)))
    result = await world.reader.read(grant(ActionKind.SYNC, steps=6))
    assert result.sync is not None
    assert not result.sync.complete
    assert world.driver.calls.count("scroll") == 1  # the list stopped growing
    g2 = await world.store.get_group(G2)
    assert g2 is not None
    assert g2.state is GroupState.MEMBER
    fresh = await world.store.get_group(new)
    assert fresh is not None and fresh.state is GroupState.UNVERIFIED  # not opened yet
    assert [k for k, _ in world.events.notes] == ["sync_incomplete"]


# -- the notifications panel ------------------------------------------------------------------

LOGIN = "We noticed a new login from a device or location you don't usually use. Please review."
PASSWORD = "Your password was changed."


def panel(*unread: str, age: str = "6d") -> str:
    """Facebook's notifications panel with a fresh notice and ``unread`` account notices, all
    of one ``age``, or each its own when given as ``"words@age"``."""
    items = "".join(f" Unread {n.partition('@')[0]} {n.partition('@')[2] or age}" for n in unread)
    return (
        "Notifications All Unread New See all Unread Ana Petrova and 11 other friends responded"
        f" to events happening soon. 3m Earlier{items} See previous notifications"
    )


def panel_notes(world: World) -> list[tuple[str, str]]:
    return [(k, t) for k, t in world.events.notes if k.startswith("panel_notice:")]


def fb_age(days: int) -> str:
    """The age Facebook shows an item ``days`` old: days up to six, then whole weeks."""
    return f"{days}d" if days < 7 else f"{days // 7}w"


async def feed_look(world: World, *dialogs: str, n: int = 1) -> ReadResult:
    """One feed read whose page shows ``dialogs``."""
    now = world.clock.time()
    world.driver.navigations.append(
        nav(
            FEED_URL, feed_document(), feed_body(story(post(n), G1, now - 60)), dialog_texts=dialogs
        )
    )
    return await world.reader.read(grant(ActionKind.FEED, steps=0))


@contextlib.asynccontextmanager
async def restarted(world: World) -> AsyncIterator[World]:
    """The collector started again: a new store connection, ladder, reader and alert sink over
    the same state database (kv persists, nothing in memory does)."""
    async with Store(world.tmp / "state.sqlite", clock=world.clock) as store:
        events = Recorder()
        ladder = Ladder(store, world.clock, events=events)
        reader = Reader(
            store=store,
            driver=world.driver,
            scheduler=world.scheduler,
            ladder=ladder,
            events=events,
            clock=world.clock,
            config=ReaderConfig("home-1", world.tmp / "raw", world.tmp / "shots"),
        )
        yield World(
            world.clock, store, world.driver, world.scheduler, ladder, events, reader, world.tmp
        )


async def test_a_login_notice_in_the_panel_is_told_once_and_the_feed_read_goes_on(
    world: World,
) -> None:
    """Block #2 of 2026-10-01: the panel came back with the unread login notice."""
    now = world.clock.time()
    world.driver.navigations.append(
        nav(
            FEED_URL,
            feed_document(),
            feed_body(story(post(1), G1, T0 + 500)),
            dialog_texts=(panel(LOGIN, age="5d"),),
        )
    )
    world.driver.scrolls.append(
        scroll(FEED_URL, feed_body(story(post(2), G1, T0 + 400)), dialog_texts=(panel(LOGIN),))
    )
    result = await world.reader.read(grant(ActionKind.FEED, steps=1))
    assert result.page_state is PageState.OK
    assert not result.close_chrome
    assert await world.store.is_seen(G1, post(1))
    assert await world.store.is_seen(G1, post(2))
    assert await world.store.current_block() is None
    assert (await world.ladder.state()).unknown_streak == 0
    arrival = int(now - 5 * DAY)
    key = panel_notice_key(LOGIN, arrival)
    assert panel_notes(world) == [(key, panel_notice_text(LOGIN, "5d"))]
    assert world.events.dedups[key] == NOTICE_ONCE
    told = await world.store.kv_get_json(KV_PANEL_TOLD + panel_notice_digest(LOGIN))
    assert told == [[arrival, int(now), 0]]
    assert world.events.kinds() == []


async def test_an_old_notice_is_told_once_across_days_and_a_restart(world: World) -> None:
    """The owner, 2026-10-01: an old notice they cannot find in the app is told once, never
    daily, through 5d → 6d → 1w → 2w and a restart of the collector."""
    await feed_look(world, panel(LOGIN, age="5d"))
    assert len(panel_notes(world)) == 1
    for day in range(1, 4):
        world.clock.advance(DAY)
        await feed_look(world, panel(LOGIN, age=fb_age(5 + day)), n=day + 1)
    async with restarted(world) as again:
        for day in range(4, 20):
            again.clock.advance(DAY)
            again.clock.advance(1800)  # reads drift over the day
            await feed_look(again, panel(LOGIN, age=fb_age(5 + day)), n=day + 1)
        assert panel_notes(again) == []
    assert len(panel_notes(world)) == 1


async def test_the_age_rollover_of_one_notice_is_told_once(world: World) -> None:
    """Facebook's coarse ages, read late in each unit: 5d (5.9 days), 6d, 1w (13.9 days)."""
    world.clock.advance(0.9 * DAY)  # the notice is 5.9 days old, shown "5d"
    await feed_look(world, panel(LOGIN, age="5d"))
    world.clock.advance(DAY)
    await feed_look(world, panel(LOGIN, age="6d"), n=2)
    world.clock.advance(DAY)
    await feed_look(world, panel(LOGIN, age="1w"), n=3)
    world.clock.advance(6 * DAY)
    await feed_look(world, panel(LOGIN, age="1w"), n=4)
    world.clock.advance(DAY)
    await feed_look(world, panel(LOGIN, age="2w"), n=5)
    assert len(panel_notes(world)) == 1


@pytest.mark.parametrize("old_unread", [False, True])
async def test_a_new_login_first_seen_days_old_is_told(world: World, old_unread: bool) -> None:
    """Day 0, the old "6d" login notice is told (and the owner
    reads it, or not); day 10, an attacker's login in Facebook's same generic words is first
    seen at "2d" (the collector was blocked, paused or asleep). It is a new notice."""
    start = world.clock.time()
    await feed_look(world, panel(LOGIN, age="6d"))
    world.clock.advance(10 * DAY)
    day_10 = panel(f"{LOGIN}@2d", f"{LOGIN}@2w") if old_unread else panel(LOGIN, age="2d")
    await feed_look(world, day_10, n=2)
    notes = panel_notes(world)
    assert notes == [
        (panel_notice_key(LOGIN, int(start - 6 * DAY)), panel_notice_text(LOGIN, "6d")),
        (panel_notice_key(LOGIN, int(start + 8 * DAY)), panel_notice_text(LOGIN, "2d")),
    ]
    # Day 15, both still unread ("1w", "3w") and a password change: only that is new.
    world.clock.advance(5 * DAY)
    await feed_look(world, panel(f"{LOGIN}@1w", f"{LOGIN}@3w", PASSWORD), n=3)
    assert [k for k, _ in panel_notes(world)][2:] == [
        panel_notice_key(PASSWORD, int(start + 9 * DAY)),
    ]


async def test_a_notice_first_seen_fresh_is_told_once(world: World) -> None:
    """ "2m", then "1d" a day later, then "2d", is one notice."""
    await feed_look(world, panel(LOGIN, age="2m"))
    world.clock.advance(DAY)
    await feed_look(world, panel(LOGIN, age="1d"), n=2)
    world.clock.advance(DAY - 300)
    await feed_look(world, panel(LOGIN, age="1d"), n=3)
    world.clock.advance(3600)
    await feed_look(world, panel(LOGIN, age="2d"), n=4)
    assert [t for _, t in panel_notes(world)] == [panel_notice_text(LOGIN, "2m")]


async def test_a_day_of_slack_absorbs_an_age_off_by_a_day(world: World) -> None:
    """ "Yesterday" reads "1d", yet it may be a calendar day: an item ten minutes old just
    after midnight. A day later it shows "1d", more than its unit away: the day of slack
    keeps it the same notice (so would it a clock stepped between looks)."""
    yesterday = panel(LOGIN).replace(f"{LOGIN} 6d", f"{LOGIN} Yesterday")
    await feed_look(world, yesterday)
    world.clock.advance(DAY + 300)
    await feed_look(world, panel(LOGIN, age="1d"), n=2)
    assert [t for _, t in panel_notes(world)] == [panel_notice_text(LOGIN, "1d")]


async def test_just_now_then_minutes_is_one_notice(world: World) -> None:
    just_now = panel(LOGIN).replace(f"{LOGIN} 6d", f"{LOGIN} Just now")
    await feed_look(world, just_now)
    world.clock.advance(600)
    await feed_look(world, panel(LOGIN, age="10m"), n=2)
    assert [t for _, t in panel_notes(world)] == [panel_notice_text(LOGIN, None)]
    assert "just now (or no age shown)" in panel_notes(world)[0][1]


async def test_two_alike_items_with_different_ages_are_two_notices(world: World) -> None:
    """Two logins are two items of one generic text: each is told, once, and
    no third alert."""
    start = world.clock.time()
    await feed_look(world, panel(f"{LOGIN}@2m", f"{LOGIN}@6d"))
    assert [k for k, _ in panel_notes(world)] == [
        panel_notice_key(LOGIN, int(start - 120)),
        panel_notice_key(LOGIN, int(start - 6 * DAY)),
    ]
    world.clock.advance(DAY)
    await feed_look(world, panel(f"{LOGIN}@1d", f"{LOGIN}@1w"), n=2)
    world.clock.advance(DAY)
    await feed_look(world, panel(f"{LOGIN}@2d", f"{LOGIN}@1w"), n=3)
    assert len(panel_notes(world)) == 2
    # A third login, between the two: told.
    await feed_look(world, panel(f"{LOGIN}@2d", f"{LOGIN}@4d", f"{LOGIN}@1w"), n=4)
    assert len(panel_notes(world)) == 3


@pytest.mark.parametrize("old_first", [True, False])
async def test_the_nearest_told_arrival_pairs_first(world: World, old_first: bool) -> None:
    """Day 1: the old notice now "1w" and a new one "6d", which is within a day of the old
    one's arrival too. The old item pairs with its own arrival, whatever the panel order, and
    the new one is told."""
    await feed_look(world, panel(LOGIN, age="6d"))
    world.clock.advance(DAY)
    both = [f"{LOGIN}@1w", f"{LOGIN}@6d"]
    await feed_look(world, panel(*(both if old_first else both[::-1])), n=2)
    assert [t for _, t in panel_notes(world)] == [
        panel_notice_text(LOGIN, "6d"),
        panel_notice_text(LOGIN, "6d"),
    ]
    assert panel_notes(world)[0][0] != panel_notes(world)[1][0]


async def test_alike_items_of_one_age_are_two_notices(world: World) -> None:
    start = world.clock.time()
    await feed_look(world, panel(LOGIN, PASSWORD, LOGIN))
    arrival = int(start - 6 * DAY)
    assert [k for k, _ in panel_notes(world)] == [
        panel_notice_key(LOGIN, arrival),
        panel_notice_key(PASSWORD, arrival),
        panel_notice_key(LOGIN, arrival, nth=2),
    ]
    world.clock.advance(DAY)
    await feed_look(world, panel(LOGIN, PASSWORD, LOGIN, age="1w"), n=2)
    assert len(panel_notes(world)) == 3


async def test_an_age_like_number_inside_a_notice_neither_cuts_nor_dates_it(
    world: World,
) -> None:
    """ "3 times in 24h." is the notice's, "5d" its age."""
    tries = "Someone tried to log in to your account 3 times in 24h."
    await feed_look(world, panel(tries, age="5d"))
    world.clock.advance(DAY)
    await feed_look(world, panel(tries, age="6d"), n=2)
    assert panel_notes(world) == [
        (panel_notice_key(tries, int(world.clock.time() - 6 * DAY)), panel_notice_text(tries, "5d"))
    ]


@pytest.mark.parametrize(
    ("subscribe", "state"),
    [("IS_SUBSCRIBED", PageState.OK), ("CAN_SUBSCRIBE", PageState.NOT_FOLLOWED)],
)
async def test_each_panel_notice_is_told_on_a_group_page(
    world: World, subscribe: str, state: PageState
) -> None:
    """A flagged page lets the read go on as well (the ladder's FLAG)."""
    world.driver.navigations.append(
        nav(
            GROUP1,
            group_document(subscribe=subscribe),
            group_body(story(post(1), G1, T0)),
            dialog_texts=(panel(LOGIN, PASSWORD),),
        )
    )
    result = await world.reader.read(grant(ActionKind.BACKSTOP, group=G1, steps=0))
    assert result.page_state is state
    assert await world.store.is_seen(G1, post(1))
    assert [t for _, t in panel_notes(world)] == [
        panel_notice_text(LOGIN, "6d"),
        panel_notice_text(PASSWORD, "6d"),
    ]


@pytest.mark.parametrize("bad", ['[[1, 2], "garbage"]', '{"a": 1}', "not json"])
async def test_a_bad_told_record_never_fails_the_read(
    world: World, bad: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Whatever the heads-up bookkeeping meets, the read goes on (the final review)."""
    await world.store.kv_set(KV_PANEL_TOLD + panel_notice_digest(LOGIN), bad)
    world.driver.navigations.append(
        nav(
            FEED_URL,
            feed_document(),
            feed_body(story(post(1), G1, T0 + 500)),
            dialog_texts=(panel(LOGIN),),
        )
    )
    result = await world.reader.read(grant(ActionKind.FEED, steps=0))
    assert result.page_state is PageState.OK
    assert await world.store.is_seen(G1, post(1))
    assert await world.store.current_block() is None
    assert "panel notices of a look skipped" in caplog.text


async def test_the_resume_probe_tells_the_panel_notice_and_lifts_the_block(world: World) -> None:
    block_id = await probe_world(world)
    seen_at = world.clock.time()
    world.driver.navigations.append(
        nav(
            FEED_URL,
            feed_document(),
            feed_body(story(post(1), G1, T0 + 7 * 3600)),
            dialog_texts=(panel(LOGIN),),
        )
    )
    world.driver.dwells.append(nav(FEED_URL, None, status=None))  # the panel has closed
    dwell = world.driver.dwell

    async def slow_dwell(seconds: float) -> Step:
        world.clock.advance(seconds)
        return await dwell(seconds)

    world.driver.dwell = slow_dwell  # type: ignore[method-assign]
    probe = grant(ActionKind.PROBE, steps=0, block_id=block_id, min_dwell=15.0)
    result = await world.reader.read(probe)
    assert result.page_state is PageState.OK
    assert await world.store.current_block() is None
    assert await world.store.is_seen(G1, post(1))
    # Dated by the step that showed it, not by the final look 15 s later.
    told = await world.store.kv_get_json(KV_PANEL_TOLD + panel_notice_digest(LOGIN))
    assert told[0][0] == int(seen_at - 6 * DAY)
    assert told[0][1] >= seen_at + 15
    assert [k for k, _ in panel_notes(world)] == [panel_notice_key(LOGIN, told[0][0])]


async def test_a_probe_that_fails_on_its_final_look_tells_no_panel_notice(world: World) -> None:
    """The notice says "Reading goes on": not beside the probe's own block alert (review)."""
    block_id = await probe_world(world)
    world.driver.navigations.append(
        nav(
            FEED_URL,
            feed_document(),
            feed_body(story(post(1), G1, T0 + 7 * 3600)),
            dialog_texts=(panel(LOGIN),),
        )
    )
    world.driver.dwells.append(
        nav(FEED_URL, None, status=None, dialog_texts=("You're Temporarily Blocked",))
    )
    probe = grant(ActionKind.PROBE, steps=0, block_id=block_id, min_dwell=15.0)
    result = await world.reader.read(probe)
    assert result.close_chrome
    assert await world.store.current_block() is not None
    assert panel_notes(world) == []
    assert await world.store.kv_get(KV_PANEL_TOLD + panel_notice_digest(LOGIN)) is None


@pytest.mark.parametrize(
    "trouble",
    [
        "Your account is at risk. Secure your account.",
        "We've reset your password to keep your account safe.",
        "Your password was reset. Log in with your new password.",
        "Your account has been flagged for review.",
        "Complete the checkpoint to continue.",
    ],
)
async def test_a_trouble_notice_in_the_panel_tells_nothing_and_stops(
    world: World, trouble: str
) -> None:
    world.driver.navigations.append(
        nav(
            FEED_URL,
            feed_document(),
            feed_body(story(post(1), G1, T0)),
            dialog_texts=(panel(LOGIN, trouble),),
        )
    )
    result = await world.reader.read(grant(ActionKind.FEED, steps=0))
    assert result.page_state is PageState.UNKNOWN
    assert result.close_chrome
    assert panel_notes(world) == []
    assert not await world.store.is_seen(G1, post(1))


async def test_a_panel_notice_whose_notify_raises_is_told_at_the_next_look(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """``notify`` itself raising (its alert ledger failing, outside a held read): the read
    never fails for it, nothing is recorded, and the next look tells it."""
    world.events.fail_notify = True
    with caplog.at_level(logging.WARNING):
        result = await feed_look(world, panel(LOGIN))
    assert result.page_state is PageState.OK
    assert await world.store.is_seen(G1, post(1))
    assert "panel notice not sent: OSError" in caplog.text
    assert LOGIN not in caplog.text
    assert await world.store.kv_get(KV_PANEL_TOLD + panel_notice_digest(LOGIN)) is None
    world.events.fail_notify = False
    world.clock.advance(600)
    await feed_look(world, panel(LOGIN), n=2)
    assert [t for _, t in panel_notes(world)] == [panel_notice_text(LOGIN, "6d")]


async def test_a_panel_notice_the_bot_did_not_deliver_is_told_at_the_next_look(
    world: World,
) -> None:
    """The bot failed or rate-limited the alert: nothing is recorded as told (the review of
    the fix: recording when ``notify`` returned made one Telegram outage a year of silence)."""
    world.events.undelivered = True
    await feed_look(world, panel(LOGIN))
    assert len(panel_notes(world)) == 1
    assert await world.store.kv_get(KV_PANEL_TOLD + panel_notice_digest(LOGIN)) is None
    world.events.undelivered = False
    world.clock.advance(600)
    await feed_look(world, panel(LOGIN), n=2)
    assert len(panel_notes(world)) == 2
    assert await world.store.kv_get(KV_PANEL_TOLD + panel_notice_digest(LOGIN)) is not None
    world.clock.advance(600)
    await feed_look(world, panel(LOGIN), n=3)
    assert len(panel_notes(world)) == 2


class Bot:
    """Stands in for the alert bot under the real ``CollectorEvents``: answers ``result``."""

    def __init__(self) -> None:
        self.result = AlertResult.FAILED
        self.sent: list[str] = []

    async def alert(
        self, key: str, text: str, *, critical: bool = False, dedup: float | None = None
    ) -> AlertResult:
        del text, critical, dedup
        self.sent.append(key)
        return self.result


@pytest.mark.parametrize("answer", [AlertResult.FAILED, AlertResult.RATE_LIMITED])
async def test_a_held_panel_notice_is_recorded_only_once_delivered(
    world: World, answer: AlertResult
) -> None:
    """Through the real ``CollectorEvents``, held during the read as the runner holds it: a
    notice the bot does not deliver at the flush is not recorded and is told at the next
    read; one delivered is recorded then, and told once. Within one read, the navigation and
    the scroll show it: one alert (the review of the fix)."""
    bot = Bot()
    bot.result = answer
    events = CollectorEvents(world.store, bot, collector_id="home-1", local_mode=True)
    reader = Reader(
        store=world.store,
        driver=world.driver,
        scheduler=world.scheduler,
        ladder=world.ladder,
        events=events,
        clock=world.clock,
        config=ReaderConfig("home-1", world.tmp / "raw", world.tmp / "shots"),
    )
    digest_key = KV_PANEL_TOLD + panel_notice_digest(LOGIN)

    async def read(n: int) -> None:
        now = world.clock.time()
        world.driver.navigations.append(
            nav(
                FEED_URL,
                feed_document(),
                feed_body(story(post(n), G1, now - 60)),
                dialog_texts=(panel(LOGIN),),
            )
        )
        later = feed_body(story(post(n + 1), G1, now - 90))
        world.driver.scrolls.append(scroll(FEED_URL, later, dialog_texts=(panel(LOGIN),)))
        before = await world.store.kv_get(digest_key)
        events.hold_alerts()
        await reader.read(grant(ActionKind.FEED, steps=1))
        assert await world.store.kv_get(digest_key) == before  # held: not yet delivered
        await events.flush_alerts()

    await read(1)
    assert len(bot.sent) == 1
    assert await world.store.kv_get(digest_key) is None
    bot.result = AlertResult.SENT
    world.clock.advance(600)
    await read(3)
    assert len(bot.sent) == 2
    assert (await world.store.kv_get_json(digest_key))[0][0] == int(world.clock.time() - 6 * DAY)
    world.clock.advance(600)
    await read(5)
    assert len(bot.sent) == 2


def read_panel(*items: str) -> str:
    """The panel with ``items`` as they stand: "Unread <words> <age>" or "<words> <age>"."""
    return (
        "Notifications All Unread New See all Unread Ana Petrova and 11 other friends responded"
        f" to events happening soon. 3m Earlier {' '.join(items)} See previous notifications"
    )


async def test_a_new_login_first_seen_a_week_old_after_the_old_one_was_read_is_told(
    world: World,
) -> None:
    """The review of the fix: the owner's login notice is told at "6d" and read; a second
    login half a day later is first seen at "1w", a week on, within a week and a day of the
    told arrival. The panel showed the old notice read in between: it stands for no unread
    item, and the new one is told, once."""
    await feed_look(world, panel(LOGIN, age="6d"))
    world.clock.advance(DAY / 2)
    await feed_look(world, read_panel(f"{LOGIN} 6d"), n=2)  # the owner read it
    world.clock.advance(7.5 * DAY)
    await feed_look(world, read_panel(f"Unread {LOGIN} 1w"), n=3)
    assert [t for _, t in panel_notes(world)] == [
        panel_notice_text(LOGIN, "6d"),
        panel_notice_text(LOGIN, "1w"),
    ]
    async with restarted(world) as again:
        for day in range(1, 4):
            again.clock.advance(DAY)
            await feed_look(again, read_panel(f"Unread {LOGIN} 1w", f"{LOGIN} 2w"), n=3 + day)
        assert panel_notes(again) == []


async def test_an_old_read_notice_in_the_same_look_lets_the_new_one_be_told(
    world: World,
) -> None:
    """The same, with no look in between: the look that first shows the new login "1w" shows
    the old one read, "2w". The read one pairs with the told arrival (nearest, and first on
    a tie), so the new one is told."""
    await feed_look(world, panel(LOGIN, age="6d"))
    world.clock.advance(8 * DAY)
    await feed_look(world, read_panel(f"Unread {LOGIN} 1w", f"{LOGIN} 2w"), n=2)
    assert [t for _, t in panel_notes(world)] == [
        panel_notice_text(LOGIN, "6d"),
        panel_notice_text(LOGIN, "1w"),
    ]
    told = await world.store.kv_get_json(KV_PANEL_TOLD + panel_notice_digest(LOGIN))
    assert [row[2] for row in told] == [1, 0]  # the old one read, the new one not
    # An old unread notice is still told once beside older read ones of its words.
    world.clock.advance(DAY)
    await feed_look(world, read_panel(f"Unread {LOGIN} 1w", f"{LOGIN} 2w"), n=3)
    assert len(panel_notes(world)) == 2


async def test_on_a_tie_the_read_item_pairs_first(world: World) -> None:
    """Two alike items both "1w", one read, as near the told arrival: the unread one is
    told. It may be the new login; the read one the owner has seen in the app."""
    await feed_look(world, panel(LOGIN, age="6d"))
    world.clock.advance(8 * DAY)
    for both in ([f"Unread {LOGIN} 1w", f"{LOGIN} 1w"], [f"{LOGIN} 1w", f"Unread {LOGIN} 1w"]):
        async with restarted(world) as again:
            await feed_look(again, read_panel(*both), n=2)
            assert [t for _, t in panel_notes(again)] == [panel_notice_text(LOGIN, "1w")]
            key = KV_PANEL_TOLD + panel_notice_digest(LOGIN)
            told = await again.store.kv_get_json(key)
            assert [row[2] for row in told] == [1, 0]  # the told one taken as read
            await again.store.kv_set_json(key, [[*told[0][:2], 0]])  # again, in the other order


async def test_the_told_record_is_bounded(world: World) -> None:
    """At most ``PANEL_TOLD_KEEP`` told notices per words, none told over 400 days ago; the
    told time ages a record, not the arrival (a "1y" item stays told)."""
    now = int(world.clock.time())
    key = KV_PANEL_TOLD + panel_notice_digest(LOGIN)
    old = [[now - 900 * 86400, now - 401 * 86400]]
    many = [[now - (30 + n) * 86400, now - n * 3600] for n in range(25)]
    await world.store.kv_set_json(key, old + many)
    await feed_look(world, panel(LOGIN, age="2m"))
    told = await world.store.kv_get_json(key)
    assert len(told) == 20
    assert told[-1] == [now - 120, now, 0]
    assert [*old[0], 0] not in told
    assert told[:-1] == [[*n, 0] for n in sorted(many, key=lambda n: n[1])[-19:]]
    # A notice over 400 days old by its arrival, told 10 days ago and still shown "1y", is
    # not told again, and stays recorded when another one is.
    year = [now - 450 * 86400, now - 10 * 86400]
    await world.store.kv_set_json(key, [year])
    world.events.notes.clear()
    world.clock.advance(60)
    await feed_look(world, panel(f"{LOGIN}@1y", f"{LOGIN}@5m"), n=2)
    assert [t for _, t in panel_notes(world)] == [panel_notice_text(LOGIN, "5m")]
    assert await world.store.kv_get_json(key) == [[*year, 0], [now + 60 - 300, now + 60, 0]]


def test_the_panel_notice_key_and_text() -> None:
    digest = panel_notice_digest(LOGIN)
    assert digest == panel_notice_digest(f"  {LOGIN.upper()}  ")  # case and spacing ignored
    assert digest != panel_notice_digest(PASSWORD)
    assert len(digest) == 12
    assert panel_notice_key(LOGIN, 1_789_000_000) == f"panel_notice:{digest}:1789000000"
    assert panel_notice_key(LOGIN, 1_789_000_000, nth=2) == f"panel_notice:{digest}:1789000000:2"
    assert panel_notice_text(LOGIN, "6d") == (
        f"🔔 FB NOTICE: unread on the account, 6d ago: «{LOGIN}»\n"
        "Reading goes on. This notice is told once. A later one in the same words (a new"
        " login) is told on its own, but one first seen a week or more old, close to this one,"
        " may not be: look at the account's notifications now and then. If the login or the"
        " change was not yours, `fbcollector pause` and secure the account by hand."
    )


# -- parser health ----------------------------------------------------------------------------


async def test_three_reads_that_accept_nothing_break_the_parser(world: World) -> None:
    broken = {"__typename": "Story", "comet_sections": {"x": 1}}  # post-shaped, no post id
    for n in range(3):
        world.driver.navigations.append(
            nav(FEED_URL, feed_document(), feed_body(broken, broken, broken, story(post(n))))
        )
        await world.reader.read(grant(ActionKind.FEED, steps=0))
    assert await world.store.kv_get(KV_PARSER_BROKEN) is not None
    assert world.events.kinds().count("parser_broken") == 1


def wordless(post_id: str) -> dict[str, Any]:
    """A post with no message, no shared story and no card: empty text nothing explains."""
    return story(post_id, comet_sections={"content": {"story": {"message": None}}})


async def test_a_post_read_again_counts_once_in_the_empty_text_alert(world: World) -> None:
    """Three reads of the same 11 posts (the phase-0 "11 of 33"): 2 without words is 2 of 11
    distinct posts, no alert; a third makes 3 of 11, one alert, keyed by its kind."""
    posts = [story(post(n)) for n in range(9)]
    for _ in range(3):
        world.driver.navigations.append(
            nav(FEED_URL, feed_document(), feed_body(*posts, wordless(post(9)), wordless(post(10))))
        )
        await world.reader.read(grant(ActionKind.FEED, steps=0))
    assert [k for k, _ in world.events.notes if k.startswith("parser_warning")] == []
    posts[0] = wordless(post(0))
    for _ in range(3):
        world.driver.navigations.append(
            nav(FEED_URL, feed_document(), feed_body(*posts, wordless(post(9)), wordless(post(10))))
        )
        await world.reader.read(grant(ActionKind.FEED, steps=0))
    warnings = [(k, t) for k, t in world.events.notes if k.startswith("parser_warning")]
    assert warnings[0] == (
        "parser_warning:empty_text",
        "⚠️ PARSER: 3 of 11 distinct posts with empty text nothing explains over the last reads.",
    )
    assert {k for k, _ in warnings} == {"parser_warning:empty_text"}
    assert await world.store.kv_get(KV_PARSER_BROKEN) is None


async def test_the_parser_history_keeps_posts_and_drops_old_entries(world: World) -> None:
    """Entries written before the history kept posts are dropped: the window starts afresh."""
    old = {"found": 10, "accepted": 0, "dom_posts": 0, "no_created_at": 0, "empty_text": 0}
    await world.store.kv_set(KV_PARSER_HISTORY, json.dumps([old, old, old]))
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0), story(post(2), G1, T0)))
    await world.reader.read(grant(ActionKind.FEED, steps=0))
    history = json.loads(await world.store.kv_get(KV_PARSER_HISTORY) or "[]")
    assert history == [
        {
            "found": 2,
            "accepted": 2,
            "dom_posts": 3,  # feed_nav's page shows three posts
            "failures": 0,
            "posts": {post(1): [1, 0, None], post(2): [1, 0, None]},
        }
    ]
    assert await world.store.kv_get(KV_PARSER_BROKEN) is None  # three old reads of 0 of 10


async def test_state_that_keeps_reads_shut_is_fsynced(world: World) -> None:
    from .trace_support import durable_writes, traced

    broken = {"__typename": "Story", "comet_sections": {"x": 1}}  # post-shaped, no post id
    async with traced(world.store) as statements:
        world.driver.navigations.append(
            nav(FEED_URL, feed_document(chrono=False), feed_body(story(post(1), G1, T0)))
        )
        await world.reader.read(grant(ActionKind.FEED))
        world.driver.navigations.append(nav(GROUP1, group_document(join_state="NOT_MEMBER")))
        await world.reader.read(grant(ActionKind.BACKSTOP, group=G1))
        for n in range(3):
            world.driver.navigations.append(
                nav(FEED_URL, feed_document(), feed_body(broken, broken, story(post(10 + n))))
            )
            await world.reader.read(grant(ActionKind.FEED, steps=0))
    writes = durable_writes(statements)
    assert f"kv:{KV_BACKSTOP_ONLY}" in writes
    assert f"kv:{KV_PARSER_BROKEN}" in writes
    assert f'flags:{G1}:["{FLAG_PAUSED}"]' in writes


def test_page_facts_map_the_parser_state() -> None:
    from fbcollector.parser import ReadParser

    parser = ReadParser(Surface.FEED)
    parser.parse(feed_nav(story(post(1))))
    facts = page_facts(parser)
    assert facts.user_id is not None
    assert facts.feed_preloader is not None
    assert facts.feed_preloader.chronological
    assert facts.stories == 1
    joins = ReadParser(Surface.JOINS)
    assert page_facts(joins).stories == 0


async def test_raw_steps_round_trip(world: World) -> None:
    world.driver.navigations.append(feed_nav(story(post(1), G1, T0)))
    await world.reader.read(grant(ActionKind.FEED, steps=0))
    saved = list(iter_steps(world.tmp / "raw", since=T0))
    assert saved[0].surface is Surface.FEED
    assert saved[0].step.graphql[0].body.startswith(b"for (;;);")
    assert list(iter_steps(world.tmp / "raw", since=T0 + 10**6)) == []
    assert list(iter_steps(world.tmp / "missing", since=0)) == []
    (world.tmp / "raw" / "0000000001-junk.step.json.gz").write_bytes(b"not gzip")
    assert len(list(iter_steps(world.tmp / "raw", since=0))) == 2
    assert json.loads(json.dumps(saved[0].step.dom.see_more)) == 0


# -- the machine going to sleep mid-read ------------------------------------------------------


class AbortingDriver(FakeDriver):
    """Sets ``abort`` at one point of the read (the OS announcing a sleep), maybe then fails
    the step as a tab closed under it would."""

    def __init__(self, point: str, abort: asyncio.Event, *, fail: bool = False) -> None:
        super().__init__()
        self.point = point
        self.abort = abort
        self.fail = fail

    def _hit(self, point: str) -> None:
        if point == self.point:
            self.abort.set()
            if self.fail:
                raise RuntimeError("Target page, context or browser has been closed")

    async def navigate(self, url: str) -> Step:
        step = await super().navigate(url)
        self._hit("navigate")
        return step

    async def cookie_names(self, url: str = "https://www.facebook.com/") -> frozenset[str]:
        self._hit("judging")
        if self.abort.is_set():
            return frozenset()  # what a Chrome being closed used to answer
        return await super().cookie_names(url)

    async def scroll_step(self) -> Step:
        step = await super().scroll_step()
        self._hit("scroll")
        return step

    async def dwell(self, seconds: float) -> Step:
        step = await super().dwell(seconds)
        self._hit("dwell")
        return step


@pytest.mark.parametrize(
    ("point", "fail", "navigated", "judged"),
    [
        ("before", False, 0, 0),
        ("navigate", False, 1, 0),
        ("navigate", True, 1, 0),
        # The step came back whole, but the sleep lands while its cookies are read: they are
        # a closing Chrome's (none, or an error), so the step is not judged either.
        ("judging", False, 1, 0),
        ("judging", True, 1, 0),
        ("scroll", False, 1, 1),
        ("dwell", False, 1, 2),
    ],
)
async def test_a_sleep_mid_read_ends_it_and_judges_nothing_after(
    world: World, point: str, fail: bool, navigated: int, judged: int
) -> None:
    abort = asyncio.Event()
    driver = AbortingDriver(point, abort, fail=fail)
    driver.navigations.append(feed_nav(story(post(1), G1, T0)))
    driver.scrolls.append(scroll(FEED_URL, feed_body(story(post(2), G1, T0 - 60))))
    reader = world.reader
    reader._driver = driver
    if point == "before":
        abort.set()
    result = await reader.read(grant(ActionKind.FEED, steps=1), abort=abort)
    assert result.outcome is ReadOutcome.ABORTED
    assert (result.close_chrome, result.planned_close) == (True, True)  # a sleep: planned
    assert result.reason == "the machine is going to sleep"
    assert sum(c.startswith("navigate") for c in driver.calls) == navigated
    assert "park" not in driver.calls  # Chrome is being closed, not parked
    assert await world.store.current_block() is None
    assert world.events.kinds() == []
    ladder = await world.store.kv_get_json("ladder") or {}
    assert not ladder.get("backoff_until")
    assert not ladder.get("unknown_at")
    read = await world.store.last_read(Surface.FEED)
    assert read is not None
    assert len(read.stats["verdicts"]) == judged
