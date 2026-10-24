"""The AI rule on the brain: wishes, the posts waiting for the model, and what it said.

A user writes a wish for a group in plain words ("a two-room flat to rent in Lozenets, up to
700 euro, straight from the owner"); every new post of that group is judged by a cheap model
(:mod:`ddiver_fb.judge`), which answers how likely the post fits, in percent, and why.

- **The queue is durable.** :meth:`AiStore.enqueue` runs inside the ingest of the post, before
  the brain answers ``stored`` (``FacebookSource``): a post is in the queue, or the collector
  sends it again. Only wishes made before the post was written take it (D10: no backfill).
- **The worker** (:class:`AiWorker`, every few seconds) asks the model about due posts, a few at
  a time. A failed call is retried with a growing delay, for up to six hours from the post's
  arrival; then its verdict records the failure. A daily cap on calls bounds the cost of a
  runaway (a wish on a busy group, a loop); the owner is told once a day when it is reached, and
  once when the model keeps failing and once when it answers again.
- **Shadow first.** A wish starts in shadow: verdicts are recorded and ``/ai`` shows what would
  have been sent, but nothing is. Sending (``live``) comes once the shadow run has shown how
  the rule catches (the owner's plan of 2026-10-12).
- **Invariant 8.** Wishes, texts and the model's reasons are never logged above DEBUG; the
  reasons retell the posts, so verdicts are pruned with the texts after seven days.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Protocol

from pydantic import ValidationError

from ddiver_core.i18n import DEFAULT_LANG, t
from ddiver_core.storage import Storage
from ddiver_core.timeutil import Clock, utcnow
from ddiver_fb.judge import MAX_TEXT_CHARS, JudgeError, Verdict
from ddiver_fb.store import FbStore
from fbwire import Item

log = logging.getLogger(__name__)

DEFAULT_THRESHOLD: Final = 70
"""Send from this percent: in the comparison every true post scored 85-100 and the false ones
of the chosen model 0-30."""
GIVE_UP_AFTER: Final = 6 * 3600
"""A post still unjudged this long after it arrived is given up: a notification that late
would be news nobody can act on."""
BACKOFF: Final = (60, 300, 900, 1800, 3600)
"""Seconds before the 1st, 2nd, … retry of a failed call; the last repeats."""
BATCH: Final = 16
CONCURRENCY: Final = 4
FAILURES_TO_ALERT: Final = 3
"""Failed calls in a row before the owner hears that the model is not answering."""
DAY: Final = 86400
MAX_WISH_CHARS: Final = 500
MAX_WISHES: Final = 20
"""Per user: each wish is a model call per post of its group."""


@dataclass(frozen=True, slots=True)
class Wish:
    id: int
    user_id: int
    group_id: int
    wish: str
    threshold: int
    live: bool
    created_at: int


@dataclass(frozen=True, slots=True)
class Queued:
    wish_id: int
    group_id: int
    post_id: int
    queued_at: int
    attempts: int


@dataclass(frozen=True, slots=True)
class Judged:
    """A verdict that reached its wish's threshold, with the post's link if it is still kept."""

    wish_id: int
    group_id: int
    post_id: int
    judged_at: int
    percent: int
    reason: str
    url: str | None


@dataclass(frozen=True, slots=True)
class WishStats:
    judged: int = 0
    would_send: int = 0
    failed: int = 0
    queued: int = 0
    cost: float = 0.0


class Judge(Protocol):
    model: str

    async def judge(self, wish: str, text: str, *, context: str = "") -> Verdict: ...


def post_text(item: Item) -> str:
    """What the model reads of a post: its text, a marketplace card's fields, a share mark."""
    parts = [item.text]
    if item.listing is not None:
        listing = item.listing
        fields = [
            f"{name}: {value}"
            for name, value in (
                ("title", listing.title),
                ("price", listing.price),
                ("location", listing.location),
            )
            if value
        ]
        if fields:
            parts.append("[marketplace listing] " + "; ".join(fields))
    if item.is_share:
        parts.append("[shared post]")
    return "\n".join(p for p in parts if p).strip()[:MAX_TEXT_CHARS]


def _wish(row: sqlite3.Row) -> Wish:
    return Wish(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        group_id=int(row["group_id"]),
        wish=str(row["wish"]),
        threshold=int(row["threshold"]),
        live=bool(row["live"]),
        created_at=int(row["created_at"]),
    )


