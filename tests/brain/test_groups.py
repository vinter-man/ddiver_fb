"""Group sync: memberships become chat rows, and only a readable group is ever monitored."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ddiver_core.storage import Storage
from ddiver_fb.groups import MAX_TITLE, GroupBook, next_state, plan_snapshot
from ddiver_fb.store import FbGroup, FbStore
from fbwire import GroupInfo, GroupPrivacy, GroupState
from tests.brain.helpers import (
    EPOCH0,
    G1,
    G2,
    G3,
    MOD,
    OWNER,
    Invalidations,
    Outbox,
    info,
    snapshot,
)

M, U, L, X = GroupState.MEMBER, GroupState.UNVERIFIED, GroupState.LEFT, GroupState.LOST


def gi(state: GroupState, *, listed: bool = True) -> GroupInfo:
    return info(G1, state=state, listed=listed)


class TestNextState:
    @pytest.mark.parametrize(
        ("current", "incoming", "complete", "expected"),
        [
            # absent from the snapshot
            (None, None, True, None),
            (M, None, True, U),
            (M, None, False, M),  # an incomplete list proves nothing
            (U, None, True, U),  # absence never makes "left": only the collector can check
            (L, None, True, L),
            (X, None, True, X),
            # a group seen for the first time is added only when readable
            (None, gi(M), True, M),
            (None, gi(U), True, None),
            (None, gi(L), True, None),
            (None, gi(X), True, None),
            # the collector's verdict wins
            (M, gi(L), True, L),
            (U, gi(M), False, M),
            (M, gi(X), True, X),
            # listed=false in a complete snapshot is absent from it, whatever it says
            (M, gi(M, listed=False), True, U),
            (None, gi(M, listed=False), True, None),
            (U, gi(M, listed=False), True, U),
            (M, gi(M, listed=False), False, M),  # an incomplete list proves nothing
            (M, gi(L, listed=False), True, L),  # the collector's own verdict still stands
            # LOST is sticky until a sync lists the group as a member
            (X, gi(M, listed=False), True, X),
            (X, gi(M, listed=True), False, M),
            (X, gi(U), True, X),
            (X, gi(L), True, L),
        ],
    )
    def test_table(self, current, incoming, complete, expected):
        assert next_state(current, incoming, complete=complete) == expected


def row(gid: int = G1, **kw) -> FbGroup:
    values: dict[str, Any] = {
        "chat_id": gid,
        "title": f"Group {gid}",
        "url": f"https://www.facebook.com/groups/{gid}/",
    }
    values.update({"state": GroupState.MEMBER, "state_changed_at": EPOCH0 - 3600})
    values.update(kw)
    return FbGroup(**values)


class TestPlanSnapshot:
    def test_a_new_member_group_is_planned_with_the_snapshot_time(self):
        rows = plan_snapshot({}, snapshot(info(G1, "Оренда Варна")), now=EPOCH0 + 10)
        assert len(rows) == 1
        new = rows[0]
        assert (new.chat_id, new.title, new.state) == (G1, "Оренда Варна", GroupState.MEMBER)
        assert new.last_synced_at == new.last_listed_at == EPOCH0
        assert new.state_changed_at == EPOCH0 + 10

    def test_a_collector_clock_ahead_of_ours_does_not_date_the_future(self):
        rows = plan_snapshot({}, snapshot(info(G1), taken_at=EPOCH0 + 3600), now=EPOCH0)
        assert rows[0].last_synced_at == EPOCH0

    def test_what_the_snapshot_does_not_know_is_kept(self):
        before = row(
            slug="varna.rent",
            privacy=GroupPrivacy.PRIVATE,
            followed=True,
            listings=True,
            tier=3,
            sort_ignored=True,
            shared=True,
        )
        rows = plan_snapshot(
            {G1: before}, snapshot(info(G1, "   ", privacy=GroupPrivacy.UNKNOWN)), now=EPOCH0
        )
        after = rows[0]
        assert after.title == before.title  # a blank name keeps the known one
        assert (after.slug, after.privacy, after.followed) == (
            "varna.rent",
            GroupPrivacy.PRIVATE,
            True,
        )
        assert (after.listings, after.tier, after.sort_ignored, after.shared) == (
            True,
            3,
            True,
            True,
        )
        assert after.state_changed_at == before.state_changed_at  # the state did not change

    def test_the_collector_says_when_the_sort_is_honoured_again(self):
        """Round 2 F7: only an event set sort_ignored, and every snapshot copied it."""
        before = row(sort_ignored=True)
        (after,) = plan_snapshot({G1: before}, snapshot(info(G1, sort_ignored=False)), now=EPOCH0)
        assert after.sort_ignored is False
        rows = plan_snapshot({G1: before}, snapshot(info(G1, sort_ignored=None)), now=EPOCH0)
        assert all(r.sort_ignored for r in rows)  # not known: the brain keeps what it has

    def test_a_title_is_capped(self):
        rows = plan_snapshot({}, snapshot(info(G1, "x" * 1000)), now=EPOCH0)
        assert len(rows[0].title) == MAX_TITLE == 128  # the core's digest budget assumes it

    def test_an_unchanged_group_is_not_rewritten(self):
        first = plan_snapshot({}, snapshot(info(G1)), now=EPOCH0)[0]
        assert plan_snapshot({G1: first}, snapshot(info(G1)), now=EPOCH0 + 60) == []

    def test_absent_groups_change_only_on_a_complete_snapshot(self):
        current = {G1: row(G1), G2: row(G2)}
        partial = plan_snapshot(current, snapshot(info(G1), complete=False), now=EPOCH0)
        assert all(r.chat_id != G2 for r in partial)
        complete = plan_snapshot(current, snapshot(info(G1)), now=EPOCH0)
        missing = next(r for r in complete if r.chat_id == G2)
        assert missing.state is GroupState.UNVERIFIED
        assert missing.state_changed_at == EPOCH0

    def test_listed_false_is_not_counted_as_seen_in_this_sync(self):
        before = row(last_listed_at=EPOCH0 - 86400)
        rows = plan_snapshot({G1: before}, snapshot(info(G1, listed=False)), now=EPOCH0)
        assert rows[0].last_listed_at == EPOCH0 - 86400
        # and a member it no longer lists is unverified, as if it had been left out
        assert rows[0].state is GroupState.UNVERIFIED and not rows[0].available


class TestApplySnapshot:
    async def test_groups_become_chat_rows_without_a_username(
        self, book: GroupBook, storage: Storage, fb: FbStore
    ):
        await book.apply_snapshot(
            snapshot(info(G1, "Оренда Варна", slug="varna.rent", privacy=GroupPrivacy.PRIVATE)),
            now=EPOCH0,
        )
        chat = await storage.get_chat(G1)
        assert chat is not None
        # The slug must never reach chats.username: that column builds t.me links.
        assert (chat.ref.kind, chat.ref.username, chat.ref.title) == ("group", None, "Оренда Варна")
        assert chat.available and not chat.shared
        group = await fb.group(G1)
        assert group is not None
        assert (group.slug, group.privacy) == ("varna.rent", GroupPrivacy.PRIVATE)

    async def test_state_and_availability_move_together(self, book: GroupBook, storage: Storage):
        await book.apply_snapshot(snapshot(info(G1), info(G2)), now=EPOCH0)
        await storage.subscribe(OWNER, G1)
        await storage.subscribe(OWNER, G2)
        assert await storage.monitored_chat_ids() == {G1, G2}
        await book.apply_snapshot(snapshot(info(G1), info(G2, state=GroupState.LEFT)), now=EPOCH0)
        assert await storage.monitored_chat_ids() == {G1}
        chat = await storage.get_chat(G2)
        assert chat is not None and not chat.available

    async def test_a_member_sent_only_for_its_metadata_stops_being_monitored(
        self, book: GroupBook, storage: Storage, fb: FbStore
    ):
        await book.apply_snapshot(snapshot(info(G1), info(G2)), now=EPOCH0)
        await storage.subscribe(OWNER, G1)
        await storage.subscribe(OWNER, G2)
        # G2 has dropped out of "All groups you've joined"; the collector still sends it,
        # flagged listed=false, with the "member" it remembers from the earlier sync
        await book.apply_snapshot(snapshot(info(G1), info(G2, listed=False)), now=EPOCH0 + 60)
        group = await fb.group(G2)
        assert group is not None and group.state is GroupState.UNVERIFIED
        chat = await storage.get_chat(G2)
        assert chat is not None and not chat.available
        assert await storage.monitored_chat_ids() == {G1}

    async def test_sharing_is_never_touched_by_a_sync(self, book: GroupBook, storage: Storage):
        await book.apply_snapshot(snapshot(info(G1)), now=EPOCH0)
        await storage.set_chat_shared(G1, True)
        await book.apply_snapshot(snapshot(info(G1, "Renamed")), now=EPOCH0 + 60)
        chat = await storage.get_chat(G1)
        assert chat is not None and chat.shared and chat.ref.title == "Renamed"

    async def test_subscribers_hear_about_access_they_lose_and_regain(
        self, book: GroupBook, storage: Storage, told: Outbox, invalidations: Invalidations
    ):
        await book.apply_snapshot(snapshot(info(G1, "Оренда")), now=EPOCH0)
        await storage.set_chat_shared(G1, True)
        assert await storage.subscribe(MOD, G1)
        invalidations.calls.clear()
        await book.apply_snapshot(snapshot(info(G1, "Оренда", state=GroupState.LOST)), now=EPOCH0)
        assert told.to(MOD) == [
            "⚠️ Акаунт Facebook більше не може читати Оренда; нагляд призупинено."
        ]
        assert invalidations.calls == [None]
        await book.apply_snapshot(snapshot(info(G1, "Оренда")), now=EPOCH0 + 60)
        assert told.to(MOD)[-1] == "✅ Акаунт Facebook знову може читати Оренда."

    async def test_a_rename_alone_neither_notifies_nor_invalidates(
        self, book: GroupBook, storage: Storage, told: Outbox, invalidations: Invalidations
    ):
        await book.apply_snapshot(snapshot(info(G1)), now=EPOCH0)
        await storage.subscribe(OWNER, G1)
        invalidations.calls.clear()
        report = await book.apply_snapshot(snapshot(info(G1, "New name")), now=EPOCH0 + 60)
        assert told.sent == [] and invalidations.calls == []
        assert not report.noteworthy

    async def test_the_report_says_what_changed(self, book: GroupBook):
        await book.apply_snapshot(snapshot(info(G1), info(G2)), now=EPOCH0)
        report = await book.apply_snapshot(
            snapshot(info(G1, state=GroupState.LOST), info(G3, "Robota")), now=EPOCH0 + 60
        )
        assert report.added == ["Robota"]
        assert report.became(GroupState.LOST) == [f"Group {G1}"]
        assert report.became(GroupState.UNVERIFIED) == [f"Group {G2}"]
        assert report.noteworthy and report.complete and report.total == 2

    async def test_a_failed_apply_changes_nothing_and_takes_nobody_else_down(
        self, fb: FbStore, storage: Storage
    ):
        class Bogus:
            value = "bogus"  # fails fb_groups' CHECK after the chats row was written

        good, bad = row(G2), row(G3)
        object.__setattr__(bad, "state", Bogus())
        # Another coroutine writes through the core's connection meanwhile. The first version
        # rolled the failed script back with a ROLLBACK queued behind that write, which undid
        # it although its caller had been told it succeeded.
        results = await asyncio.gather(
            fb.write_groups([good, bad], now=EPOCH0),
            fb.kv_set("probe", "kept"),
            return_exceptions=True,
        )
        assert isinstance(results[0], sqlite3.IntegrityError) and results[1] is None
        assert await fb.kv_get("probe") == "kept"
        # the chats rows written before the failure were rolled back with it
        assert await storage.get_chat(G2) is None
        assert await storage.get_chat(G3) is None
        assert not storage.db.in_transaction
        await fb.write_groups([good], now=EPOCH0)  # and the next transaction starts clean
        assert await fb.group(G2) is not None

    async def test_a_busy_database_raises_and_leaves_nothing_open(
        self, fb: FbStore, storage: Storage, tmp_path
    ):
        writer = await fb._connect_writer()
        await writer.execute("PRAGMA busy_timeout=50")
        other = sqlite3.connect(tmp_path / "fb.sqlite", isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            with pytest.raises(sqlite3.OperationalError):
                await fb.write_groups([row(G1)], now=EPOCH0)
        finally:
            other.execute("ROLLBACK")
            other.close()
        assert not writer.in_transaction
        await fb.write_groups([row(G1)], now=EPOCH0)
        assert await fb.group(G1) is not None

    async def test_a_cancelled_transaction_is_rolled_back(self, fb: FbStore, storage: Storage):
        entered = asyncio.Event()

        async def hold() -> None:
            async with fb.transaction() as tx:
                await fb._write_groups(tx, [row(G1)], now=EPOCH0)
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(hold())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await storage.get_chat(G1) is None
        await fb.kv_set("after", "1")  # the lock is free again
        assert await fb.kv_get("after") == "1"

    async def test_the_second_connection_needs_a_file(self, clock):
        memory = Storage(":memory:", owner_id=OWNER, clock=clock)
        await memory.open()
        try:
            with pytest.raises(RuntimeError, match="must be a file"):
                await FbStore(memory).write_groups([row(G1)], now=EPOCH0)
        finally:
            await memory.close()


class TestAfterTheCommit:
    """What a change causes must not depend on a step after its commit."""

    async def test_a_failed_matcher_refresh_does_not_fail_the_durable_change(
        self, book: GroupBook, storage: Storage, told: Outbox, invalidations: Invalidations
    ):
        await book.apply_snapshot(snapshot(info(G1, "Оренда")), now=EPOCH0)
        await storage.set_chat_shared(G1, True)
        await storage.subscribe(MOD, G1)

        async def broken(chat_id: int | None = None) -> None:
            raise RuntimeError("matcher down")

        book._invalidate = broken
        lost = snapshot(info(G1, "Оренда", state=GroupState.LOST))
        report = await book.apply_snapshot(lost, now=EPOCH0 + 60)  # no exception, no 503
        assert report.became(GroupState.LOST) == ["Оренда"]
        assert await storage.monitored_chat_ids() == set()
        assert len(told.to(MOD)) == 1  # the subscriber still hears about it

    async def test_a_notice_that_could_not_be_sent_is_sent_later(
        self, book: GroupBook, storage: Storage, told: Outbox, fb: FbStore, clock
    ):
        await book.apply_snapshot(snapshot(info(G1, "Оренда")), now=EPOCH0)
        await storage.set_chat_shared(G1, True)
        await storage.subscribe(MOD, G1)
        told.fail = True
        await book.mark(G1, now=EPOCH0 + 60, state=GroupState.LOST)
        assert told.sent == []
        # the same change again changes nothing, so a retry could never have re-sent it
        assert await book.mark(G1, now=EPOCH0 + 120, state=GroupState.LOST) is None
        told.fail = False
        assert await book.flush() == 1
        assert told.to(MOD) == [
            "⚠️ Акаунт Facebook більше не може читати Оренда; нагляд призупинено."
        ]
        assert await book.flush() == 0  # delivered once
        (row,) = await (await fb.db.execute("SELECT attempts, sent_at FROM fb_notices")).fetchall()
        assert (row["attempts"], row["sent_at"] is not None) == (1, True)

    async def test_notices_nobody_could_deliver_for_a_day_are_dropped(
        self, book: GroupBook, storage: Storage, told: Outbox, clock
    ):
        await book.apply_snapshot(snapshot(info(G1)), now=EPOCH0)
        await storage.subscribe(OWNER, G1)
        told.fail = True
        await book.mark(G1, now=EPOCH0, state=GroupState.LOST)
        clock.tick(86400 + 1)
        told.fail = False
        assert await book.flush() == 0 and told.sent == []

    async def test_two_flushes_at_once_send_each_notice_once(
        self, book: GroupBook, storage: Storage, told: Outbox
    ):
        await book.apply_snapshot(snapshot(info(G1)), now=EPOCH0)
        await storage.subscribe(OWNER, G1)
        told.fail = True
        await book.mark(G1, now=EPOCH0, state=GroupState.LOST)
        told.fail = False
        await asyncio.gather(book.flush(), book.flush())
        assert len(told.to(OWNER)) == 1

    async def test_the_notice_is_written_with_the_change_or_not_at_all(
        self, book: GroupBook, storage: Storage, fb: FbStore, monkeypatch
    ):
        await book.apply_snapshot(snapshot(info(G1)), now=EPOCH0)
        await storage.subscribe(OWNER, G1)

        async def refuse(*args, **kw) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(fb, "add_notices", refuse)
        with pytest.raises(sqlite3.OperationalError):
            await book.mark(G1, now=EPOCH0, state=GroupState.LOST)
        group = await fb.group(G1)
        assert group is not None and group.state is GroupState.MEMBER  # rolled back together


class TestMark:
    async def test_marking_an_unknown_group_is_a_no_op(self, book: GroupBook):
        assert await book.mark(G1, now=EPOCH0, state=GroupState.LOST) is None

    async def test_marking_the_same_value_twice_changes_nothing(self, book: GroupBook):
        await book.apply_snapshot(snapshot(info(G1)), now=EPOCH0)
        first = await book.mark(G1, now=EPOCH0, followed=False, sort_ignored=True)
        assert first is not None and first.after.followed is False and first.after.sort_ignored
        assert await book.mark(G1, now=EPOCH0 + 1, followed=False) is None

    async def test_lost_takes_the_group_out_of_monitoring_at_once(
        self, book: GroupBook, storage: Storage, told: Outbox
    ):
        await book.apply_snapshot(snapshot(info(G1)), now=EPOCH0)
        await storage.subscribe(OWNER, G1)
        change = await book.mark(G1, now=EPOCH0 + 5, state=GroupState.LOST)
        assert change is not None and change.became_unavailable
        assert change.after.state_changed_at == EPOCH0 + 5
        assert await storage.monitored_chat_ids() == set()
        assert len(told.to(OWNER)) == 1


states = st.sampled_from(list(GroupState))
entries = st.builds(
    lambda gid, state, listed: info(gid, state=state, listed=listed),
    st.sampled_from([G1, G2, G3]),
    states,
    st.booleans(),
)


class TestProperties:
    @settings(
        max_examples=60,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        rounds=st.lists(
            st.tuples(st.lists(entries, max_size=3, unique_by=lambda g: g.id), st.booleans()),
            min_size=1,
            max_size=6,
        )
    )
    async def test_available_always_equals_member_and_lost_needs_a_listing(
        self, book: GroupBook, fb: FbStore, storage: Storage, rounds
    ):
        await storage.db.execute("DELETE FROM fb_groups")
        await storage.db.execute("DELETE FROM chats")
        for groups, complete in rounds:
            before = {g.chat_id: g for g in await fb.groups()}
            await book.apply_snapshot(snapshot(*groups, complete=complete), now=EPOCH0)
            for group in await fb.groups():
                chat = await storage.get_chat(group.chat_id)
                assert chat is not None
                assert chat.available == (group.state is GroupState.MEMBER)
                listed_member = any(
                    int(g.id) == group.chat_id and g.listed and g.state is GroupState.MEMBER
                    for g in groups
                )
                if complete and group.state is GroupState.MEMBER:
                    # a complete snapshot leaves only what it lists as a member readable
                    assert listed_member
                previous = before.get(group.chat_id)
                relisted = previous is not None and previous.state is GroupState.LOST
                if relisted and group.state is GroupState.MEMBER:
                    assert any(
                        int(g.id) == group.chat_id and g.listed and g.state is GroupState.MEMBER
                        for g in groups
                    )
