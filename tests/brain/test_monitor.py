"""The collector as the brain sees it: states, episodes, offline, events, silence."""

from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
from typing import Any

import pytest

from ddiver_core.storage import Storage
from ddiver_core_testing import FakeClock
from ddiver_fb.control import ControlDesk, LeaseOutcome
from ddiver_fb.groups import GroupBook, SyncReport
from ddiver_fb.monitor import (
    CollectorMonitor,
    effective_state,
    format_duration,
    idle_grace,
)
from ddiver_fb.store import FbStore
from fbwire import (
    MAX_MONITORED,
    CollectorState,
    Event,
    EventKind,
    EventsRequest,
    GroupsSnapshot,
    GroupState,
)
from tests.brain.helpers import (
    CID,
    EPOCH0,
    G1,
    G2,
    MOD,
    OWNER,
    Outbox,
    block,
    epoch,
    info,
    seed_groups,
    snapshot,
    status,
    until,
)

S = CollectorState


def events(*items: tuple[int, EventKind, dict[str, Any]], collector_id: str = CID) -> EventsRequest:
    return EventsRequest(
        collector_id=collector_id,
        events=tuple(
            Event(collector_id=collector_id, event_id=eid, at=EPOCH0, kind=kind, **kw)
            for eid, kind, kw in items
        ),
    )


async def report(monitor: CollectorMonitor, clock: FakeClock, state: S = S.OK, **kw) -> None:
    await monitor.on_status(status(state, sent_at=epoch(clock), **kw))
    await monitor.idle()


async def deliver(monitor: CollectorMonitor, request: EventsRequest) -> list[int]:
    """``on_events`` and then whatever its nudge sent (the request itself sends nothing)."""
    acked = await monitor.on_events(request)
    await monitor.idle()
    return acked


async def apply(monitor: CollectorMonitor, sync: GroupsSnapshot) -> SyncReport | None:
    report = await monitor.on_snapshot(sync)
    await monitor.idle()
    return report


class TestHelpers:
    @pytest.mark.parametrize(
        ("seconds", "lang", "text"),
        [
            (10, "en", "1 min"),
            (45 * 60, "en", "45 min"),
            (90 * 60, "en", "1.5 h"),
            (2 * 3600, "uk", "2 год"),
            (50 * 3600, "ru", "2 д"),
        ],
    )
    def test_format_duration(self, seconds: float, lang: str, text: str):
        assert format_duration(seconds, lang) == text

    def test_no_row_or_no_status_is_no_state(self):
        assert effective_state(None, now=EPOCH0, offline_after=1200) is None


class TestStateAlerts:
    async def test_a_block_is_alerted_once_per_block(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox
    ):
        await report(monitor, clock)
        assert told.sent == []
        await report(monitor, clock, S.BLOCKED, block=block(17, until=EPOCH0 + 6 * 3600))
        await report(monitor, clock, S.BLOCKED, block=block(17, until=EPOCH0 + 6 * 3600))
        assert len(told.to(OWNER)) == 1
        text = told.to(OWNER)[0]
        assert "block #17: suspect automated behaviour" in text and "/resume" in text
        assert "(Europe/Kyiv)" in text  # in the owner's own zone
        await report(monitor, clock, S.BLOCKED, block=block(18, until=None, rung=3))
        assert len(told.to(OWNER)) == 2
        assert "last rung" in told.to(OWNER)[1]

    async def test_back_to_ok_is_said_once_and_neutral_states_say_nothing(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox
    ):
        await report(monitor, clock, S.PARSER_BROKEN, detail="0 of 12 stories parsed")
        assert told.to(OWNER) == [
            "⚠️ Collector home-1: 🧩 cannot read Facebook's pages — stopped\n0 of 12 stories parsed"
        ]
        await report(monitor, clock, S.QUIET)
        await report(monitor, clock, S.WARMUP)
        assert len(told.sent) == 1
        await report(monitor, clock, S.OK)
        await report(monitor, clock, S.OK)
        assert told.to(OWNER)[1:] == ["✅ Collector home-1 is reading again."]

    async def test_unknown_is_shown_but_does_not_page(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox
    ):
        await report(monitor, clock, S.UNKNOWN)
        assert told.sent == [] and await monitor.state() is S.UNKNOWN

    async def test_a_collector_without_the_lease_drives_nothing(
        self, monitor: CollectorMonitor, desk: ControlDesk, clock: FakeClock, told: Outbox
    ):
        await desk.control_for(CID)
        await monitor.on_status(status(S.BLOCKED, collector_id="laptop", block=block()))
        await monitor.news()
        assert told.sent == []
        view = await monitor.view()
        assert [r.collector_id for r in view.others] == ["laptop"]
        assert view.holder is not None and view.holder.collector_id == CID

    async def test_a_failed_alert_is_retried(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox
    ):
        told.fail = True
        await report(monitor, clock, S.CRASH_LOOP)
        told.fail = False
        await monitor.tick()
        assert len(told.to(OWNER)) == 1
        await monitor.tick()
        assert len(told.to(OWNER)) == 1


