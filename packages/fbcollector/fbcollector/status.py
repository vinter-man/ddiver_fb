"""What the collector says it is doing: a record for the owner's CLI, a ``Status`` for the brain.

The read loop records its state at every turn (``note_state``): the state of the last hold or
read, why, since when, until when a planned pause lasts, and the gates it last checked. The
uplink turns that record into the wire ``Status`` once a minute (``StatusBuilder``), adding what
only the database knows at that moment:

- **The open block wins.** A block on disk *is* the state, whatever the last turn recorded: a
  status must never say "ok" while the ladder holds a block. A "blocked" record without an open
  block is stale (a clean probe closed it a moment ago) and is reported as reading again.
- **Named causes** instead of a generic pause: a broken parser is ``parser_broken`` and reading
  in backstop-only mode is ``feed_not_chrono``, the states the brain alerts on and explains.
- ``resumes_at`` for the planned pauses (quiet hours, a break, an UNKNOWN back-off), so the brain
  shows when reading resumes and knows how long a silence is excused (wire change 3).
- The current cadence as counters (``feed_interval_max_seconds``, ``backstop_interval_seconds``)
  from the planner itself, so the delays users are promised follow what the collector really
  does, the shoulder around quiet hours and stretched backstops included (wire change 11).
- ``group_checked``: per monitored group, the start of its last backstop that reached its
  target — the group's own page read back past everything the collector had, so a group with no
  new post since then is quiet, not unseen (wire change 17). The brain's silence alert counts
  from it. Not for a group whose page ignores the chronological sort: old posts bumped by new
  comments can end its read while a new post sits further down.

Gates are not evaluated here. Evaluating them from a second task would have side effects on the
read loop's own checks (the sleep gate reports each detected sleep exactly once, to whoever asks
first), so the status reports the gates the loop itself last checked.

The status carries states, reasons, ids, counts and versions — never a post (invariant 8).
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import math
import re
from collections.abc import Iterable, Mapping
from typing import Any, Final, Protocol

from fbcollector.budget import knobs_for
from fbcollector.clock import Clock
from fbcollector.config import Settings
from fbcollector.model import CollectorState, PageState, Surface
from fbcollector.reader import FLAG_SORT_IGNORED, KV_PARSER_BROKEN
from fbcollector.scheduler import Cadence
from fbcollector.store import (
    KV_EVENTS_DROPPED,
    KV_OUTBOX_DROPPED,
    KV_OUTBOX_REJECTED,
    KV_REFUSED,
    UPLINK_ENDPOINTS,
    Block,
    Store,
)
from fbwire.models import (
    EPOCH_MAX,
    EPOCH_MIN,
    MAX_MONITORED,
    MAX_REASON_LEN,
    WIRE_VERSION,
    BlockInfo,
    Status,
)

KV_STATUS: Final = "status"
KV_HALT: Final = "halt"
"""Why the collector stopped itself until a human acts (written by the runner)."""
KV_VERSIONS: Final = "versions"
IDLE_STATES: Final = frozenset({CollectorState.QUIET, CollectorState.BREAK, CollectorState.UNKNOWN})
"""Planned pauses that end by themselves: their end is announced as ``resumes_at``."""
_KEY: Final = re.compile(r"^[a-z0-9_]{1,40}$")
_GROUP_ID: Final = re.compile(r"^[1-9][0-9]{0,18}$")
MAX_VERSION_LEN: Final = 64
FEED_COUNTER: Final = "feed_interval_max_seconds"
BACKSTOP_COUNTER: Final = "backstop_interval_seconds"


async def note_state(
    store: Store,
    state: CollectorState,
    reasons: Iterable[str],
    *,
    now: float,
    until: float | None = None,
    gates: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    """Record the loop's state; returns the record it replaced (``{}`` when there was none).

    ``since`` survives turns in the same state; ``gates`` survive turns that checked none (a
    planner wait), so the status shows the last gates actually checked.
    """
    raw = await store.kv_get_json(KV_STATUS)
    before: dict[str, Any] = raw if isinstance(raw, dict) else {}
    same = before.get("state") == state.value and before.get("since") is not None
    record: dict[str, Any] = {
        "state": state.value,
        "reasons": list(reasons),
        "at": now,
        "since": before["since"] if same else now,
    }
    if until is not None:
        record["until"] = until
    kept = dict(gates) if gates is not None else before.get("gates")
    if kept:
        record["gates"] = kept
    await store.kv_set_json(KV_STATUS, record)
    return before


class CadenceSource(Protocol):
    """The scheduler, as far as the status needs it."""

    async def cadence(self) -> Cadence: ...


def package_version() -> str:
    with contextlib.suppress(importlib.metadata.PackageNotFoundError):
        return importlib.metadata.version("fbcollector")
    return "unknown"


class StatusBuilder:
    def __init__(
        self,
        *,
        store: Store,
        scheduler: CadenceSource,
        settings: Settings,
        clock: Clock,
        version: str | None = None,
    ) -> None:
        self._store = store
        self._scheduler = scheduler
        self._settings = settings
        self._clock = clock
        self._version = version or package_version()

    async def build(self, *, seq: int | None = None, incarnation: str | None = None) -> Status:
        store = self._store
        now = int(self._clock.time())
        raw = await store.kv_get_json(KV_STATUS)
        record: dict[str, Any] = raw if isinstance(raw, dict) else {}
        cadence = await self._scheduler.cadence()
        block = await store.current_block()
        state, reasons, since = await self._state(record, block, cadence)
        resumes_at = _epoch(record.get("until")) if state in IDLE_STATES else None
        knobs = knobs_for(self._settings, cadence.profile)
        return Status(
            collector_id=self._settings.collector_id,
            sent_at=now,
            state=state,
            state_since=since,
            detail=_detail(reasons),
            profile=cadence.profile,
            budget=tuple(await store.budget_usage(knobs.windows())),
            gates=_gates(record.get("gates")),
            counters=await self._counters(cadence),
            block=_block_info(block) if block is not None else None,
            versions=await self._versions(),
            seq=seq,
            incarnation=incarnation,
            resumes_at=resumes_at,
            group_checked=await self._group_checked(),
        )

    async def _state(
        self, record: Mapping[str, Any], block: Block | None, cadence: Cadence
    ) -> tuple[CollectorState, list[str], int | None]:
        recorded = _collector_state(record.get("state"))
        reasons = [str(r) for r in record.get("reasons", ()) if r]
        since = _epoch(record.get("since"))
        if block is not None:
            state = (
                CollectorState.LOGGED_OUT
                if block.state is PageState.LOGGED_OUT
                else CollectorState.BLOCKED
            )
            return state, [block.reason], _epoch(block.opened_at)
        if recorded in {CollectorState.BLOCKED, CollectorState.LOGGED_OUT}:
            return CollectorState.OK, [], None  # lifted since the loop's last turn
        store = self._store
        if (
            recorded is CollectorState.PAUSED
            and await store.kv_get(KV_PARSER_BROKEN) is not None
            and await store.kv_get(KV_HALT) is None
        ):
            return CollectorState.PARSER_BROKEN, reasons, since
        if recorded is CollectorState.OK and cadence.backstop_only:
            return CollectorState.FEED_NOT_CHRONO, reasons, since
        return recorded, reasons, since

    async def _counters(self, cadence: Cadence) -> dict[str, int]:
        store = self._store
        counters = {
            "outbox_depth": await store.outbox_depth(),
            "outbox_dropped": await store.kv_int(KV_OUTBOX_DROPPED),
            "outbox_rejected": await store.kv_int(KV_OUTBOX_REJECTED),
            "events_dropped": await store.kv_int(KV_EVENTS_DROPPED),
            FEED_COUNTER: math.ceil(cadence.feed_max),
        }
        if cadence.backstop is not None and math.isfinite(cadence.backstop):
            counters[BACKSTOP_COUNTER] = math.ceil(cadence.backstop)
        for endpoint in UPLINK_ENDPOINTS:
            # Requests the brain answered but refused: why a reading collector's posts or
            # events do not arrive, visible to the brain even while the refusal lasts.
            if refused := await store.kv_int(KV_REFUSED + endpoint):
                counters[KV_REFUSED + endpoint] = refused
        return {k: max(0, v) for k, v in counters.items()}

    async def _group_checked(self) -> dict[str, int]:
        store = self._store
        checked: dict[str, int] = {}
        ids = [
            g.id
            for g in await store.list_groups(monitored_only=True)
            # An id the wire would refuse must not cost the whole status.
            if FLAG_SORT_IGNORED not in g.flags and _GROUP_ID.match(g.id) and int(g.id) < 2**63
        ]
        for group_id in sorted(ids)[:MAX_MONITORED]:
            read = await store.last_read(Surface.GROUP, group_id=group_id, target_reached=True)
            if read is not None and (started := _epoch(read.started_at)) is not None:
                checked[group_id] = started
        return checked

    async def _versions(self) -> dict[str, str]:
        found = await self._store.kv_get_json(KV_VERSIONS)
        versions: dict[str, Any] = {
            **(found if isinstance(found, dict) else {}),
            "fbcollector": self._version,
            "wire": str(WIRE_VERSION),
        }
        return {
            k: str(v)[:MAX_VERSION_LEN]
            for k, v in versions.items()
            if v is not None and isinstance(k, str) and _KEY.match(k)
        }


def _collector_state(value: Any) -> CollectorState:
    try:
        state = CollectorState(value)
    except ValueError:
        return CollectorState.OK  # no record yet: nothing has stopped the loop
    # OFFLINE is the brain's verdict, never the collector's.
    return CollectorState.OK if state is CollectorState.OFFLINE else state


def _epoch(value: Any) -> int | None:
    if not isinstance(value, int | float) or not math.isfinite(value):
        return None
    epoch = int(value)
    return epoch if EPOCH_MIN <= epoch <= EPOCH_MAX else None


def _detail(reasons: Iterable[str]) -> str | None:
    text = "; ".join(reasons).strip()
    return text[:MAX_REASON_LEN] or None


def _gates(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    return {str(k): bool(v) for k, v in value.items() if isinstance(k, str) and _KEY.match(k)}


def _block_info(block: Block) -> BlockInfo:
    return BlockInfo(
        id=block.id,
        opened_at=int(block.opened_at),
        until=None if block.until is None else int(block.until),
        reason=block.reason[:MAX_REASON_LEN] or block.state.value,
        rung=block.rung,
        no_probe=block.no_probe,
    )
