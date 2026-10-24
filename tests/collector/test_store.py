from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

import fbcollector.store as store_module
from fbcollector.clock import FakeClock
from fbcollector.model import (
    Coverage,
    DeferredReason,
    GroupMeta,
    PageState,
    ReadOutcome,
    Sighting,
    Surface,
)
from fbcollector.store import (
    DAY,
    HOUR,
    INVALID,
    KV_EVENTS_DROPPED,
    KV_OUTBOX_DROPPED,
    KV_OUTBOX_REJECTED,
    DatabaseCorruptError,
    ProcessKind,
    SchemaTooNewError,
    Store,
    prune_files,
)
from fbwire import (
    MAX_BATCH_BYTES,
    MAX_TEXT_BYTES,
    BudgetKind,
    EventKind,
    GroupPrivacy,
    GroupState,
    Item,
    ItemOutcome,
    ItemResult,
    ItemsRequest,
    ItemsResponse,
    MonitoredGroup,
    Via,
    items_body,
)

GID = "1234567890"
NAV = BudgetKind.NAVIGATION
SCROLL = BudgetKind.SCROLL


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.sqlite"


@pytest.fixture
async def store(db_path: Path, clock: FakeClock) -> AsyncIterator[Store]:
    s = Store(db_path, clock=clock)
    await s.open()
    yield s
    await s.close()


def make_item(post_id: str, *, group_id: str = GID, text: str = "квартира", **kw: Any) -> Item:
    data: dict[str, Any] = {
        "collector_id": "home-1",
        "seq": 0,
        "group_id": group_id,
        "post_id": post_id,
        "url": f"https://www.facebook.com/groups/{group_id}/posts/{post_id}/",
        "group_name": "Оренда",
        "group_url": f"https://www.facebook.com/groups/{group_id}/",
        "created_at": 1_789_999_000,
        "first_seen_at": 1_790_000_000,
        "via": "feed",
        "text": text,
    }
    data.update(kw)
    return Item.model_validate(data)


def sighting(post_id: str, *, group_id: str | None = GID, **kw: Any) -> Sighting:
    return Sighting(post_id=post_id, group_id=group_id, surface=Surface.FEED, **kw)