class TestOrder:
    async def test_a_delayed_status_does_not_bring_back_an_old_block(
        self, monitor: CollectorMonitor, desk: ControlDesk, clock: FakeClock, told: Outbox
    ):
        """An old status that arrives late overwrote the newer one and could close a
        valid resume for the new block."""
        await report(monitor, clock, S.BLOCKED, block=block(17), seq=10, incarnation="db-a1b2c3d4")
        await report(monitor, clock, S.BLOCKED, block=block(18), seq=12, incarnation="db-a1b2c3d4")
        await desk.request_resume(18, block(18))
        applied = await monitor.on_status(
            status(S.BLOCKED, block=block(17), seq=11, incarnation="db-a1b2c3d4")
        )
        assert not applied
        view = await monitor.view()
        assert view.block is not None and view.block.id == 18
        commands = (await desk.control_for(CID, "db-a1b2c3d4")).commands
        assert [getattr(c, "block_id", None) for c in commands] == [18]

    async def test_without_a_counter_the_collectors_own_clock_orders_them(
        self, monitor: CollectorMonitor, clock: FakeClock
    ):
        await monitor.on_status(status(S.OK, sent_at=EPOCH0 + 120))
        assert not await monitor.on_status(status(S.QUIET, sent_at=EPOCH0 + 60))
        assert await monitor.state() is S.OK

    async def test_a_delayed_snapshot_does_not_relist_a_group_an_event_lost(
        self, monitor: CollectorMonitor, storage: Storage, fb: FbStore
    ):
        await apply(monitor, snapshot(info(G1), taken_at=EPOCH0 - 600))
        await storage.subscribe(OWNER, G1)
        lost = events((1, EventKind.GROUP_LOST, {"group_id": str(G1)}))  # at EPOCH0
        await deliver(monitor, lost)
        # a sync taken before the loss, delayed by retries, arrives now; it is newer than the
        # last snapshot applied, so it is applied, but not to the group the event is about
        report = await apply(monitor, snapshot(info(G1), info(G2), taken_at=EPOCH0 - 60))
        assert report is not None and not report.stale
        group = await fb.group(G1)
        assert group is not None and group.state is GroupState.LOST
        assert await storage.monitored_chat_ids() == set()
        assert await fb.group(G2) is not None
        # a sync taken after it lists the group as a member again: that one counts
        await apply(monitor, snapshot(info(G1), info(G2), taken_at=EPOCH0 + 60))
        group = await fb.group(G1)
        assert group is not None and group.state is GroupState.MEMBER

    async def test_a_delayed_event_does_not_undo_a_newer_sync(
        self, monitor: CollectorMonitor, storage: Storage, fb: FbStore
    ):
        await apply(monitor, snapshot(info(G1), taken_at=EPOCH0 + 600))
        await storage.subscribe(OWNER, G1)
        # a loss seen before that sync, queued through an outage, arrives after it
        assert await deliver(monitor, events((1, EventKind.GROUP_LOST, {"group_id": str(G1)})))
        group = await fb.group(G1)
        assert group is not None and group.state is GroupState.MEMBER
        assert await fb.event_applied((CID, "", 1))  # applied, as in: nothing left to do
        assert await storage.monitored_chat_ids() == {G1}

    async def test_an_older_snapshot_changes_nothing_at_all(
        self, monitor: CollectorMonitor, fb: FbStore, told: Outbox
    ):
        await apply(monitor, snapshot(info(G1), info(G2), taken_at=EPOCH0))
        report = await apply(monitor, snapshot(info(G1), taken_at=EPOCH0 - 3600))
        assert report is not None and report.stale and report.changes == ()
        group = await fb.group(G2)
        assert group is not None and group.state is GroupState.MEMBER  # not "absent" now


class TestOffline:
    async def test_twenty_minutes_of_silence_in_the_day_is_offline(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox
    ):
        await report(monitor, clock)
        clock.tick(19 * 60)
        await monitor.tick()
        assert told.sent == [] and await monitor.state() is S.OK
        clock.tick(60)
        await monitor.tick()
        await monitor.tick()
        assert told.to(OWNER) == [
            "📴 Collector home-1 has not reported for 20 min. Nothing is being read."
        ]
        assert monitor.last_holder_status_at == EPOCH0
        await report(monitor, clock)
        assert told.to(OWNER)[-1] == "✅ Collector home-1 is reading again."

    @pytest.mark.parametrize(
        ("idle", "longest"), [(S.QUIET, 9 * 3600), (S.NOT_HOME, 12 * 3600), (S.BREAK, 40 * 60)]
    )
    async def test_an_announced_idle_is_not_offline_but_not_for_ever(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox, idle: S, longest: int
    ):
        """C1-01: a collector that died during a break used to show "on a break" for good."""
        clock.now = dt.datetime(2026, 9, 21, 22, 0, tzinfo=dt.UTC)  # 01:00 in Kyiv
        await report(monitor, clock, idle)
        clock.tick(longest + 20 * 60 - 1)
        await monitor.tick()
        assert told.sent == [] and await monitor.state() is idle
        clock.tick(1)
        assert await monitor.state() is S.OFFLINE
        clock.now = max(clock.now, dt.datetime(2026, 9, 22, 6, 0, tzinfo=dt.UTC))  # daytime
        await monitor.tick()
        assert told.to(OWNER)[0].startswith("📴 Collector home-1 has not reported for")

    async def test_the_announced_end_of_an_idle_state_is_when_silence_starts_to_count(
        self, monitor: CollectorMonitor, clock: FakeClock
    ):
        await report(monitor, clock, S.BREAK, resumes_at=EPOCH0 + 15 * 60)
        clock.tick(15 * 60 + 20 * 60 - 1)
        assert await monitor.state() is S.BREAK
        clock.tick(1)
        assert await monitor.state() is S.OFFLINE

    def test_an_announced_end_never_lengthens_the_longest_idle(self):
        long_break = status(S.BREAK, resumes_at=EPOCH0 + 86400)
        assert idle_grace(long_break) == 40 * 60
        assert idle_grace(status(S.BREAK, resumes_at=EPOCH0 - 60)) == 0  # already over
        assert idle_grace(status(S.OK, resumes_at=EPOCH0 + 600)) == 0

    async def test_at_night_it_waits_for_the_morning(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox
    ):
        clock.now = dt.datetime(2026, 9, 22, 19, 30, tzinfo=dt.UTC)  # 22:30 in Kyiv
        await report(monitor, clock)
        clock.tick(3600)  # 23:30 Kyiv: offline, but not daytime
        await monitor.tick()
        assert told.sent == [] and await monitor.state() is S.OFFLINE
        clock.now = dt.datetime(2026, 9, 23, 5, 5, tzinfo=dt.UTC)  # 08:05 Kyiv
        await monitor.tick()
        assert "has not reported for 9.6 h" in told.to(OWNER)[0]

    async def test_the_collector_zone_setting_wins_over_the_owner_zone(
        self, monitor: CollectorMonitor, clock: FakeClock, storage: Storage
    ):
        clock.now = dt.datetime(2026, 9, 22, 4, 0, tzinfo=dt.UTC)  # 07:00 Kyiv, 05:00 London
        assert await monitor.daytime() is False
        await storage.update_user(OWNER, tz="Asia/Tokyo")  # 13:00: but the setting says Kyiv
        assert await monitor.daytime() is False


