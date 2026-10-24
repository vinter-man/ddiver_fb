"""What the brain tells the collector: the lease, the monitored set, pacing, and commands.

``GET /v1/control`` is answered from here. Two kinds of content, as the wire defines them:

- **State**, re-sent on every poll: the lease, the monitored groups, the pace cap, the pause.
  A collector that missed a poll or restarted gets the same answer next time.
- **Commands** (``resume``, ``refresh_groups``) are edges. Each carries a fresh nonce and the
  collector consumes a nonce on first sight, so the brain re-sends an open command on every poll
  until it has evidence it landed (the block closed or changed, a snapshot echoed it) or it
  expires. A lost answer therefore costs a minute, never the command.

**The lease** is sticky. The first collector to check in holds it; any other one is told it does
not (``Lease(holder, granted=True)`` names the holder, so the other's ``lease_held_by`` is false
and it stops reading). It never moves by itself — not when the holder goes quiet, not after a
timeout — because two machines alternating on one Facebook account is precisely the device
pattern the design avoids (D4). The wire has no account id yet; with one listener account (v1)
there is one lease.

Only the owner moves it (``/lease <id>``), to a collector that has checked in, and in **two
steps**: the holder is revoked first and nobody is granted; the new collector gets the lease
when the old one has checked in since and been told to stop. The old holder keeps its last
answer until it hears otherwise, so granting at once would let a holder that cannot reach the
brain (a dead tunnel, a forgotten laptop) read alongside the new one indefinitely.
``/lease <id> now`` skips the wait, for a holder the owner has switched off by hand.

**A collector is its id and its state database.** The lease is granted to a ``collector_id``
*and* the incarnation its state database was created with, which every poll carries. The id
alone was a name any installation could share (all of them defaulted to ``home-1``): a forgotten
Mac and the laptop that replaced it would both have held the one lease, and ``/lease home-1``
could not tell them apart. A second database under the holder's id is now a contender like any
other — told to stop (``granted=False``, since its id is the holder's), the owner told — and
``/lease <that id>`` moves the lease to it: a machine replaced under the same name, or a state
database recreated. A lease recorded without an incarnation (an older brain, or a collector
that sends none) is bound to the first one its holder presents.

Every move bumps the lease's **generation**, and every command is issued to one collector under
one generation and only ever sent to it: a resume meant for the old holder's block 17 can never
reach the new holder, whose own block 17 is a different block. Moves, grants and the control
answer are each one transaction, and :attr:`ControlDesk.lease_lock` serialises them with the
monitor's lease-checked writes, so nothing is applied for a holder that has just been replaced.

**Nothing here waits for Telegram.** What the owner should hear about a poll — a completed move,
a contender — is written down in the answer's own transaction (kv ``lease_moved``, the
contender's ``refused_by``) and sent by the monitor's own task. The first version sent it before
answering: while Telegram stalled, the revoked holder's poll timed out, it kept its last grant,
and the new holder read beside it — the two-machine pattern the two-step move exists to prevent.

**The monitored set** is every readable group somebody is subscribed to, capped at 25 (D17):
over the cap, the groups with the most subscribers win and the rest are reported to the owner,
never silently dropped — ``Control`` itself refuses more than 25.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Final

import aiosqlite

from ddiver_core.storage import Storage
from ddiver_core.timeutil import Clock, utcnow
from ddiver_fb.store import CollectorRow, CommandRow, EventKey, FbStore
from fbwire import (
    MAX_MONITORED,
    BlockInfo,
    Command,
    Control,
    GroupsSnapshot,
    Lease,
    MonitoredGroup,
    Profile,
    RefreshGroupsCommand,
    ResumeCommand,
    Status,
)

log = logging.getLogger(__name__)

KV_LEASE: Final = "lease"
KV_GENERATION: Final = "lease_generation"
KV_MOVING_TO: Final = "lease_moving_to"
KV_INCARNATION: Final = "lease_incarnation"
KV_MOVING_INCARNATION: Final = "lease_moving_incarnation"
KV_REVOKED_AT: Final = "lease_revoked_at"
KV_MOVED: Final = "lease_moved"
"""The last completed two-step move, for the monitor to tell the owner about."""
KV_PAUSED: Final = "paused"
KV_PACE: Final = "pace_cap"

KIND_RESUME: Final = "resume"
KIND_REFRESH: Final = "refresh_groups"

RESUME_TTL: Final = 2 * 3600
"""Long enough for a collector that is between polls, asleep or restarting; a resume older than
this is no longer the owner's decision about the block in front of them."""
REFRESH_TTL: Final = 12 * 3600
"""A refresh asked for at night waits for the morning's open gates."""
REFRESH_PER_DAY: Final = 3
REFRESH_SKEW: Final = 30
"""A snapshot that does not echo a refresh's nonce settles it only if taken at least this long
after the command was created. The collector starts a commanded sync no sooner than 30 s after
it received the command (``COMMAND_DELAY``), so a sync still in flight from before the command
cannot pass unless the collector's clock runs 30 s ahead of the brain's."""
LEASE_VALID_FOR: Final = 6 * 3600
"""How long a grant stays good without a newer answer (``Lease.valid_for``): long enough to
read through a brain restart or a tunnel outage, short enough that a holder cut off from the
brain does not read on for days after the lease has moved."""
DAY: Final = 86400

