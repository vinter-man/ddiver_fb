"""The brain's tables (the ``fb`` track), next to the core's.

Groups are ddiver-core ``chats`` rows (``kind = 'group'``, ``id`` = the numeric Facebook group
id, ``username`` always NULL so nothing can ever build a ``t.me`` link from one) plus an
``fb_groups`` row with what only Facebook has: the slug, the URL, privacy, the membership state.
``state`` decides ``chats.available``, and both are written in **one transaction**, so
``monitored_chat_ids()`` never lists a group the account cannot read, not even for a moment.

That transaction runs on a **second connection** that nothing but :meth:`FbStore.transaction`
uses. The core keeps one aiosqlite connection in autocommit mode and every coroutine shares it,
so a ``BEGIN`` issued there would sweep whatever another coroutine executes meanwhile into this
transaction, and a ``ROLLBACK`` after a failure would quietly undo their writes too (which is
what the first version, one ``executescript`` on the shared connection, did whenever the script
failed). On its own connection a transaction contains only its own statements; the other
connection waits for the write lock (``busy_timeout``) like any second writer, and reads never
wait at all (WAL). Everything that has to change together — a group and the notices it
causes, an event's effect and the mark that it was applied, a lease and its commands — goes
through it.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import sqlite3
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

import aiosqlite
from pydantic import ValidationError

from ddiver_core.storage import Storage
from fbwire import Event, GroupPrivacy, GroupState, Item, Status

PRUNE_BATCH: Final = 5000
EventKey = tuple[str, str, int]
"""``(collector_id, incarnation, event_id)``: event ids count per state database, so an event
is known by the database that queued it too. The incarnation is ``""`` from a collector that
sends none."""
_PRUNABLE: Final = frozenset({("fb_ai_verdicts", "judged_at"), ("fb_ai_queue", "queued_at")})
BUSY_TIMEOUT_MS: Final = 5000
"""The core's own value: a writer waits this long for the other connection's lock."""

_UPSERT_CHAT: Final = """
INSERT INTO chats (id, title, username, kind, is_forum, available, updated_at)
VALUES (?, ?, NULL, 'group', 0, ?, ?)
ON CONFLICT(id) DO UPDATE SET title=excluded.title, username=NULL, kind='group', is_forum=0,
  available=excluded.available, updated_at=excluded.updated_at
"""

_UPSERT_GROUP: Final = """
INSERT INTO fb_groups (chat_id, slug, url, privacy, state, followed, last_synced_at,
  last_listed_at, state_changed_at, sort_ignored, evidence_at, evidence_by)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(chat_id) DO UPDATE SET slug=excluded.slug, url=excluded.url,
  privacy=excluded.privacy, state=excluded.state, followed=excluded.followed,
  last_synced_at=excluded.last_synced_at, last_listed_at=excluded.last_listed_at,
  state_changed_at=excluded.state_changed_at, sort_ignored=excluded.sort_ignored,
  evidence_at=excluded.evidence_at, evidence_by=excluded.evidence_by
"""

_GROUP_SELECT: Final = """
SELECT c.id AS chat_id, c.title, c.shared, g.slug, g.url, g.privacy, g.state,
  g.followed, g.last_synced_at, g.last_listed_at, g.state_changed_at, g.listings, g.tier,
  g.sort_ignored, g.evidence_at, g.evidence_by
FROM chats c JOIN fb_groups g ON g.chat_id = c.id
"""


@dataclass(frozen=True, slots=True)
class FbGroup:
    """A group as the brain knows it: the core's chat row joined with ``fb_groups``."""

    chat_id: int
    title: str
    url: str
    state: GroupState
    state_changed_at: int
    slug: str | None = None
    privacy: GroupPrivacy = GroupPrivacy.UNKNOWN
    followed: bool | None = None
    last_synced_at: int | None = None
    last_listed_at: int | None = None
    listings: bool = False
    tier: int = 1
    sort_ignored: bool = False
    shared: bool = False
    evidence_at: int | None = None
    """When the evidence behind this row's state was seen, by ``evidence_by``'s own clock (a
    snapshot's ``taken_at``, an event's ``at``): older evidence from that collector, arriving
    late, does not overwrite it."""
    evidence_by: str | None = None

    def newer_than(self, collector_id: str, at: int) -> bool:
        """Whether this row rests on evidence that collector saw after ``at``."""
        return (
            self.evidence_by == collector_id
            and self.evidence_at is not None
            and at < self.evidence_at
        )

    @property
    def available(self) -> bool:
        """What ``chats.available`` says; always written together with ``state``."""
        return self.state is GroupState.MEMBER

    @property
    def private(self) -> bool:
        return self.privacy is GroupPrivacy.PRIVATE