def file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def write(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def raw_rows(path: Path, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Read through an independent connection: only committed data is visible there."""
    with sqlite3.connect(path) as conn:
        return conn.execute(sql, args).fetchall()


class TestLifecycle:
    async def test_fresh_file_is_migrated_to_the_latest_version(self, store: Store) -> None:
        assert await store.schema_version() == len(store_module._MIGRATIONS)
        tables = {
            r[0]
            for r in raw_rows(
                Path(store._path), "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "seen",
            "sightings",
            "reads",
            "deferred",
            "outbox",
            "events",
            "budget_events",
            "process_events",
            "blocks",
            "groups",
            "kv",
        } <= tables

    async def test_wal_and_private_file(self, store: Store, db_path: Path) -> None:
        cur = await store.db.execute("PRAGMA journal_mode")
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == "wal"
        assert file_mode(db_path) == 0o600

    async def test_reopen_does_not_rerun_migrations(self, db_path: Path, clock: FakeClock) -> None:
        async with Store(db_path, clock=clock) as first:
            await first.kv_set("k", "v")
        async with Store(db_path, clock=clock) as second:
            assert await second.kv_get("k") == "v"
            assert await second.schema_version() == len(store_module._MIGRATIONS)

    async def test_a_v1_file_gains_the_seen_outcome(
        self, db_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(store_module, "_MIGRATIONS", store_module._MIGRATIONS[:1])
        async with Store(db_path, clock=clock) as old:  # what a v1 collector wrote
            await old.db.execute(
                "INSERT INTO seen (group_id, post_id, created_at, first_seen_at, via)"
                " VALUES (?, '1', NULL, 0, 'feed')",
                (GID,),
            )
        monkeypatch.undo()
        async with Store(db_path, clock=clock) as new:
            assert await new.schema_version() == len(store_module._MIGRATIONS)
            await new.mark_seen(sighting("2"), outcome="queued")
        rows = raw_rows(db_path, "SELECT post_id, outcome FROM seen ORDER BY post_id")
        assert rows == [("1", None), ("2", "queued")]

    async def test_newer_schema_is_refused(self, db_path: Path, clock: FakeClock) -> None:
        async with Store(db_path, clock=clock) as s:
            await s.db.execute("PRAGMA user_version=99")
        with pytest.raises(SchemaTooNewError, match="v99"):
            await Store(db_path, clock=clock).open()

    async def test_garbage_file_is_reported_corrupt(self, db_path: Path, clock: FakeClock) -> None:
        write(db_path, b"this is not a database" * 100)
        s = Store(db_path, clock=clock)
        with pytest.raises(DatabaseCorruptError):
            await s.open()
        with pytest.raises(RuntimeError, match="not open"):
            _ = s.db

    async def test_failed_quick_check_closes_the_connection(
        self, db_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def bad(self: Store) -> None:
            raise DatabaseCorruptError("row 3 missing from index")

        monkeypatch.setattr(Store, "_quick_check", bad)
        s = Store(db_path, clock=clock)
        with pytest.raises(DatabaseCorruptError, match="row 3"):
            await s.open()
        assert s._db is None

    async def test_operational_errors_pass_through(
        self, db_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def locked(self: Store) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(Store, "_quick_check", locked)
        s = Store(db_path, clock=clock)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            await s.open()
        assert s._db is None

    async def test_close_twice_is_harmless(self, db_path: Path, clock: FakeClock) -> None:
        s = Store(db_path, clock=clock)
        await s.open()
        await s.close()
        await s.close()

    async def test_memory_database(self, clock: FakeClock) -> None:
        async with Store(":memory:", clock=clock) as s:
            await s.kv_set("a", "b")
            assert await s.kv_get("a") == "b"


class TestTransactions:
    async def test_failure_rolls_back_and_restores_normal_sync(self, store: Store) -> None:
        with pytest.raises(RuntimeError, match="boom"):
            async with store._tx(durable=True) as db:
                await db.execute("INSERT INTO kv (key, value, updated_at) VALUES ('x','1',0)")
                raise RuntimeError("boom")
        assert await store.kv_get("x") is None
        cur = await store.db.execute("PRAGMA synchronous")
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == 1  # NORMAL

    async def test_concurrent_writers_never_share_a_transaction(self, store: Store) -> None:
        async def writer(i: int) -> None:
            await store.record_budget_event(NAV)
            await store.mark_seen(sighting(str(1000 + i)), item=make_item(str(1000 + i)))
            await store.kv_incr("n")

        await asyncio.gather(*(writer(i) for i in range(40)))
        assert await store.kv_int("n") == 40
        assert await store.outbox_depth() == 40
        assert await store.budget_used(NAV, HOUR) == 40


class TestWriteAheadBudget:
    async def test_event_is_fsynced_and_committed_before_returning(
        self, store: Store, db_path: Path
    ) -> None:
        statements: list[str] = []
        await store.db.set_trace_callback(lambda sql: statements.append(" ".join(sql.split())))
        await store.record_budget_event(NAV, surface=Surface.FEED, read_id=7)
        await store.db.set_trace_callback(lambda _sql: None)
        order = [s.split(" (")[0] for s in statements]
        assert order == [
            "PRAGMA synchronous=FULL",
            "BEGIN IMMEDIATE",
            "INSERT INTO budget_events",
            "COMMIT",
            "PRAGMA synchronous=NORMAL",
        ]
        # Visible to a separate connection the moment the call returned: committed, not cached.
        assert raw_rows(db_path, "SELECT kind, surface, read_id FROM budget_events") == [
            ("navigation", "feed", 7)
        ]

    async def test_process_events_and_blocks_are_durable_too(self, store: Store) -> None:
        statements: list[str] = []
        await store.db.set_trace_callback(statements.append)
        await store.record_process_event(ProcessKind.CHROME_LAUNCH)
        await store.open_block(state=PageState.BLOCKED, reason="checkpoint", until=None, rung=0)
        await store.claim_nonce("abcdefgh", kind="resume")
        await store.db.set_trace_callback(lambda _sql: None)
        assert statements.count("PRAGMA synchronous=FULL") == 3
        assert statements.count("PRAGMA synchronous=NORMAL") == 3

    async def test_gate_state_can_be_written_durably(self, store: Store) -> None:
        from .trace_support import durable_writes, traced

        await store.upsert_group(GroupMeta(id="1"))
        async with traced(store) as statements:
            await store.kv_set("plain", "1")
            await store.kv_set("kept", "1", durable=True)
            await store.kv_set_json("kept_json", {"a": 1}, durable=True)
            await store.set_group_flags("1", ["paused"], durable=True)
            await store.set_group_flags("1", ["quiet"])
        assert durable_writes(statements) == {"kv:kept", "kv:kept_json", 'flags:1:["paused"]'}

    async def test_sliding_windows_over_utc_epoch(self, store: Store, clock: FakeClock) -> None:
        start = clock.time()
        await store.record_budget_event(NAV)
        clock.advance(10 * 60)
        await store.record_budget_event(NAV)
        await store.record_budget_event(SCROLL)
        clock.advance(50 * 60)  # exactly one hour after the first event
        assert await store.budget_used(NAV, HOUR) == 2  # the window's edge counts
        assert await store.budget_used(NAV, 15 * 60) == 0
        assert await store.budget_used(SCROLL, HOUR) == 1
        clock.advance(1)
        assert await store.budget_used(NAV, HOUR) == 1
        assert await store.budget_used(NAV, DAY) == 2
        assert await store.budget_used(NAV, HOUR, now=start + 60) == 2

    async def test_events_from_the_future_still_count(self, store: Store, clock: FakeClock) -> None:
        await store.record_budget_event(NAV)
        clock.jump_wall(-2 * HOUR)  # NTP stepped the clock back
        assert await store.budget_used(NAV, HOUR) == 1
        last = await store.last_budget_event_at()
        assert last is not None
        assert last > clock.time()

    async def test_suspend_moves_the_windows(self, store: Store, clock: FakeClock) -> None:
        await store.record_budget_event(NAV)
        clock.suspend(2 * HOUR)  # wall time moved while the machine slept
        assert await store.budget_used(NAV, HOUR) == 0

    async def test_last_event_by_kind_and_group(self, store: Store, clock: FakeClock) -> None:
        assert await store.last_budget_event_at() is None
        t0 = clock.time()
        await store.record_budget_event(NAV, surface=Surface.GROUP, group_id="7")
        clock.advance(100)
        await store.record_budget_event(SCROLL, surface=Surface.GROUP, group_id="7")
        clock.advance(100)
        await store.record_budget_event(NAV, surface=Surface.FEED)
        assert await store.last_budget_event_at() == t0 + 200
        assert await store.last_budget_event_at(NAV) == t0 + 200
        assert await store.last_budget_event_at(SCROLL) == t0 + 100
        assert await store.last_budget_event_at(group_id="7") == t0 + 100
        assert await store.last_budget_event_at(NAV, group_id="7") == t0
        assert await store.last_budget_event_at(group_id="8") is None

    async def test_budget_usage_reports_remaining(self, store: Store) -> None:
        for _ in range(4):
            await store.record_budget_event(NAV)
        usage = await store.budget_usage([(NAV, 3600, 10), (NAV, 900, 3), (SCROLL, 3600, 30)])
        assert [(w.used, w.remaining) for w in usage] == [(4, 6), (4, 0), (0, 30)]

    async def test_windows_survive_a_restart(self, db_path: Path, clock: FakeClock) -> None:
        async with Store(db_path, clock=clock) as s:
            await s.record_process_event(ProcessKind.PROCESS_START)
            for _ in range(3):
                await s.record_budget_event(NAV)
                clock.advance(60)
            last = await s.last_budget_event_at()
        clock.advance(30)
        async with Store(db_path, clock=clock) as s:
            assert await s.budget_used(NAV, HOUR) == 3
            assert await s.last_budget_event_at() == last
            assert await s.process_events_since(HOUR) == 1

    async def test_process_events_by_kind(self, store: Store, clock: FakeClock) -> None:
        await store.record_process_event(ProcessKind.PROCESS_START, "pid 1")
        await store.record_process_event(ProcessKind.CHROME_LAUNCH)
        await store.record_process_event(ProcessKind.CHROME_LAUNCH)
        assert await store.process_events_since(HOUR) == 3
        assert await store.process_events_since(HOUR, kinds={ProcessKind.CHROME_LAUNCH}) == 2
        clock.advance(HOUR + 1)
        assert await store.process_events_since(HOUR) == 0
        assert await store.process_events_since(DAY) == 3

    async def test_process_events_count_the_unplanned_ones_by_default(self, store: Store) -> None:
        await store.record_process_event(ProcessKind.PROCESS_START, planned=True)
        await store.record_process_event(ProcessKind.CHROME_LAUNCH, "selftest", planned=True)
        await store.record_process_event(ProcessKind.CHROME_LAUNCH)
        await store.record_process_event(ProcessKind.CHROME_LAUNCH, planned=False)
        launches = {ProcessKind.CHROME_LAUNCH}
        assert await store.process_events_since(HOUR) == 2
        assert await store.process_events_since(HOUR, kinds=launches) == 2
        assert await store.process_events_since(HOUR, kinds=launches, planned=True) == 1
        assert await store.process_events_since(HOUR, kinds=launches, planned=None) == 3
        assert await store.process_events_since(HOUR, planned=None) == 4

    async def test_a_v3_file_marks_its_process_events_planned(
        self, db_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """They cannot be classified, and the live collector was stuck in CRASH_LOOP by
        planned launches only: none counts against the unplanned caps, all against the cap
        on every launch."""
        monkeypatch.setattr(store_module, "_MIGRATIONS", store_module._MIGRATIONS[:3])
        async with Store(db_path, clock=clock) as old:  # what a v3 collector wrote
            for kind in ["process_start"] + ["chrome_launch"] * 9:
                await old.db.execute(
                    "INSERT INTO process_events (at, kind) VALUES (?, ?)", (clock.time(), kind)
                )
        monkeypatch.undo()
        async with Store(db_path, clock=clock) as new:
            assert await new.schema_version() == len(store_module._MIGRATIONS)
            assert await new.process_events_since(DAY) == 0
            assert await new.process_events_since(DAY, planned=True) == 10
            assert await new.process_events_since(DAY, planned=None) == 10
            await new.record_process_event(ProcessKind.CHROME_LAUNCH)  # new rows: unplanned
            assert await new.process_events_since(DAY) == 1
        assert raw_rows(db_path, "SELECT planned, COUNT(*) FROM process_events GROUP BY 1") == [
            (0, 1),
            (1, 10),
        ]


class TestSeenAndOutbox:
    async def test_new_post_is_seen_sighted_and_queued_in_one_go(
        self, store: Store, db_path: Path
    ) -> None:
        result = await store.mark_seen(
            sighting("11", position=3, batch=1, cursor="c1", created_at=1_789_999_000),
            via=Via.FEED,
            item=make_item("11"),
        )
        assert result.new
        assert result.seq == 1
        assert result.outbox_depth == 1
        assert await store.is_seen(GID, "11")
        (row,) = raw_rows(db_path, "SELECT payload, size FROM outbox")
        payload = json.loads(row[0])
        assert payload["seq"] == 1  # the outbox assigned it
        assert row[1] == len(row[0].encode())
        assert raw_rows(db_path, "SELECT position, batch, cursor, surface FROM sightings") == [
            (3, 1, "c1", "feed")
        ]
        assert raw_rows(db_path, "SELECT via, created_at FROM seen") == [("feed", 1_789_999_000)]

    async def test_second_sighting_is_recorded_but_not_queued(self, store: Store) -> None:
        await store.mark_seen(sighting("11"), item=make_item("11"))
        again = await store.mark_seen(sighting("11", position=9), item=make_item("11"))
        assert not again.new
        assert again.seq is None
        assert again.outbox_depth == 1
        cur = await store.db.execute("SELECT COUNT(*) FROM sightings")
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == 2

    async def test_unmonitored_post_is_seen_and_never_sent(self, store: Store) -> None:
        result = await store.mark_seen(sighting("12"))
        assert result.new
        assert result.seq is None
        assert await store.outbox_depth() == 0
        assert await store.is_seen(GID, "12")

    async def test_seen_among(self, store: Store) -> None:
        await store.mark_seen(sighting("1"))
        await store.mark_seen(sighting("2", group_id="9"))
        found = await store.seen_among([(GID, "1"), (GID, "2"), ("9", "2"), (GID, "1")])
        assert found == {(GID, "1"), ("9", "2")}

    async def test_record_sighting_alone(self, store: Store, db_path: Path) -> None:
        await store.record_sighting(sighting("77", group_id=None))
        assert raw_rows(db_path, "SELECT post_id, group_id FROM sightings") == [("77", None)]
        assert not await store.is_seen(GID, "77")

    async def test_groupless_story_cannot_be_marked_seen(self, store: Store) -> None:
        with pytest.raises(ValueError, match="defer"):
            await store.mark_seen(sighting("1", group_id=None))

    async def test_item_must_match_the_sighting(self, store: Store) -> None:
        with pytest.raises(ValueError, match="not the sighted story"):
            await store.mark_seen(sighting("1"), item=make_item("2"))

    async def test_a_failed_enqueue_leaves_nothing_behind(self, store: Store) -> None:
        # An item that skipped validation and cannot pass it: the whole transaction must go.
        broken = make_item("5").model_copy(update={"url": "https://evil/"})
        with pytest.raises(ValueError, match="url"):
            await store.mark_seen(sighting("5"), item=broken)
        assert not await store.is_seen(GID, "5")
        assert await store.outbox_depth() == 0
        cur = await store.db.execute("SELECT COUNT(*) FROM sightings")
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == 0

    async def test_text_is_capped_on_the_way_in(self, store: Store, db_path: Path) -> None:
        oversized = make_item("6").model_copy(update={"text": "ж" * MAX_TEXT_BYTES})
        await store.mark_seen(sighting("6"), item=oversized)
        (row,) = raw_rows(db_path, "SELECT payload FROM outbox")
        payload = json.loads(row[0])
        assert payload["truncated"] is True
        assert len(payload["text"].encode()) <= MAX_TEXT_BYTES
        assert "\\u" not in row[0]  # stored as UTF-8, sized as sent (ensure_ascii=False)

    async def test_marking_seen_resolves_a_deferral(self, store: Store) -> None:
        await store.defer("31", DeferredReason.NO_TEXT, group_id=GID)
        await store.mark_seen(sighting("31"), item=make_item("31"))
        assert await store.get_deferred("31") is None

    async def test_outbox_cap_drops_the_oldest_and_counts(
        self, db_path: Path, clock: FakeClock
    ) -> None:
        async with Store(db_path, clock=clock, outbox_cap=3) as s:
            results = [
                await s.mark_seen(sighting(str(i)), item=make_item(str(i))) for i in range(1, 6)
            ]
            assert [r.dropped for r in results] == [0, 0, 0, 1, 1]
            assert results[-1].outbox_depth == 3
            batch = await s.next_batch("home-1")
            assert [row.seq for row in batch] == [3, 4, 5]
            assert await s.kv_int(KV_OUTBOX_DROPPED) == 2

    async def test_a_batch_never_mixes_collector_ids(self, store: Store) -> None:
        """Round 2 F9: rows queued before FBC_COLLECTOR_ID changed keep their own id; a
        request carries one id for all its items."""
        for post_id, owner in (("1", "mac-1"), ("2", "mac-1"), ("3", "home-1"), ("4", "mac-1")):
            await store.mark_seen(sighting(post_id), item=make_item(post_id, collector_id=owner))
        runs = []
        while batch := await store.next_batch("home-1"):
            runs.append((batch[0].collector_id, [row.post_id for row in batch]))
            assert {row.collector_id for row in batch} == {batch[0].collector_id}
            await store.apply_results(
                batch,
                ItemsResponse(
                    results=tuple(
                        ItemResult(group_id=GID, post_id=row.post_id, result=ItemOutcome.STORED)
                        for row in batch
                    )
                ),
            )
        assert runs == [("mac-1", ["1", "2"]), ("home-1", ["3"]), ("mac-1", ["4"])]

    async def test_a_payload_that_names_no_collector_goes_under_the_callers(
        self, store: Store
    ) -> None:
        await store.mark_seen(sighting("5"), item=make_item("5"))
        await store.db.execute("UPDATE outbox SET payload='not json'")
        (row,) = await store.next_batch("laptop-1")
        assert row.collector_id == "laptop-1"

    async def test_already_queued_item_is_not_queued_twice(self, store: Store) -> None:
        await store.mark_seen(sighting("40"), item=make_item("40"))
        # Its seen row is pruned while the brain is still down; the post shows up again.
        await store.db.execute("DELETE FROM seen")
        result = await store.mark_seen(sighting("40"), item=make_item("40"))
        assert result.new
        assert result.seq is None
        assert await store.outbox_depth() == 1

    async def test_sequence_is_never_reused(self, store: Store) -> None:
        first = await store.mark_seen(sighting("1"), item=make_item("1"))
        batch = await store.next_batch("home-1")
        await store.apply_results(
            batch,
            ItemsResponse(
                results=(ItemResult(group_id=GID, post_id="1", result=ItemOutcome.STORED),)
            ),
        )
        second = await store.mark_seen(sighting("2"), item=make_item("2"))
        assert first.seq == 1
        assert second.seq == 2


class TestBatches:
    async def test_empty_outbox(self, store: Store) -> None:
        assert await store.next_batch("home-1") == []

    async def test_batch_body_is_what_the_model_would_send(self, store: Store) -> None:
        for i in range(1, 4):
            await store.mark_seen(sighting(str(i)), item=make_item(str(i), text=f"текст {i}"))
        batch = await store.next_batch("home-1")
        body = items_body("home-1", [row.payload for row in batch])
        request = ItemsRequest.model_validate_json(body)
        assert [item.seq for item in request.items] == [1, 2, 3]
        assert [item.post_id for item in request.items] == ["1", "2", "3"]

    async def test_batches_respect_the_byte_limit(self, store: Store) -> None:
        for i in range(1, 7):
            await store.mark_seen(sighting(str(i)), item=make_item(str(i), text="x" * 1000))
        sizes = [row.size for row in await store.next_batch("home-1", limit=10**9)]
        limit = 2 * sizes[0] + 200
        batch = await store.next_batch("home-1", limit=limit)
        assert len(items_body("home-1", [r.payload for r in batch])) <= limit
        assert [r.seq for r in batch] == [1, 2]

    async def test_real_limit_splits_a_big_backlog(self, store: Store) -> None:
        for i in range(1, 60):
            await store.mark_seen(sighting(str(i)), item=make_item(str(i), text="ґ" * 8000))
        sent: list[int] = []
        while batch := await store.next_batch("home-1"):
            assert len(items_body("home-1", [r.payload for r in batch])) <= MAX_BATCH_BYTES
            sent += [r.seq for r in batch]
            results = tuple(
                ItemResult(group_id=r.group_id, post_id=r.post_id, result=ItemOutcome.STORED)
                for r in batch
            )
            await store.apply_results(batch, ItemsResponse(results=results))
        assert sent == list(range(1, 60))

    async def test_per_item_results(self, store: Store, db_path: Path) -> None:
        for i in range(1, 6):
            await store.mark_seen(sighting(str(i)), item=make_item(str(i)))
        batch = await store.next_batch("home-1")
        response = ItemsResponse(
            results=(
                ItemResult(group_id=GID, post_id="1", result=ItemOutcome.STORED),
                ItemResult(group_id=GID, post_id="2", result=ItemOutcome.DUPLICATE),
                ItemResult(group_id=GID, post_id="3", result=ItemOutcome.NOT_MONITORED),
                ItemResult(group_id=GID, post_id="4", result=ItemOutcome.REJECTED, reason="x"),
                ItemResult(group_id="99", post_id="4", result=ItemOutcome.STORED),
            )
        )
        applied = await store.apply_results(batch, response)
        assert (applied.deleted, applied.rejected, applied.kept, applied.unexpected) == (4, 1, 1, 1)
        assert raw_rows(db_path, "SELECT post_id, attempts, last_error FROM outbox") == [
            ("5", 1, "no result")
        ]
        assert await store.kv_int(KV_OUTBOX_REJECTED) == 1

    async def test_transport_failure_keeps_everything(self, store: Store, db_path: Path) -> None:
        await store.mark_seen(sighting("1"), item=make_item("1"))
        batch = await store.next_batch("home-1")
        await store.note_send_failure(batch, "HTTP 502 " + "x" * 1000)
        await store.note_send_failure(batch, "timeout")
        assert raw_rows(db_path, "SELECT attempts, last_error FROM outbox") == [(2, "timeout")]


class TestEvents:
    async def test_queue_ack_and_ids(self, store: Store) -> None:
        first = await store.enqueue_event(
            EventKind.BLOCK_OPENED, collector_id="home-1", block_id=1, detail="checkpoint"
        )
        second = await store.enqueue_event(
            EventKind.GROUP_LOST, collector_id="home-1", group_id=GID
        )
        assert (first.event_id, second.event_id) == (1, 2)
        assert await store.pending_events() == [first, second]
        assert await store.ack_events([1]) == 1
        assert await store.pending_events() == [second]
        assert await store.ack_events([]) == 0

    async def test_invalid_event_is_not_queued(self, store: Store) -> None:
        with pytest.raises(ValueError, match="group_id"):
            await store.enqueue_event(EventKind.GROUP_LOST, collector_id="home-1")
        assert await store.pending_events() == []

    async def test_cap(self, db_path: Path, clock: FakeClock) -> None:
        async with Store(db_path, clock=clock, events_cap=2) as s:
            for _ in range(4):
                await s.enqueue_event(EventKind.PARSER_BROKEN, collector_id="home-1")
            assert [e.event_id for e in await s.pending_events()] == [3, 4]
            assert await s.kv_int(KV_EVENTS_DROPPED) == 2

    async def test_the_cap_never_drops_a_block_or_a_lost_group(
        self, db_path: Path, clock: FakeClock
    ) -> None:
        async with Store(db_path, clock=clock, events_cap=3) as s:
            block = await s.open_block(state=PageState.BLOCKED, reason="x", until=None, rung=0)
            await s.enqueue_event(EventKind.BLOCK_OPENED, collector_id="home-1", block_id=block.id)
            await s.enqueue_event(EventKind.GROUP_LOST, collector_id="home-1", group_id=GID)
            for _ in range(10):  # an outage full of dedupable noise
                await s.enqueue_event(EventKind.OUTBOX_HIGH, collector_id="home-1")
            await s.enqueue_event(EventKind.LOGGED_OUT, collector_id="home-1", block_id=block.id)
            kinds = [e.kind for e in await s.pending_events()]
            assert kinds == [EventKind.BLOCK_OPENED, EventKind.GROUP_LOST, EventKind.LOGGED_OUT]
            assert await s.kv_int(KV_EVENTS_DROPPED) == 10


class TestDeferred:
    async def test_retry_counter_and_merge(self, store: Store, clock: FakeClock) -> None:
        t0 = clock.time()
        assert await store.defer("50", DeferredReason.NO_GROUP, raw_path="raw/a.gz") == 1
        clock.advance(60)
        assert (
            await store.defer(
                "50", DeferredReason.NO_TEXT, group_id=GID, sighting=sighting("50", group_id=GID)
            )
            == 2
        )
        row = await store.get_deferred("50")
        assert row is not None
        assert row.attempts == 2
        assert row.reason is DeferredReason.NO_TEXT
        assert row.group_id == GID
        assert row.raw_path == "raw/a.gz"  # not wiped by the retry that had none
        assert (row.first_at, row.last_at) == (t0, t0 + 60)

    async def test_list_and_drop(self, store: Store, clock: FakeClock) -> None:
        await store.defer("1", DeferredReason.NO_CREATED_AT)
        clock.advance(100)
        await store.defer("2", DeferredReason.NO_TEXT)
        assert [d.post_id for d in await store.list_deferred()] == ["1", "2"]
        assert [d.post_id for d in await store.list_deferred(since=clock.time())] == ["2"]
        assert await store.drop_deferred("1")
        assert not await store.drop_deferred("1")
        assert await store.get_deferred("1") is None


class TestQuarantine:
    """A post whose item does not fit the wire model was marked seen
    without an outbox row, and replay skipped it for good."""

    async def test_a_quarantined_post_stays_unseen_until_it_becomes_an_item(
        self, store: Store, clock: FakeClock, db_path: Path
    ) -> None:
        first = sighting("21", read_id=4, created_at=1_789_999_000)
        assert await store.quarantine(first, errors=["url:value_error"], raw_path="/r/a.gz") == 1
        assert not await store.is_seen(GID, "21")
        row = await store.get_quarantined(GID, "21")
        assert row is not None
        assert (row.read_id, row.surface, row.created_at) == (4, Surface.FEED, 1_789_999_000)
        assert (row.errors, row.raw_path, row.attempts) == (("url:value_error",), "/r/a.gz", 1)
        assert raw_rows(db_path, "SELECT post_id FROM sightings") == [("21",)]
        clock.advance(60)
        errors = ["group_name:string_too_long", "url:value_error", "url:value_error"]
        assert await store.quarantine(sighting("21", read_id=5), errors=errors) == 2
        row = await store.get_quarantined(GID, "21")
        assert row is not None
        assert row.errors == ("group_name:string_too_long", "url:value_error")
        assert (row.read_id, row.raw_path, row.last_at - row.first_at) == (5, "/r/a.gz", 60)
        assert await store.quarantined_among([(GID, "21"), (GID, "22")]) == {(GID, "21")}
        assert [r.key for r in await store.list_quarantined()] == [(GID, "21")]
        marked = await store.mark_seen(sighting("21"), item=make_item("21"), outcome="queued")
        assert marked.seq is not None  # it became an item: the quarantine is over
        assert await store.get_quarantined(GID, "21") is None
        assert await store.list_quarantined() == []

    async def test_an_invalid_post_is_never_marked_seen_without_an_item(
        self, store: Store, db_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="quarantined, not seen"):
            await store.mark_seen(sighting("23"), outcome=INVALID)
        assert raw_rows(db_path, "SELECT * FROM seen") == []
        with pytest.raises(ValueError, match="deferred, not quarantined"):
            await store.quarantine(sighting("24", group_id=None), errors=["url:x"])

    async def test_a_quarantined_post_too_old_to_be_an_item_is_pruned(
        self, store: Store, clock: FakeClock
    ) -> None:
        await store.quarantine(sighting("25"), errors=["url:value_error"])
        clock.advance(6 * DAY)
        await store.quarantine(sighting("26"), errors=["url:value_error"])
        assert (await store.cleanup()).quarantined == 0
        clock.advance(DAY + 1)
        assert (await store.cleanup()).quarantined == 1  # last seen 7 days ago
        assert [r.post_id for r in await store.list_quarantined()] == ["26"]

    async def test_a_v2_file_gains_the_quarantine(
        self, db_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(store_module, "_MIGRATIONS", store_module._MIGRATIONS[:2])
        async with Store(db_path, clock=clock) as old:  # what a v2 collector wrote
            await old.db.execute(
                "INSERT INTO seen (group_id, post_id, created_at, first_seen_at, via, outcome)"
                " VALUES (?, '27', NULL, 0, 'feed', 'queued')",
                (GID,),
            )
        monkeypatch.undo()
        async with Store(db_path, clock=clock) as new:
            assert await new.schema_version() == len(store_module._MIGRATIONS)
            await new.quarantine(sighting("28"), errors=["url:value_error"])
            assert await new.is_seen(GID, "27")
            assert [r.post_id for r in await new.list_quarantined()] == ["28"]


class TestReads:
    async def test_start_finish_and_read_back(self, store: Store, clock: FakeClock) -> None:
        t0 = clock.time()
        read_id = await store.start_read(Surface.GROUP, group_id=GID, target=t0 - HOUR)
        clock.advance(40)
        await store.finish_read(
            read_id,
            coverage=Coverage(newest=1_790_000_000, oldest=1_789_990_000, target_reached=True),
            steps=2,
            outcome=ReadOutcome.COMPLETE,
            page_state=PageState.OK,
            stats={"friendly_names": {"GroupsCometFeedRegularStoriesPaginationQuery": 2}},
        )
        record = await store.get_read(read_id)
        assert record is not None
        assert record.surface is Surface.GROUP
        assert (record.started_at, record.ended_at) == (t0, t0 + 40)
        assert record.coverage == Coverage(
            newest=1_790_000_000, oldest=1_789_990_000, target=t0 - HOUR, target_reached=True
        )
        assert record.outcome is ReadOutcome.COMPLETE
        assert record.page_state is PageState.OK
        assert record.stats["friendly_names"] == {"GroupsCometFeedRegularStoriesPaginationQuery": 2}
        assert await store.get_read(999) is None

    async def test_last_read_targets(self, store: Store, clock: FakeClock) -> None:
        async def read(surface: Surface, reached: bool, group: str | None = None) -> int:
            read_id = await store.start_read(surface, group_id=group)
            clock.advance(10)
            await store.finish_read(
                read_id,
                coverage=Coverage(target_reached=reached),
                steps=1,
                outcome=ReadOutcome.COMPLETE if reached else ReadOutcome.GAP,
            )
            clock.advance(300)
            return read_id

        good = await read(Surface.FEED, True)
        gap = await read(Surface.FEED, False)
        group_read = await read(Surface.GROUP, True, "7")
        await store.start_read(Surface.FEED)  # unfinished: never a target
        last = await store.last_read(Surface.FEED)
        assert last is not None
        assert last.id == gap
        reached = await store.last_read(Surface.FEED, target_reached=True)
        assert reached is not None
        assert reached.id == good
        grp = await store.last_read(Surface.GROUP, group_id="7")
        assert grp is not None
        assert grp.id == group_read
        assert await store.last_read(Surface.GROUP, group_id="8") is None
        assert await store.last_read(Surface.JOINS) is None


class TestBlocks:
    async def test_open_supersede_and_close_by_id_only(
        self, store: Store, clock: FakeClock
    ) -> None:
        assert await store.current_block() is None
        first = await store.open_block(
            state=PageState.BLOCKED,
            reason="checkpoint",
            until=clock.time() + 6 * HOUR,
            rung=0,
            excerpt="We suspect automated behavior",
            screenshot="shots/1.png",
        )
        assert first.open
        clock.advance(60)
        second = await store.open_block(
            state=PageState.LOGGED_OUT, reason="logout", until=None, rung=1, no_probe=True
        )
        latest = await store.latest_block()
        assert latest == second
        current = await store.current_block()
        assert current is not None
        assert current.id == second.id
        assert current.no_probe
        assert current.until is None
        # A stale resume for the first block must not clear the second.
        assert not await store.close_block(first.id, reason="resumed")
        assert await store.current_block() == second
        assert await store.close_block(second.id, reason="probe ok")
        assert not await store.close_block(second.id, reason="probe ok")
        assert await store.current_block() is None
        closed = await store.latest_block()
        assert closed is not None
        assert (closed.close_reason, closed.closed_at) == ("probe ok", clock.time())
        cur = await store.db.execute("SELECT close_reason FROM blocks WHERE id=?", (first.id,))
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == "superseded"

    async def test_only_one_block_can_be_open(self, store: Store) -> None:
        await store.open_block(state=PageState.BLOCKED, reason="a", until=None, rung=0)
        with pytest.raises(sqlite3.IntegrityError):
            await store.db.execute(
                "INSERT INTO blocks (opened_at, rung, state, reason) VALUES (0, 0, 'blocked', 'b')"
            )

    async def test_block_survives_a_restart(self, db_path: Path, clock: FakeClock) -> None:
        async with Store(db_path, clock=clock) as s:
            block = await s.open_block(state=PageState.UNKNOWN, reason="x", until=5.0, rung=2)
        async with Store(db_path, clock=clock) as s:
            assert await s.current_block() == block


class TestNonces:
    async def test_each_nonce_once_ever(self, db_path: Path, clock: FakeClock) -> None:
        async with Store(db_path, clock=clock) as s:
            assert await s.claim_nonce("n-123456", kind="resume")
            assert not await s.claim_nonce("n-123456", kind="resume")
            assert not await s.claim_nonce("n-123456", kind="refresh_groups")
        async with Store(db_path, clock=clock) as s:
            assert not await s.claim_nonce("n-123456", kind="resume")
            assert await s.claim_nonce("n-654321", kind="resume")


class TestGroups:
    async def test_first_sight_sets_the_baseline_once(self, store: Store, clock: FakeClock) -> None:
        t0 = clock.time()
        row = await store.upsert_group(
            GroupMeta(id=GID, name="Оренда", slug="rent", privacy=GroupPrivacy.PRIVATE),
            synced=True,
        )
        assert row.baseline_at == t0
        assert row.last_synced_at == t0
        assert row.state is GroupState.MEMBER
        assert row.meta().slug == "rent"
        clock.advance(HOUR)
        again = await store.upsert_group(
            GroupMeta(id=GID, join_state="MEMBER", subscribe_status="IS_SUBSCRIBED")
        )
        assert again.baseline_at == t0
        assert again.name == "Оренда"  # an unknown value never wipes a known one
        assert again.privacy is GroupPrivacy.PRIVATE
        assert again.join_state == "MEMBER"
        assert again.meta().is_followed
        assert again.last_synced_at == t0
        assert again.updated_at == t0 + HOUR

    async def test_state_and_sync_counter(self, store: Store) -> None:
        for gid in ("1", "2", "3"):
            await store.upsert_group(GroupMeta(id=gid, name=gid), synced=True)
        assert await store.note_complete_sync({"1", "2"}) == []
        assert await store.note_complete_sync({"1"}) == ["3"]
        assert await store.note_complete_sync({"1", "5"}) == ["2", "3"]
        row = await store.upsert_group(GroupMeta(id="3"), synced=True)
        assert row.missing_syncs == 0
        lost = await store.upsert_group(GroupMeta(id="2"), state=GroupState.LOST)
        assert lost.state is GroupState.LOST
        assert lost.missing_syncs == 2  # a page read is not a sync
        kept = await store.upsert_group(GroupMeta(id="2"))
        assert kept.state is GroupState.LOST

    async def test_becoming_monitored_moves_the_baseline(
        self, store: Store, clock: FakeClock
    ) -> None:
        t0 = clock.time()
        for gid in ("1", "2", "3"):
            await store.upsert_group(GroupMeta(id=gid, name=gid))
        await store.set_monitored([MonitoredGroup(group_id="1")])
        clock.advance(HOUR)
        unknown = await store.set_monitored(
            [
                MonitoredGroup(group_id="1", tier=2),
                MonitoredGroup(group_id="2", listings=True),
                MonitoredGroup(group_id="404"),
            ]
        )
        assert unknown == ["404"]
        rows = {g.id: g for g in await store.list_groups()}
        assert rows["1"].baseline_at == t0  # was already monitored: untouched
        assert rows["1"].tier == 2
        assert rows["2"].baseline_at == t0 + HOUR  # no backfill of what came before
        assert rows["2"].listings
        assert not rows["3"].monitored
        assert await store.monitored_ids() == {"1", "2"}
        await store.set_monitored([MonitoredGroup(group_id="2")])
        assert await store.monitored_ids() == {"2"}
        assert [g.id for g in await store.list_groups(monitored_only=True)] == ["2"]

    async def test_flags_and_missing_group(self, store: Store) -> None:
        await store.upsert_group(GroupMeta(id="1"))
        assert await store.set_group_flags("1", ["sort_ignored", "not_followed", "sort_ignored"])
        row = await store.get_group("1")
        assert row is not None
        assert row.flags == {"sort_ignored", "not_followed"}
        assert not await store.set_group_flags("2", [])
        assert await store.get_group("2") is None


class TestKv:
    async def test_roundtrips(self, store: Store) -> None:
        assert await store.kv_get("x") is None
        assert await store.kv_get_json("x") is None
        await store.kv_set_json("home", {"asn": [1, 2], "tz": "Europe/Sofia"})
        assert await store.kv_get_json("home") == {"asn": [1, 2], "tz": "Europe/Sofia"}
        await store.kv_set("home", "replaced")
        assert await store.kv_get("home") == "replaced"
        assert await store.kv_delete("home")
        assert not await store.kv_delete("home")

    async def test_counters(self, store: Store) -> None:
        assert await store.kv_int("c") == 0
        assert await store.kv_incr("c") == 1
        assert await store.kv_incr("c", 5) == 6
        assert await store.kv_int("c") == 6


class TestCleanup:
    async def test_retention(self, store: Store, clock: FakeClock, db_path: Path) -> None:
        await store.mark_seen(sighting("1"))
        await store.defer("9", DeferredReason.NO_TEXT)
        read_id = await store.start_read(Surface.FEED)
        await store.record_budget_event(NAV)
        await store.record_process_event(ProcessKind.PROCESS_START)
        clock.advance(31 * DAY)
        await store.mark_seen(sighting("2"))
        await store.defer("10", DeferredReason.NO_TEXT)
        await store.record_budget_event(NAV)
        stats = await store.cleanup()
        assert (stats.seen, stats.sightings, stats.reads, stats.deferred) == (1, 1, 1, 1)
        assert (stats.budget_events, stats.process_events) == (1, 1)
        assert not await store.is_seen(GID, "1")
        assert await store.is_seen(GID, "2")
        assert await store.get_read(read_id) is None
        assert await store.get_deferred("10") is not None

    async def test_budget_history_is_never_cut_inside_its_windows(
        self, store: Store, clock: FakeClock
    ) -> None:
        await store.record_budget_event(NAV)
        clock.advance(DAY + HOUR)
        stats = await store.cleanup(seen_retention=HOUR)
        assert stats.budget_events == 0
        assert await store.budget_used(NAV, 2 * DAY) == 1

    async def test_deferred_window(self, store: Store, clock: FakeClock) -> None:
        await store.defer("1", DeferredReason.NO_TEXT)
        clock.advance(6 * DAY)
        assert (await store.cleanup()).deferred == 0
        clock.advance(DAY + 1)
        assert (await store.cleanup()).deferred == 1

    async def test_raw_files(self, store: Store, clock: FakeClock, tmp_path: Path) -> None:
        raw = tmp_path / "raw"
        (raw / "2026-09-25").mkdir(parents=True)
        old = raw / "2026-09-25" / "old.json.gz"
        new = raw / "new.json.gz"
        for path in (old, new):
            path.write_bytes(b"x")
        now = clock.time()
        os.utime(old, (now - 49 * HOUR, now - 49 * HOUR))
        os.utime(new, (now - 47 * HOUR, now - 47 * HOUR))
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"keep")
        os.utime(outside, (now - 99 * DAY, now - 99 * DAY))
        (raw / "link").symlink_to(outside)
        stats = await store.cleanup(raw_dirs=[raw, tmp_path / "missing"])
        assert stats.files == 1
        assert not old.exists()
        assert new.exists()
        assert outside.exists()
        assert (raw / "link").is_symlink()

    def test_prune_refuses_a_symlinked_directory(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        (real / "f").write_bytes(b"x")
        os.utime(real / "f", (0, 0))
        (tmp_path / "raw").symlink_to(real)
        assert prune_files(tmp_path / "raw", before=10**10) == 0
        assert (real / "f").exists()


async def test_a_write_cancelled_while_its_begin_waits_leaves_no_transaction_open(
    store: Store,
) -> None:
    """A hung read is cancelled (``Reader._bounded``); aiosqlite still runs a statement whose
    caller was cancelled, so a cancelled BEGIN must be followed by its ROLLBACK."""
    task = asyncio.create_task(store.kv_set("cut", "1"))
    await asyncio.sleep(0)  # the task now waits for its BEGIN IMMEDIATE
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await store.kv_set("after", "2")  # no "cannot start a transaction within a transaction"
    assert await store.kv_get("after") == "2"
    assert await store.kv_get("cut") is None
