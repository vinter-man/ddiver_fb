"""Where queued items go: the brain later, a local JSONL file in phase 0 — through one interface.

Invariant 5 is the same for both: an item is in the outbox (written in the same transaction as
its ``seen`` row) before anything else happens to it, and the outbox row is deleted only after
the sink answered for it. The phase-0 sink answers ``stored`` once the line is appended and
fsynced; the uplink will answer with the brain's per-item results. A crash between the append
and the delete re-sends the row, so the JSONL file is at-least-once: readers dedupe by
``(group_id, post_id)``.

``os.write`` may write less than it was given (a disk filling up mid-batch): the sink writes
until every byte is down, and a write that fails or makes no progress cuts the file back to
where the batch began and raises, so ``drain`` keeps every row of the batch queued and the file
never ends in half a line.

The file is created 0600 in the collector's root (it holds post text, invariant 8).
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from fbcollector.store import OutboxRow, Store
from fbwire.models import ItemOutcome, ItemResult, ItemsResponse

log = logging.getLogger(__name__)

ITEMS_FILE: Final = "items.jsonl"
MAX_BATCHES_PER_DRAIN: Final = 20


class ItemSink(Protocol):
    async def send(self, collector_id: str, rows: Sequence[OutboxRow]) -> ItemsResponse:
        """Deliver ``rows``; the response names every row that was processed."""
        ...


class JsonlSink:
    """Phase 0: one item per line in ``<root>/items.jsonl``, fsynced before it is answered."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def send(self, collector_id: str, rows: Sequence[OutboxRow]) -> ItemsResponse:  # noqa: ARG002 - one interface
        if rows:
            await asyncio.to_thread(self._append, [row.payload for row in rows])
        return ItemsResponse(
            results=tuple(
                ItemResult(group_id=row.group_id, post_id=row.post_id, result=ItemOutcome.STORED)
                for row in rows
            )
        )

    def _append(self, lines: Sequence[str]) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            start = os.lseek(fd, 0, os.SEEK_END)
            try:
                write_all(fd, "".join(f"{line}\n" for line in lines).encode("utf-8"))
            except OSError:
                with contextlib.suppress(OSError):
                    os.ftruncate(fd, start)
                raise
            os.fsync(fd)
        finally:
            os.close(fd)


def write_all(fd: int, data: bytes) -> None:
    """Write every byte of ``data``; a write that makes no progress is an ``OSError``."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            msg = f"short write: {len(view)} bytes not written"
            raise OSError(errno.ENOSPC, msg)
        view = view[written:]


@dataclass(frozen=True, slots=True)
class DrainResult:
    sent: int = 0
    kept: int = 0
    failed: bool = False


async def drain(
    store: Store, sink: ItemSink, collector_id: str, *, max_batches: int = MAX_BATCHES_PER_DRAIN
) -> DrainResult:
    """Send queued items until the outbox is empty, a send fails, or ``max_batches`` went out."""
    sent = kept = 0
    for _ in range(max_batches):
        batch = await store.next_batch(collector_id)
        if not batch:
            break
        try:
            response = await sink.send(collector_id, batch)
        except Exception as exc:  # noqa: BLE001 - every row stays queued; the next drain retries
            log.warning("sending %d items failed: %s", len(batch), type(exc).__name__)
            await store.note_send_failure(batch, type(exc).__name__)
            return DrainResult(sent, kept + len(batch), failed=True)
        applied = await store.apply_results(batch, response)
        sent += applied.deleted
        kept += applied.kept
        if applied.deleted == 0:
            break  # nothing was answered: do not spin on the same rows
    return DrainResult(sent, kept)