class AiStore:
    """The ``fb_ai_*`` tables (schema v5)."""

    def __init__(self, fb: FbStore) -> None:
        self._fb = fb

    # -- wishes ----------------------------------------------------------------------------

    async def add_wish(
        self, user_id: int, group_id: int, wish: str, *, now: int, threshold: int
    ) -> Wish:
        cur = await self._fb.db.execute(
            "INSERT INTO fb_ai_wishes (user_id, group_id, wish, threshold, created_at) "
            "VALUES (?,?,?,?,?)",
            (user_id, group_id, wish, threshold, now),
        )
        found = await self.wish(cur.lastrowid) if cur.lastrowid is not None else None
        if found is None:
            msg = "the new wish was not found"
            raise sqlite3.DatabaseError(msg)
        return found

    async def wish(self, wish_id: int) -> Wish | None:
        cur = await self._fb.db.execute("SELECT * FROM fb_ai_wishes WHERE id=?", (wish_id,))
        row = await cur.fetchone()
        return _wish(row) if row is not None else None

    async def wishes(self, user_id: int | None = None) -> list[Wish]:
        if user_id is None:
            cur = await self._fb.db.execute("SELECT * FROM fb_ai_wishes ORDER BY id")
        else:
            cur = await self._fb.db.execute(
                "SELECT * FROM fb_ai_wishes WHERE user_id=? ORDER BY id", (user_id,)
            )
        return [_wish(row) for row in await cur.fetchall()]

    async def delete_wish(self, wish_id: int, user_id: int) -> bool:
        """The wish and its waiting posts; its verdicts stay until they are pruned."""
        async with self._fb.transaction() as db:
            cur = await db.execute(
                "DELETE FROM fb_ai_wishes WHERE id=? AND user_id=?", (wish_id, user_id)
            )
            if cur.rowcount != 1:
                return False
            await db.execute("DELETE FROM fb_ai_queue WHERE wish_id=?", (wish_id,))
        return True

    # -- the queue -------------------------------------------------------------------------

    async def enqueue(self, group_id: int, post_id: int, *, created_at: int, now: int) -> int:
        """Queue the post for every wish on its group made before it was written; one statement,
        and a resent post queues nothing twice, not even once it was judged (a resend can follow
        a busy database after this step, and the worker may have judged it meanwhile)."""
        cur = await self._fb.db.execute(
            "INSERT OR IGNORE INTO fb_ai_queue (wish_id, group_id, post_id, queued_at, due_at) "
            "SELECT w.id, ?, ?, ?, ? FROM fb_ai_wishes w WHERE w.group_id=? AND w.created_at<=? "
            "AND NOT EXISTS (SELECT 1 FROM fb_ai_verdicts v "
            "WHERE v.wish_id=w.id AND v.group_id=? AND v.post_id=?)",
            (group_id, post_id, now, now, group_id, created_at, group_id, post_id),
        )
        return max(cur.rowcount, 0)

    async def due(self, *, now: int, limit: int) -> list[Queued]:
        cur = await self._fb.db.execute(
            "SELECT wish_id, group_id, post_id, queued_at, attempts FROM fb_ai_queue "
            "WHERE due_at<=? ORDER BY due_at, queued_at LIMIT ?",
            (now, limit),
        )
        return [
            Queued(
                wish_id=int(r["wish_id"]),
                group_id=int(r["group_id"]),
                post_id=int(r["post_id"]),
                queued_at=int(r["queued_at"]),
                attempts=int(r["attempts"]),
            )
            for r in await cur.fetchall()
        ]

    async def item(self, group_id: int, post_id: int) -> Item | None:
        """The post as it came, or ``None`` when it is no longer kept (or no longer valid)."""
        cur = await self._fb.db.execute(
            "SELECT item FROM fb_texts WHERE group_id=? AND post_id=?", (group_id, post_id)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        try:
            return Item.model_validate_json(row["item"])
        except ValidationError:
            return None

    async def record(
        self, queued: Queued, *, now: int, verdict: Verdict | None = None, error: str | None = None
    ) -> None:
        """The post's verdict (or the failure that ended its tries), and out of the queue."""
        async with self._fb.transaction() as db:
            await db.execute(
                "INSERT OR REPLACE INTO fb_ai_verdicts (wish_id, group_id, post_id, judged_at, "
                "percent, reason, error, model, cost, calls) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    queued.wish_id,
                    queued.group_id,
                    queued.post_id,
                    now,
                    verdict.percent if verdict else None,
                    verdict.reason if verdict else None,
                    error,
                    verdict.model if verdict else None,
                    verdict.cost if verdict else None,
                    queued.attempts + 1,
                ),
            )
            await db.execute(
                "DELETE FROM fb_ai_queue WHERE wish_id=? AND group_id=? AND post_id=?",
                (queued.wish_id, queued.group_id, queued.post_id),
            )

    async def drop(self, queued: Queued) -> None:
        await self._fb.db.execute(
            "DELETE FROM fb_ai_queue WHERE wish_id=? AND group_id=? AND post_id=?",
            (queued.wish_id, queued.group_id, queued.post_id),
        )

    async def retry(self, queued: Queued, *, error: str, due_at: int) -> None:
        await self._fb.db.execute(
            "UPDATE fb_ai_queue SET attempts=attempts+1, due_at=?, last_error=? "
            "WHERE wish_id=? AND group_id=? AND post_id=?",
            (due_at, error, queued.wish_id, queued.group_id, queued.post_id),
        )

    # -- what was said ---------------------------------------------------------------------

    async def calls_since(self, since: int) -> int:
        """Model calls made after ``since``, near enough for a cost cap: each verdict's tries,
        and the failed tries of the posts still waiting (an empty answer's second ask is free
        here)."""
        cur = await self._fb.db.execute(
            "SELECT (SELECT COALESCE(SUM(calls), 0) FROM fb_ai_verdicts WHERE judged_at>?)"
            " + (SELECT COALESCE(SUM(attempts), 0) FROM fb_ai_queue WHERE queued_at>?) AS n",
            (since, since),
        )
        row = await cur.fetchone()
        return int(row["n"]) if row is not None else 0

    async def stats(self, wish: Wish, *, since: int) -> WishStats:
        cur = await self._fb.db.execute(
            "SELECT COUNT(percent) AS judged, SUM(percent>=?) AS would, "
            "SUM(percent IS NULL) AS failed, COALESCE(SUM(cost), 0) AS cost "
            "FROM fb_ai_verdicts WHERE wish_id=? AND judged_at>=?",
            (wish.threshold, wish.id, since),
        )
        row = await cur.fetchone()
        cur = await self._fb.db.execute(
            "SELECT COUNT(*) AS n FROM fb_ai_queue WHERE wish_id=?", (wish.id,)
        )
        queued = await cur.fetchone()
        return WishStats(
            judged=int(row["judged"] or 0) if row else 0,
            would_send=int(row["would"] or 0) if row else 0,
            failed=int(row["failed"] or 0) if row else 0,
            queued=int(queued["n"]) if queued else 0,
            cost=float(row["cost"] or 0) if row else 0.0,
        )

    async def matched(self, user_id: int, *, since: int, limit: int) -> list[Judged]:
        """The user's verdicts at or over their wish's threshold, newest first."""
        cur = await self._fb.db.execute(
            "SELECT v.wish_id, v.group_id, v.post_id, v.judged_at, v.percent, v.reason, t.item "
            "FROM fb_ai_verdicts v JOIN fb_ai_wishes w ON w.id = v.wish_id "
            "LEFT JOIN fb_texts t ON t.group_id = v.group_id AND t.post_id = v.post_id "
            "WHERE w.user_id=? AND v.judged_at>=? AND v.percent>=w.threshold "
            "ORDER BY v.judged_at DESC LIMIT ?",
            (user_id, since, limit),
        )
        found = []
        for r in await cur.fetchall():
            url = None
            if r["item"] is not None:
                with contextlib.suppress(ValidationError):
                    url = Item.model_validate_json(r["item"]).url
            found.append(
                Judged(
                    wish_id=int(r["wish_id"]),
                    group_id=int(r["group_id"]),
                    post_id=int(r["post_id"]),
                    judged_at=int(r["judged_at"]),
                    percent=int(r["percent"]),
                    reason=str(r["reason"] or ""),
                    url=url,
                )
            )
        return found

    async def prune(self, *, before: int) -> int:
        """Verdicts, and posts that have waited, since before ``before``."""
        removed = await self._fb.prune_rows("fb_ai_verdicts", "judged_at", before)
        return removed + await self._fb.prune_rows("fb_ai_queue", "queued_at", before)