NonceFactory = Callable[[], str]


def new_nonce() -> str:
    return secrets.token_urlsafe(16)  # 22 characters of [A-Za-z0-9_-], what the wire accepts


class LeaseOutcome(enum.StrEnum):
    MOVED = "moved"  # granted at once: nobody held it, or the owner said "now"
    REVOKING = "revoking"  # the holder is told to stop; the new one gets it after that
    REPLACED = "replaced"  # MOVED, to another state database under the holder's own id
    REPLACING = "replacing"  # REVOKING, to another state database under the holder's own id
    CANCELLED = "cancelled"  # a pending move back to the holder: nothing moves
    SAME = "same"
    UNKNOWN = "unknown"


class RefreshOutcome(enum.StrEnum):
    SENT = "sent"
    PENDING = "pending"
    LIMIT = "limit"
    NO_HOLDER = "no_holder"


class ResumeVerdict(enum.StrEnum):
    """Whether a remote resume may be sent for a block (mirrors the collector's ladder)."""

    OK = "ok"
    NO_BLOCK = "no_block"
    TOO_EARLY = "too_early"
    FINAL = "final"  # the last rung: only a local re-plan lifts it
    NO_PROBE = "no_probe"  # advanced protection: only with the owner's "the lock is lifted"


class ResumeOutcome(enum.StrEnum):
    SENT = "sent"
    PENDING = "pending"
    STALE = "stale"
    REFUSED = "refused"
    NO_HOLDER = "no_holder"


def identity(collector_id: str, incarnation: str | None) -> str:
    """One collector as the lease sees it: its id and its state database."""
    return f"{collector_id}@{incarnation}" if incarnation else collector_id


def short_incarnation(incarnation: str | None) -> str:
    """Enough of an incarnation for the owner to tell two databases apart."""
    return incarnation[:8] if incarnation else "?"


@dataclass(frozen=True, slots=True)
class LeaseState:
    holder: str | None
    """Who holds it; during a move still the old holder, until it has heard."""
    generation: int = 0
    moving_to: str | None = None
    revoked_at: int | None = None
    incarnation: str | None = None
    """The holder's state database; ``None``: not known yet (bound at its next check-in)."""
    moving_incarnation: str | None = None

    @property
    def settled(self) -> bool:
        """Somebody holds it and it is not moving: commands can be issued."""
        return self.holder is not None and self.moving_to is None

    @property
    def label(self) -> str | None:
        return None if self.holder is None else identity(self.holder, self.incarnation)

    def holds(self, collector_id: str, incarnation: str | None) -> bool:
        """Whether this collector, from this state database, is the holder."""
        return self.holder == collector_id and self.incarnation in (None, incarnation)

    def moving_here(self, collector_id: str, incarnation: str | None) -> bool:
        """Whether a move under way is to this collector and database."""
        return self.moving_to == collector_id and self.moving_incarnation in (None, incarnation)


@dataclass(frozen=True, slots=True)
class LeaseMove:
    """A two-step move completed at the old holder's poll: it was told to stop."""

    old: str
    new: str
    generation: int


@dataclass(frozen=True, slots=True)
class MonitoredSet:
    sent: tuple[MonitoredGroup, ...]
    over_cap: tuple[int, ...]


def _twin(row: CollectorRow | None, state: LeaseState) -> str | None:
    """The other state database that last asked for the lease under the holder's own id."""
    if row is None or row.refused_by is None or row.refused_by != state.label:
        return None
    if row.refused_incarnation in (None, state.incarnation):
        return None
    return row.refused_incarnation


