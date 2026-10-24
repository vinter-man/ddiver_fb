"""The ``fb`` track's tables: migrations, items, collectors, events, commands and episodes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ddiver_core.storage import Storage
from ddiver_core_testing import FakeClock
from ddiver_fb import schema
from ddiver_fb.store import FbStore, Mark, is_newer
from fbwire import CollectorState, Event, EventKind
from tests.brain.helpers import CID, EPOCH0, G1, G2, OWNER, status


class TestTrack:
    async def test_the_track_is_recorded_apart_from_the_core_version(self, storage: Storage):
        assert await storage.track_version(schema.TRACK) == len(schema.MIGRATIONS)
        tables = {
            r["name"]
            for r in await (
                await storage.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        assert {"fb_groups", "fb_items", "collector_state", "collector_events"} <= tables
        assert {"fb_commands", "fb_kv", "fb_alerts", "schema_tracks"} <= tables

    async def test_reopening_runs_nothing_twice(self, tmp_path: Path, clock: FakeClock):
        path = tmp_path / "again.sqlite"
        for _ in range(2):
            s = Storage(path, owner_id=OWNER, clock=clock, tracks={schema.TRACK: schema.MIGRATIONS})
            await s.open()
            assert await s.track_version(schema.TRACK) == len(schema.MIGRATIONS)
            await s.close()

    async def test_v3_keeps_the_events_v2_stored_and_keys_them_by_database(
        self, tmp_path: Path, clock: FakeClock
    ):
        path = tmp_path / "v2.sqlite"
        old = Storage(
            path, owner_id=OWNER, clock=clock, tracks={schema.TRACK: schema.MIGRATIONS[:2]}
        )
        await old.open()
        await old.db.execute(
            "INSERT INTO collector_events (collector_id, event_id, kind, at, received_at, "
            "applied_at, alerted_at) VALUES (?, 1, 'group_lost', ?, ?, ?, ?)",
            (CID, EPOCH0, EPOCH0, EPOCH0, EPOCH0),
        )
        await old.close()
        new = Storage(path, owner_id=OWNER, clock=clock, tracks={schema.TRACK: schema.MIGRATIONS})
        await new.open()
        fb = FbStore(new)
        try:
            assert await fb.event_applied((CID, "", 1))  # v2's row, under "no incarnation"
            assert await fb.add_event(event(1), incarnation="laptop-db-7f3a", now=EPOCH0 + 60)
            assert not await fb.event_applied((CID, "laptop-db-7f3a", 1))  # a new database's
            assert await fb.unalerted_events(since=0) != []
        finally:
            await fb.close()
            await new.close()

    def test_no_post_text_column_exists(self):
        """Invariant 8: the brain keeps ids and times, never a post's text, outside the outbox."""
        ddl = " ".join(schema.MIGRATIONS).lower()
        assert " text text" not in ddl and "message" not in ddl


class TestItems:
    async def test_record_is_once_and_has_item_sees_it(self, fb: FbStore):
        assert not await fb.has_item(G1, 5)
        kw: dict[str, Any] = {
            "created_at": EPOCH0,
            "received_at": EPOCH0,
            "via": "feed",
            "late": False,
        }
        assert await fb.record_item(G1, 5, claimed=True, **kw)
        assert not await fb.record_item(G1, 5, claimed=False, **kw)
        assert await fb.has_item(G1, 5)
        assert not await fb.has_item(G2, 5)  # the key is (group, post)

    async def test_times_last_items_and_prune(self, fb: FbStore):
        for pid, created, received in ((1, 100, 1000), (2, 300, 2000), (3, 200, 3000)):
            await fb.record_item(
                G1,
                pid,
                created_at=EPOCH0 + created,
                received_at=EPOCH0 + received,
                via="feed",
                late=False,
                claimed=False,
            )
        await fb.record_item(
            G2, 9, created_at=EPOCH0, received_at=EPOCH0, via="backstop", late=True, claimed=False
        )
        assert await fb.item_times(G1, since=EPOCH0 + 150) == [EPOCH0 + 200, EPOCH0 + 300]
        assert await fb.last_item_times() == {G1: EPOCH0 + 300, G2: EPOCH0}
        assert await fb.prune_items(received_before=EPOCH0 + 2500) == 3
        assert await fb.item_times(G1, since=0) == [EPOCH0 + 200]