Notify = Callable[[int, str], Awaitable[None]]
"""Queue a message for a user (the monitor's sender delivers it)."""


class AiWorker:
    """Asks the model about due posts; one :meth:`run_once` per tick."""

    def __init__(
        self,
        store: AiStore,
        fb: FbStore,
        storage: Storage,
        judge: Judge | None,
        *,
        owner_id: int,
        notify: Notify,
        daily_calls: int,
        clock: Clock = utcnow,
    ) -> None:
        self._store = store
        self._fb = fb
        self._storage = storage
        self.judge = judge
        self._owner_id = owner_id
        self._notify = notify
        self.daily_calls = daily_calls
        self._clock = clock
        self._failures = 0
        self._failing_told = False
        self._cap_told_at: int | None = None

    def _now(self) -> int:
        return int(self._clock().timestamp())

    async def run_once(self) -> int:
        """Judge what is due, within the day's cap; returns how many posts were handled."""
        if self.judge is None:
            return 0
        now = self._now()
        room = self.daily_calls - await self._store.calls_since(now - DAY)
        if room <= 0:
            await self._cap_reached(now)
            return 0
        due = await self._store.due(now=now, limit=min(BATCH, room))
        if not due:
            return 0
        gate = asyncio.Semaphore(CONCURRENCY)

        async def one(queued: Queued) -> None:
            async with gate:
                await self._one(queued)

        results = await asyncio.gather(*(one(q) for q in due), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                # A busy database: the post stays queued and is tried at the next tick.
                log.warning("an AI verdict was not recorded: %s", type(result).__name__)
        return len(due)

    async def _one(self, queued: Queued) -> None:
        judge = self.judge
        if judge is None:
            return
        wish = await self._store.wish(queued.wish_id)
        item = await self._store.item(queued.group_id, queued.post_id)
        if wish is None or item is None:
            await self._store.drop(queued)  # the wish was deleted, or the post is gone
            return
        group = await self._fb.group(queued.group_id)
        context = group.title if group is not None and group.title else item.group_name
        try:
            verdict = await judge.judge(wish.wish, post_text(item), context=context)
        except JudgeError as exc:
            await self._failed(queued, exc.kind)
            return
        except Exception as exc:  # noqa: BLE001 - whatever it was, back off and count the call
            await self._failed(queued, type(exc).__name__)
            return
        await self._store.record(queued, now=self._now(), verdict=verdict)
        log.info(
            "AI wish %d judged post %d/%d: %d%%",
            wish.id,
            queued.group_id,
            queued.post_id,
            verdict.percent,
        )
        await self._answered()

    async def _failed(self, queued: Queued, kind: str) -> None:
        now = self._now()
        log.warning(
            "AI call failed for post %d/%d (try %d): %s",
            queued.group_id,
            queued.post_id,
            queued.attempts + 1,
            kind,
        )
        if now - queued.queued_at >= GIVE_UP_AFTER:
            await self._store.record(queued, now=now, error=kind)
        else:
            delay = BACKOFF[min(queued.attempts, len(BACKOFF) - 1)]
            await self._store.retry(queued, error=kind, due_at=now + delay)
        self._failures += 1
        if self._failures >= FAILURES_TO_ALERT and not self._failing_told:
            # Set before the await, so the calls failing beside this one do not tell it too;
            # put back if the notice could not be queued, so a later failure tries again.
            self._failing_told = True
            try:
                await self._tell("fb.ai.failing", error=kind)
            except BaseException:
                self._failing_told = False
                raise

    async def _answered(self) -> None:
        self._failures = 0
        if self._failing_told:
            self._failing_told = False
            try:
                await self._tell("fb.ai.recovered")
            except BaseException:
                self._failing_told = True
                raise

    async def _cap_reached(self, now: int) -> None:
        if self._cap_told_at is not None and now - self._cap_told_at < DAY:
            return
        log.warning("the AI rule's daily cap of %d calls is reached", self.daily_calls)
        await self._tell("fb.ai.cap", cap=self.daily_calls)
        self._cap_told_at = now  # run_once is not concurrent with itself: no race here

    async def _tell(self, key: str, **params: object) -> None:
        user = await self._storage.get_user(self._owner_id)
        lang = (user.lang if user else None) or DEFAULT_LANG
        await self._notify(self._owner_id, t(lang, key, **params))