class TestEvents:
    async def test_a_lost_group_stops_being_monitored_and_everyone_is_told_once(
        self, monitor: CollectorMonitor, book: GroupBook, storage: Storage, told: Outbox
    ):
        await seed_groups(book, G1)
        await storage.set_chat_shared(G1, True)
        await storage.subscribe(MOD, G1)
        request = events((1, EventKind.GROUP_LOST, {"group_id": str(G1)}))
        assert await deliver(monitor, request) == [1]
        assert await deliver(monitor, request) == [1]  # a resend: acked, applied once
        assert await storage.monitored_chat_ids() == set()
        assert len(told.to(MOD)) == 1
        assert told.to(OWNER) == [
            (
                f"🚫 The account can no longer read Group {G1}. It stays paused until a group "
                "sync lists the account as a member again."
            )
        ]

    async def test_an_effect_that_failed_is_applied_when_the_event_comes_back(
        self,
        monitor: CollectorMonitor,
        book: GroupBook,
        storage: Storage,
        fb: FbStore,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Stored is not applied: a resend of an event whose effect failed must still apply it."""
        await seed_groups(book, G1)
        await storage.subscribe(OWNER, G1)
        original = book.mark
        calls = {"n": 0}

        async def busy_once(*args: Any, **kw: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return await original(*args, **kw)

        monkeypatch.setattr(book, "mark", busy_once)
        request = events((1, EventKind.GROUP_LOST, {"group_id": str(G1)}))
        with pytest.raises(sqlite3.OperationalError):
            await deliver(monitor, request)  # the ingest answers 503: nothing is acked
        assert not await fb.event_applied((CID, "", 1))
        assert await deliver(monitor, request) == [1]
        group = await fb.group(G1)
        assert group is not None and group.state is GroupState.LOST
        assert await storage.monitored_chat_ids() == set()
        assert await fb.event_applied((CID, "", 1))
        await deliver(monitor, request)
        assert calls["n"] == 2  # applied once: the third copy found it in place

    async def test_a_failed_group_write_does_not_mark_the_event_applied(
        self, monitor: CollectorMonitor, book: GroupBook, fb: FbStore, monkeypatch
    ):
        await seed_groups(book, G1)

        async def refuse(*args: Any, **kw: Any) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(fb, "_write_groups", refuse)
        with pytest.raises(sqlite3.OperationalError):
            await deliver(monitor, events((1, EventKind.NOT_FOLLOWED, {"group_id": str(G1)})))
        assert not await fb.event_applied((CID, "", 1))  # one transaction: both or neither

    async def test_a_closed_block_whose_effect_failed_is_closed_on_the_resend(
        self, monitor: CollectorMonitor, desk: ControlDesk, clock: FakeClock, fb, monkeypatch
    ):
        await report(monitor, clock, S.BLOCKED, block=block(17))
        await desk.request_resume(17, block(17))
        original = fb.finish_commands
        calls = {"n": 0}

        async def busy_once(*args: Any, **kw: Any) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            await original(*args, **kw)

        monkeypatch.setattr(fb, "finish_commands", busy_once)
        request = events((2, EventKind.BLOCK_CLOSED, {"block_id": 17}))
        with pytest.raises(sqlite3.OperationalError):
            await deliver(monitor, request)
        assert len((await desk.control_for(CID)).commands) == 1
        assert await deliver(monitor, request) == [2]
        assert (await desk.control_for(CID)).commands == ()

    async def test_a_new_state_database_is_not_taken_for_the_old_one(
        self, monitor: CollectorMonitor, book: GroupBook, storage: Storage, fb: FbStore, told
    ):
        """Round 2 F3: the collector's event ids restart at 1 in a new state database (the
        laptop, or a database recreated after corruption); keyed by collector id alone, its
        first GROUP_LOST was acked as an old event and dropped."""
        await seed_groups(book, G1)
        await storage.subscribe(OWNER, G1)
        mac = events((1, EventKind.OUTBOX_HIGH, {}))
        await deliver(monitor, mac.model_copy(update={"incarnation": "mac-db-00000001"}))
        laptop = events((1, EventKind.GROUP_LOST, {"group_id": str(G1)}))
        acked = await deliver(
            monitor, laptop.model_copy(update={"incarnation": "laptop-db-000002"})
        )
        assert acked == [1]
        group = await fb.group(G1)
        assert group is not None and group.state is GroupState.LOST
        assert await storage.monitored_chat_ids() == set()
        assert any(text.startswith("🚫 The account can no longer") for text in told.to(OWNER))

    async def test_block_1_of_a_new_database_pages_the_owner(
        self, monitor: CollectorMonitor, desk: ControlDesk, clock: FakeClock, told: Outbox
    ):
        """Block ids count per database too: the old block 1's episode must not mute it."""
        await desk.control_for(CID, "mac-db-00000001")
        old = block(1, until=EPOCH0 + 6 * 3600)
        await report(monitor, clock, S.BLOCKED, block=old, incarnation="mac-db-00000001")
        await desk.control_for(CID, "laptop-db-000002")  # the replacement asks for the lease
        assert (await desk.transfer_lease(CID, now=True))[0] is LeaseOutcome.REPLACED
        clock.tick(60)
        await report(
            monitor, clock, S.BLOCKED, block=block(1, until=None), incarnation="laptop-db-000002"
        )
        assert len([m for m in told.to(OWNER) if m.startswith("⛔")]) == 2
        opened = events((5, EventKind.BLOCK_OPENED, {"block_id": 1}))
        await deliver(monitor, opened.model_copy(update={"incarnation": "laptop-db-000002"}))
        paged = [m for m in told.to(OWNER) if m.startswith(("⛔", "⚠️"))]
        assert len(paged) == 2  # the event and the status of one block: one page

    async def test_a_block_announced_twice_is_alerted_once(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox
    ):
        await report(monitor, clock, S.BLOCKED, block=block(17))
        await deliver(monitor, events((1, EventKind.BLOCK_OPENED, {"block_id": 17})))
        assert len(told.to(OWNER)) == 1

    async def test_an_event_first_then_the_status(
        self, monitor: CollectorMonitor, desk: ControlDesk, clock: FakeClock, told: Outbox
    ):
        await desk.control_for(CID)
        request = events((1, EventKind.BLOCK_OPENED, {"block_id": 17, "detail": "checkpoint"}))
        await deliver(monitor, request)
        assert told.to(OWNER) == [
            "⚠️ Collector home-1: ⛔ stopped by a Facebook warning (#17: checkpoint)"
        ]
        await report(monitor, clock, S.BLOCKED, block=block(17))
        assert len(told.to(OWNER)) == 1

    async def test_a_closed_block_ends_its_resume_and_says_so_once(
        self, monitor: CollectorMonitor, desk: ControlDesk, clock: FakeClock, told: Outbox
    ):
        await report(monitor, clock, S.BLOCKED, block=block(17))
        await desk.request_resume(17, block(17))
        await deliver(monitor, events((2, EventKind.BLOCK_CLOSED, {"block_id": 17})))
        assert (await desk.control_for(CID)).commands == ()
        assert told.to(OWNER)[-1].startswith("✅ Block #17 lifted")
        await report(monitor, clock, S.OK)  # no second "reading again"
        assert len(told.to(OWNER)) == 2

    async def test_a_forced_logout_pages_once_whatever_arrives_first(
        self, monitor: CollectorMonitor, desk: ControlDesk, clock: FakeClock, told: Outbox
    ):
        """C1-09: the ladder opens a block for a logout and announces both."""
        await desk.control_for(CID)
        opened = (1, EventKind.BLOCK_OPENED, {"block_id": 17, "detail": "logged out"})
        logged_out = (2, EventKind.LOGGED_OUT, {"block_id": 17, "detail": "USER_ID 0"})
        await deliver(monitor, events(opened, logged_out))
        await report(monitor, clock, S.LOGGED_OUT, block=block(17, reason="logged out"))
        assert len(told.to(OWNER)) == 1

    async def test_a_logout_status_first_is_its_blocks_episode_too(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox
    ):
        await report(monitor, clock, S.LOGGED_OUT, block=block(17, reason="logged out"))
        await deliver(monitor, events((1, EventKind.BLOCK_OPENED, {"block_id": 17})))
        await deliver(monitor, events((2, EventKind.LOGGED_OUT, {"block_id": 17})))
        assert told.to(OWNER) == ["⚠️ Collector home-1: 🚪 logged out of Facebook"]

    async def test_a_status_and_an_event_at_once_page_once(
        self, monitor: CollectorMonitor, desk: ControlDesk, told: Outbox
    ):
        """Check, send and record were three steps any other request could interleave.
        Round 2: and neither request waits for the send any more."""
        await desk.control_for(CID)
        gate = asyncio.Event()
        sending: list[str] = []
        original = told.__call__

        async def slow_tell(user_id: int, text: str) -> bool:
            sending.append(text)
            await gate.wait()
            return await original(user_id, text)

        monitor._tell = slow_tell
        async with asyncio.timeout(5):
            await monitor.on_events(events((1, EventKind.BLOCK_OPENED, {"block_id": 17})))
            await until(lambda: len(sending) == 1)  # the event's alert is being sent
            assert await monitor.on_status(status(S.BLOCKED, block=block(17)))  # answered
        gate.set()
        await monitor.idle()
        assert len(told.to(OWNER)) == 1

    async def test_a_failing_pass_is_logged_and_nothing_is_sent_after_close(
        self,
        monitor: CollectorMonitor,
        clock: FakeClock,
        told: Outbox,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        async def broken() -> None:
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(monitor, "news", broken)
        with caplog.at_level("ERROR"):
            await report(monitor, clock, S.CRASH_LOOP)
        assert "news were not all sent" in caplog.text
        monkeypatch.undo()
        await monitor.close()
        await monitor.on_status(status(S.PARSER_BROKEN, sent_at=epoch(clock) + 60))
        await monitor.idle()
        assert told.sent == []  # closed: the next start's tick sends it

    async def test_no_request_waits_for_a_telegram_that_never_answers(
        self,
        monitor: CollectorMonitor,
        desk: ControlDesk,
        book: GroupBook,
        fb: FbStore,
        storage: Storage,
    ):
        """Round 2 F1/F2: a send that hangs held the request, a lock and an ingest slot."""
        stuck = asyncio.Event()

        async def hang(user_id: int, text: str) -> bool:
            stuck.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monitor._tell = hang
        book._tell = hang
        await seed_groups(book, G1)
        await storage.set_chat_shared(G1, True)
        assert await storage.subscribe(MOD, G1)
        await fb.touch_collector("laptop", now=EPOCH0)
        async with asyncio.timeout(5):
            await desk.control_for(CID)
            await monitor.on_status(status(S.BLOCKED, block=block(17)))
            await until(stuck.is_set)  # the block's alert is on its way, and stays there
            lost = events((1, EventKind.GROUP_LOST, {"group_id": str(G1)}))
            assert await monitor.on_events(lost) == [1]
            assert await monitor.on_status(status(S.OK, sent_at=EPOCH0 + 60))
            assert await monitor.on_snapshot(snapshot(info(G2), complete=False)) is not None
            await desk.transfer_lease("laptop")
            told_to_stop = await desk.control_for(CID)
            assert not told_to_stop.lease_held_by(CID)
            assert (await desk.control_for("laptop")).lease_held_by("laptop")
        await monitor.close()  # the stuck send is cancelled; what it carried stays queued
        assert await fb.alert_episode("collector") is None
        pending = await fb.pending_notices(since=EPOCH0 - 60)
        # "lost access" and the owner's sync summary: still to be delivered, not lost
        assert sorted(n.user_id for n in pending) == [OWNER, MOD]

    async def test_state_events_share_the_state_episode(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox
    ):
        await deliver(monitor, events((1, EventKind.LOGGED_OUT, {"detail": "USER_ID 0"})))
        await report(monitor, clock, S.LOGGED_OUT)
        assert told.to(OWNER) == ["⚠️ Collector home-1: 🚪 logged out of Facebook\nUSER_ID 0"]

    async def test_group_flags_and_their_reset_by_a_sync(
        self, monitor: CollectorMonitor, book: GroupBook, fb: FbStore, told: Outbox
    ):
        await seed_groups(book, G1)
        await deliver(
            monitor,
            events(
                (1, EventKind.NOT_FOLLOWED, {"group_id": str(G1)}),
                (2, EventKind.SORT_IGNORED, {"group_id": str(G1)}),
                (3, EventKind.NOT_FOLLOWED, {"group_id": str(G1)}),
            ),
        )
        group = await fb.group(G1)
        assert group is not None and group.followed is False and group.sort_ignored
        assert len(told.to(OWNER)) == 2  # the repeated NOT_FOLLOWED is the same episode
        await apply(monitor, snapshot(info(G1, followed=True)))
        await deliver(monitor, events((4, EventKind.NOT_FOLLOWED, {"group_id": str(G1)})))
        assert told.to(OWNER)[-1].startswith("👁 The account does not follow")

    async def test_events_from_a_collector_without_the_lease_change_nothing(
        self, monitor: CollectorMonitor, desk: ControlDesk, book: GroupBook, storage: Storage
    ):
        await seed_groups(book, G1)
        await storage.subscribe(OWNER, G1)
        await desk.control_for(CID)
        request = events((1, EventKind.GROUP_LOST, {"group_id": str(G1)}), collector_id="laptop")
        await deliver(monitor, request)
        assert await storage.monitored_chat_ids() == {G1}

    async def test_other_kinds(self, monitor: CollectorMonitor, fb: FbStore, told: Outbox):
        await deliver(monitor, events((1, EventKind.OUTBOX_HIGH, {})))
        assert told.to(OWNER)[-1].startswith("📦 Collector home-1: its outbox")
        await fb.db.execute(
            "INSERT INTO collector_events (collector_id, event_id, kind, at, received_at) "
            "VALUES (?,?,?,?,?)",
            (CID, 9, "from_the_future", EPOCH0, EPOCH0),
        )
        await monitor.tick()
        assert told.to(OWNER)[-1] == "ℹ️ Collector home-1: from_the_future"

    async def test_undelivered_alerts_are_retried_for_a_day_then_dropped(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox
    ):
        told.fail = True
        await deliver(monitor, events((1, EventKind.OUTBOX_HIGH, {})))
        await deliver(monitor, events((2, EventKind.CRASH_LOOP, {})))
        clock.tick(3600)
        told.fail = False
        await monitor.tick()
        assert len(told.to(OWNER)) == 2
        told.fail = True
        await deliver(monitor, events((3, EventKind.OUTBOX_HIGH, {})))
        clock.tick(86400 + 1)
        told.fail = False
        await monitor.tick()
        assert len(told.to(OWNER)) == 2  # a day-old alert misleads more than it helps


class TestSnapshots:
    async def test_only_the_lease_holder_syncs_and_the_owner_gets_a_summary(
        self, monitor: CollectorMonitor, desk: ControlDesk, told: Outbox, fb: FbStore
    ):
        await desk.control_for(CID)
        assert await apply(monitor, snapshot(info(G1), collector_id="laptop")) is None
        assert await fb.groups() == []
        result = await apply(monitor, snapshot(info(G1, "Оренда Варна")))
        assert result is not None and result.added == ["Оренда Варна"]
        assert told.to(OWNER) == [
            (
                "🔄 Groups synced from the account: 1.\n➕ New: Оренда Варна. Open them under "
                "🗂 Shared groups if others should see them."
            )
        ]
        await apply(monitor, snapshot(info(G1, "Оренда Варна")))
        assert len(told.to(OWNER)) == 1  # nothing new, nothing said

    async def test_a_summary_that_could_not_be_sent_goes_out_at_the_next_tick(
        self, monitor: CollectorMonitor, told: Outbox
    ):
        told.fail = True
        await apply(monitor, snapshot(info(G1, "Оренда Варна")))
        assert told.sent == []
        told.fail = False
        await monitor.tick()
        await monitor.tick()
        assert [t.split("\n")[0] for t in told.to(OWNER)] == [
            "🔄 Groups synced from the account: 1."
        ]

    async def test_a_resent_snapshot_still_closes_its_refresh_and_rearms_alerts(
        self,
        monitor: CollectorMonitor,
        desk: ControlDesk,
        fb: FbStore,
        told: Outbox,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Round 2 C-F4: when the answer failed after the snapshot's commit, the resend was
        stale and skipped closing the refresh (open for 12 h, the owner could not ask again)
        and clearing the group's alert (its next loss never paged anybody)."""
        await apply(monitor, snapshot(info(G1)))
        await deliver(monitor, events((1, EventKind.GROUP_LOST, {"group_id": str(G1)})))
        assert await fb.alert_episode(f"group_lost:{G1}") == "1"
        await desk.control_for(CID)
        await desk.request_refresh()
        (command,) = await fb.open_commands(now=EPOCH0)
        relisted = snapshot(info(G1), taken_at=EPOCH0 + 60).model_copy(
            update={"nonce": command.nonce, "seq": 7, "incarnation": "db-a1b2c3d4"}
        )
        original = desk.snapshot_arrived

        async def busy(sync: Any) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(desk, "snapshot_arrived", busy)
        with pytest.raises(sqlite3.OperationalError):
            await monitor.on_snapshot(relisted)  # committed, then the answer failed: 503
        monkeypatch.setattr(desk, "snapshot_arrived", original)
        assert await fb.alert_episode(f"group_lost:{G1}") == "1"  # nothing after the commit ran
        report = await apply(monitor, relisted)  # the collector resends it
        assert report is not None and report.stale
        assert await fb.open_commands(now=EPOCH0) == []
        assert await fb.alert_episode(f"group_lost:{G1}") is None
        await deliver(monitor, events((2, EventKind.GROUP_LOST, {"group_id": str(G1)})))
        lost = [t for t in told.to(OWNER) if t.startswith("🚫 The account can no longer")]
        assert len(lost) == 2

    async def test_a_relisted_group_can_be_reported_lost_again(
        self, monitor: CollectorMonitor, book: GroupBook, told: Outbox
    ):
        await apply(monitor, snapshot(info(G1)))
        await deliver(monitor, events((1, EventKind.GROUP_LOST, {"group_id": str(G1)})))
        await apply(monitor, snapshot(info(G1)))
        await deliver(monitor, events((2, EventKind.GROUP_LOST, {"group_id": str(G1)})))
        lost = [t for t in told.to(OWNER) if t.startswith("🚫 The account can no longer")]
        assert len(lost) == 2


async def seed_posts(fb: FbStore, gid: int, times: list[int]) -> None:
    for n, created in enumerate(times):
        await fb.record_item(
            gid,
            n + 1,
            created_at=created,
            received_at=created,
            via="feed",
            late=False,
            claimed=False,
        )


async def read_on(
    monitor: CollectorMonitor, clock: FakeClock, until: int, *, state: S = S.OK, every: int = 240
) -> None:
    """The collector reports ``state`` every few minutes, each report followed by the brain's
    minute tick, up to and including ``until``."""
    while True:
        await report(monitor, clock, state)
        await monitor.tick()
        if epoch(clock) >= until:
            return
        clock.tick(min(every, until - epoch(clock)))


class TestSilence:
    @pytest.fixture
    async def rhythm(
        self,
        monitor: CollectorMonitor,
        book: GroupBook,
        storage: Storage,
        fb: FbStore,
        clock: FakeClock,
    ) -> None:
        clock.tick(-10 * 86400)
        await seed_groups(book, G1)
        await storage.subscribe(OWNER, G1)
        clock.tick(10 * 86400)
        # a post every 30 minutes for the last ~4 h: p90 gap 30 min, threshold max(1.5 h, 2 h)
        await seed_posts(fb, G1, [EPOCH0 - 4 * 3600 + n * 1800 for n in range(8)])
        await report(monitor, clock)

    async def test_a_quiet_group_is_reported_once_per_silence(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox, fb: FbStore, rhythm
    ):
        last = EPOCH0 - 4 * 3600 + 7 * 1800
        await read_on(monitor, clock, last + 2 * 3600 - 60)
        assert told.sent == []
        clock.tick(120)
        await read_on(monitor, clock, epoch(clock))
        await monitor.tick()
        assert told.to(OWNER) == [
            (
                f"🔕 No new posts from Group {G1} for 2 h, nights not counted (it usually posts "
                "at least every 30 min). The collector says it is reading — check the group by "
                "hand."
            )
        ]
        # A new post ends that silence. Its 2 h gap now counts in the rhythm (p90 = 2 h, so the
        # next alert needs 6 h), and a second silence is a second episode.
        await fb.record_item(
            G1,
            100,
            created_at=epoch(clock),
            received_at=epoch(clock),
            via="feed",
            late=False,
            claimed=False,
        )
        post = epoch(clock)
        threshold = 3 * (post - last)  # 6 h 3 min
        await read_on(monitor, clock, post + threshold - 60)
        assert len(told.to(OWNER)) == 1
        clock.tick(120)  # 22:35 in Kyiv: still the collector's day
        await read_on(monitor, clock, epoch(clock))
        assert told.to(OWNER)[1].startswith(f"🔕 No new posts from Group {G1} for 6.1 h")
        assert "every 2 h" in told.to(OWNER)[1]

    async def test_not_before_the_collector_has_caught_up_after_a_pause(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox, fb: FbStore, rhythm
    ):
        # The night of 2026-10-10: idle for hours, and the minute between its end and the first
        # read made the busiest group look 7.8 h silent; its night's posts came a minute later.
        await read_on(monitor, clock, EPOCH0 + 4 * 3600, state=S.QUIET)
        woke = epoch(clock)  # its first ok report is now
        await read_on(monitor, clock, woke + 60)
        assert told.sent == []
        for n in range(8):  # the night kept the 30 min rhythm: the threshold stays 2 h
            await fb.record_item(
                G1,
                100 + n,
                created_at=EPOCH0 + n * 1800,
                received_at=epoch(clock),
                via="feed",
                late=False,
                claimed=False,
            )
        await read_on(monitor, clock, woke + 3600)
        assert told.sent == []

    async def test_a_silence_still_counts_once_the_collector_has_caught_up(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox, rhythm
    ):
        await read_on(monitor, clock, EPOCH0 + 4 * 3600, state=S.BREAK)
        woke = epoch(clock)  # its first ok report is now
        await read_on(monitor, clock, woke + 3600 - 240)
        assert told.sent == []
        await read_on(monitor, clock, woke + 3600)
        assert told.to(OWNER)[0].startswith(f"🔕 No new posts from Group {G1} for 5.5 h")

    async def test_a_hole_in_the_statuses_starts_the_catch_up_again(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox, rhythm
    ):
        await read_on(monitor, clock, EPOCH0 + 3600 - 240)
        for _ in range(10):  # the machine asleep: its last state is still ok, but nothing read
            clock.tick(60)
            await monitor.tick()
        await read_on(monitor, clock, epoch(clock) + 3600 - 240)
        assert told.sent == []
        await read_on(monitor, clock, epoch(clock) + 240)
        assert len(told.sent) == 1

    async def test_a_failing_tick_does_not_cost_the_stretch(
        self,
        monitor: CollectorMonitor,
        clock: FakeClock,
        told: Outbox,
        rhythm,
        monkeypatch: pytest.MonkeyPatch,
    ):
        await read_on(monitor, clock, EPOCH0 + 20 * 60)

        async def broken() -> None:
            raise sqlite3.OperationalError("database is locked")

        with monkeypatch.context() as patch:
            patch.setattr(monitor, "news", broken)
            for _ in range(30):
                clock.tick(60)
                await report(monitor, clock)
                with pytest.raises(sqlite3.OperationalError):
                    await monitor.tick()
        # Caught up at EPOCH0 + 1 h; the 2 h silence is reached at EPOCH0 + 1.5 h.
        await read_on(monitor, clock, EPOCH0 + 90 * 60 + 240)
        assert len(told.sent) == 1

    async def test_a_hole_in_the_ticks_starts_the_catch_up_again(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox, rhythm
    ):
        await read_on(monitor, clock, EPOCH0 + 3600 - 240)
        clock.tick(10 * 60)  # the brain looked away; whatever it missed may have been a pause
        await read_on(monitor, clock, epoch(clock) + 3600 - 240)
        assert told.sent == []
        await read_on(monitor, clock, epoch(clock) + 240)
        assert len(told.sent) == 1

    async def test_not_while_the_collector_is_not_reading(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox, rhythm
    ):
        clock.tick(5 * 3600)
        await report(monitor, clock, S.QUIET)
        await monitor.tick()
        assert told.sent == []

    async def test_not_at_night(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox, rhythm
    ):
        clock.now = dt.datetime(2026, 9, 22, 21, 0, tzinfo=dt.UTC)  # midnight in Kyiv
        await read_on(monitor, clock, epoch(clock) + 3 * 3600)
        assert told.sent == []

    async def test_not_for_a_group_the_account_does_not_follow(
        self,
        monitor: CollectorMonitor,
        book: GroupBook,
        clock: FakeClock,
        told: Outbox,
        rhythm,
    ):
        # It gets only the slow backstop, hours apart, and the owner was told so.
        await book.apply_snapshot(snapshot(info(G1, followed=False)), now=epoch(clock))
        await read_on(monitor, clock, EPOCH0 + 3 * 3600)
        assert not [t for t in told.to(OWNER) if t.startswith("🔕")]

    async def test_the_night_is_not_counted(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox, fb: FbStore, rhythm
    ):
        # 2026-10-11: a rideshare group's last post at 20:26 and its next at 09:52; at 09:59 its
        # 13.6 h, night included, were over its threshold by half a minute.
        evening = int(dt.datetime(2026, 9, 22, 19, 30, tzinfo=dt.UTC).timestamp())  # 22:30
        await seed_posts(fb, G1, [EPOCH0 + n * 1800 for n in range(1, 16)])
        await fb.db.execute("UPDATE fb_items SET post_id=post_id+100 WHERE created_at>?", (EPOCH0,))
        await fb.record_item(
            G1, 999, created_at=evening, received_at=evening, via="feed", late=False, claimed=False
        )
        clock.now = dt.datetime.fromtimestamp(evening, dt.UTC)
        morning = int(dt.datetime(2026, 9, 23, 5, 0, tzinfo=dt.UTC).timestamp())  # 08:00
        await read_on(monitor, clock, morning, state=S.QUIET, every=1800)
        await read_on(monitor, clock, morning + 3600)  # 0.5 h of the evening, 1 h of the morning
        assert told.sent == []
        await read_on(monitor, clock, morning + 90 * 60)
        assert told.to(OWNER) == [
            (
                f"🔕 No new posts from Group {G1} for 2 h, nights not counted (it usually posts "
                "at least every 30 min). The collector says it is reading — check the group by "
                "hand."
            )
        ]

    async def test_a_page_read_through_shows_the_group_quiet_not_unseen(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox, rhythm
    ):
        # 2026-10-11, a Sunday evening: no post for 2.55 h against a threshold of 2.53 h, and
        # the backstop had read the group's page through twice in that time.
        last = EPOCH0 - 4 * 3600 + 7 * 1800
        checked = {str(G1): last + 3600}
        until = last + 3 * 3600 - 60
        while epoch(clock) < until:
            await report(monitor, clock, group_checked=checked)
            await monitor.tick()
            clock.tick(240)
        assert told.sent == []
        clock.now = dt.datetime.fromtimestamp(last + 3 * 3600, dt.UTC)
        await report(monitor, clock, group_checked=checked)
        await monitor.tick()
        assert told.to(OWNER)[0].startswith(f"🔕 No new posts from Group {G1} for 3 h,")

    async def test_a_check_from_a_clock_gone_wrong_is_ignored(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox, rhythm
    ):
        # A day ahead: it would hold the alert off as long as the collector's clock is wrong.
        while epoch(clock) < EPOCH0 + 95 * 60:  # paged at 1.5 h, as without the check
            assert told.sent == []
            await report(monitor, clock, group_checked={str(G1): epoch(clock) + 86400})
            await monitor.tick()
            clock.tick(240)
        assert len(told.sent) == 1

    async def test_not_for_a_group_that_posts_only_at_night(
        self,
        *,
        monitor: CollectorMonitor,
        book: GroupBook,
        storage: Storage,
        fb: FbStore,
        clock: FakeClock,
        told: Outbox,
    ):
        # Its gaps have no daytime in them; counted as zeros they made a 2 h threshold
        # and a page every morning.
        clock.tick(-10 * 86400)
        await seed_groups(book, G2)
        await storage.subscribe(OWNER, G2)
        clock.tick(10 * 86400)
        # A dozen posts from 00:00 to 00:55 in Kyiv every night: nine gaps in ten within a night.
        nights = [EPOCH0 - n * 86400 + 9 * 3600 + m * 300 for n in range(1, 8) for m in range(12)]
        await seed_posts(fb, G2, sorted(nights))
        await read_on(monitor, clock, EPOCH0 + 3 * 3600)
        assert told.sent == []

    async def test_not_for_a_group_with_too_little_history(
        self, monitor: CollectorMonitor, clock: FakeClock, told: Outbox, fb: FbStore, rhythm
    ):
        await fb.db.execute("DELETE FROM fb_items WHERE post_id > 4")
        await read_on(monitor, clock, EPOCH0 + 3 * 3600)
        assert told.sent == []

    async def test_not_before_the_watchers_have_watched_that_long(
        self,
        *,
        monitor: CollectorMonitor,
        book: GroupBook,
        storage: Storage,
        fb: FbStore,
        clock: FakeClock,
        told: Outbox,
    ):
        await seed_groups(book, G2)
        await seed_posts(fb, G2, [EPOCH0 - 30 * 86400 + n * 600 for n in range(8)])
        await fb.db.execute("UPDATE fb_items SET created_at=created_at+? ", (20 * 86400,))
        await storage.subscribe(OWNER, G2)  # just now: the old silence is our gap, not theirs
        await read_on(monitor, clock, EPOCH0 + 90 * 60)
        assert told.sent == []


class TestOverCap:
    async def test_over_the_cap_is_alerted_once_and_forgotten_when_fixed(
        self,
        monitor: CollectorMonitor,
        book: GroupBook,
        storage: Storage,
        fb: FbStore,
        told: Outbox,
    ):
        ids = [5_000_000 + n for n in range(MAX_MONITORED + 1)]
        await book.apply_snapshot(snapshot(*(info(g) for g in ids)), now=EPOCH0)
        for gid in ids:
            await storage.subscribe(OWNER, gid)
        await monitor.tick()
        await monitor.tick()
        over = [t for t in told.to(OWNER) if "over the 25-group limit" in t]
        assert len(over) == 1 and f"Group {ids[-1]}" in over[0]
        await storage.unsubscribe(OWNER, ids[-1])
        await monitor.tick()
        assert await fb.alert_episode("over_cap") is None


class TestContender:
    async def test_a_second_collector_is_reported_once(
        self, monitor: CollectorMonitor, desk: ControlDesk, told: Outbox
    ):
        await desk.control_for(CID)
        await desk.control_for("laptop")
        await monitor.news()
        await desk.control_for("laptop")
        await monitor.news()
        assert told.to(OWNER) == [
            (
                "🔒 Collector laptop asked to read, but home-1 holds the lease, so laptop was "
                "told to stop. If laptop replaces home-1: /lease laptop"
            )
        ]


class TestSecondDatabase:
    """Round 2 F4: a second state database under the holder's id (a second machine, or a
    recreated state) is a contender: it drives nothing, and the owner is told once."""

    MAC, LAPTOP = "mac-db-7f3a0001", "laptop-db-c29e002"

    async def test_it_drives_nothing_and_does_not_overwrite_the_holders_report(
        self,
        monitor: CollectorMonitor,
        desk: ControlDesk,
        book: GroupBook,
        *,
        storage: Storage,
        fb: FbStore,
        clock: FakeClock,
    ):
        await seed_groups(book, G1)
        await storage.subscribe(OWNER, G1)
        await desk.control_for(CID, self.MAC)
        await report(monitor, clock, S.OK, incarnation=self.MAC)
        blocked = status(S.BLOCKED, block=block(3), incarnation=self.LAPTOP, sent_at=EPOCH0 + 5)
        assert not await monitor.on_status(blocked)
        assert await monitor.state() is S.OK  # the holder's own report stands
        lost = events((1, EventKind.GROUP_LOST, {"group_id": str(G1)}))
        await deliver(monitor, lost.model_copy(update={"incarnation": self.LAPTOP}))
        assert await storage.monitored_chat_ids() == {G1}  # stored, alerted, applied to nothing
        assert (
            await apply(monitor, snapshot(info(G2)).model_copy(update={"incarnation": self.LAPTOP}))
            is None
        )
        assert await fb.group(G2) is None

    async def test_the_owner_is_told_once_and_again_after_a_move(
        self, monitor: CollectorMonitor, desk: ControlDesk, told: Outbox
    ):
        await desk.control_for(CID, self.MAC)
        await desk.control_for(CID, self.LAPTOP)
        await monitor.news()
        await desk.control_for(CID, self.LAPTOP)
        await monitor.news()
        (twin,) = told.to(OWNER)
        assert twin.startswith("🔒 A second state database reports as home-1 (database laptop-d")
        assert "Only home-1 with database mac-db-7 reads" in twin and "/lease home-1" in twin
        await desk.transfer_lease(CID, now=True)
        await desk.control_for(CID, self.MAC)  # the old one, still running
        await monitor.news()
        assert len(told.to(OWNER)) == 2 and "(database mac-db-7" in told.to(OWNER)[1]


class TestLeaseMove:
    async def test_the_owner_hears_when_the_old_holder_has_been_told(
        self, monitor: CollectorMonitor, desk: ControlDesk, fb: FbStore, told: Outbox
    ):
        await desk.control_for(CID)
        await fb.touch_collector("laptop", now=EPOCH0)
        await desk.transfer_lease("laptop")
        await desk.control_for("laptop")  # the new one waits, and is no contender
        await monitor.news()
        assert told.sent == []
        told.fail = True
        await desk.control_for(CID)
        await monitor.news()  # Telegram is down: the notice waits, written down
        told.fail = False
        await monitor.tick()
        await monitor.tick()
        assert told.to(OWNER) == [
            (
                "🔒 home-1 has checked in and was told to stop. laptop gets the lease at its "
                "next check-in."
            )
        ]

    async def test_the_revoked_holder_still_reports_until_it_has_heard(
        self, monitor: CollectorMonitor, desk: ControlDesk, fb: FbStore, clock: FakeClock
    ):
        """What it sends meanwhile was read under its lease: a block it hit is still news."""
        await desk.control_for(CID)
        await fb.touch_collector("laptop", now=EPOCH0)
        await desk.transfer_lease("laptop")
        await report(monitor, clock, S.BLOCKED, block=block(9))
        assert await monitor.state() is S.BLOCKED


class TestView:
    async def test_the_view_before_anything_reported(self, monitor: CollectorMonitor):
        view = await monitor.view()
        assert view.holder is None and view.state is None and view.status is None
        assert view.block is None and not view.paused and view.groups == ()

    async def test_the_view_with_a_holder(
        self, monitor: CollectorMonitor, clock: FakeClock, desk: ControlDesk, book, storage
    ):
        await seed_groups(book, G1)
        await storage.subscribe(OWNER, G1)
        await report(monitor, clock, S.BLOCKED, block=block(3))
        await desk.pause()
        view = await monitor.view()
        assert view.state is S.BLOCKED and view.block is not None and view.block.id == 3
        assert view.paused and view.monitored == (G1,) and view.over_cap == ()
        assert [g.state for g in view.groups] == [GroupState.MEMBER]