class TestCollectors:
    async def test_touch_then_status(self, fb: FbStore):
        await fb.touch_collector(CID, now=EPOCH0, control=True)
        await fb.touch_collector(CID, now=EPOCH0 + 60)
        row = await fb.collector(CID)
        assert row is not None
        assert (row.first_seen_at, row.last_seen_at, row.last_control_at) == (
            EPOCH0,
            EPOCH0 + 60,
            EPOCH0,
        )
        assert row.status is None and row.last_status_at is None
        await fb.save_status(status(CollectorState.QUIET), now=EPOCH0 + 120)
        row = await fb.collector(CID)
        assert row is not None and row.status is not None
        assert row.status.state is CollectorState.QUIET and row.last_status_at == EPOCH0 + 120

    async def test_a_status_the_model_no_longer_accepts_reads_as_none(self, fb: FbStore):
        await fb.save_status(status(), now=EPOCH0)
        await fb.db.execute('UPDATE collector_state SET status=\'{"state": "martian"}\'')
        row = await fb.collector(CID)
        assert row is not None and row.status is None

    async def test_collectors_are_listed_newest_first(self, fb: FbStore):
        await fb.touch_collector("old", now=EPOCH0)
        await fb.touch_collector("new", now=EPOCH0 + 1)
        assert [r.collector_id for r in await fb.collectors()] == ["new", "old"]
        assert await fb.collector("nobody") is None


def event(event_id: int, kind: EventKind = EventKind.OUTBOX_HIGH, **kw) -> Event:
    return Event(collector_id=CID, event_id=event_id, at=EPOCH0, kind=kind, **kw)


class TestEvents:
    async def test_an_event_is_stored_once(self, fb: FbStore):
        assert await fb.add_event(event(1), now=EPOCH0)
        assert not await fb.add_event(event(1), now=EPOCH0 + 5)
        rows = await fb.unalerted_events(since=0)
        assert [(r.event_id, r.kind, r.received_at) for r in rows] == [(1, "outbox_high", EPOCH0)]

    async def test_an_event_is_known_by_its_database_too(self, fb: FbStore):
        """Round 2 F3: ids count per state database; a new one starts again at 1."""
        assert await fb.add_event(event(1), incarnation="mac-db-00000001", now=EPOCH0)
        assert await fb.add_event(event(1), incarnation="laptop-db-000002", now=EPOCH0 + 5)
        await fb.mark_event_applied((CID, "mac-db-00000001", 1), now=EPOCH0 + 1)
        assert not await fb.event_applied((CID, "laptop-db-000002", 1))
        rows = await fb.unalerted_events(since=0)
        assert [r.key for r in rows] == [(CID, "mac-db-00000001", 1), (CID, "laptop-db-000002", 1)]
        await fb.mark_events_alerted([rows[1].key], now=EPOCH0 + 9)
        assert [r.key for r in await fb.unalerted_events(since=0)] == [rows[0].key]

    async def test_applied_is_recorded_apart_from_stored(self, fb: FbStore):
        await fb.add_event(event(1), now=EPOCH0)
        assert not await fb.event_applied((CID, "", 1))
        assert not await fb.event_applied((CID, "", 99))  # never stored
        await fb.mark_event_applied((CID, "", 1), now=EPOCH0 + 5)
        await fb.mark_event_applied((CID, "", 1), now=EPOCH0 + 9)  # the first time stands
        assert await fb.event_applied((CID, "", 1))
        cur = await fb.db.execute("SELECT applied_at FROM collector_events")
        assert [r["applied_at"] for r in await cur.fetchall()] == [EPOCH0 + 5]

    async def test_alerted_expired_and_pruned(self, fb: FbStore):
        await fb.add_event(event(1, EventKind.GROUP_LOST, group_id=str(G1)), now=EPOCH0)
        await fb.add_event(event(2), now=EPOCH0 + 100)
        await fb.add_event(event(3), now=EPOCH0 + 200)
        await fb.mark_events_alerted([(CID, "", 3)], now=EPOCH0 + 300)
        assert await fb.expire_unalerted(received_before=EPOCH0 + 50, now=EPOCH0 + 300) == 1
        assert [r.event_id for r in await fb.unalerted_events(since=0)] == [2]
        assert await fb.prune_events(received_before=EPOCH0 + 150) == 2


class TestCommands:
    async def test_open_finish_expire_count_prune(self, fb: FbStore):
        target: dict[str, Any] = {"collector_id": CID, "lease_generation": 1}
        a = await fb.add_command("resume", "nonce-aaaa", now=EPOCH0, ttl=100, block_id=7, **target)
        b = await fb.add_command("refresh_groups", "nonce-bbbb", now=EPOCH0, ttl=1000, **target)
        assert [c.nonce for c in await fb.open_commands(now=EPOCH0)] == ["nonce-aaaa", "nonce-bbbb"]
        assert await fb.expire_commands(now=EPOCH0 + 100) == 1
        assert [c.id for c in await fb.open_commands(now=EPOCH0 + 100)] == [b.id]
        await fb.finish_commands([b.id, a.id], "done", now=EPOCH0 + 200)
        assert await fb.open_commands(now=EPOCH0 + 200) == []
        assert await fb.count_commands("resume", since=EPOCH0) == 1
        assert await fb.prune_commands(created_before=EPOCH0 + 1) == 2

    async def test_open_commands_by_target(self, fb: FbStore):
        await fb.add_command(
            "resume",
            "nonce-aaaa",
            now=EPOCH0,
            ttl=99,
            block_id=7,
            collector_id=CID,
            lease_generation=1,
            lock_lifted=True,
        )
        await fb.add_command(
            "resume",
            "nonce-bbbb",
            now=EPOCH0,
            ttl=99,
            block_id=7,
            collector_id=CID,
            lease_generation=2,
        )
        await fb.add_command(
            "resume",
            "nonce-cccc",
            now=EPOCH0,
            ttl=99,
            block_id=7,
            collector_id="laptop",
            lease_generation=2,
        )
        await fb.db.execute(  # a v1 row: issued before commands had a target
            "INSERT INTO fb_commands (kind, block_id, nonce, created_at, expires_at) "
            "VALUES ('resume', 7, 'nonce-dddd', ?, ?)",
            (EPOCH0, EPOCH0 + 99),
        )
        mine = await fb.open_commands(now=EPOCH0, collector_id=CID, lease_generation=2)
        assert [c.nonce for c in mine] == ["nonce-bbbb"]
        (old,) = await fb.open_commands(now=EPOCH0, collector_id=CID, lease_generation=1)
        assert old.lock_lifted
        assert len(await fb.open_commands(now=EPOCH0)) == 4


