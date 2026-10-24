"""The collector's durable state: one SQLite file (WAL), one connection, one writer.

Three guarantees live here and nowhere else:

- **Write-ahead budget** (invariant 6). A navigation or scroll step is recorded, committed and
  fsynced (``synchronous=FULL`` for that commit) *before* the caller issues it, so a crash or a
  power cut between the two can only over-count, never under-count. Caps are counted over
  sliding UTC windows straight from that table, and the spacing rule reads the last recorded
  event, so both survive restarts. Process starts and Chrome launches are recorded the same way
  for the crash-loop gate.
- **Durable before acknowledged** (invariant 5). A new post's ``seen`` row, its sighting and its
  outbox row are written in one transaction; an outbox row is deleted only when the brain
  returned a result for it. The outbox is capped (oldest dropped, counted) so an unreachable
  brain cannot fill the disk. A post whose item does not fit the wire model is never marked
  seen: it is *quarantined* (``quarantine``: its identity, the step payload it came from, the
  categories of its validation errors and how often it failed), retried on every sighting and by
  ``fbcollector replay``, and leaves the table only by becoming an item (``mark_seen``) or by
  growing too old to be one.
- **A block is on disk before anything else happens** (invariant 3). Blocks are durable writes,
  at most one is open (a partial unique index enforces it), and a block closes only by its own
  id — a stale ``/resume`` cannot clear a newer block. The small state that keeps a gate shut
  (the ladder's backoff and UNKNOWN streak, halts, a broken parser, backstop-only mode, a
  paused group) is written with ``durable=True`` as well.

Every statement runs under one asyncio lock. aiosqlite serialises statements but not
transactions: without the lock, a coroutine's statement could land inside another coroutine's
open ``BEGIN`` on the shared connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import itertools
import json
import logging
import os
import sqlite3
from collections.abc import AsyncIterator, Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Self

import aiosqlite

from fbcollector.clock import Clock
from fbcollector.model import (
    Coverage,
    DeferredReason,
    GroupMeta,
    PageState,
    ReadOutcome,
    ReadRecord,
    Sighting,
    Surface,
)
from fbwire.models import (
    MAX_BATCH_BYTES,
    BudgetKind,
    BudgetWindow,
    Event,
    EventKind,
    GroupPrivacy,
    GroupState,
    Item,
    ItemOutcome,
    ItemsResponse,
    MonitoredGroup,
    Via,
    items_overhead,
    plan_batches,
)

log = logging.getLogger(__name__)

HOUR: Final = 3600.0
DAY: Final = 86400.0
DEFAULT_OUTBOX_CAP: Final = 5000
DEFAULT_EVENTS_CAP: Final = 1000
BUDGET_KEEP_FLOOR: Final = 2 * DAY
"""Budget and process events are never pruned younger than this, whatever the caller asks:
the widest cap window is 24 h and the crash-loop gate looks back a day."""

KV_OUTBOX_DROPPED: Final = "outbox_dropped"
KV_OUTBOX_REJECTED: Final = "outbox_rejected"
KV_EVENTS_DROPPED: Final = "events_dropped"
KV_REFUSED: Final = "uplink_refused_"
"""Prefix of the per-endpoint counters of requests the brain answered but refused."""
UPLINK_ENDPOINTS: Final = ("control", "groups", "events", "items", "status")
NONCE_PREFIX: Final = "nonce:"
KV_LAST_COMPLETE_SYNC: Final = "last_complete_sync_at"
"""When the latest complete group sync was applied (epoch seconds, the store's clock)."""
KEPT_EVENTS: Final = frozenset(
    {EventKind.BLOCK_OPENED, EventKind.BLOCK_CLOSED, EventKind.LOGGED_OUT, EventKind.GROUP_LOST}
)
"""Events the capped queue never drops."""


class ProcessKind(enum.StrEnum):
    PROCESS_START = "process_start"
    CHROME_LAUNCH = "chrome_launch"


class DatabaseCorruptError(Exception):
    """``PRAGMA quick_check`` failed; the file needs restoring or removing by hand."""


class SchemaTooNewError(Exception):
    """The file was written by a newer collector; opening it would risk losing its state."""


_MIGRATIONS: Final[list[str]] = [
    # v1
    """
    CREATE TABLE kv (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL,
      updated_at REAL NOT NULL
    );
    CREATE TABLE seen (
      group_id TEXT NOT NULL,
      post_id TEXT NOT NULL,
      created_at INTEGER,
      first_seen_at REAL NOT NULL,
      via TEXT,
      PRIMARY KEY (group_id, post_id)
    ) WITHOUT ROWID;
    CREATE INDEX seen_first_seen ON seen(first_seen_at);
    CREATE TABLE sightings (
      id INTEGER PRIMARY KEY,
      post_id TEXT NOT NULL,
      group_id TEXT,
      read_id INTEGER,
      surface TEXT NOT NULL CHECK (surface IN ('feed','group','joins')),
      position INTEGER NOT NULL,
      batch INTEGER NOT NULL,
      cursor TEXT,
      is_highlight INTEGER NOT NULL DEFAULT 0,
      is_anchor INTEGER NOT NULL DEFAULT 0,
      created_at INTEGER,
      seen_at REAL NOT NULL
    );
    CREATE INDEX sightings_post ON sightings(post_id);
    CREATE INDEX sightings_read ON sightings(read_id);
    CREATE INDEX sightings_seen_at ON sightings(seen_at);
    CREATE TABLE reads (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      surface TEXT NOT NULL CHECK (surface IN ('feed','group','joins')),
      group_id TEXT,
      started_at REAL NOT NULL,
      ended_at REAL,
      target REAL,
      newest INTEGER,
      oldest INTEGER,
      target_reached INTEGER NOT NULL DEFAULT 0,
      steps INTEGER NOT NULL DEFAULT 0,
      outcome TEXT,
      page_state TEXT,
      stats TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX reads_surface ON reads(surface, group_id, started_at);
    CREATE TABLE deferred (
      post_id TEXT PRIMARY KEY,
      group_id TEXT,
      reason TEXT NOT NULL,
      first_at REAL NOT NULL,
      last_at REAL NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 1,
      raw_path TEXT
    );
    CREATE TABLE outbox (
      seq INTEGER PRIMARY KEY AUTOINCREMENT,
      group_id TEXT NOT NULL,
      post_id TEXT NOT NULL,
      payload TEXT NOT NULL,
      size INTEGER NOT NULL,
      created_at REAL NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 0,
      last_error TEXT,
      UNIQUE (group_id, post_id)
    );
    CREATE TABLE events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      payload TEXT NOT NULL,
      created_at REAL NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE budget_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      at REAL NOT NULL,
      kind TEXT NOT NULL CHECK (kind IN ('navigation','scroll')),
      surface TEXT,
      group_id TEXT,
      read_id INTEGER
    );
    CREATE INDEX budget_events_kind_at ON budget_events(kind, at);
    CREATE INDEX budget_events_group_at ON budget_events(group_id, at);
    CREATE TABLE process_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      at REAL NOT NULL,
      kind TEXT NOT NULL CHECK (kind IN ('process_start','chrome_launch')),
      detail TEXT
    );
    CREATE INDEX process_events_kind_at ON process_events(kind, at);
    CREATE TABLE blocks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      opened_at REAL NOT NULL,
      until REAL,
      rung INTEGER NOT NULL,
      state TEXT NOT NULL,
      reason TEXT NOT NULL,
      excerpt TEXT,
      screenshot TEXT,
      no_probe INTEGER NOT NULL DEFAULT 0,
      closed_at REAL,
      close_reason TEXT
    );
    CREATE UNIQUE INDEX blocks_one_open ON blocks((closed_at IS NULL)) WHERE closed_at IS NULL;
    CREATE TABLE groups (
      id TEXT PRIMARY KEY,
      name TEXT,
      slug TEXT,
      privacy TEXT NOT NULL DEFAULT 'unknown',
      state TEXT NOT NULL DEFAULT 'member',
      join_state TEXT,
      subscribe_status TEXT,
      sorting_setting TEXT,
      monitored INTEGER NOT NULL DEFAULT 0,
      tier INTEGER NOT NULL DEFAULT 1,
      listings INTEGER NOT NULL DEFAULT 0,
      baseline_at REAL NOT NULL,
      missing_syncs INTEGER NOT NULL DEFAULT 0,
      flags TEXT NOT NULL DEFAULT '[]',
      last_synced_at REAL,
      updated_at REAL NOT NULL
    );
    """,
    # v2: what became of a post when it was first seen (queued, before_baseline, stale,
    # not_monitored, ...), so the phase-0 report measures delivered posts only.
    """
    ALTER TABLE seen ADD COLUMN outcome TEXT;
    """,
    # v3: posts whose item did not fit the wire model, kept unseen for a retry: before, they were
    # marked seen with no outbox row and lost for good.
    """
    CREATE TABLE quarantine (
      group_id TEXT NOT NULL,
      post_id TEXT NOT NULL,
      read_id INTEGER,
      surface TEXT,
      created_at INTEGER,
      errors TEXT NOT NULL,
      raw_path TEXT,
      first_at REAL NOT NULL,
      last_at REAL NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 1,
      PRIMARY KEY (group_id, post_id)
    ) WITHOUT ROWID;
    """,
    # v4: whether a process start or a Chrome launch was planned (a restart someone asked for,
    # a relaunch after the collector closed Chrome itself); the crash-loop gate counts the
    # unplanned ones only. Rows from before cannot be classified and are marked planned: on
    # 2026-09-30 the live collector was stuck in CRASH_LOOP by planned launches alone (breaks,
    # quiet hours, sleeps, operator updates), and counting them would hold it there for up to
    # a day. The cap on all launches, planned or not, still counts them.
    """
    ALTER TABLE process_events ADD COLUMN planned INTEGER NOT NULL DEFAULT 0;
    UPDATE process_events SET planned=1;
    """,
]
INVALID: Final = "invalid"
"""The outcome of a post whose item does not fit the wire model: quarantined, never seen."""


@dataclass(frozen=True, slots=True)
class MarkResult:
    new: bool
    """``False``: the post was already seen; only the sighting was recorded."""
    seq: int | None = None
    """The outbox sequence number when an item was queued."""
    outbox_depth: int = 0
    dropped: int = 0
    """Oldest outbox rows dropped by this insert to stay under the cap."""


@dataclass(frozen=True, slots=True)
class OutboxRow:
    seq: int
    group_id: str
    post_id: str
    payload: str
    size: int
    attempts: int
    created_at: float
    collector_id: str = ""
    """The id the item was queued under (in its payload); ``""`` when not read."""

    @property
    def key(self) -> tuple[str, str]:
        return (self.group_id, self.post_id)


@dataclass(frozen=True, slots=True)
class ApplyResult:
    deleted: int
    rejected: int
    kept: int
    """Rows of the batch without a result: they stay queued and are resent."""
    unexpected: int
    """Results for keys that were not in the batch (ignored)."""


@dataclass(frozen=True, slots=True)
class Block:
    id: int
    opened_at: float
    until: float | None
    rung: int
    state: PageState
    reason: str
    excerpt: str | None
    screenshot: str | None
    no_probe: bool
    closed_at: float | None
    close_reason: str | None

    @property
    def open(self) -> bool:
        return self.closed_at is None


@dataclass(frozen=True, slots=True)
class GroupRow:
    id: str
    name: str | None
    slug: str | None
    privacy: GroupPrivacy
    state: GroupState
    join_state: str | None
    subscribe_status: str | None
    sorting_setting: str | None
    monitored: bool
    tier: int
    listings: bool
    baseline_at: float
    missing_syncs: int
    flags: frozenset[str]
    last_synced_at: float | None
    updated_at: float

    def meta(self) -> GroupMeta:
        return GroupMeta(
            id=self.id,
            name=self.name,
            slug=self.slug,
            privacy=self.privacy,
            join_state=self.join_state,
            subscribe_status=self.subscribe_status,
            sorting_setting=self.sorting_setting,
        )


@dataclass(frozen=True, slots=True)
class DeferredRow:
    post_id: str
    group_id: str | None
    reason: DeferredReason
    first_at: float
    last_at: float
    attempts: int
    raw_path: str | None


@dataclass(frozen=True, slots=True)
class QuarantineRow:
    """A post whose item did not fit the wire model, waiting for a retry."""

    group_id: str
    post_id: str
    read_id: int | None
    surface: Surface | None
    created_at: int | None
    errors: tuple[str, ...]
    """Where and how validation failed (``url:value_error``): never a value, which may be text."""
    raw_path: str | None
    """The saved step payload the story came from (kept 48 h; ``replay`` reads it)."""
    first_at: float
    last_at: float
    attempts: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.group_id, self.post_id)


