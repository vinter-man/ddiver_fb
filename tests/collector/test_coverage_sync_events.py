"""Stop rules and targets, the group-sync rules, and the event → alert path."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from fbcollector.alerts import AlertResult, NullAlerts
from fbcollector.clock import FakeClock
from fbcollector.coverage import (
    BOOTSTRAP_LOOKBACK,
    CoverageTracker,
    backstop_target,
    feed_target,
    second_oldest,
)
from fbcollector.events import CollectorEvents
from fbcollector.joins import JoinedGroups
from fbcollector.model import GroupMeta, GroupState, PageState, ParsedStory, Surface
from fbcollector.parser import PageInfo
from fbcollector.scheduler import FLAG_PAUSED
from fbcollector.store import Store
from fbcollector.sync import apply_sync, is_complete, local_monitored
from fbwire.models import EventKind

NOW = 1_790_000_000.0
G1, G2, G3 = "400000000000001", "400000000000002", "400000000000003"


def st(
    n: int,
    created: float | None,
    *,
    batch: int = 1,
    group: str | None = G1,
    highlight: bool = False,
    anchor: bool = False,
) -> ParsedStory:
    return ParsedStory(
        post_id=str(500 + n),
        group_id=group,
        created_at=None if created is None else int(created),
        url=None,
        text="x",
        text_path="message",
        position=n,
        batch=batch,
        is_highlight=highlight,
        is_anchor=anchor,
    )


def reached(tracker: CoverageTracker) -> bool:
    return tracker.reached  # a call, so mypy does not narrow the property between asserts


def test_second_oldest() -> None:
    assert second_oldest([]) is None
    assert second_oldest([5]) == 5
    assert second_oldest([9, 3, 7]) == 7


def test_feed_rule_needs_old_batch_and_a_seen_streak() -> None:
    target = NOW - 3600
    tracker = CoverageTracker(Surface.FEED, target, NOW)
    tracker.add([st(0, NOW - 60), st(1, NOW - 120)], set())
    assert not reached(tracker)
    assert tracker.gap
    old = [st(2 + i, target - 3600 - i, batch=2) for i in range(4)]
    tracker.add(old, set())
    assert not reached(tracker)  # old enough, but nothing seen: maybe a hole in coverage
    tracker.add([st(9, target - 7200, batch=3), st(10, target - 7300, batch=3)], set())
    assert not reached(tracker)
    seen = {"511", "512", "513"}
    tracker.add([st(11 + i, target - 8000 - i, batch=4) for i in range(3)], seen)
    assert tracker.streak == 3
    assert reached(tracker)
    coverage = tracker.coverage()
    assert coverage.target_reached
    assert coverage.newest == int(NOW - 60)
    assert coverage.oldest == int(target - 8001)
    assert not tracker.gap


def test_highlights_anchors_and_undated_stories_do_not_count() -> None:
    tracker = CoverageTracker(Surface.GROUP, NOW, NOW)
    tracker.add(
        [
            st(0, NOW - 99999, highlight=True),
            st(1, NOW - 99999, anchor=True),
            st(2, None),
            st(3, NOW - 10),
        ],
        set(),
    )
    assert tracker.oldest == int(NOW - 10)
    assert not tracker.reached
    empty = CoverageTracker(Surface.GROUP, NOW, NOW)
    assert empty.oldest is None
    assert empty.last_batch is None
    assert empty.gap_groups() == []
    assert not empty.gap


def test_backstop_rule_ignores_the_seen_set_and_bootstrap_waives_the_streak() -> None:
    backstop = CoverageTracker(Surface.GROUP, NOW - 3600, NOW)
    backstop.add([st(0, NOW - 9000), st(1, NOW - 9100)], set())
    assert backstop.reached
    bootstrap = CoverageTracker(Surface.FEED, NOW - 3600, NOW, bootstrap=True)
    bootstrap.add([st(0, NOW - 9000), st(1, NOW - 9100)], set())
    assert bootstrap.reached


def test_future_stamps_are_clamped_and_gap_groups_are_unique() -> None:
    tracker = CoverageTracker(Surface.FEED, NOW - 600, NOW)
    tracker.add(
        [
            st(0, NOW + 10**6, group=G1),
            st(1, NOW - 10, group=G2),
            st(2, NOW - 20, group=G1),
            st(3, NOW - 30, group=None),
        ],
        set(),
    )
    assert tracker.coverage().newest == int(NOW + 300)
    assert tracker.gap_groups() == [G1, G2]


def test_targets() -> None:
    assert feed_target(last_covered_start=NOW - 100, baselines=[0.0], now=NOW) == (NOW - 100, False)
    target, bootstrap = feed_target(last_covered_start=None, baselines=[NOW - 86400], now=NOW)
    assert bootstrap
    assert target == NOW - BOOTSTRAP_LOOKBACK
    assert feed_target(last_covered_start=None, baselines=[], now=NOW) == (NOW, True)
    assert backstop_target(last_covered_start=None, baseline=NOW - 60, now=NOW, novelty=99) == (
        NOW - 60
    )
    # A re-monitored group: its old backstop is older than its new baseline.
    assert (
        backstop_target(last_covered_start=NOW - 5000, baseline=NOW - 60, now=NOW, novelty=99999)
        == NOW - 60
    )
    assert backstop_target(last_covered_start=NOW - 10**7, baseline=0, now=NOW, novelty=100) == (
        NOW - 100
    )


# -- sync ---------------------------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    async with Store(tmp_path / "state.sqlite", clock=FakeClock(start=NOW)) as opened:
        yield opened


def joined(*ids: str) -> JoinedGroups:
    return JoinedGroups(tuple(GroupMeta(id=g, name=f"n{g[-1]}", join_state="MEMBER") for g in ids))


DONE = PageInfo("joined_groups", False, None)


def test_is_complete() -> None:
    assert is_complete(2, 2, DONE)
    assert not is_complete(2, 3, DONE)
    assert not is_complete(2, None, DONE)
    assert not is_complete(2, 2, PageInfo("joined_groups", True, "c"))
    assert not is_complete(2, 2, None)


async def test_sync_lifecycle_unverified_left_and_relisted(store: Store) -> None:
    first = await apply_sync(store, joined(G1, G2, G3), header_count=3, page_info=DONE)
    assert first.complete
    assert first.added == (G1, G2, G3)
    # G2's page said "not a member" before it disappeared from the list.
    await store.upsert_group(GroupMeta(id=G2), state=GroupState.LOST)
    await store.set_group_flags(G2, {FLAG_PAUSED})
    second = await apply_sync(store, joined(G1), header_count=1, page_info=DONE)
    assert set(second.unverified) == {G3}  # G2 stays lost, G3 was never checked
    assert second.left == ()
    third = await apply_sync(store, joined(G1), header_count=1, page_info=DONE)
    assert third.left == (G2,)
    assert third.unverified == ()
    g2 = await store.get_group(G2)
    g3 = await store.get_group(G3)
    assert g2 is not None and g2.state is GroupState.LEFT
    assert g3 is not None and g3.state is GroupState.UNVERIFIED
    fourth = await apply_sync(store, joined(G1, G2, G3), header_count=3, page_info=DONE)
    assert set(fourth.relisted) == {G2, G3}
    g2 = await store.get_group(G2)
    assert g2 is not None
    assert g2.state is GroupState.MEMBER
    assert FLAG_PAUSED not in g2.flags
    assert fourth.summary()["relisted"] == [G2, G3]


async def test_incomplete_sync_touches_only_what_it_saw(store: Store) -> None:
    await apply_sync(store, joined(G1, G2), header_count=2, page_info=DONE)
    partial = await apply_sync(store, joined(G1), header_count=5, page_info=DONE)
    assert not partial.complete
    g2 = await store.get_group(G2)
    assert g2 is not None
    assert g2.state is GroupState.MEMBER
    assert g2.missing_syncs == 0
    nothing = await apply_sync(store, None, header_count=None, page_info=None)
    assert nothing.listed == 0
    assert nothing.has_next_page is None


G9 = "400000000000099"


async def test_only_a_complete_sync_makes_a_member(store: Store, tmp_path: Path) -> None:
    """A suggestion or a stale entry of an incomplete sync is never opened (DESIGN)."""
    await apply_sync(store, joined(G1, G2), header_count=2, page_info=DONE)
    await store.upsert_group(GroupMeta(id=G2), state=GroupState.LOST)
    await store.set_group_flags(G2, {FLAG_PAUSED})
    partial = await apply_sync(
        store, joined(G1, G2, G9), header_count=7, page_info=PageInfo("x", True, "c")
    )
    assert not partial.complete
    assert partial.pending == (G9,)
    assert partial.added == partial.relisted == ()
    g2, g99 = await store.get_group(G2), await store.get_group(G9)
    assert g2 is not None and g2.state is GroupState.LOST and FLAG_PAUSED in g2.flags
    assert g99 is not None and g99.state is GroupState.UNVERIFIED
    assert g99.name == "n9"  # known by name, not opened
    monitored = await local_monitored(store, tmp_path / "monitored.toml")
    assert [m.group_id for m in monitored] == [G1]
    # The next complete sync that lists them makes them members.
    full = await apply_sync(store, joined(G1, G2, G9), header_count=3, page_info=DONE)
    assert full.complete
    assert set(full.relisted) == {G2, G9}
    assert {m.group_id for m in await local_monitored(store, tmp_path / "m.toml")} == {
        G1,
        G2,
        G9,
    }


async def test_local_monitored_follows_the_file_or_every_member(
    store: Store, tmp_path: Path
) -> None:
    await apply_sync(store, joined(G1, G2, G3), header_count=3, page_info=DONE)
    await store.upsert_group(GroupMeta(id=G3), state=GroupState.LOST)
    path = tmp_path / "monitored.toml"
    default = await local_monitored(store, path)
    assert [m.group_id for m in default] == [G1, G2]
    path.write_text(f'[[group]]\nid = "{G2}"\nlistings = true\n[[group]]\nid = "{G3}"\n')
    chosen = await local_monitored(store, path)
    assert [(m.group_id, m.listings) for m in chosen] == [(G2, True)]
    assert await store.monitored_ids() == {G2}


# -- events -------------------------------------------------------------------------------------


async def test_the_outbox_level_is_an_event_on_its_rising_edge_only(store: Store) -> None:
    from fbcollector.events import outbox_edge

    rising = [await outbox_edge(store, depth, 100) for depth in (10, 79, 80, 95, 100, 81)]
    assert rising == [False, False, True, False, False, False]
    assert not await outbox_edge(store, 50, 100)  # below the mark again: re-armed
    assert await outbox_edge(store, 90, 100)


async def test_the_quarantine_is_one_durable_event_while_any_post_waits(store: Store) -> None:
    """The first post in quarantine is an event; more while it lasts are
    not; an empty quarantine re-arms it. The detail and the alert carry no post text."""
    from fbcollector.events import KV_QUARANTINE, quarantine_edge
    from fbcollector.model import Sighting, Surface
    from fbcollector.store import MarkResult

    from .trace_support import durable_writes, traced

    def seen(post_id: str) -> Sighting:
        return Sighting(post_id=post_id, group_id=G1, surface=Surface.FEED)

    assert await quarantine_edge(store) is None
    await store.quarantine(seen("11"), errors=["url:value_error"])
    async with traced(store) as statements:
        detail = await quarantine_edge(store)
    assert detail == "1 post(s) did not fit the wire model: url:value_error"
    assert f"kv:{KV_QUARANTINE}" in durable_writes(statements)  # a restart does not repeat it
    await store.quarantine(seen("12"), errors=["created_at:greater_than_equal"])
    assert await quarantine_edge(store) is None  # still waiting: no second event
    for post_id in ("11", "12"):
        marked = await store.mark_seen(seen(post_id), outcome="stale")
        assert marked == MarkResult(new=True, outbox_depth=0)
    assert await quarantine_edge(store) is None  # empty: re-armed, nothing to say
    assert await store.kv_get(KV_QUARANTINE) is None
    await store.quarantine(seen("13"), errors=["url:value_error"])
    assert await quarantine_edge(store) is not None
    alerts = NullAlerts()
    events = CollectorEvents(store, alerts, collector_id="home-1", local_mode=False)
    await events(EventKind.ITEMS_INVALID, detail="1 post(s) did not fit the wire model: url:x")
    assert [e.kind for e in await store.pending_events()] == [EventKind.ITEMS_INVALID]
    ((key, text),) = alerts.sent
    assert key == "items_invalid:"
    assert "ITEMS_INVALID: 1 post(s)" in text and "fbcollector replay --since 2d" in text


class Answering:
    """An alert bot that answers each alert from ``answers``, in order."""

    def __init__(self, *answers: AlertResult) -> None:
        self.answers = list(answers)
        self.sent: list[str] = []

    async def alert(
        self, key: str, text: str, *, critical: bool = False, dedup: float | None = None
    ) -> AlertResult:
        del text, critical, dedup
        self.sent.append(key)
        return self.answers.pop(0)


async def test_on_sent_runs_only_for_a_delivered_alert(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    """A panel notice is recorded as told from ``on_sent``: after the bot delivered the alert
    (sent, a duplicate of one sent, or logged with no bot), never after a failed or
    rate-limited post; held during a read, it runs at the flush (the review of the fix)."""
    results = list(AlertResult)
    bot = Answering(*results, AlertResult.SENT, AlertResult.SENT)
    events = CollectorEvents(store, bot, collector_id="home-1", local_mode=True)
    done: list[str] = []

    def record(key: str) -> Callable[[], Awaitable[None]]:
        async def on_sent() -> None:
            done.append(key)

        return on_sent

    for result in results:
        await events.notify(result.value, "text", on_sent=record(result.value))
    assert done == ["sent", "duplicate", "disabled"]
    done.clear()
    events.hold_alerts()

    async def broken() -> None:
        raise OSError("disk full")

    await events.notify("first", "text", on_sent=broken)
    await events.notify("second", "text", on_sent=record("second"))
    assert done == []  # held
    with caplog.at_level(logging.WARNING):
        await events.flush_alerts()
    assert done == ["second"]  # a failed record does not stop the next alert
    assert "alert first delivered, its record failed: OSError" in caplog.text


async def test_events_queue_for_the_brain_and_alert_the_owner(store: Store) -> None:
    alerts = NullAlerts()
    remote = CollectorEvents(store, alerts, collector_id="home-1", local_mode=False)
    block = await store.open_block(
        state=PageState.BLOCKED, reason="url:checkpoint", until=None, rung=3
    )
    await remote(EventKind.BLOCK_OPENED, block_id=block.id, detail="x")
    await remote(EventKind.LOGGED_OUT, block_id=block.id, detail="x")
    await remote(EventKind.GROUP_LOST, group_id=G1, group_name="Flats", detail="join_state")
    await remote(EventKind.BLOCK_CLOSED, block_id=block.id, detail="clean")
    await remote(EventKind.OUTBOX_HIGH, detail="4000")
    await remote(EventKind.BLOCK_OPENED, block_id=999, detail="gone")
    pending = await store.pending_events()
    assert [e.kind for e in pending] == [
        EventKind.BLOCK_OPENED,
        EventKind.LOGGED_OUT,
        EventKind.GROUP_LOST,
        EventKind.BLOCK_CLOSED,
        EventKind.OUTBOX_HIGH,
        EventKind.BLOCK_OPENED,
    ]
    keys = [k for k, _ in alerts.sent]
    assert keys == [
        f"block_opened:{block.id}",
        f"group_lost:{G1}",
        f"block_closed:{block.id}",
        "outbox_high:",
        "block_opened:999",
    ]
    texts = [t for _, t in alerts.sent]
    assert "fbcollector resume" in texts[0]
    assert "Flats" in texts[1]
    assert "lifted" in texts[2]

    held = CollectorEvents(store, alerts, collector_id="home-1", local_mode=False)
    sent = len(alerts.sent)
    held.hold_alerts()
    await held(EventKind.GROUP_LOST, group_id=G2, detail="members only")
    await held.notify("note", "later")
    assert len(alerts.sent) == sent  # held back while a read is under way
    assert (await store.pending_events())[-1].kind is EventKind.GROUP_LOST  # queued at once
    await held.flush_alerts()
    assert [k for k, _ in alerts.sent[sent:]] == [f"group_lost:{G2}", "note"]
    await held.notify("after", "now")  # no longer holding
    assert alerts.sent[-1][0] == "after"
    await store.ack_events([e.event_id for e in await store.pending_events()][-1:])

    local = CollectorEvents(store, alerts, collector_id="home-1", local_mode=True)
    await local(EventKind.PARSER_BROKEN, detail="accepted 0 of 9")
    await local.notify("k", "text")
    assert len(await store.pending_events()) == 6  # nothing queued in local mode
    assert alerts.sent[-2][1].startswith("⚠️ PARSER_BROKEN")
    assert alerts.sent[-1] == ("k", "text")