class TestKeyValueAndEpisodes:
    async def test_kv(self, fb: FbStore):
        assert await fb.kv_get("k") is None
        await fb.kv_set("k", "first")
        async with fb.transaction() as tx:
            assert await fb.kv_get("k", db=tx) == "first"
            await fb.kv_set("k", "second", db=tx)
        assert await fb.kv_get("k") == "second"
        await fb.kv_set("k", "third")
        assert await fb.kv_get("k") == "third"
        await fb.kv_delete("k")
        assert await fb.kv_get("k") is None

    async def test_episodes(self, fb: FbStore):
        assert await fb.alert_episode("collector") is None
        await fb.set_alert("collector", "blocked:1", now=EPOCH0)
        await fb.set_alert("collector", "ok", now=EPOCH0 + 1)
        assert await fb.alert_episode("collector") == "ok"
        await fb.clear_alert("collector")
        assert await fb.alert_episode("collector") is None


class TestOrder:
    """A delayed status or snapshot must not overwrite a newer one."""

    @pytest.mark.parametrize(
        ("stored", "incoming", "newer"),
        [
            (None, Mark(None, None, EPOCH0), True),
            (Mark(5, "db-aaaaaaaa", EPOCH0), Mark(6, "db-aaaaaaaa", EPOCH0 - 99), True),
            (Mark(5, "db-aaaaaaaa", EPOCH0), Mark(5, "db-aaaaaaaa", EPOCH0 + 99), False),
            (Mark(5, "db-aaaaaaaa", EPOCH0), Mark(4, "db-aaaaaaaa", EPOCH0 + 99), False),
            # a new counter (the collector's state was reset): its own clock decides
            (Mark(500, "db-aaaaaaaa", EPOCH0), Mark(1, "db-bbbbbbbb", EPOCH0 + 1), True),
            (Mark(500, "db-aaaaaaaa", EPOCH0), Mark(1, "db-bbbbbbbb", EPOCH0 - 1), False),
            # an older collector without a counter
            (Mark(None, None, EPOCH0), Mark(None, None, EPOCH0), True),
            (Mark(None, None, EPOCH0), Mark(None, None, EPOCH0 - 1), False),
            (Mark(None, None, EPOCH0), Mark(3, "db-aaaaaaaa", EPOCH0 + 1), True),
        ],
    )
    def test_is_newer(self, stored: Mark | None, incoming: Mark, newer: bool):
        assert is_newer(stored, incoming) is newer

    @given(
        seqs=st.lists(st.integers(min_value=0, max_value=50), min_size=1, max_size=20),
    )
    def test_in_any_arrival_order_only_the_highest_ends_up_stored(self, seqs: list[int]):
        stored: Mark | None = None
        for seq in seqs:
            incoming = Mark(seq, "db-aaaaaaaa", EPOCH0)
            if is_newer(stored, incoming):
                stored = incoming
        assert stored is not None and stored.seq == max(seqs)

    async def test_a_stale_status_is_not_stored(self, fb: FbStore):
        assert await fb.save_status(
            status(CollectorState.QUIET, seq=8, incarnation="db-aaaaaaaa"), now=EPOCH0
        )
        assert not await fb.save_status(
            status(CollectorState.OK, seq=7, incarnation="db-aaaaaaaa"), now=EPOCH0 + 60
        )
        row = await fb.collector(CID)
        assert row is not None and row.status is not None
        assert row.status.state is CollectorState.QUIET and row.last_status_at == EPOCH0
        assert row.last_seen_at == EPOCH0 + 60  # it was heard, though

    async def test_snapshot_marks(self, fb: FbStore):
        assert await fb.snapshot_mark(CID) is None
        async with fb.transaction() as tx:
            await fb.set_snapshot_mark(CID, Mark(3, "db-aaaaaaaa", EPOCH0), now=EPOCH0, db=tx)
        assert await fb.snapshot_mark(CID) == Mark(3, "db-aaaaaaaa", EPOCH0)
