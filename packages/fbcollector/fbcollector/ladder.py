"""The block ladder: a warning is terminal until a human says otherwise (invariant 3).

Rungs are 6 h → 24 h → 72 h → stop until re-plan. A wall that comes straight back after its
window is saying the window was not the answer, so the next block climbs one rung. Strikes
expire, though (owner decision of 2026-09-25): once 30 consecutive days (``strike_decay_days``)
have passed since the latest block was lifted without another one, the next block starts again
at the first rung (6 h). The last rung has no ``until``; only a local, explicit re-plan can lift
it, and the 30 days count from that lift. An open block never decays: a failed resume probe
always climbs.

Order matters more than anything else here. ``trip`` writes the block (fsynced) *before* it
touches any other state, so a crash right after a detector fired can only leave a block
behind, never a collector that forgot it saw a wall.

Resuming needs three things, each checked against the *currently open* block:

1. a resume command carrying that block's id and a nonce never used before — the nonce is
   consumed on first sight whatever the outcome, so a command is an edge: a ``/resume`` that
   arrives too early, or for an older block, can never take effect later by being replayed;
2. the block's ``until`` has passed (and, for ``checkpoint/advanced_protection``, the owner has
   confirmed the lock is lifted — until then there are no probes at all);
3. one probe of the Groups feed that comes back clean. A failed probe opens the next rung.
   Clean means no warning: OK, or a recognised state that is no warning at all — the feed not
   chronological (backstop-only mode, possibly for good; a probe that failed on it would climb
   every block up to the last rung), a group not followed, a sort ignored. Only a blocked,
   logged-out or unrecognised page (or a lost group) fails it.

An unrecognised page is not yet a warning: the first one backs off 5 → 10 → 20 → 40 minutes
(escalating over 24 h); a second one *in a row* — with no recognised page in between — or a
fifth within 24 h goes onto the ladder. Two groups lost within 24 h are a warning too.

After a clean probe the collector runs the cautious profile for 7 days (14 after a forced
logout); that deadline only ever moves later.

Two tasks change the ladder's state: the read loop (verdicts, the probe's result) and the
uplink (a remote resume). Every change is a read-modify-write of kv ``ladder`` under one lock,
from the row as it is on disk: a save from a stale copy once undid the other task's change — a
resume arriving during a clean probe put back the old state and wiped the cautious period.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any, Final, Protocol

from fbcollector.clock import Clock
from fbcollector.gates import GateContext, GateResult
from fbcollector.model import CollectorState, PageState
from fbwire.models import EventKind

if TYPE_CHECKING:
    from fbcollector.detectors import Verdict
    from fbcollector.store import Block

log = logging.getLogger(__name__)

HOUR: Final = 3600.0
DAY: Final = 86400.0
RUNGS: Final[tuple[float | None, ...]] = (6 * HOUR, 24 * HOUR, 72 * HOUR, None)
STRIKE_DECAY_DAYS: Final = 30.0
"""Days without any block after the latest one was lifted, before strikes start over."""
UNKNOWN_BACKOFF: Final = (5 * 60.0, 10 * 60.0, 20 * 60.0, 40 * 60.0)
UNKNOWN_WINDOW: Final = DAY
GROUP_LOST_WINDOW: Final = DAY
GROUP_LOST_LIMIT: Final = 2
KV_LADDER: Final = "ladder"
REASON_MAX: Final = 300
PROBE_CLEAN: Final = frozenset(
    {PageState.OK, PageState.FEED_NOT_CHRONO, PageState.NOT_FOLLOWED, PageState.SORT_IGNORED}
)
"""Probe verdicts that lift a block: recognised pages that are not warnings."""


class BlockStore(Protocol):
    """The part of the store the ladder uses (``Store`` satisfies it)."""

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
    ) -> Block: ...

    async def current_block(self) -> Block | None: ...

    async def latest_block(self) -> Block | None: ...

    async def close_block(self, block_id: int, *, reason: str) -> bool: ...

    async def claim_nonce(self, nonce: str, *, kind: str) -> bool: ...

    async def kv_get(self, key: str) -> str | None: ...

    async def kv_set(self, key: str, value: str, *, durable: bool = False) -> None: ...


class EventSink(Protocol):
    async def __call__(
        self, kind: EventKind, *, block_id: int | None = None, detail: str | None = None
    ) -> None: ...


class Outcome(enum.StrEnum):
    CONTINUE = "continue"  # a recognised page; carry on
    BACKOFF = "backoff"  # an unrecognised page: stop the read, close Chrome, wait
    BLOCKED = "blocked"  # a block was opened: close Chrome, alert
    GROUP_PAUSED = "group_paused"  # this group is lost; pause it and alert
    FLAG = "flag"  # a per-surface condition for the caller (not followed, sort ignored, ...)
    RESUMED = "resumed"  # the probe was clean; the block is closed
    IGNORED = "ignored"  # a probe result for a block that is no longer the open one


@dataclass(frozen=True, slots=True)
class LadderOutcome:
    outcome: Outcome
    block: Block | None = None
    backoff_until: float | None = None
    reason: str = ""


class ResumeResult(enum.StrEnum):
    ACCEPTED = "accepted"  # a probe is armed
    REPLAYED = "replayed"  # this nonce was used before
    NO_BLOCK = "no_block"
    STALE = "stale"  # the command names a block that is not the open one
    TOO_EARLY = "too_early"
    NEEDS_CONFIRMATION = "needs_confirmation"  # advanced_protection: owner must confirm
    PERMANENT = "permanent"  # the last rung: only a local re-plan lifts it


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    block_id: int
    not_before: float


@dataclass(frozen=True, slots=True)
class LadderState:
    """The ladder's small persisted state (the blocks themselves live in the ``blocks`` table)."""

    unknown_streak: int = 0
    unknown_at: tuple[float, ...] = ()
    group_lost_at: tuple[float, ...] = ()
    backoff_until: float | None = None
    probe: ProbeRequest | None = None
    cautious_until: float | None = None

    def to_json(self) -> str:
        data = asdict(self)
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | None) -> LadderState:
        if not raw:
            return cls()
        data: dict[str, Any] = json.loads(raw)
        probe = data.get("probe")
        return cls(
            unknown_streak=int(data.get("unknown_streak", 0)),
            unknown_at=tuple(float(t) for t in data.get("unknown_at", ())),
            group_lost_at=tuple(float(t) for t in data.get("group_lost_at", ())),
            backoff_until=_opt_float(data.get("backoff_until")),
            probe=(
                ProbeRequest(int(probe["block_id"]), float(probe["not_before"]))
                if isinstance(probe, dict)
                else None
            ),
            cautious_until=_opt_float(data.get("cautious_until")),
        )


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