def resume_verdict(
    block: BlockInfo | None, *, now: int, lock_lifted: bool = False
) -> ResumeVerdict:
    """The same rules, in the same order, as the collector's ladder, so no nonce is wasted.

    The collector consumes a resume's nonce whatever it concludes, so a command it would refuse
    (too early, last rung, advanced protection without the owner's confirmation) is not merely
    useless: the owner would think they had resumed.
    """
    if block is None:
        return ResumeVerdict.NO_BLOCK
    if block.until is None:
        return ResumeVerdict.FINAL
    if now < block.until:
        return ResumeVerdict.TOO_EARLY
    if block.no_probe and not lock_lifted:
        return ResumeVerdict.NO_PROBE
    return ResumeVerdict.OK


class ControlDesk:
    def __init__(
        self,
        storage: Storage,
        fb: FbStore,
        *,
        clock: Clock = utcnow,
        nonce: NonceFactory = new_nonce,
    ) -> None:
        self._storage = storage
        self._fb = fb
        self._clock = clock
        self._nonce = nonce
        self.lease_lock = asyncio.Lock()
        """Held by whatever reads the lease and acts on it: a move, a control answer, and the
        monitor applying a collector's status, events or snapshot."""

    def _now(self) -> int:
        return int(self._clock().timestamp())

    # -- lease -----------------------------------------------------------------------------

    async def lease(self, *, db: aiosqlite.Connection | None = None) -> LeaseState:
        fb = self._fb
        holder = await fb.kv_get(KV_LEASE, db=db)
        stored = await fb.kv_get(KV_GENERATION, db=db)
        generation = int(stored) if stored else 0
        if holder is not None:
            # The first brain recorded a holder without a generation, and the wire's
            # generations start at 1: a 0 here failed every control answer with a 500.
            generation = max(1, generation)
        revoked_at = await fb.kv_get(KV_REVOKED_AT, db=db)
        return LeaseState(
            holder=holder,
            generation=generation,
            moving_to=await fb.kv_get(KV_MOVING_TO, db=db),
            revoked_at=int(revoked_at) if revoked_at else None,
            incarnation=await fb.kv_get(KV_INCARNATION, db=db),
            moving_incarnation=await fb.kv_get(KV_MOVING_INCARNATION, db=db),
        )

    async def holder(self) -> str | None:
        return (await self.lease()).holder

    async def _grant(
        self,
        db: aiosqlite.Connection,
        state: LeaseState,
        collector_id: str,
        incarnation: str | None,
        *,
        now: int,
    ) -> LeaseState:
        """Give the lease to ``collector_id`` (and its state database) under a new generation;
        close every command."""
        generation = state.generation + 1
        fb = self._fb
        await fb.kv_set(KV_LEASE, collector_id, db=db)
        await fb.kv_set(KV_GENERATION, str(generation), db=db)
        await self._set_or_delete(KV_INCARNATION, incarnation, db=db)
        for key in (KV_MOVING_TO, KV_MOVING_INCARNATION, KV_REVOKED_AT):
            await fb.kv_delete(key, db=db)
        # Commands were meant for the old holder's view of the account (its block, its sync).
        opened = await fb.open_commands(now=now, db=db)
        await fb.finish_commands((c.id for c in opened), "lease moved", now=now, db=db)
        log.info(
            "lease granted to %s (generation %d)", identity(collector_id, incarnation), generation
        )
        return LeaseState(collector_id, generation, incarnation=incarnation)

    async def _bind(
        self, db: aiosqlite.Connection, state: LeaseState, incarnation: str
    ) -> LeaseState:
        """A lease recorded without an incarnation is bound to the first its holder presents."""
        await self._fb.kv_set(KV_INCARNATION, incarnation, db=db)
        log.info("lease of %s bound to its state database", state.holder)
        return replace(state, incarnation=incarnation)

    async def _set_or_delete(
        self, key: str, value: str | None, *, db: aiosqlite.Connection
    ) -> None:
        if value is None:
            await self._fb.kv_delete(key, db=db)
        else:
            await self._fb.kv_set(key, value, db=db)

    async def _settle_claim(
        self,
        db: aiosqlite.Connection,
        state: LeaseState,
        collector_id: str,
        incarnation: str | None,
    ) -> LeaseState:
        """Grant a free lease to the caller, or bind an unbound one to the holder's database."""
        if state.holder is None:
            return await self._grant(db, state, collector_id, incarnation, now=self._now())
        if state.holds(collector_id, incarnation) and state.incarnation is None and incarnation:
            return await self._bind(db, state, incarnation)
        return state

    async def claim(self, collector_id: str, incarnation: str | None = None) -> bool:
        """Whether this collector and state database hold the lease, granting it when nobody
        does (the first collector to check in). The caller holds :attr:`lease_lock`. A revoked
        holder still counts until it has heard: what it reports was read under its lease."""
        state = await self.lease()
        unbound = state.incarnation is None and incarnation is not None
        if state.holder is None or (unbound and state.holds(collector_id, incarnation)):
            async with self._fb.transaction() as tx:
                state = await self._settle_claim(
                    tx, await self.lease(db=tx), collector_id, incarnation
                )
        return state.holds(collector_id, incarnation)

    async def ensure_lease(self, collector_id: str, incarnation: str | None = None) -> str:
        """The lease holder, granting the lease to ``collector_id`` when nobody holds it."""
        async with self.lease_lock:
            await self.claim(collector_id, incarnation)
            holder = await self.holder()
        return holder if holder is not None else collector_id

    async def transfer_lease(
        self, collector_id: str, *, now: bool = False
    ) -> tuple[LeaseOutcome, str | None]:
        """Move the lease (owner only); returns what happened and who held it before.

        With a holder, the move is two-step (see the module docstring) unless ``now``. To the
        holder's own id it moves to the other state database that asked for it under that id,
        if any; otherwise it cancels a pending move, or there is nothing to do.
        """
        row = await self._fb.collector(collector_id)
        async with self.lease_lock, self._fb.transaction() as tx:
            state = await self.lease(db=tx)
            old = state.holder
            same_id = old == collector_id
            if same_id:
                pending_twin = state.moving_to == collector_id
                if state.moving_to is not None and not (pending_twin and now):
                    for key in (KV_MOVING_TO, KV_MOVING_INCARNATION, KV_REVOKED_AT):
                        await self._fb.kv_delete(key, db=tx)
                    return LeaseOutcome.CANCELLED, old
                target = state.moving_incarnation if pending_twin else _twin(row, state)
                if target is None:
                    return LeaseOutcome.SAME, old
            elif row is None:
                return LeaseOutcome.UNKNOWN, old
            else:
                target = row.incarnation  # the database it last checked in from
            stamp = self._now()
            if old is None or now:
                await self._grant(tx, state, collector_id, target, now=stamp)
                return (LeaseOutcome.REPLACED if same_id else LeaseOutcome.MOVED), old
            await self._fb.kv_set(KV_MOVING_TO, collector_id, db=tx)
            await self._set_or_delete(KV_MOVING_INCARNATION, target, db=tx)
            if state.revoked_at is None:
                await self._fb.kv_set(KV_REVOKED_AT, str(stamp), db=tx)
            opened = await self._fb.open_commands(now=stamp, db=tx)
            await self._fb.finish_commands((c.id for c in opened), "lease moving", now=stamp, db=tx)
            return (LeaseOutcome.REPLACING if same_id else LeaseOutcome.REVOKING), old

    # -- pause and pace --------------------------------------------------------------------

    async def paused(self) -> bool:
        return await self._fb.kv_get(KV_PAUSED) is not None

    async def pause(self) -> bool:
        """``True`` when this call paused (``False``: it already was)."""
        if await self.paused():
            return False
        await self._fb.kv_set(KV_PAUSED, str(self._now()))
        return True

    async def unpause(self) -> bool:
        if not await self.paused():
            return False
        await self._fb.kv_delete(KV_PAUSED)
        return True

    async def pace_cap(self) -> Profile | None:
        raw = await self._fb.kv_get(KV_PACE)
        try:
            return Profile(raw) if raw else None
        except ValueError:
            return None

    async def set_pace_cap(self, profile: Profile | None) -> None:
        if profile is None:
            await self._fb.kv_delete(KV_PACE)
        else:
            await self._fb.kv_set(KV_PACE, profile.value)

    # -- monitored set ---------------------------------------------------------------------

    async def monitored_ids(self) -> list[int]:
        """The ids :meth:`monitored` sends, in its order."""
        return [int(m.group_id) for m in (await self.monitored()).sent]

    async def monitored(self) -> MonitoredSet:
        """Readable groups with an active, unpaused subscriber; the 25 most wanted are sent."""
        wanted = await self._storage.monitored_chat_ids()
        groups = {g.chat_id: g for g in await self._fb.groups() if g.chat_id in wanted}
        demand = {gid: len(await self._storage.subscribers_of(gid)) for gid in groups}
        ranked = sorted(groups, key=lambda gid: (-demand[gid], gid))
        sent = tuple(
            MonitoredGroup(group_id=str(gid), tier=groups[gid].tier, listings=groups[gid].listings)
            for gid in ranked[:MAX_MONITORED]
        )
        return MonitoredSet(sent=sent, over_cap=tuple(ranked[MAX_MONITORED:]))

    # -- the control answer ----------------------------------------------------------------

    async def control_for(self, collector_id: str, incarnation: str | None = None) -> Control:
        """The answer to one poll, and nothing that waits for Telegram (see the module)."""
        now = self._now()
        async with self.lease_lock:
            async with self._fb.transaction() as tx:
                await self._fb.touch_collector(
                    collector_id, now=now, control=True, incarnation=incarnation, db=tx
                )
                state = await self.lease(db=tx)
                moved = False
                if state.moving_to is not None and state.holds(collector_id, incarnation):
                    # The revoked holder is hearing it now: this answer tells it to stop, and
                    # the new collector is granted at its own next poll.
                    old = state
                    state = await self._grant(
                        tx, state, state.moving_to, state.moving_incarnation, now=now
                    )
                    await self._record_move(tx, old, state)
                    moved = True
                else:
                    state = await self._settle_claim(tx, state, collector_id, incarnation)
                contender = (
                    not moved
                    and state.holder is not None
                    and not state.holds(collector_id, incarnation)
                    and not state.moving_here(collector_id, incarnation)
                )
                await self._fb.set_refused(
                    collector_id, incarnation, state.label if contender else None, db=tx
                )
                commands = (
                    await self._fb.open_commands(
                        now=now, collector_id=collector_id, lease_generation=state.generation, db=tx
                    )
                    if state.settled and state.holds(collector_id, incarnation)
                    else []
                )
            return await self._answer(collector_id, incarnation, state, commands, now=now)

    async def _record_move(
        self, db: aiosqlite.Connection, old: LeaseState, new: LeaseState
    ) -> None:
        """Written with the grant, for the monitor to tell the owner once (by generation)."""
        before, after = old.holder or "?", new.holder or "?"
        if before == after:  # the same name, another state database: say which is which
            before = f"{before} ({short_incarnation(old.incarnation)})"
            after = f"{after} ({short_incarnation(new.incarnation)})"
        move = {"old": before, "new": after, "generation": new.generation}
        await self._fb.kv_set(KV_MOVED, json.dumps(move), db=db)

    async def last_move(self) -> LeaseMove | None:
        """The last completed two-step move (the monitor tells the owner once per generation)."""
        raw = await self._fb.kv_get(KV_MOVED)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return LeaseMove(str(data["old"]), str(data["new"]), int(data["generation"]))
        except (ValueError, KeyError, TypeError):
            log.warning("the recorded lease move is unreadable; ignored")
            return None

    async def _answer(
        self,
        collector_id: str,
        incarnation: str | None,
        state: LeaseState,
        rows: list[CommandRow],
        *,
        now: int,
    ) -> Control:
        cap = await self.pace_cap()
        paused = await self.paused()
        holder = state.holder or collector_id
        if not state.settled:
            # A move is under way: nobody reads until the old holder has heard.
            lease = Lease(collector_id=holder, granted=False, generation=state.generation)
            return Control(issued_at=now, lease=lease, pace_cap=cap, paused=paused)
        if not state.holds(collector_id, incarnation):
            # Nothing to read and nothing to do: only the lease, which tells it to stop. It
            # names the holder; to a database under the holder's own id it must also say "not
            # granted", or that collector would take the grant for its own.
            granted = holder != collector_id
            lease = Lease(collector_id=holder, granted=granted, generation=state.generation)
            return Control(issued_at=now, lease=lease, pace_cap=cap, paused=paused)
        lease = Lease(
            collector_id=holder,
            granted=True,
            generation=state.generation,
            valid_for=LEASE_VALID_FOR,
        )
        commands: list[Command] = []
        generation = state.generation
        for row in rows:
            if row.kind == KIND_RESUME and row.block_id is not None:
                commands.append(
                    ResumeCommand(
                        block_id=row.block_id,
                        nonce=row.nonce,
                        lock_lifted=row.lock_lifted,
                        collector_id=collector_id,
                        lease_generation=generation,
                    )
                )
            elif row.kind == KIND_REFRESH:
                commands.append(
                    RefreshGroupsCommand(
                        nonce=row.nonce, collector_id=collector_id, lease_generation=generation
                    )
                )
        monitored = await self.monitored()
        return Control(
            issued_at=now,
            monitored=monitored.sent,
            lease=lease,
            commands=tuple(commands),
            pace_cap=cap,
            paused=paused,
        )

    # -- commands --------------------------------------------------------------------------

    async def _open(self, kind: str, collector_id: str | None = None) -> list[CommandRow]:
        """Open commands of ``kind`` issued to ``collector_id`` (the holder by default) under
        the current generation."""
        state = await self.lease()
        target = collector_id or state.holder
        if target is None:
            return []
        rows = await self._fb.open_commands(
            now=self._now(), collector_id=target, lease_generation=state.generation
        )
        return [c for c in rows if c.kind == kind]

    async def request_refresh(self) -> RefreshOutcome:
        async with self.lease_lock:
            now = self._now()
            state = await self.lease()
            if not state.settled or state.holder is None:
                return RefreshOutcome.NO_HOLDER
            if await self._open(KIND_REFRESH):
                return RefreshOutcome.PENDING
            if await self._fb.count_commands(KIND_REFRESH, since=now - DAY) >= REFRESH_PER_DAY:
                # The collector enforces the same limit and would drop a fourth; saying so here
                # is the difference between an honest "not today" and a button that does nothing.
                return RefreshOutcome.LIMIT
            await self._fb.add_command(
                KIND_REFRESH,
                self._nonce(),
                now=now,
                ttl=REFRESH_TTL,
                collector_id=state.holder,
                lease_generation=state.generation,
            )
            return RefreshOutcome.SENT

    async def request_resume(
        self, block_id: int, current: BlockInfo | None, *, lock_lifted: bool = False
    ) -> ResumeOutcome:
        """Queue a resume for ``block_id`` if it is still the open block and may be resumed.

        ``lock_lifted`` is the owner's confirmation that an advanced-protection lock is gone;
        it is what lets a resume through for a ``no_probe`` block, and nothing else.
        """
        async with self.lease_lock:
            now = self._now()
            if current is None or current.id != block_id:
                return ResumeOutcome.STALE
            if resume_verdict(current, now=now, lock_lifted=lock_lifted) is not ResumeVerdict.OK:
                return ResumeOutcome.REFUSED
            state = await self.lease()
            if not state.settled or state.holder is None:
                return ResumeOutcome.NO_HOLDER
            if any(c.block_id == block_id for c in await self._open(KIND_RESUME)):
                return ResumeOutcome.PENDING
            await self._fb.add_command(
                KIND_RESUME,
                self._nonce(),
                now=now,
                ttl=RESUME_TTL,
                collector_id=state.holder,
                lease_generation=state.generation,
                block_id=block_id,
                lock_lifted=lock_lifted and current.no_probe,
            )
            return ResumeOutcome.SENT

    async def settle(self, status: Status) -> None:
        """Close the resume commands a collector's status shows as moot (its block is gone or
        new). Only its own commands: another collector's block ids are another numbering."""
        block_id = status.block.id if status.block is not None else None
        opened = await self._open(KIND_RESUME, status.collector_id)
        moot = [c.id for c in opened if c.block_id != block_id]
        await self._fb.finish_commands(moot, "block changed", now=self._now())

    async def block_closed(
        self, collector_id: str, block_id: int, *, event: EventKey | None = None
    ) -> None:
        """Close the resume commands for a block the collector closed; with ``event``, mark that
        event applied in the same transaction."""
        done = [c.id for c in await self._open(KIND_RESUME, collector_id) if c.block_id == block_id]
        now = self._now()
        async with self._fb.transaction() as tx:
            await self._fb.finish_commands(done, "block closed", now=now, db=tx)
            if event is not None:
                await self._fb.mark_event_applied(event, now=now, db=tx)

    async def snapshot_arrived(self, snapshot: GroupsSnapshot) -> None:
        """Close the refresh this snapshot answers: the one whose nonce it echoes, or, from a
        collector that does not echo, any taken clearly after the command was created."""
        opened = await self._open(KIND_REFRESH, snapshot.collector_id)
        if snapshot.nonce is not None:
            done = [c.id for c in opened if c.nonce == snapshot.nonce]
        else:
            done = [c.id for c in opened if snapshot.taken_at >= c.created_at + REFRESH_SKEW]
        await self._fb.finish_commands(done, "snapshot received", now=self._now())