@dataclass(frozen=True, slots=True)
class GroupChange:
    """One group whose row changed; ``before`` is ``None`` for a group seen the first time."""

    before: FbGroup | None
    after: FbGroup

    @property
    def became_available(self) -> bool:
        return self.after.available and (self.before is None or not self.before.available)

    @property
    def became_unavailable(self) -> bool:
        return self.before is not None and self.before.available and not self.after.available

    @property
    def state_changed(self) -> bool:
        return self.before is None or self.before.state is not self.after.state


@dataclass(frozen=True, slots=True)
class CollectorRow:
    collector_id: str
    first_seen_at: int
    last_seen_at: int
    last_status_at: int | None
    status: Status | None
    last_control_at: int | None
    refused_by: str | None = None
    """The lease holder (``id@incarnation``) a control poll of this collector was refused for,
    until a poll from the same database is not refused (``None``: none was)."""
    refused_incarnation: str | None = None
    """The state database of that refused poll: under the holder's own id, a second one."""
    incarnation: str | None = None
    """The state database its last control poll came from."""


@dataclass(frozen=True, slots=True)
class EventRow:
    collector_id: str
    event_id: int
    kind: str
    at: int
    group_id: int | None
    block_id: int | None
    detail: str | None
    received_at: int
    incarnation: str = ""

    @property
    def key(self) -> EventKey:
        return (self.collector_id, self.incarnation, self.event_id)


@dataclass(frozen=True, slots=True)
class NoticeRow:
    """A message for one user, kept until the bot delivered it (see :meth:`FbStore.add_notices`)."""

    id: int
    user_id: int
    body: str
    created_at: int
    attempts: int


@dataclass(frozen=True, slots=True)
class CommandRow:
    id: int
    kind: str
    block_id: int | None
    nonce: str
    created_at: int
    expires_at: int
    collector_id: str | None = None
    """The lease holder it was issued to; only that collector is ever sent it."""
    lease_generation: int | None = None
    lock_lifted: bool = False


@dataclass(frozen=True, slots=True)
class Mark:
    """Where a collector's status or snapshot stands in that collector's own order."""

    seq: int | None
    incarnation: str | None
    at: int
    """``sent_at`` / ``taken_at``: the collector's clock, compared only with itself."""


def is_newer(stored: Mark | None, incoming: Mark) -> bool:
    """Whether ``incoming`` may replace ``stored`` (the last one applied from that collector).

    The sequence number decides when both sides carry one from the same counter; an equal one
    is the same message again. Otherwise (an older collector, or a new counter after its state
    was reset) the collector's own clock decides, so a delayed message from before the reset
    cannot win either; an equal time is a resend and changes nothing it has not already.
    """
    if stored is None:
        return True
    if (
        incoming.seq is not None
        and stored.seq is not None
        and incoming.incarnation == stored.incarnation
    ):
        return incoming.seq > stored.seq
    return incoming.at >= stored.at


def planned_update(group: FbGroup, *, now: int, **changes: Any) -> FbGroup:
    """``group`` with ``changes``; a new state gets ``now`` as its ``state_changed_at``."""
    after = replace(group, **changes)
    if after.state is not group.state:
        after = replace(after, state_changed_at=now)
    return after