class Ladder:
    def __init__(
        self,
        store: BlockStore,
        clock: Clock,
        *,
        events: EventSink | None = None,
        cautious_days_after_warning: float = 7,
        cautious_days_after_logout: float = 14,
        strike_decay_days: float = STRIKE_DECAY_DAYS,
    ) -> None:
        self._store = store
        self._clock = clock
        self._events = events
        self._after_warning = cautious_days_after_warning * DAY
        self._after_logout = cautious_days_after_logout * DAY
        self._decay = strike_decay_days * DAY
        self._lock = asyncio.Lock()
        """Held for every read-modify-write of kv ``ladder`` (see the module)."""

    # -- state ---------------------------------------------------------------------------------

    async def state(self) -> LadderState:
        return LadderState.from_json(await self._store.kv_get(KV_LADDER))

    async def _save(self, state: LadderState) -> None:
        # Durable: a power cut must not forget a backoff, an UNKNOWN streak or a lost group,
        # or the next unrecognised page would count as a first one again. The caller holds
        # the lock.
        await self._store.kv_set(KV_LADDER, state.to_json(), durable=True)

    async def _change(self, update: Callable[[LadderState], LadderState]) -> LadderState:
        """Apply ``update`` to the state as it is on disk now, under the lock."""
        async with self._lock:
            current = await self.state()
            changed = update(current)
            if changed != current:
                await self._save(changed)
            return changed

    async def current_block(self) -> Block | None:
        return await self._store.current_block()

    async def cautious_until(self) -> float | None:
        return (await self.state()).cautious_until

    async def probe_request(self) -> ProbeRequest | None:
        """The armed probe, if it belongs to the block that is open right now."""
        block = await self._store.current_block()
        probe = (await self.state()).probe
        if block is None or probe is None or probe.block_id != block.id:
            return None
        return probe

    # -- verdicts ------------------------------------------------------------------------------

    async def on_verdict(
        self,
        verdict: Verdict,
        *,
        probe_block_id: int | None = None,
        screenshot: str | None = None,
    ) -> LadderOutcome:
        """Apply one detector verdict. A probe's verdict must name the block it probed."""
        if probe_block_id is not None:
            return await self._probe_result(probe_block_id, verdict, screenshot)
        state = verdict.state
        if state in {PageState.BLOCKED, PageState.LOGGED_OUT}:
            block = await self.trip(
                state,
                reason=_reason(verdict),
                excerpt=verdict.excerpt,
                screenshot=screenshot,
                advanced_protection=verdict.advanced_protection,
            )
            return LadderOutcome(Outcome.BLOCKED, block=block, reason=block.reason)
        if state is PageState.UNKNOWN:
            return await self._unknown(verdict, screenshot)
        # Any recognised page ends an UNKNOWN streak.
        await self._change(lambda s: replace(s, unknown_streak=0))
        if state is PageState.GROUP_LOST:
            return await self._group_lost(verdict, screenshot)
        if state is PageState.OK:
            return LadderOutcome(Outcome.CONTINUE)
        return LadderOutcome(Outcome.FLAG, reason=_reason(verdict))

    async def _unknown(self, verdict: Verdict, screenshot: str | None) -> LadderOutcome:
        now = self._clock.time()
        why: str | None = None
        until = now
        async with self._lock:
            current = await self.state()
            recent = (*(t for t in current.unknown_at if now - t < UNKNOWN_WINDOW), now)
            if current.unknown_streak >= 1 or len(recent) > len(UNKNOWN_BACKOFF):
                why = "twice in a row" if current.unknown_streak >= 1 else "too often in 24 h"
            else:
                until = now + UNKNOWN_BACKOFF[len(recent) - 1]
                await self._save(
                    replace(current, unknown_streak=1, unknown_at=recent, backoff_until=until)
                )
        if why is not None:
            block = await self.trip(
                PageState.UNKNOWN,
                reason=f"unrecognised page {why}: {_reason(verdict)}",
                excerpt=verdict.excerpt,
                screenshot=screenshot,
            )
            return LadderOutcome(Outcome.BLOCKED, block=block, reason=block.reason)
        log.warning("unrecognised page (%s); backing off until %.0f", _reason(verdict), until)
        return LadderOutcome(Outcome.BACKOFF, backoff_until=until, reason=_reason(verdict))

    async def _group_lost(self, verdict: Verdict, screenshot: str | None) -> LadderOutcome:
        now = self._clock.time()
        async with self._lock:
            current = await self.state()
            recent = (*(t for t in current.group_lost_at if now - t < GROUP_LOST_WINDOW), now)
            warning = len(recent) >= GROUP_LOST_LIMIT
            if not warning:
                await self._save(replace(current, group_lost_at=recent))
        if warning:
            block = await self.trip(
                PageState.BLOCKED,
                reason=f"{len(recent)} groups lost within 24 h: {_reason(verdict)}",
                excerpt=verdict.excerpt,
                screenshot=screenshot,
            )
            return LadderOutcome(Outcome.BLOCKED, block=block, reason=block.reason)
        return LadderOutcome(Outcome.GROUP_PAUSED, reason=_reason(verdict))

    async def trip(
        self,
        state: PageState,
        *,
        reason: str,
        excerpt: str | None = None,
        screenshot: str | None = None,
        advanced_protection: bool = False,
    ) -> Block:
        """Open the next rung. The block is on disk before anything else changes."""
        latest = await self._store.latest_block()
        now = self._clock.time()
        rung = next_rung(latest, now, decay=self._decay)
        if latest is not None and rung == 0:
            log.warning(
                "strikes expired: block %d was lifted %.0f days ago; starting at the first rung",
                latest.id,
                (now - (latest.closed_at or now)) / DAY,
            )
        window = RUNGS[rung]
        block = await self._store.open_block(
            state=state,
            reason=reason[:REASON_MAX],
            until=None if window is None else now + window,
            rung=rung,
            excerpt=excerpt,
            screenshot=screenshot,
            no_probe=advanced_protection,
        )
        await self._change(lambda s: replace(s, unknown_streak=0, backoff_until=None, probe=None))
        log.error("blocked (rung %d, %s): %s", rung, state.value, block.reason)
        await self._emit(EventKind.BLOCK_OPENED, block.id, block.reason)
        if state is PageState.LOGGED_OUT:
            await self._emit(EventKind.LOGGED_OUT, block.id, block.reason)
        return block

    # -- resume and probe ----------------------------------------------------------------------

    async def resume(
        self,
        block_id: int,
        nonce: str,
        *,
        probe_delay: float = 0.0,
        lock_lifted: bool = False,
        replanned: bool = False,
    ) -> ResumeResult:
        """Arm the probe for the open block if, and only if, this command may lift it.

        ``lock_lifted`` is the owner's confirmation for an ``advanced_protection`` block;
        ``replanned`` lifts the last rung and is only ever passed by a local command.
        """
        # First, so a command is used up whatever it finds: an edge, never a standing order.
        if not await self._store.claim_nonce(nonce, kind="resume"):
            return ResumeResult.REPLAYED
        block = await self._store.current_block()
        if block is None:
            return ResumeResult.NO_BLOCK
        if block.id != block_id:
            log.warning("resume for block %d ignored: block %d is open", block_id, block.id)
            return ResumeResult.STALE
        now = self._clock.time()
        if block.until is None and not replanned:
            return ResumeResult.PERMANENT
        if block.until is not None and now < block.until:
            return ResumeResult.TOO_EARLY
        if block.no_probe and not lock_lifted:
            return ResumeResult.NEEDS_CONFIRMATION
        probe = ProbeRequest(block.id, now + probe_delay)
        await self._change(lambda s: replace(s, probe=probe))
        log.info("resume accepted for block %d; probe not before +%.0f s", block.id, probe_delay)
        return ResumeResult.ACCEPTED

    async def _probe_result(
        self, block_id: int, verdict: Verdict, screenshot: str | None
    ) -> LadderOutcome:
        probe = await self.probe_request()
        if probe is None or probe.block_id != block_id:
            return LadderOutcome(Outcome.IGNORED, reason="probe is not armed for the open block")
        block = await self._store.current_block()
        if block is None:  # pragma: no cover - probe_request just saw it open
            return LadderOutcome(Outcome.IGNORED)
        if verdict.state not in PROBE_CLEAN:
            new = await self.trip(
                verdict.state,
                reason=f"probe failed: {_reason(verdict)}",
                excerpt=verdict.excerpt,
                screenshot=screenshot,
                advanced_protection=verdict.advanced_protection,
            )
            return LadderOutcome(Outcome.BLOCKED, block=new, reason=new.reason)
        if not await self._store.close_block(block.id, reason="resumed after a clean probe"):
            return LadderOutcome(Outcome.IGNORED)  # pragma: no cover - racing close
        now = self._clock.time()
        span = self._after_logout if block.state is PageState.LOGGED_OUT else self._after_warning
        state = await self._change(
            lambda s: replace(
                s,
                probe=None,
                unknown_streak=0,
                backoff_until=None,
                cautious_until=max(s.cautious_until or 0.0, now + span),
            )
        )
        cautious = state.cautious_until or now + span
        await self._emit(EventKind.BLOCK_CLOSED, block.id, "resumed after a clean probe")
        log.warning("block %d lifted after a clean probe; cautious until %.0f", block.id, cautious)
        return LadderOutcome(Outcome.RESUMED, block=block)

    # -- gate ----------------------------------------------------------------------------------

    async def gate(self, context: GateContext) -> GateResult:
        now = self._clock.time()
        block = await self._store.current_block()
        if block is not None:
            probe = await self.probe_request()
            if context.probe and probe is not None and now >= probe.not_before:
                return GateResult("block", ok=True, reason=f"probe of block {block.id}")
            state = (
                CollectorState.LOGGED_OUT
                if block.state is PageState.LOGGED_OUT
                else CollectorState.BLOCKED
            )
            retry = probe.not_before if probe is not None else None
            return GateResult(
                "block",
                ok=False,
                reason=f"block {block.id} (rung {block.rung}): {block.reason}",
                state=state,
                retry_at=retry,
            )
        backoff = (await self.state()).backoff_until
        if backoff is not None and now < backoff:
            return GateResult(
                "block",
                ok=False,
                reason="backing off after an unrecognised page",
                state=CollectorState.UNKNOWN,
                retry_at=backoff,
            )
        return GateResult("block", ok=True)

    async def _emit(self, kind: EventKind, block_id: int | None, detail: str | None) -> None:
        if self._events is not None:
            await self._events(kind, block_id=block_id, detail=(detail or "")[:REASON_MAX] or None)


def next_rung(latest: Block | None, now: float, *, decay: float = STRIKE_DECAY_DAYS * DAY) -> int:
    """The rung a block opened ``now`` gets, after ``latest`` (the newest block, open or not).

    It climbs one rung from ``latest`` (the last rung repeats), unless ``latest`` was lifted at
    least ``decay`` seconds ago with no block since — then the strikes have expired and it is
    the first rung again. A block that is still open, or one closed by a newer block
    (superseded), has no quiet time after it; neither has a lift the wall clock puts in the
    future (a clock stepped back never expires strikes early).
    """
    if latest is None:
        return 0
    closed = latest.closed_at
    if closed is not None and now - closed >= decay:
        return 0
    return min(latest.rung + 1, len(RUNGS) - 1)


def _reason(verdict: Verdict) -> str:
    reasons: Sequence[str] = verdict.reasons or (verdict.state.value,)
    return ", ".join(reasons)[:REASON_MAX]