@dataclass(frozen=True, slots=True)
class CleanupStats:
    seen: int = 0
    sightings: int = 0
    reads: int = 0
    deferred: int = 0
    quarantined: int = 0
    budget_events: int = 0
    process_events: int = 0
    files: int = 0


class Store:
    """The collector's SQLite state. Open once, share, close on exit."""

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Clock,
        outbox_cap: int = DEFAULT_OUTBOX_CAP,
        events_cap: int = DEFAULT_EVENTS_CAP,
    ) -> None:
        self._path = str(path)
        self._clock = clock
        self._outbox_cap = outbox_cap
        self._events_cap = events_cap
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------------------------

    async def open(self) -> None:
        on_disk = self._path != ":memory:" and not self._path.startswith("file:")
        if on_disk:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self._path, isolation_level=None, uri=True)
        db.row_factory = aiosqlite.Row
        self._db = db
        try:
            # Even the first PRAGMA can fail on a corrupt file, and aiosqlite's worker thread
            # is non-daemon: a leaked connection would hang the interpreter on exit.
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute("PRAGMA busy_timeout=5000")
            await self._quick_check()
            await self._migrate()
        except sqlite3.DatabaseError as exc:
            await self._abandon(db)
            if isinstance(exc, sqlite3.OperationalError):
                raise
            raise DatabaseCorruptError(str(exc)) from exc
        except BaseException:
            await self._abandon(db)
            raise
        if on_disk:
            self._tighten_files()

    async def _abandon(self, db: aiosqlite.Connection) -> None:
        self._db = None
        await db.close()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            msg = "Store is not open"
            raise RuntimeError(msg)
        return self._db

    def _tighten_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(self._path + suffix)
            with contextlib.suppress(FileNotFoundError):
                path.chmod(0o600)

    async def _quick_check(self) -> None:
        cur = await self.db.execute("PRAGMA quick_check")
        row = await cur.fetchone()
        result = str(row[0]) if row else "unknown"
        if result != "ok":
            raise DatabaseCorruptError(result)

    async def _migrate(self) -> None:
        version = await self.schema_version()
        if version > len(_MIGRATIONS):
            msg = f"state.sqlite has schema v{version}; this collector knows v{len(_MIGRATIONS)}"
            raise SchemaTooNewError(msg)
        for index, script in enumerate(_MIGRATIONS[version:], start=version + 1):
            # executescript() commits any open transaction first, so the transaction has to
            # live inside the script itself for the migration to be atomic.
            await self.db.executescript(f"BEGIN;\n{script}\nPRAGMA user_version={index};\nCOMMIT;")

    async def schema_version(self) -> int:
        cur = await self.db.execute("PRAGMA user_version")
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    @contextlib.asynccontextmanager
    async def _tx(self, *, durable: bool = False) -> AsyncIterator[aiosqlite.Connection]:
        """One write transaction under the lock; ``durable`` fsyncs the WAL at its commit."""
        async with self._lock:
            db = self.db
            if durable:
                await db.execute("PRAGMA synchronous=FULL")
            try:
                try:
                    # Inside the rollback's reach: a read cancelled while it waits for the BEGIN
                    # (a hung read is cancelled) leaves the BEGIN queued, and it still runs.
                    await db.execute("BEGIN IMMEDIATE")
                    yield db
                    await db.execute("COMMIT")
                except BaseException:
                    with contextlib.suppress(sqlite3.Error):
                        await db.execute("ROLLBACK")
                    raise
            finally:
                if durable:
                    await db.execute("PRAGMA synchronous=NORMAL")

    @contextlib.asynccontextmanager
    async def _read(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            yield self.db

    def _now(self) -> float:
        return self._clock.time()

    def now(self) -> float:
        """The store's clock (the collector's): what its rows are stamped with."""
        return self._clock.time()

    # -- kv ----------------------------------------------------------------------------------

    async def kv_get(self, key: str) -> str | None:
        async with self._read() as db:
            cur = await db.execute("SELECT value FROM kv WHERE key=?", (key,))
            row = await cur.fetchone()
        return str(row["value"]) if row else None

    async def kv_set(self, key: str, value: str, *, durable: bool = False) -> None:
        """``durable`` fsyncs the commit: for state that keeps a gate shut (the ladder's
        backoff and streak, halts, a broken parser), which a power cut must not forget."""
        async with self._tx(durable=durable) as db:
            await _kv_put(db, key, value, self._now())

    async def kv_update(
        self, key: str, change: Callable[[str | None], str | None], *, durable: bool = False
    ) -> str | None:
        """Read one kv row, ``change`` it and write it back in one write transaction; returns
        the row's value afterwards. ``change`` returning ``None`` leaves the row as it is.

        ``BEGIN IMMEDIATE`` takes the database's write lock before the read, so a writer in
        another process (a CLI ``pause`` next to the running collector) cannot land between the
        read and the write and be overwritten by a value computed from the old row.
        """
        async with self._tx(durable=durable) as db:
            cur = await db.execute("SELECT value FROM kv WHERE key=?", (key,))
            row = await cur.fetchone()
            current = str(row["value"]) if row else None
            value = change(current)
            if value is None:
                return current
            await _kv_put(db, key, value, self._now())
        return value

    async def kv_delete(self, key: str) -> bool:
        async with self._tx() as db:
            cur = await db.execute("DELETE FROM kv WHERE key=?", (key,))
        return cur.rowcount == 1

    async def kv_get_json(self, key: str) -> Any:
        raw = await self.kv_get(key)
        return None if raw is None else json.loads(raw)

    async def kv_set_json(self, key: str, value: Any, *, durable: bool = False) -> None:
        await self.kv_set(
            key, json.dumps(value, ensure_ascii=False, sort_keys=True), durable=durable
        )

    async def kv_incr(self, key: str, by: int = 1, *, durable: bool = False) -> int:
        """``durable`` fsyncs the commit: for a counter another machine orders messages by
        (the uplink's ``seq``), which must never go back after a power cut."""
        async with self._tx(durable=durable) as db:
            return await _kv_incr(db, key, by, self._now())

    async def kv_int(self, key: str) -> int:
        raw = await self.kv_get(key)
        return int(raw) if raw is not None else 0

    async def claim_nonce(self, nonce: str, *, kind: str) -> bool:
        """``True`` exactly once per nonce, ever: a control command is an edge, not a state."""
        async with self._tx(durable=True) as db:
            cur = await db.execute(
                "INSERT OR IGNORE INTO kv (key, value, updated_at) VALUES (?,?,?)",
                (NONCE_PREFIX + nonce, kind, self._now()),
            )
        return cur.rowcount == 1

    # -- budget and process events (write-ahead) ---------------------------------------------

    async def record_budget_event(
        self,
        kind: BudgetKind,
        *,
        surface: Surface | None = None,
        group_id: str | None = None,
        read_id: int | None = None,
    ) -> int:
        """Record a navigation or scroll step *before* it is issued; returns once it is fsynced."""
        async with self._tx(durable=True) as db:
            cur = await db.execute(
                """INSERT INTO budget_events (at, kind, surface, group_id, read_id)
                   VALUES (?,?,?,?,?)""",
                (self._now(), kind.value, surface.value if surface else None, group_id, read_id),
            )
        return _rowid(cur)

    async def budget_used(
        self, kind: BudgetKind, window: float, *, now: float | None = None
    ) -> int:
        """Events of ``kind`` in the sliding window ``[now - window, …)``.

        Events stamped after ``now`` (the wall clock was stepped back) still count: a cap is
        only ever over-counted.
        """
        since = (self._now() if now is None else now) - window
        async with self._read() as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM budget_events WHERE kind=? AND at>=?", (kind.value, since)
            )
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def budget_usage(
        self, windows: Iterable[tuple[BudgetKind, int, int]], *, now: float | None = None
    ) -> list[BudgetWindow]:
        """Each ``(kind, window seconds, cap)`` with its current use, as the status reports it."""
        at = self._now() if now is None else now
        return [
            BudgetWindow(
                kind=kind,
                window_seconds=seconds,
                cap=cap,
                used=await self.budget_used(kind, seconds, now=at),
            )
            for kind, seconds, cap in windows
        ]

    async def last_budget_event_at(
        self, kind: BudgetKind | None = None, *, group_id: str | None = None
    ) -> float | None:
        """When the last recorded step was, for the spacing rules that must survive restarts."""
        clauses, args = [], []
        if kind is not None:
            clauses.append("kind=?")
            args.append(kind.value)
        if group_id is not None:
            clauses.append("group_id=?")
            args.append(group_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self._read() as db:
            cur = await db.execute(f"SELECT MAX(at) FROM budget_events{where}", args)  # noqa: S608
            row = await cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None

    async def budget_event_times(self, kind: BudgetKind, *, since: float) -> list[float]:
        """When each ``kind`` event at or after ``since`` was recorded, oldest first.

        The scheduler smooths its flexible reads against these (a backstop must leave room in
        every window for the next feed read); the caps themselves are counted by ``budget_used``.
        """
        async with self._read() as db:
            cur = await db.execute(
                "SELECT at FROM budget_events WHERE kind=? AND at>=? ORDER BY at, id",
                (kind.value, since),
            )
            rows = await cur.fetchall()
        return [float(r[0]) for r in rows]

    async def record_process_event(
        self, kind: ProcessKind, detail: str | None = None, *, planned: bool = False
    ) -> int:
        """Record a process start or a Chrome launch before it happens (crash-loop gate).

        ``planned``: a start after a stop someone asked for, a launch after the collector
        closed Chrome itself, or a human's command. Anything else is unplanned (fail closed).
        """
        async with self._tx(durable=True) as db:
            cur = await db.execute(
                "INSERT INTO process_events (at, kind, detail, planned) VALUES (?,?,?,?)",
                (self._now(), kind.value, detail, int(planned)),
            )
        return _rowid(cur)

    async def record_process_start(self, *, taking: str) -> str | None:
        """Record a process start, planned exactly when the kv marker ``taking`` was there;
        the marker is deleted in the same durable transaction. Its value, or ``None``: the
        start was not planned."""
        async with self._tx(durable=True) as db:
            cur = await db.execute("SELECT value FROM kv WHERE key=?", (taking,))
            row = await cur.fetchone()
            await db.execute("DELETE FROM kv WHERE key=?", (taking,))
            await db.execute(
                "INSERT INTO process_events (at, kind, detail, planned) VALUES (?,?,?,?)",
                (self._now(), ProcessKind.PROCESS_START.value, None, int(row is not None)),
            )
        return None if row is None else str(row[0])

    async def process_events_since(
        self,
        window: float,
        *,
        kinds: Collection[ProcessKind] | None = None,
        now: float | None = None,
        planned: bool | None = False,
    ) -> int:
        """How many ``kinds`` events the last ``window`` seconds hold: the unplanned ones by
        default, the planned ones with ``planned=True``, all of them with ``None``."""
        since = (self._now() if now is None else now) - window
        wanted = [k.value for k in (kinds if kinds is not None else ProcessKind)]
        marks = ",".join("?" * len(wanted))
        where = f"at>=? AND kind IN ({marks})"
        args: list[Any] = [since, *wanted]
        if planned is not None:
            where += " AND planned=?"
            args.append(int(planned))
        async with self._read() as db:
            cur = await db.execute(
                f"SELECT COUNT(*) FROM process_events WHERE {where}",  # noqa: S608
                args,
            )
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    # -- seen, sightings, outbox -------------------------------------------------------------

    async def is_seen(self, group_id: str, post_id: str) -> bool:
        async with self._read() as db:
            cur = await db.execute(
                "SELECT 1 FROM seen WHERE group_id=? AND post_id=?", (group_id, post_id)
            )
            return await cur.fetchone() is not None

    async def seen_among(self, keys: Iterable[tuple[str, str]]) -> set[tuple[str, str]]:
        wanted = list(dict.fromkeys(keys))
        found: set[tuple[str, str]] = set()
        async with self._read() as db:
            for group_id, post_id in wanted:
                cur = await db.execute(
                    "SELECT 1 FROM seen WHERE group_id=? AND post_id=?", (group_id, post_id)
                )
                if await cur.fetchone() is not None:
                    found.add((group_id, post_id))
        return found

    async def record_sighting(self, sighting: Sighting) -> None:
        """A story seen again (or not yet resolvable): only its appearance is recorded."""
        async with self._tx() as db:
            await _insert_sighting(db, sighting, self._now())

    async def mark_seen(
        self,
        sighting: Sighting,
        *,
        via: Via | None = None,
        item: Item | None = None,
        outcome: str | None = None,
    ) -> MarkResult:
        """Mark a story seen, record the sighting and, if ``item`` is given, queue it — atomically.

        ``item`` is ``None`` only for an intentional terminal outcome — a post of a group that
        is not monitored or not known, older than its group's baseline or the novelty window:
        such posts are seen, never sent. Never for a post whose item did not fit the wire model
        (``INVALID``), which is quarantined instead (``quarantine``). The item's ``seq`` is
        assigned here from the outbox's own sequence. ``outcome`` says why (``queued``,
        ``before_baseline``, ``stale``, ``not_monitored``, ...); the report measures latency and
        volume on queued posts only. Marking a post seen ends its deferral or quarantine.

        Not fsynced, unlike the budget: a power cut can only lose the last transactions whole,
        seen row and outbox row together, and a post lost that way is simply new again on the
        next read.
        """
        group_id = sighting.group_id
        if group_id is None:
            msg = "a story without a group cannot be marked seen; defer it"
            raise ValueError(msg)
        if item is not None and item.key != (group_id, sighting.post_id):
            msg = "the item is not the sighted story"
            raise ValueError(msg)
        if item is None and outcome == INVALID:
            msg = "a post whose item does not fit the wire model is quarantined, not seen"
            raise ValueError(msg)
        now = self._now()
        async with self._tx() as db:
            cur = await db.execute(
                """INSERT OR IGNORE INTO seen
                     (group_id, post_id, created_at, first_seen_at, via, outcome)
                   VALUES (?,?,?,?,?,?)""",
                (
                    group_id,
                    sighting.post_id,
                    sighting.created_at,
                    now,
                    via.value if via else None,
                    outcome,
                ),
            )
            new = cur.rowcount == 1
            await _insert_sighting(db, sighting, now)
            await db.execute("DELETE FROM deferred WHERE post_id=?", (sighting.post_id,))
            await db.execute(
                "DELETE FROM quarantine WHERE group_id=? AND post_id=?",
                (group_id, sighting.post_id),
            )
            if not new or item is None:
                return MarkResult(new=new, outbox_depth=await _count(db, "outbox"))
            seq = await self._enqueue(db, item, now)
            dropped = await self._enforce_cap(
                db,
                table="outbox",
                order_by="seq",
                cap=self._outbox_cap,
                counter=KV_OUTBOX_DROPPED,
                now=now,
            )
            depth = await _count(db, "outbox")
        if dropped:
            log.warning("outbox full: dropped %d oldest items (cap %d)", dropped, self._outbox_cap)
        return MarkResult(new=True, seq=seq, outbox_depth=depth, dropped=dropped)

    async def _enqueue(self, db: aiosqlite.Connection, item: Item, now: float) -> int | None:
        cur = await db.execute(
            """INSERT OR IGNORE INTO outbox (group_id, post_id, payload, size, created_at)
               VALUES (?,?,'',0,?)""",
            (item.group_id, item.post_id, now),
        )
        if cur.rowcount != 1:  # already queued (its seen row was pruned meanwhile)
            return None
        seq = _rowid(cur)
        # Re-validate rather than model_copy: the text cap and every field check run again on
        # exactly what will be sent.
        final = Item.model_validate({**item.model_dump(mode="json"), "seq": seq})
        payload = final.model_dump_json()
        await db.execute(
            "UPDATE outbox SET payload=?, size=? WHERE seq=?",
            (payload, len(payload.encode("utf-8")), seq),
        )
        return seq

    async def _enforce_cap(
        self,
        db: aiosqlite.Connection,
        *,
        table: str,
        order_by: str,
        cap: int,
        counter: str,
        now: float,
    ) -> int:
        over = await _count(db, table) - cap
        if over <= 0:
            return 0
        await db.execute(
            f"DELETE FROM {table} WHERE {order_by} IN "  # noqa: S608 - fixed identifiers
            f"(SELECT {order_by} FROM {table} ORDER BY {order_by} LIMIT ?)",
            (over,),
        )
        await _kv_incr(db, counter, over, now)
        return over

    async def outbox_depth(self) -> int:
        async with self._read() as db:
            return await _count(db, "outbox")

    async def next_batch(
        self, collector_id: str, *, limit: int = MAX_BATCH_BYTES
    ) -> list[OutboxRow]:
        """The oldest outbox rows whose ``ItemsRequest`` body fits ``limit`` bytes, all queued
        under one collector id: the oldest row's (``collector_id`` for a payload that names
        none). A request carries one id for all its items, and the brain refuses an item whose
        id differs from the request's, so rows queued before ``FBC_COLLECTOR_ID`` changed go
        out under their own id rather than being refused (and deleted)."""
        async with self._read() as db:
            cur = await db.execute(
                "SELECT seq, size, CASE WHEN json_valid(payload) "
                "THEN json_extract(payload, '$.collector_id') END AS cid FROM outbox ORDER BY seq"
            )
            rows = list(await cur.fetchall())
            if not rows:
                return []
            owner = rows[0]["cid"] or collector_id
            run = list(itertools.takewhile(lambda r: (r["cid"] or collector_id) == owner, rows))
            batches = plan_batches(
                [int(r["size"]) for r in run], overhead=items_overhead(owner), limit=limit
            )
            start, end = batches[0]
            first, last = int(run[start]["seq"]), int(run[end - 1]["seq"])
            cur = await db.execute(
                "SELECT *, CASE WHEN json_valid(payload) "
                "THEN json_extract(payload, '$.collector_id') END AS cid FROM outbox "
                "WHERE seq BETWEEN ? AND ? ORDER BY seq",
                (first, last),
            )
            return [
                replace(_outbox_row(r), collector_id=r["cid"] or collector_id)
                for r in await cur.fetchall()
            ]

    async def apply_results(
        self, batch: Sequence[OutboxRow], response: ItemsResponse
    ) -> ApplyResult:
        """Delete every row the brain answered for; the rest stay queued with one more attempt."""
        results = response.by_key()
        sent = {row.key for row in batch}
        deleted = rejected = kept = 0
        async with self._tx() as db:
            for row in batch:
                result = results.get(row.key)
                if result is None:
                    kept += 1
                    await db.execute(
                        "UPDATE outbox SET attempts=attempts+1, last_error=? WHERE seq=?",
                        ("no result", row.seq),
                    )
                    continue
                cur = await db.execute("DELETE FROM outbox WHERE seq=?", (row.seq,))
                deleted += cur.rowcount
                if result.result is ItemOutcome.REJECTED:
                    rejected += 1
                    log.warning(
                        "brain rejected post %s/%s: %s", row.group_id, row.post_id, result.reason
                    )
            if rejected:
                await _kv_incr(db, KV_OUTBOX_REJECTED, rejected, self._now())
        unexpected = len(set(results) - sent)
        if unexpected:
            log.warning("brain answered for %d items that were not in the batch", unexpected)
        return ApplyResult(deleted=deleted, rejected=rejected, kept=kept, unexpected=unexpected)

    async def note_send_failure(self, batch: Sequence[OutboxRow], error: str) -> None:
        """A 5xx or a transport error: every row stays queued."""
        async with self._tx() as db:
            await db.executemany(
                "UPDATE outbox SET attempts=attempts+1, last_error=? WHERE seq=?",
                [(error[:300], row.seq) for row in batch],
            )

    # -- events queue ------------------------------------------------------------------------

    async def enqueue_event(
        self,
        kind: EventKind,
        *,
        collector_id: str,
        group_id: str | None = None,
        block_id: int | None = None,
        detail: str | None = None,
    ) -> Event:
        """Queue a one-off event for the brain; durable, because it may describe a block."""
        now = self._now()
        async with self._tx(durable=True) as db:
            cur = await db.execute(
                "INSERT INTO events (payload, created_at) VALUES ('', ?)", (now,)
            )
            event_id = _rowid(cur)
            event = Event(
                collector_id=collector_id,
                event_id=event_id,
                at=int(now),
                kind=kind,
                group_id=group_id,
                block_id=block_id,
                detail=detail,
            )
            await db.execute(
                "UPDATE events SET payload=? WHERE id=?", (event.model_dump_json(), event_id)
            )
            dropped = await self._enforce_events_cap(db, now)
        if dropped:
            log.warning("event queue full: dropped %d oldest events", dropped)
        return event

    async def _enforce_events_cap(self, db: aiosqlite.Connection, now: float) -> int:
        """Drop the oldest events over the cap — never a block, a logout or a lost group.

        Those transitions are what ``/v1/events`` exists to deliver after an outage; they are
        rare, so the queue may exceed its cap by them alone.
        """
        over = await _count(db, "events") - self._events_cap
        if over <= 0:
            return 0
        kept = ",".join(f"'{kind.value}'" for kind in KEPT_EVENTS)
        cur = await db.execute(
            "DELETE FROM events WHERE id IN (SELECT id FROM events"  # noqa: S608 - fixed values
            f" WHERE json_extract(payload, '$.kind') NOT IN ({kept}) ORDER BY id LIMIT ?)",
            (over,),
        )
        dropped = max(cur.rowcount, 0)
        if dropped:
            await _kv_incr(db, KV_EVENTS_DROPPED, dropped, now)
        return dropped

    async def events_depth(self) -> int:
        async with self._read() as db:
            return await _count(db, "events")

    async def pending_events(self, limit: int = 50) -> list[Event]:
        async with self._read() as db:
            cur = await db.execute("SELECT payload FROM events ORDER BY id LIMIT ?", (limit,))
            rows = await cur.fetchall()
        return [Event.model_validate_json(r["payload"]) for r in rows]

    async def ack_events(self, event_ids: Iterable[int]) -> int:
        ids = list(event_ids)
        if not ids:
            return 0
        async with self._tx() as db:
            cur = await db.executemany("DELETE FROM events WHERE id=?", [(i,) for i in ids])
        return max(cur.rowcount, 0)

    # -- deferred ----------------------------------------------------------------------------

    async def defer(
        self,
        post_id: str,
        reason: DeferredReason,
        *,
        group_id: str | None = None,
        raw_path: str | None = None,
        sighting: Sighting | None = None,
    ) -> int:
        """Keep a story that could not become an item yet; returns how often it was deferred."""
        now = self._now()
        async with self._tx() as db:
            cur = await db.execute(
                """INSERT INTO deferred (post_id, group_id, reason, first_at, last_at, raw_path)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(post_id) DO UPDATE SET
                     attempts=deferred.attempts+1, last_at=excluded.last_at,
                     reason=excluded.reason,
                     group_id=COALESCE(excluded.group_id, deferred.group_id),
                     raw_path=COALESCE(excluded.raw_path, deferred.raw_path)
                   RETURNING attempts""",
                (post_id, group_id, reason.value, now, now, raw_path),
            )
            row = await cur.fetchone()
            if sighting is not None:
                await _insert_sighting(db, sighting, now)
        return int(row["attempts"]) if row else 0

    async def get_deferred(self, post_id: str) -> DeferredRow | None:
        async with self._read() as db:
            cur = await db.execute("SELECT * FROM deferred WHERE post_id=?", (post_id,))
            row = await cur.fetchone()
        return _deferred_row(row) if row else None

    async def list_deferred(self, *, since: float | None = None) -> list[DeferredRow]:
        async with self._read() as db:
            cur = await db.execute(
                "SELECT * FROM deferred WHERE last_at>=? ORDER BY first_at",
                (since if since is not None else float("-inf"),),
            )
            rows = await cur.fetchall()
        return [_deferred_row(r) for r in rows]

    async def drop_deferred(self, post_id: str) -> bool:
        async with self._tx() as db:
            cur = await db.execute("DELETE FROM deferred WHERE post_id=?", (post_id,))
        return cur.rowcount == 1

    # -- quarantine --------------------------------------------------------------------------

    async def quarantine(
        self, sighting: Sighting, *, errors: Sequence[str], raw_path: str | None = None
    ) -> int:
        """Keep a post whose item did not fit the wire model, unseen, with its sighting; returns
        how often it has failed. It is retried on its next sighting and by ``replay``."""
        group_id = sighting.group_id
        if group_id is None:
            msg = "a story without a group is deferred, not quarantined"
            raise ValueError(msg)
        now = self._now()
        categories = json.dumps(sorted(set(errors)))
        async with self._tx() as db:
            cur = await db.execute(
                """INSERT INTO quarantine (group_id, post_id, read_id, surface, created_at,
                     errors, raw_path, first_at, last_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(group_id, post_id) DO UPDATE SET
                     attempts=quarantine.attempts+1, last_at=excluded.last_at,
                     errors=excluded.errors, read_id=excluded.read_id,
                     surface=excluded.surface, created_at=excluded.created_at,
                     raw_path=COALESCE(excluded.raw_path, quarantine.raw_path)
                   RETURNING attempts""",
                (
                    group_id,
                    sighting.post_id,
                    sighting.read_id,
                    sighting.surface.value,
                    sighting.created_at,
                    categories,
                    raw_path,
                    now,
                    now,
                ),
            )
            row = await cur.fetchone()
            await _insert_sighting(db, sighting, now)
        return int(row["attempts"]) if row else 0

    async def get_quarantined(self, group_id: str, post_id: str) -> QuarantineRow | None:
        async with self._read() as db:
            cur = await db.execute(
                "SELECT * FROM quarantine WHERE group_id=? AND post_id=?", (group_id, post_id)
            )
            row = await cur.fetchone()
        return _quarantine_row(row) if row else None

    async def list_quarantined(self) -> list[QuarantineRow]:
        async with self._read() as db:
            cur = await db.execute("SELECT * FROM quarantine ORDER BY first_at, group_id, post_id")
            rows = await cur.fetchall()
        return [_quarantine_row(r) for r in rows]

    async def quarantined_among(self, keys: Iterable[tuple[str, str]]) -> set[tuple[str, str]]:
        wanted = list(dict.fromkeys(keys))
        found: set[tuple[str, str]] = set()
        async with self._read() as db:
            for group_id, post_id in wanted:
                cur = await db.execute(
                    "SELECT 1 FROM quarantine WHERE group_id=? AND post_id=?", (group_id, post_id)
                )
                if await cur.fetchone() is not None:
                    found.add((group_id, post_id))
        return found

    # -- reads -------------------------------------------------------------------------------

    async def start_read(
        self, surface: Surface, *, group_id: str | None = None, target: float | None = None
    ) -> int:
        async with self._tx() as db:
            cur = await db.execute(
                "INSERT INTO reads (surface, group_id, started_at, target) VALUES (?,?,?,?)",
                (surface.value, group_id, self._now(), target),
            )
        return _rowid(cur)

    async def finish_read(
        self,
        read_id: int,
        *,
        coverage: Coverage,
        steps: int,
        outcome: ReadOutcome,
        page_state: PageState | None = None,
        stats: dict[str, Any] | None = None,
    ) -> None:
        async with self._tx() as db:
            await db.execute(
                """UPDATE reads SET ended_at=?, target=COALESCE(?, target), newest=?, oldest=?,
                     target_reached=?, steps=?, outcome=?, page_state=?, stats=?
                   WHERE id=?""",
                (
                    self._now(),
                    coverage.target,
                    coverage.newest,
                    coverage.oldest,
                    int(coverage.target_reached),
                    steps,
                    outcome.value,
                    page_state.value if page_state else None,
                    json.dumps(stats or {}, ensure_ascii=False, sort_keys=True),
                    read_id,
                ),
            )

    async def get_read(self, read_id: int) -> ReadRecord | None:
        async with self._read() as db:
            cur = await db.execute("SELECT * FROM reads WHERE id=?", (read_id,))
            row = await cur.fetchone()
        return _read_record(row) if row else None

    async def last_read(
        self,
        surface: Surface,
        *,
        group_id: str | None = None,
        target_reached: bool | None = None,
    ) -> ReadRecord | None:
        """The latest finished read of a surface (and group); the stop rules' targets come from
        the latest one that reached its target."""
        sql = "SELECT * FROM reads WHERE surface=? AND ended_at IS NOT NULL"
        args: list[Any] = [surface.value]
        if group_id is not None:
            sql += " AND group_id=?"
            args.append(group_id)
        if target_reached is not None:
            sql += " AND target_reached=?"
            args.append(int(target_reached))
        sql += " ORDER BY started_at DESC, id DESC LIMIT 1"
        async with self._read() as db:
            cur = await db.execute(sql, args)
            row = await cur.fetchone()
        return _read_record(row) if row else None

    async def last_covering_read(
        self, surface: Surface, *, group_id: str | None = None
    ) -> ReadRecord | None:
        """The latest read of a surface (and group) that finished its stepping — it reached its
        target or recorded a gap — which is where the next read's stop target comes from."""
        sql = (
            "SELECT * FROM reads WHERE surface=? AND ended_at IS NOT NULL"
            " AND outcome IN ('complete','gap')"
        )
        args: list[Any] = [surface.value]
        if group_id is not None:
            sql += " AND group_id=?"
            args.append(group_id)
        sql += " ORDER BY started_at DESC, id DESC LIMIT 1"
        async with self._read() as db:
            cur = await db.execute(sql, args)
            row = await cur.fetchone()
        return _read_record(row) if row else None

    async def posting_rates(self, since: float) -> dict[str, float]:
        """Posts per day per group, from creation times at or after ``since`` (gap targets)."""
        now = self._now()
        days = max((now - since) / DAY, 1 / 24)
        async with self._read() as db:
            cur = await db.execute(
                "SELECT group_id, COUNT(*) FROM seen WHERE created_at>=? GROUP BY group_id",
                (since,),
            )
            rows = await cur.fetchall()
        return {str(r[0]): int(r[1]) / days for r in rows}

    async def first_read_at(self, surface: Surface) -> float | None:
        """When the oldest kept read of ``surface`` that finished its stepping started."""
        async with self._read() as db:
            cur = await db.execute(
                "SELECT MIN(started_at) FROM reads WHERE surface=?"
                " AND outcome IN ('complete','gap')",
                (surface.value,),
            )
            row = await cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None

    async def last_sighting_at(self, group_id: str, surface: Surface) -> float | None:
        """When a story of the group was last seen on ``surface`` (feed absence)."""
        async with self._read() as db:
            cur = await db.execute(
                "SELECT MAX(seen_at) FROM sightings WHERE group_id=? AND surface=?",
                (group_id, surface.value),
            )
            row = await cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None

    async def found_by_backstop(self, group_id: str, *, since: float, until: float) -> int:
        """Posts of the group created in ``[since, until]`` that a backstop saw first."""
        async with self._read() as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM seen WHERE group_id=? AND created_at>=? AND created_at<=?"
                " AND via IN (?, ?)",
                (group_id, since, until, Via.BACKSTOP.value, Via.BACKSTOP_LISTINGS.value),
            )
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def last_created_at(self, group_id: str) -> int | None:
        """The newest creation time seen in a group (for the ``no_posts_7d`` flag)."""
        async with self._read() as db:
            cur = await db.execute("SELECT MAX(created_at) FROM seen WHERE group_id=?", (group_id,))
            row = await cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None

    # -- blocks ------------------------------------------------------------------------------

    async def open_block(
        self,
        *,
        state: PageState,
        reason: str,
        until: float | None,
        rung: int,
        excerpt: str | None = None,
        screenshot: str | None = None,
        no_probe: bool = False,
    ) -> Block:
        """Write a block to disk (fsynced) before anything else happens; supersedes an open one."""
        now = self._now()
        async with self._tx(durable=True) as db:
            await db.execute(
                "UPDATE blocks SET closed_at=?, close_reason='superseded' WHERE closed_at IS NULL",
                (now,),
            )
            cur = await db.execute(
                """INSERT INTO blocks
                     (opened_at, until, rung, state, reason, excerpt, screenshot, no_probe)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (now, until, rung, state.value, reason, excerpt, screenshot, int(no_probe)),
            )
            block_id = _rowid(cur)
            cur = await db.execute("SELECT * FROM blocks WHERE id=?", (block_id,))
            row = await cur.fetchone()
        if row is None:  # pragma: no cover - impossible after the insert
            raise KeyError(block_id)
        return _block(row)

    async def current_block(self) -> Block | None:
        async with self._read() as db:
            cur = await db.execute("SELECT * FROM blocks WHERE closed_at IS NULL")
            row = await cur.fetchone()
        return _block(row) if row else None

    async def latest_block(self) -> Block | None:
        """The most recent block, open or closed: the ladder climbs from it."""
        async with self._read() as db:
            cur = await db.execute("SELECT * FROM blocks ORDER BY id DESC LIMIT 1")
            row = await cur.fetchone()
        return _block(row) if row else None

    async def close_block(self, block_id: int, *, reason: str) -> bool:
        """Close the open block if and only if it is ``block_id``."""
        async with self._tx(durable=True) as db:
            cur = await db.execute(
                "UPDATE blocks SET closed_at=?, close_reason=? WHERE id=? AND closed_at IS NULL",
                (self._now(), reason, block_id),
            )
        return cur.rowcount == 1

    # -- groups ------------------------------------------------------------------------------

    async def upsert_group(
        self, meta: GroupMeta, *, state: GroupState | None = None, synced: bool = False
    ) -> GroupRow:
        """Insert or refresh a group. Known values are never wiped by an unknown (None) one.

        ``baseline_at`` is set when the group first becomes known and is never moved here:
        novelty starts there, so a group found by sync never yields its history.
        """
        now = self._now()
        privacy = None if meta.privacy is GroupPrivacy.UNKNOWN else meta.privacy.value
        async with self._tx() as db:
            await db.execute(
                """INSERT INTO groups (id, name, slug, privacy, state, join_state,
                     subscribe_status, sorting_setting, baseline_at, last_synced_at, updated_at)
                   VALUES (?,?,?,COALESCE(?, 'unknown'),?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=COALESCE(excluded.name, groups.name),
                     slug=COALESCE(excluded.slug, groups.slug),
                     privacy=COALESCE(?, groups.privacy),
                     state=COALESCE(?, groups.state),
                     join_state=COALESCE(excluded.join_state, groups.join_state),
                     subscribe_status=COALESCE(excluded.subscribe_status, groups.subscribe_status),
                     sorting_setting=COALESCE(excluded.sorting_setting, groups.sorting_setting),
                     last_synced_at=COALESCE(excluded.last_synced_at, groups.last_synced_at),
                     missing_syncs=CASE WHEN ? THEN 0 ELSE groups.missing_syncs END,
                     updated_at=excluded.updated_at""",
                (
                    meta.id,
                    meta.name,
                    meta.slug,
                    privacy,
                    (state or GroupState.MEMBER).value,
                    meta.join_state,
                    meta.subscribe_status,
                    meta.sorting_setting,
                    now,
                    now if synced else None,
                    now,
                    privacy,
                    state.value if state else None,
                    int(synced),
                ),
            )
            row = await _fetch_group(db, meta.id)
        if row is None:  # pragma: no cover - impossible after the upsert
            raise KeyError(meta.id)
        return row

    async def get_group(self, group_id: str) -> GroupRow | None:
        async with self._read() as db:
            return await _fetch_group(db, group_id)

    async def list_groups(self, *, monitored_only: bool = False) -> list[GroupRow]:
        sql = "SELECT * FROM groups"
        if monitored_only:
            sql += " WHERE monitored=1"
        async with self._read() as db:
            cur = await db.execute(sql + " ORDER BY id")
            return [_group(r) for r in await cur.fetchall()]

    async def monitored_ids(self) -> set[str]:
        return {g.id for g in await self.list_groups(monitored_only=True)}

    async def set_monitored(self, entries: Sequence[MonitoredGroup]) -> list[str]:
        """Replace the monitored set; returns the ids that are not known groups (skipped).

        A group that *becomes* monitored gets a fresh ``baseline_at``: its posts from before
        that moment were never promised to anyone, and sending them would be a backfill (D10).
        """
        now = self._now()
        wanted = {e.group_id: e for e in entries}
        unknown: list[str] = []
        async with self._tx() as db:
            cur = await db.execute("SELECT id, monitored FROM groups")
            current = {str(r["id"]): bool(r["monitored"]) for r in await cur.fetchall()}
            for group_id, entry in wanted.items():
                if group_id not in current:
                    unknown.append(group_id)
                    continue
                await db.execute(
                    """UPDATE groups SET monitored=1, tier=?, listings=?, updated_at=?,
                         baseline_at=CASE WHEN monitored=0 THEN ? ELSE baseline_at END
                       WHERE id=?""",
                    (entry.tier, int(entry.listings), now, now, group_id),
                )
            for group_id, was in current.items():
                if was and group_id not in wanted:
                    await db.execute(
                        "UPDATE groups SET monitored=0, updated_at=? WHERE id=?", (now, group_id)
                    )
        if unknown:
            log.warning("monitored groups not known from any sync: %s", ", ".join(unknown))
        return unknown

    async def set_group_flags(
        self, group_id: str, flags: Collection[str], *, durable: bool = False
    ) -> bool:
        """Replace a group's flags; ``durable`` for a flag that keeps the group shut (paused)."""
        async with self._tx(durable=durable) as db:
            cur = await db.execute(
                "UPDATE groups SET flags=?, updated_at=? WHERE id=?",
                (json.dumps(sorted(set(flags))), self._now(), group_id),
            )
        return cur.rowcount == 1

    async def note_complete_sync(self, present: Collection[str]) -> list[str]:
        """Count complete syncs a known group was missing from; returns those missing ≥ 2.

        Being missing twice is necessary, not sufficient, for ``left``: the caller still needs
        a group-page read showing the account is not a member (DESIGN, Group sync). The time
        of the sync is kept (kv ``last_complete_sync_at``): it overrides every membership
        confirmation older than it (``fbcollector.confirm``).
        """
        listed = set(present)
        async with self._tx() as db:
            await _kv_put(db, KV_LAST_COMPLETE_SYNC, repr(self._now()), self._now())
            cur = await db.execute("SELECT id FROM groups")
            known = {str(r["id"]) for r in await cur.fetchall()}
            await db.executemany(
                "UPDATE groups SET missing_syncs=0 WHERE id=?", [(g,) for g in known & listed]
            )
            await db.executemany(
                "UPDATE groups SET missing_syncs=missing_syncs+1 WHERE id=?",
                [(g,) for g in known - listed],
            )
            cur = await db.execute("SELECT id FROM groups WHERE missing_syncs>=2 ORDER BY id")
            return [str(r["id"]) for r in await cur.fetchall()]

    # -- retention ---------------------------------------------------------------------------

    async def cleanup(
        self,
        *,
        seen_retention: float = 30 * DAY,
        deferred_retention: float = 7 * DAY,
        raw_dirs: Sequence[Path] = (),
        raw_retention: float = 48 * HOUR,
    ) -> CleanupStats:
        """Prune old rows and raw files. Budget history is never cut inside its windows."""
        now = self._now()
        old = now - seen_retention
        budget_old = now - max(seen_retention, BUDGET_KEEP_FLOOR)
        async with self._tx() as db:
            seen = await _delete(db, "DELETE FROM seen WHERE first_seen_at<?", old)
            sightings = await _delete(db, "DELETE FROM sightings WHERE seen_at<?", old)
            reads = await _delete(db, "DELETE FROM reads WHERE started_at<?", old)
            deferred = await _delete(
                db, "DELETE FROM deferred WHERE last_at<?", now - deferred_retention
            )
            # Last seen longer ago than the novelty window: too old to become an item anyway.
            quarantined = await _delete(
                db, "DELETE FROM quarantine WHERE last_at<?", now - deferred_retention
            )
            budget = await _delete(db, "DELETE FROM budget_events WHERE at<?", budget_old)
            process = await _delete(db, "DELETE FROM process_events WHERE at<?", budget_old)
        files = 0
        for directory in raw_dirs:
            files += await asyncio.to_thread(prune_files, directory, now - raw_retention)
        return CleanupStats(
            seen=seen,
            sightings=sightings,
            reads=reads,
            deferred=deferred,
            quarantined=quarantined,
            budget_events=budget,
            process_events=process,
            files=files,
        )


def prune_files(directory: Path, before: float) -> int:
    """Delete regular files under ``directory`` last modified before ``before``; never follows
    symlinks out of it."""
    removed = 0
    if not directory.is_dir() or directory.is_symlink():
        return 0
    for dirpath, _dirnames, filenames in os.walk(directory):
        for name in filenames:
            path = Path(dirpath) / name
            with contextlib.suppress(FileNotFoundError):
                info = path.lstat()
                if path.is_symlink() or info.st_mtime >= before:
                    continue
                path.unlink()
                removed += 1
    return removed


# -- row helpers -----------------------------------------------------------------------------


def _rowid(cur: aiosqlite.Cursor) -> int:
    if cur.lastrowid is None:  # pragma: no cover - sqlite always sets it after an INSERT
        msg = "insert returned no rowid"
        raise RuntimeError(msg)
    return int(cur.lastrowid)


async def _count(db: aiosqlite.Connection, table: str) -> int:
    cur = await db.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 - fixed identifiers
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _delete(db: aiosqlite.Connection, sql: str, before: float) -> int:
    cur = await db.execute(sql, (before,))
    return max(cur.rowcount, 0)


async def _kv_put(db: aiosqlite.Connection, key: str, value: str, now: float) -> None:
    await db.execute(
        """INSERT INTO kv (key, value, updated_at) VALUES (?,?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (key, value, now),
    )


async def _kv_incr(db: aiosqlite.Connection, key: str, by: int, now: float) -> int:
    cur = await db.execute(
        """INSERT INTO kv (key, value, updated_at) VALUES (?, CAST(? AS TEXT), ?)
           ON CONFLICT(key) DO UPDATE SET
             value=CAST(CAST(kv.value AS INTEGER) + ? AS TEXT), updated_at=excluded.updated_at
           RETURNING value""",
        (key, by, now, by),
    )
    row = await cur.fetchone()
    return int(row["value"]) if row else 0


async def _insert_sighting(db: aiosqlite.Connection, s: Sighting, now: float) -> None:
    await db.execute(
        """INSERT INTO sightings (post_id, group_id, read_id, surface, position, batch, cursor,
             is_highlight, is_anchor, created_at, seen_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            s.post_id,
            s.group_id,
            s.read_id,
            s.surface.value,
            s.position,
            s.batch,
            s.cursor,
            int(s.is_highlight),
            int(s.is_anchor),
            s.created_at,
            now,
        ),
    )


async def _fetch_group(db: aiosqlite.Connection, group_id: str) -> GroupRow | None:
    cur = await db.execute("SELECT * FROM groups WHERE id=?", (group_id,))
    row = await cur.fetchone()
    return _group(row) if row else None


def _group(row: aiosqlite.Row) -> GroupRow:
    return GroupRow(
        id=str(row["id"]),
        name=row["name"],
        slug=row["slug"],
        privacy=GroupPrivacy(row["privacy"]),
        state=GroupState(row["state"]),
        join_state=row["join_state"],
        subscribe_status=row["subscribe_status"],
        sorting_setting=row["sorting_setting"],
        monitored=bool(row["monitored"]),
        tier=int(row["tier"]),
        listings=bool(row["listings"]),
        baseline_at=float(row["baseline_at"]),
        missing_syncs=int(row["missing_syncs"]),
        flags=frozenset(json.loads(row["flags"])),
        last_synced_at=row["last_synced_at"],
        updated_at=float(row["updated_at"]),
    )


def _outbox_row(row: aiosqlite.Row) -> OutboxRow:
    return OutboxRow(
        seq=int(row["seq"]),
        group_id=str(row["group_id"]),
        post_id=str(row["post_id"]),
        payload=str(row["payload"]),
        size=int(row["size"]),
        attempts=int(row["attempts"]),
        created_at=float(row["created_at"]),
    )


def _quarantine_row(row: aiosqlite.Row) -> QuarantineRow:
    return QuarantineRow(
        group_id=str(row["group_id"]),
        post_id=str(row["post_id"]),
        read_id=row["read_id"],
        surface=Surface(row["surface"]) if row["surface"] else None,
        created_at=row["created_at"],
        errors=tuple(json.loads(row["errors"])),
        raw_path=row["raw_path"],
        first_at=float(row["first_at"]),
        last_at=float(row["last_at"]),
        attempts=int(row["attempts"]),
    )


def _deferred_row(row: aiosqlite.Row) -> DeferredRow:
    return DeferredRow(
        post_id=str(row["post_id"]),
        group_id=row["group_id"],
        reason=DeferredReason(row["reason"]),
        first_at=float(row["first_at"]),
        last_at=float(row["last_at"]),
        attempts=int(row["attempts"]),
        raw_path=row["raw_path"],
    )


def _block(row: aiosqlite.Row) -> Block:
    return Block(
        id=int(row["id"]),
        opened_at=float(row["opened_at"]),
        until=row["until"],
        rung=int(row["rung"]),
        state=PageState(row["state"]),
        reason=str(row["reason"]),
        excerpt=row["excerpt"],
        screenshot=row["screenshot"],
        no_probe=bool(row["no_probe"]),
        closed_at=row["closed_at"],
        close_reason=row["close_reason"],
    )


def _read_record(row: aiosqlite.Row) -> ReadRecord:
    return ReadRecord(
        id=int(row["id"]),
        surface=Surface(row["surface"]),
        group_id=row["group_id"],
        started_at=float(row["started_at"]),
        ended_at=row["ended_at"],
        coverage=Coverage(
            newest=row["newest"],
            oldest=row["oldest"],
            target=row["target"],
            target_reached=bool(row["target_reached"]),
        ),
        steps=int(row["steps"]),
        outcome=ReadOutcome(row["outcome"]) if row["outcome"] else None,
        page_state=PageState(row["page_state"]) if row["page_state"] else None,
        stats=json.loads(row["stats"]),
    )