def _iso(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat(timespec="seconds")


def _opt_bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


class FbStore:
    """Data access for the ``fb`` track. Every method takes ``now`` (epoch seconds) explicitly."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        # Serialises writers of group rows, so a sync's before/after diff is not raced by an
        # event marking a group lost in between.
        self.groups_lock = asyncio.Lock()
        self._tx_lock = asyncio.Lock()
        self._writer: aiosqlite.Connection | None = None

    @property
    def db(self) -> aiosqlite.Connection:
        """The core's shared connection, for reads and single-statement writes."""
        return self._storage.db

    # -- transactions ----------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """``BEGIN IMMEDIATE`` … ``COMMIT`` on the brain's own connection; rolled back on any
        exception (cancellation included), and nothing but the caller's statements in it.

        Inside, write only through the connection this yields: a write on the core's
        connection would wait for this transaction's lock, which is held until it returns.
        """
        async with self._tx_lock:
            db = await self._connect_writer()
            await db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    await db.execute("ROLLBACK")
                raise
            try:
                await db.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    await db.execute("ROLLBACK")
                raise

    async def _connect_writer(self) -> aiosqlite.Connection:
        if self._writer is not None:
            return self._writer
        cur = await self.db.execute("PRAGMA database_list")
        path = next((str(r["file"]) for r in await cur.fetchall() if r["name"] == "main"), "")
        if not path:
            msg = "the brain's database must be a file: its transactions use a second connection"
            raise RuntimeError(msg)
        db = await aiosqlite.connect(path, isolation_level=None)
        try:
            db.row_factory = aiosqlite.Row
            await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA synchronous=NORMAL")
        except BaseException:
            # aiosqlite's worker thread is not a daemon: a leaked connection hangs the exit.
            await db.close()
            raise
        self._writer = db
        return db

    async def close(self) -> None:
        """Close the transaction connection; the core's connection is the core's to close."""
        async with self._tx_lock:
            if self._writer is not None:
                db, self._writer = self._writer, None
                await db.close()

    # -- groups ----------------------------------------------------------------------------

    @staticmethod
    def _group(row: aiosqlite.Row) -> FbGroup:
        return FbGroup(
            chat_id=int(row["chat_id"]),
            title=str(row["title"]),
            url=str(row["url"]),
            state=GroupState(row["state"]),
            state_changed_at=int(row["state_changed_at"]),
            slug=row["slug"],
            privacy=GroupPrivacy(row["privacy"]),
            followed=_opt_bool(row["followed"]),
            last_synced_at=row["last_synced_at"],
            last_listed_at=row["last_listed_at"],
            listings=bool(row["listings"]),
            tier=int(row["tier"]),
            sort_ignored=bool(row["sort_ignored"]),
            shared=bool(row["shared"]),
            evidence_at=row["evidence_at"],
            evidence_by=row["evidence_by"],
        )

    async def groups(self) -> list[FbGroup]:
        cur = await self.db.execute(_GROUP_SELECT + " ORDER BY c.title COLLATE NOCASE, c.id")
        return [self._group(r) for r in await cur.fetchall()]

    async def group(self, chat_id: int) -> FbGroup | None:
        cur = await self.db.execute(_GROUP_SELECT + " WHERE c.id=?", (chat_id,))
        row = await cur.fetchone()
        return self._group(row) if row else None

    async def set_listings(self, chat_id: int, on: bool) -> bool:
        """The owner's per-group switch for the listings sort (DESIGN: ``CHRONOLOGICAL_LISTINGS``,
        only where phase 0 shows the plain sort missing listing posts); ``False``: no such
        group. :meth:`write_groups` never writes ``listings`` or ``tier``, so a snapshot being
        applied meanwhile cannot put back the value it read before this."""
        cur = await self.db.execute(
            "UPDATE fb_groups SET listings=? WHERE chat_id=?", (int(on), chat_id)
        )
        return cur.rowcount == 1

    async def write_groups(
        self, rows: Sequence[FbGroup], *, now: int, db: aiosqlite.Connection | None = None
    ) -> None:
        """Write whole group rows (chat and ``fb_groups``) atomically.

        ``db`` is an open :meth:`transaction` to join; without one, this is its own. The caller
        holds :attr:`groups_lock`. ``chats.shared`` is never touched here: sharing is the
        owner's decision, not the collector's.
        """
        if not rows:
            return
        if db is None:
            async with self.transaction() as tx:
                await self._write_groups(tx, rows, now=now)
        else:
            await self._write_groups(db, rows, now=now)

    @staticmethod
    async def _write_groups(db: aiosqlite.Connection, rows: Sequence[FbGroup], *, now: int) -> None:
        stamp = _iso(now)
        await db.executemany(
            _UPSERT_CHAT, [(g.chat_id, g.title, int(g.available), stamp) for g in rows]
        )
        await db.executemany(
            _UPSERT_GROUP,
            [
                (
                    g.chat_id,
                    g.slug,
                    g.url,
                    g.privacy.value,
                    g.state.value,
                    None if g.followed is None else int(g.followed),
                    g.last_synced_at,
                    g.last_listed_at,
                    g.state_changed_at,
                    int(g.sort_ignored),
                    g.evidence_at,
                    g.evidence_by,
                )
                for g in rows
            ],
        )

    # -- items -----------------------------------------------------------------------------

    async def has_item(self, group_id: int, post_id: int) -> bool:
        cur = await self.db.execute(
            "SELECT 1 FROM fb_items WHERE group_id=? AND post_id=?", (group_id, post_id)
        )
        return await cur.fetchone() is not None

    async def record_item(
        self,
        group_id: int,
        post_id: int,
        *,
        created_at: int,
        received_at: int,
        via: str,
        late: bool,
        claimed: bool,
    ) -> bool:
        """Remember a processed post; ``True`` when it was new. Written after every claim."""
        cur = await self.db.execute(
            """INSERT OR IGNORE INTO fb_items
                 (group_id, post_id, created_at, received_at, via, late, claimed)
               VALUES (?,?,?,?,?,?,?)""",
            (group_id, post_id, created_at, received_at, via, int(late), int(claimed)),
        )
        return cur.rowcount == 1

    async def record_text(self, item: Item, *, received_at: int) -> None:
        """Keep a watched group's post as it came (``fb_texts``); a resend changes nothing."""
        await self.db.execute(
            "INSERT OR IGNORE INTO fb_texts (group_id, post_id, received_at, item) "
            "VALUES (?,?,?,?)",
            (int(item.group_id), int(item.post_id), received_at, item.model_dump_json()),
        )

    async def prune_texts(self, *, received_before: int) -> int:
        return await self._prune("fb_texts", "received_at", received_before)

    async def item_times(self, group_id: int, *, since: int) -> list[int]:
        """``created_at`` of this group's recorded posts since ``since``, oldest first."""
        cur = await self.db.execute(
            "SELECT created_at FROM fb_items WHERE group_id=? AND created_at>=? "
            "ORDER BY created_at",
            (group_id, since),
        )
        return [int(r["created_at"]) for r in await cur.fetchall()]

    async def last_item_times(self) -> dict[int, int]:
        """Newest ``created_at`` per group."""
        cur = await self.db.execute(
            "SELECT group_id, MAX(created_at) AS last FROM fb_items GROUP BY group_id"
        )
        return {int(r["group_id"]): int(r["last"]) for r in await cur.fetchall()}

    async def prune_items(self, *, received_before: int) -> int:
        return await self._prune("fb_items", "received_at", received_before)

    async def prune_rows(self, table: str, column: str, before: int) -> int:
        """Rows of one of the AI rule's tables older than ``before`` (see ``ai.py``)."""
        if (table, column) not in _PRUNABLE:
            msg = f"not a prunable table: {table}.{column}"
            raise ValueError(msg)
        return await self._prune(table, column, before)

    async def _prune(self, table: str, column: str, before: int) -> int:
        removed = 0
        while True:
            cur = await self.db.execute(
                f"DELETE FROM {table} WHERE rowid IN "  # noqa: S608 - names are literals here
                f"(SELECT rowid FROM {table} WHERE {column}<? LIMIT ?)",
                (before, PRUNE_BATCH),
            )
            if cur.rowcount <= 0:
                return removed
            removed += cur.rowcount

    # -- collectors ------------------------------------------------------------------------

    async def touch_collector(
        self,
        collector_id: str,
        *,
        now: int,
        control: bool = False,
        incarnation: str | None = None,
        db: aiosqlite.Connection | None = None,
    ) -> None:
        """Note that a collector was heard from; ``control`` for a control poll, which also
        records the state database it came from (``/lease <id>`` moves the lease to that)."""
        await (db or self.db).execute(
            """INSERT INTO collector_state (collector_id, first_seen_at, last_seen_at,
                 last_control_at, incarnation)
               VALUES (?,?,?,?,?)
               ON CONFLICT(collector_id) DO UPDATE SET last_seen_at=excluded.last_seen_at,
                 last_control_at=COALESCE(excluded.last_control_at,
                                          collector_state.last_control_at),
                 incarnation=CASE WHEN excluded.last_control_at IS NULL
                                  THEN collector_state.incarnation
                                  ELSE excluded.incarnation END""",
            (collector_id, now, now, now if control else None, incarnation if control else None),
        )

    async def set_refused(
        self,
        collector_id: str,
        incarnation: str | None,
        holder: str | None,
        *,
        db: aiosqlite.Connection,
    ) -> None:
        """Record whom a control poll was refused for (``None``: it was not), in its answer's
        transaction; the monitor tells the owner about a contender from this, later.

        A poll that was not refused clears only a refusal of its own database: under the
        holder's id, the holder's polls must not wipe out the second database's.
        """
        if holder is not None:
            await db.execute(
                "UPDATE collector_state SET refused_by=?, refused_incarnation=? "
                "WHERE collector_id=?",
                (holder, incarnation, collector_id),
            )
            return
        await db.execute(
            "UPDATE collector_state SET refused_by=NULL, refused_incarnation=NULL "
            "WHERE collector_id=? AND refused_incarnation IS ?",
            (collector_id, incarnation),
        )

    async def save_status(self, status: Status, *, now: int) -> bool:
        """Store a status unless one newer than it was already stored; ``False``: stale."""
        incoming = Mark(status.seq, status.incarnation, status.sent_at)
        async with self.transaction() as tx:
            await self.touch_collector(status.collector_id, now=now, db=tx)
            cur = await tx.execute(
                "SELECT status_seq, status_incarnation, status_sent_at FROM collector_state "
                "WHERE collector_id=?",
                (status.collector_id,),
            )
            row = await cur.fetchone()
            stored = (
                Mark(row["status_seq"], row["status_incarnation"], int(row["status_sent_at"]))
                if row is not None and row["status_sent_at"] is not None
                else None
            )
            if not is_newer(stored, incoming):
                return False
            await tx.execute(
                """UPDATE collector_state SET status=?, last_status_at=?, status_seq=?,
                     status_incarnation=?, status_sent_at=? WHERE collector_id=?""",
                (
                    status.model_dump_json(),
                    now,
                    status.seq,
                    status.incarnation,
                    status.sent_at,
                    status.collector_id,
                ),
            )
        return True

    async def snapshot_mark(self, collector_id: str) -> Mark | None:
        """Where the last snapshot applied from this collector stands."""
        cur = await self.db.execute(
            "SELECT snapshot_seq, snapshot_incarnation, snapshot_taken_at FROM collector_state "
            "WHERE collector_id=?",
            (collector_id,),
        )
        row = await cur.fetchone()
        if row is None or row["snapshot_taken_at"] is None:
            return None
        return Mark(row["snapshot_seq"], row["snapshot_incarnation"], int(row["snapshot_taken_at"]))

    async def set_snapshot_mark(
        self, collector_id: str, mark: Mark, *, now: int, db: aiosqlite.Connection
    ) -> None:
        """Record the snapshot just applied, in the transaction that applied it."""
        await self.touch_collector(collector_id, now=now, db=db)
        await db.execute(
            """UPDATE collector_state SET snapshot_seq=?, snapshot_incarnation=?,
                 snapshot_taken_at=? WHERE collector_id=?""",
            (mark.seq, mark.incarnation, mark.at, collector_id),
        )

    @staticmethod
    def _collector(row: aiosqlite.Row) -> CollectorRow:
        status: Status | None = None
        if row["status"]:
            with contextlib.suppress(ValidationError):
                # A status written by an older build may not validate against today's model;
                # the next one to arrive replaces it.
                status = Status.model_validate_json(row["status"])
        return CollectorRow(
            collector_id=str(row["collector_id"]),
            first_seen_at=int(row["first_seen_at"]),
            last_seen_at=int(row["last_seen_at"]),
            last_status_at=row["last_status_at"],
            status=status,
            last_control_at=row["last_control_at"],
            refused_by=row["refused_by"],
            refused_incarnation=row["refused_incarnation"],
            incarnation=row["incarnation"],
        )

    async def collectors(self) -> list[CollectorRow]:
        cur = await self.db.execute("SELECT * FROM collector_state ORDER BY last_seen_at DESC")
        return [self._collector(r) for r in await cur.fetchall()]

    async def collector(self, collector_id: str) -> CollectorRow | None:
        cur = await self.db.execute(
            "SELECT * FROM collector_state WHERE collector_id=?", (collector_id,)
        )
        row = await cur.fetchone()
        return self._collector(row) if row else None

    # -- events ----------------------------------------------------------------------------

    async def add_event(self, event: Event, *, incarnation: str = "", now: int) -> bool:
        """Store an event once; ``True`` the first time (a resend after a lost answer is not).

        Storing is not applying: see :meth:`event_applied`.
        """
        cur = await self.db.execute(
            """INSERT OR IGNORE INTO collector_events (collector_id, incarnation, event_id,
                 kind, at, group_id, block_id, detail, received_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                event.collector_id,
                incarnation,
                event.event_id,
                event.kind.value,
                event.at,
                None if event.group_id is None else int(event.group_id),
                event.block_id,
                event.detail,
                now,
            ),
        )
        return cur.rowcount == 1

    async def event_applied(self, key: EventKey) -> bool:
        """Whether what the event says has been applied, so a resend may be acked as is."""
        cur = await self.db.execute(
            "SELECT applied_at FROM collector_events "
            "WHERE collector_id=? AND incarnation=? AND event_id=?",
            key,
        )
        row = await cur.fetchone()
        return row is not None and row["applied_at"] is not None

    async def mark_event_applied(
        self, key: EventKey, *, now: int, db: aiosqlite.Connection | None = None
    ) -> None:
        """Record that an event's effect is in place; pass the transaction that applied it."""
        await (db or self.db).execute(
            "UPDATE collector_events SET applied_at=? "
            "WHERE collector_id=? AND incarnation=? AND event_id=? AND applied_at IS NULL",
            (now, *key),
        )

    async def unalerted_events(self, *, since: int, limit: int = 20) -> list[EventRow]:
        cur = await self.db.execute(
            """SELECT * FROM collector_events WHERE alerted_at IS NULL AND received_at>=?
               ORDER BY received_at, collector_id, incarnation, event_id LIMIT ?""",
            (since, limit),
        )
        return [
            EventRow(
                collector_id=str(r["collector_id"]),
                event_id=int(r["event_id"]),
                kind=str(r["kind"]),
                at=int(r["at"]),
                group_id=r["group_id"],
                block_id=r["block_id"],
                detail=r["detail"],
                received_at=int(r["received_at"]),
                incarnation=str(r["incarnation"]),
            )
            for r in await cur.fetchall()
        ]

    async def mark_events_alerted(self, keys: Iterable[EventKey], *, now: int) -> None:
        await self.db.executemany(
            "UPDATE collector_events SET alerted_at=? "
            "WHERE collector_id=? AND incarnation=? AND event_id=?",
            [(now, *key) for key in keys],
        )

    async def expire_unalerted(self, *, received_before: int, now: int) -> int:
        """Give up on alerts nobody could deliver for a day: a stale alert misleads."""
        cur = await self.db.execute(
            "UPDATE collector_events SET alerted_at=? WHERE alerted_at IS NULL AND received_at<?",
            (now, received_before),
        )
        return cur.rowcount

    async def prune_events(self, *, received_before: int) -> int:
        return await self._prune("collector_events", "received_at", received_before)

    # -- commands --------------------------------------------------------------------------

    async def add_command(
        self,
        kind: str,
        nonce: str,
        *,
        now: int,
        ttl: int,
        collector_id: str,
        lease_generation: int,
        block_id: int | None = None,
        lock_lifted: bool = False,
    ) -> CommandRow:
        cur = await self.db.execute(
            """INSERT INTO fb_commands (kind, block_id, nonce, created_at, expires_at,
                 collector_id, lease_generation, lock_lifted)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                kind,
                block_id,
                nonce,
                now,
                now + ttl,
                collector_id,
                lease_generation,
                int(lock_lifted),
            ),
        )
        return CommandRow(
            int(cur.lastrowid or 0),
            kind,
            block_id,
            nonce,
            now,
            now + ttl,
            collector_id,
            lease_generation,
            lock_lifted,
        )

    async def open_commands(
        self,
        *,
        now: int,
        collector_id: str | None = None,
        lease_generation: int | None = None,
        db: aiosqlite.Connection | None = None,
    ) -> list[CommandRow]:
        """Open commands; with ``collector_id``, only those issued to it under
        ``lease_generation`` (a command without a target is nobody's)."""
        sql = "SELECT * FROM fb_commands WHERE done_at IS NULL AND expires_at>?"
        args: tuple[Any, ...] = (now,)
        if collector_id is not None:
            sql += " AND collector_id=? AND lease_generation IS ?"
            args = (now, collector_id, lease_generation)
        cur = await (db or self.db).execute(sql + " ORDER BY id", args)
        return [
            CommandRow(
                int(r["id"]),
                str(r["kind"]),
                r["block_id"],
                str(r["nonce"]),
                int(r["created_at"]),
                int(r["expires_at"]),
                r["collector_id"],
                r["lease_generation"],
                bool(r["lock_lifted"]),
            )
            for r in await cur.fetchall()
        ]

    async def finish_commands(
        self,
        ids: Iterable[int],
        reason: str,
        *,
        now: int,
        db: aiosqlite.Connection | None = None,
    ) -> None:
        await (db or self.db).executemany(
            "UPDATE fb_commands SET done_at=?, done_reason=? WHERE id=? AND done_at IS NULL",
            [(now, reason, command_id) for command_id in ids],
        )

    async def expire_commands(self, *, now: int) -> int:
        cur = await self.db.execute(
            """UPDATE fb_commands SET done_at=?, done_reason='expired'
               WHERE done_at IS NULL AND expires_at<=?""",
            (now, now),
        )
        return cur.rowcount

    async def count_commands(self, kind: str, *, since: int) -> int:
        cur = await self.db.execute(
            "SELECT COUNT(*) AS n FROM fb_commands WHERE kind=? AND created_at>=?", (kind, since)
        )
        row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def prune_commands(self, *, created_before: int) -> int:
        return await self._prune("fb_commands", "created_at", created_before)

    # -- notices -----------------------------------------------------------------------------

    async def add_notices(
        self,
        notices: Iterable[tuple[int, str]],
        *,
        now: int,
        db: aiosqlite.Connection | None = None,
    ) -> None:
        """Queue ``(user_id, text)`` messages in the transaction whose change they announce
        (``db``), or on their own when nothing else has to change with them.

        Telling a subscriber that a group became unreadable used to happen after the commit,
        once: a failed send, or a failure before it, lost the message for good, because the
        retried snapshot no longer changed anything. A queued notice is sent until the bot
        delivers it (or for a day, like an event's alert).
        """
        await (db or self.db).executemany(
            "INSERT INTO fb_notices (user_id, body, created_at) VALUES (?,?,?)",
            [(user_id, body, now) for user_id, body in notices],
        )

    async def pending_notices(self, *, since: int, limit: int = 50) -> list[NoticeRow]:
        cur = await self.db.execute(
            """SELECT * FROM fb_notices WHERE sent_at IS NULL AND created_at>=?
               ORDER BY id LIMIT ?""",
            (since, limit),
        )
        return [
            NoticeRow(
                id=int(r["id"]),
                user_id=int(r["user_id"]),
                body=str(r["body"]),
                created_at=int(r["created_at"]),
                attempts=int(r["attempts"]),
            )
            for r in await cur.fetchall()
        ]

    async def notice_sent(self, notice_id: int, *, now: int) -> None:
        await self.db.execute("UPDATE fb_notices SET sent_at=? WHERE id=?", (now, notice_id))

    async def notice_failed(self, notice_id: int) -> None:
        await self.db.execute("UPDATE fb_notices SET attempts=attempts+1 WHERE id=?", (notice_id,))

    async def drop_old_notices(self, *, created_before: int) -> int:
        """Forget notices nobody could deliver for a day, and sent ones past that age."""
        return await self._prune("fb_notices", "created_at", created_before)

    # -- key/value and alert episodes --------------------------------------------------------

    async def kv_get(self, key: str, *, db: aiosqlite.Connection | None = None) -> str | None:
        cur = await (db or self.db).execute("SELECT value FROM fb_kv WHERE key=?", (key,))
        row = await cur.fetchone()
        return str(row["value"]) if row else None

    async def kv_set(self, key: str, value: str, *, db: aiosqlite.Connection | None = None) -> None:
        await (db or self.db).execute(
            "INSERT INTO fb_kv (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    async def kv_delete(self, key: str, *, db: aiosqlite.Connection | None = None) -> None:
        await (db or self.db).execute("DELETE FROM fb_kv WHERE key=?", (key,))

    async def alert_episode(self, key: str) -> str | None:
        cur = await self.db.execute("SELECT episode FROM fb_alerts WHERE key=?", (key,))
        row = await cur.fetchone()
        return str(row["episode"]) if row else None

    async def set_alert(self, key: str, episode: str, *, now: int) -> None:
        await self.db.execute(
            "INSERT INTO fb_alerts (key, episode, sent_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET episode=excluded.episode, sent_at=excluded.sent_at",
            (key, episode, now),
        )

    async def clear_alert(self, key: str) -> None:
        await self.db.execute("DELETE FROM fb_alerts WHERE key=?", (key,))

    async def clear_alerts(self, keys: Iterable[str]) -> None:
        await self.db.executemany("DELETE FROM fb_alerts WHERE key=?", [(key,) for key in keys])
