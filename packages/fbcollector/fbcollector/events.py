"""One place where a transition becomes durable and reaches the owner.

Every collector event (a block opened or lifted, a group lost, the feed not chronological, the
parser broken, a crash loop, a full outbox, posts in quarantine) goes through
``CollectorEvents``: queued for the brain in the ``events`` table when an uplink exists (so a
transition during an outage is not lost), and sent to the owner through the alert bot, which is
the only path in phase 0 and the path of last resort later (D19).

The ladder calls it with ``(kind, block_id=, detail=)``; the reader adds ``group_id=`` and a
group name. Alert texts are built in ``alerts`` from these fields only — never from a post.

States that last (a full outbox, a crash loop, posts in quarantine) are events on their *edges*
only: the durable queue is capped, and one row per new post or per hold turn during the very
outage that fills it would push out the transitions ``/v1/events`` exists to keep.
``outbox_edge`` keeps the outbox's level in kv, ``quarantine_edge`` whether the quarantine was
announced; the read loop compares the crash-loop state with the last status.

While a read is under way the read loop *holds* the owner alerts (``hold_alerts``) and sends
them only once it has closed Chrome (``flush_alerts``). DESIGN's order on a warning is block on
disk → Chrome closed → owner alerted: a Telegram round trip (a 3 s spacing sleep and a 10 s
timeout) must not keep the tab on a checkpoint page, the screenshot must show the page as the
detector saw it, and the alert says Chrome is closed. The brain's queue is written at once.
An alert whose delivery must be known (a panel notice is recorded as told only once the owner
has it) carries an ``on_sent`` callback, awaited after the bot delivered it, held or not.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from typing import Final

from fbcollector import alerts
from fbcollector.alerts import AlertResult, Alerts
from fbcollector.store import Store
from fbwire.models import EventKind

log = logging.getLogger(__name__)

CRITICAL: Final = frozenset(
    {
        EventKind.BLOCK_OPENED,
        EventKind.LOGGED_OUT,
        EventKind.CRASH_LOOP,
        EventKind.PARSER_BROKEN,
    }
)
DEDUP: Final = {
    EventKind.NOT_FOLLOWED: 24 * 3600.0,
    EventKind.SORT_IGNORED: 7 * 24 * 3600.0,
    EventKind.FEED_NOT_CHRONO: 24 * 3600.0,
    EventKind.OUTBOX_HIGH: 6 * 3600.0,
    EventKind.CRASH_LOOP: 6 * 3600.0,
    EventKind.ITEMS_INVALID: 24 * 3600.0,
}
DELIVERED: Final = frozenset({AlertResult.SENT, AlertResult.DUPLICATE, AlertResult.DISABLED})
"""What the bot answers for an alert the owner has (or, with no bot set up, the log has): a
duplicate was delivered before. A failed or rate-limited one was not."""
OUTBOX_HIGH_SHARE: Final = 0.8
KV_OUTBOX_HIGH: Final = "outbox_high"
KV_QUARANTINE: Final = "quarantine_announced"
QUARANTINE_HINT: Final = (
    "Nothing is lost: they are not marked seen, and each is tried again when a read sees it "
    "again. After a parser or wire fix: `fbcollector replay --since 2d`. "
    "`fbcollector status` lists them."
)


async def outbox_edge(store: Store, depth: int, cap: int) -> bool:
    """``True`` exactly when the outbox has just reached 80 % of its cap (an event is due).

    The level is remembered in kv, so the next item above the mark is not another event, and
    dropping below the mark re-arms it.
    """
    high = depth >= OUTBOX_HIGH_SHARE * cap
    flagged = await store.kv_get(KV_OUTBOX_HIGH) is not None
    if high and not flagged:
        await store.kv_set(KV_OUTBOX_HIGH, str(depth))
        return True
    if not high and flagged:
        await store.kv_delete(KV_OUTBOX_HIGH)
    return False


async def quarantine_edge(store: Store) -> str | None:
    """The event's detail exactly when posts wait in quarantine and nobody was told yet.

    The flag is kept in kv: the next invalid post is no second event while any is waiting, and
    an empty quarantine re-arms it. In local mode the read loop drops it at every start: the
    alert it stands for may have been held for the end of a read and died with the process
    (``Collector.run``). The detail counts posts and names the fields and errors that failed
    (``url:value_error``), never a value: a value may be post text.
    """
    rows = await store.list_quarantined()
    flagged = await store.kv_get(KV_QUARANTINE) is not None
    if not rows:
        if flagged:
            await store.kv_delete(KV_QUARANTINE)
        return None
    if flagged:
        return None
    await store.kv_set(KV_QUARANTINE, str(len(rows)), durable=True)
    errors = sorted({e for row in rows for e in row.errors})
    return f"{len(rows)} post(s) did not fit the wire model: {', '.join(errors)}"[:300]


OnSent = Callable[[], Awaitable[None]]
"""What to do once an alert was delivered (``CollectorEvents.notify``)."""

GROUP_KINDS: Final = {
    EventKind.GROUP_LOST: "group_lost",
    EventKind.NOT_FOLLOWED: "not_followed",
    EventKind.SORT_IGNORED: "sort_ignored",
}


class CollectorEvents:
    def __init__(
        self,
        store: Store,
        sender: Alerts,
        *,
        collector_id: str,
        local_mode: bool,
        zone: dt.tzinfo | None = None,
    ) -> None:
        self._store = store
        self._alerts = sender
        self._collector_id = collector_id
        self._local = local_mode
        self._zone = zone
        self._held: list[tuple[str, str, bool, float | None, OnSent | None]] | None = None

    def hold_alerts(self) -> None:
        """Queue owner alerts from now on, until ``flush_alerts`` (a read is under way)."""
        if self._held is None:
            self._held = []

    async def flush_alerts(self) -> None:
        """Send the held alerts, in order, and stop holding."""
        held, self._held = self._held, None
        for key, text, critical, dedup, on_sent in held or ():
            await self._deliver(key, text, critical, dedup, on_sent)

    async def _send(
        self,
        key: str,
        text: str,
        *,
        critical: bool = False,
        dedup: float | None = None,
        on_sent: OnSent | None = None,
    ) -> None:
        if self._held is not None:
            self._held.append((key, text, critical, dedup, on_sent))
            return
        await self._deliver(key, text, critical, dedup, on_sent)

    async def _deliver(
        self, key: str, text: str, critical: bool, dedup: float | None, on_sent: OnSent | None
    ) -> None:
        result = await self._alerts.alert(key, text, critical=critical, dedup=dedup)
        if on_sent is None or result not in DELIVERED:
            return
        try:
            await on_sent()
        except Exception as exc:  # noqa: BLE001 - the next held alert still goes out
            log.warning("alert %s delivered, its record failed: %s", key, type(exc).__name__)

    async def __call__(
        self,
        kind: EventKind,
        *,
        block_id: int | None = None,
        detail: str | None = None,
        group_id: str | None = None,
        group_name: str | None = None,
    ) -> None:
        if not self._local:
            await self._store.enqueue_event(
                kind,
                collector_id=self._collector_id,
                group_id=group_id,
                block_id=block_id,
                detail=detail,
            )
        if kind is EventKind.LOGGED_OUT:
            return  # the block opened for it has already been announced with its state
        text = await self._text(kind, block_id, detail or "", group_id, group_name)
        key = f"{kind.value}:{block_id or group_id or ''}"
        await self._send(key, text, critical=kind in CRITICAL, dedup=DEDUP.get(kind))

    async def notify(
        self, key: str, text: str, *, dedup: float | None = None, on_sent: OnSent | None = None
    ) -> None:
        """An owner alert that is not a wire event (parser warnings, unverified groups, panel
        notices). ``on_sent`` is awaited once the bot delivered it (``DELIVERED``), after the
        read when held; never when the post failed or was rate-limited."""
        await self._send(key, text, dedup=dedup, on_sent=on_sent)

    async def _text(
        self,
        kind: EventKind,
        block_id: int | None,
        detail: str,
        group_id: str | None,
        group_name: str | None,
    ) -> str:
        if kind is EventKind.BLOCK_OPENED and block_id is not None:
            block = await self._store.current_block()
            if block is not None and block.id == block_id:
                return alerts.block_opened(
                    block.id,
                    block.state.value,
                    block.reason,
                    block.until,
                    no_probe=block.no_probe,
                    zone=self._zone,
                )
        if kind is EventKind.BLOCK_CLOSED and block_id is not None:
            return alerts.probe_result(block_id, True, detail)
        if kind in GROUP_KINDS and group_id is not None:
            return alerts.group_event(GROUP_KINDS[kind], group_id, group_name, detail)
        if kind is EventKind.ITEMS_INVALID:
            return alerts.simple(kind.value, detail or kind.value, hint=QUARANTINE_HINT)
        return alerts.simple(kind.value, detail or kind.value)
