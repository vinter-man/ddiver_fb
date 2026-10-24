"""Control: what the owner and the brain may tell the collector, and what that can change.

Two sources, one rulebook:

- **Local** (phase 0, a TTY): ``pause``, ``unpause``, ``resume <block_id>`` and a group refresh.
  A local resume gets a fresh nonce here, so it takes exactly the same path as a remote one.
- **Remote** (``GET /v1/control``): the lease, the pace cap, the pause flag and the monitored set
  are *state*, re-sent on every poll and persisted as last known; ``commands`` are *edges*, each
  applied at most once — its nonce is consumed on first sight, whatever it finds. Only the
  commands addressed to this collector under the lease it holds now are looked at
  (``Control.commands_for``): one issued to another collector, or under an earlier lease
  generation, is left alone with its nonce unused, so a lease move can never hand this
  collector a resume meant for another machine's view of the account.

The brain can only slow the collector down: the effective profile is
``min(local, remote)`` in caution order, so a remote ``normal`` never lifts a local ``cautious``.
Once an uplink is configured, no lease means no reads; a brain that is merely unreachable keeps
its last word, so collecting goes on through an outage — for as long as the grant says it is
good (``Lease.valid_for``, 6 h from the brain). A holder cut off from the brain for longer stops
reading: the owner may have moved the lease meanwhile, and two machines reading one account is
the device pattern D4 exists to avoid. The grant's age is the larger of the wall-clock and the
monotonic time since the answer arrived, so neither a clock step back nor a restart extends it.

A Facebook action caused by a command (the resume probe, a refresh sync) starts U(30, 300) s
after the command arrives, so reads never line up with the 60 s control poll. The delay is
stored with the request, so a restart does not skip it. The nonce of a remote refresh travels
with the sync it caused into the groups snapshot, so the brain closes exactly that command.

Every change is a read-modify-write of kv ``control`` in one write transaction of the store
(``Store.kv_update``), under one lock in this process: the uplink applies answers while the read
loop grants syncs, and a CLI ``pause`` from a TTY writes the same row from another process; a
change made from a stale copy would undo the other's.

**The terms reading runs under** — either side's pause, the lease (granted, holder, its
generation) and the pace cap — carry a ``generation`` in that row, moved on by every change of
them and by nothing else (not a poll that repeats the last answer). Changes take effect before
the next browser action, not the next scheduler turn:

- every control gate check reloads the durable row, so the owner's ``pause`` in another process
  closes the gate at the read's next scroll permit or final look;
- ``pinned`` holds the row still in this process while the scheduler checks it and records a
  step's budget event, so no answer from the brain lands between the check and the step: the
  scroll permits use the current pace cap, not the one the read was granted under;
- a grant remembers the generation it was made under and is discarded when it changed before its
  navigation (``Scheduler.still_granted``);
- ``wait_stopped`` wakes the read loop the moment this process learns that reading must stop (an
  answer of the brain, or a reload that saw the owner's pause), and the read is aborted with
  Chrome closed at once, as for a sleep.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import enum
import json
import logging
import random
import secrets
import tomllib
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Final, Protocol

from fbcollector.clock import Clock
from fbcollector.gates import GateContext, GateResult
from fbcollector.ladder import Ladder, ResumeResult
from fbcollector.model import CollectorState
from fbwire.models import (
    MAX_MONITORED,
    Control,
    GroupState,
    MonitoredGroup,
    Profile,
    RefreshGroupsCommand,
    ResumeCommand,
)

log = logging.getLogger(__name__)

KV_CONTROL: Final = "control"
COMMAND_DELAY: Final = (30.0, 300.0)
REFRESH_PER_DAY: Final = 3
DAY: Final = 86400.0


class ControlStore(Protocol):
    async def kv_get(self, key: str) -> str | None: ...

    async def kv_update(
        self, key: str, change: Callable[[str | None], str | None], *, durable: bool = False
    ) -> str | None: ...

    async def claim_nonce(self, nonce: str, *, kind: str) -> bool: ...

    async def set_monitored(self, entries: Sequence[MonitoredGroup]) -> list[str]: ...


@dataclass(frozen=True, slots=True)
class ControlState:
    """Last known control state, persisted so a restart keeps the brain's last word."""

    lease_granted: bool = False
    lease_holder: str | None = None
    lease_generation: int | None = None
    """The generation of the grant this collector holds (``Lease.generation``)."""
    lease_valid_for: int | None = None
    """Seconds the grant stays good without a newer answer; ``None``: until told otherwise."""
    lease_at: float | None = None
    """When the last answer that granted the lease arrived (wall clock)."""
    pace_cap: Profile | None = None
    remote_paused: bool = False
    local_paused: bool = False
    refresh_at: float | None = None
    refresh_nonce: str | None = None
    """The brain's nonce of the refresh waiting for its sync."""
    sync_nonce: str | None = None
    """The refresh a granted sync answers, until a groups snapshot carries it to the brain."""
    refreshes: tuple[float, ...] = ()
    """When accepted refreshes were requested (at most 3 in any 24 h)."""
    polled_at: float | None = None
    generation: int = 0
    """How often ``terms`` changed: a grant made under one generation is not used under
    another (see the module)."""

    @property
    def terms(self) -> tuple[object, ...]:
        """What reading runs under: the pauses, the lease and the pace cap."""
        return (
            self.local_paused,
            self.remote_paused,
            self.lease_granted,
            self.lease_holder,
            self.lease_generation,
            self.pace_cap,
        )

    def to_json(self) -> str:
        data = asdict(self)
        data["pace_cap"] = self.pace_cap.value if self.pace_cap else None
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | None) -> ControlState:
        if not raw:
            return cls()
        data: dict[str, Any] = json.loads(raw)
        cap = data.get("pace_cap")
        return cls(
            lease_granted=bool(data.get("lease_granted", False)),
            lease_holder=data.get("lease_holder"),
            lease_generation=_opt_int(data.get("lease_generation")),
            lease_valid_for=_opt_int(data.get("lease_valid_for")),
            lease_at=_opt_float(data.get("lease_at")),
            pace_cap=Profile(cap) if cap else None,
            remote_paused=bool(data.get("remote_paused", False)),
            local_paused=bool(data.get("local_paused", False)),
            refresh_at=_opt_float(data.get("refresh_at")),
            refresh_nonce=data.get("refresh_nonce"),
            sync_nonce=data.get("sync_nonce"),
            refreshes=tuple(float(t) for t in data.get("refreshes", ())),
            polled_at=_opt_float(data.get("polled_at")),
            generation=int(data.get("generation", 0)),
        )


def _moved_on(before: ControlState, after: ControlState) -> ControlState:
    """``after`` with the generation it gets: one more than ``before``'s when the terms changed."""
    if after.terms == before.terms:
        return replace(after, generation=before.generation)
    return replace(after, generation=before.generation + 1)


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


class RefreshResult(enum.StrEnum):
    SCHEDULED = "scheduled"
    ALREADY_PENDING = "already_pending"
    LIMIT = "limit"  # three refreshes in the last 24 h already
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    kind: str
    nonce: str
    result: str


@dataclass(frozen=True, slots=True)
class Applied:
    """What one ``Control`` changed, for the log and the tests."""

    lease: bool
    profile_cap: Profile | None
    paused: bool
    commands: tuple[CommandOutcome, ...] = ()
    unknown_groups: tuple[str, ...] = ()
    skipped: int = 0
    """Commands not meant for this collector and its current lease (left unused)."""


def effective_profile(local: Profile, remote: Profile | None) -> Profile:
    """``min(local, remote)``: the brain can make pacing more cautious, never less."""
    return Profile.most_cautious(local, remote)


class ControlCenter:
    def __init__(
        self,
        store: ControlStore,
        ladder: Ladder,
        clock: Clock,
        *,
        collector_id: str,
        local_mode: bool,
        rng: random.Random | None = None,
    ) -> None:
        self._store = store
        self._ladder = ladder
        self._clock = clock
        self._collector_id = collector_id
        self._local_mode = local_mode
        self._rng = rng or random.Random()
        self._state = ControlState()
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()
        """Set (and replaced) whenever the generation this process knows moves on."""
        self._lease_mono: float | None = None
        """Monotonic time the granting answer arrived, in this process (``None``: unknown)."""

    @property
    def state(self) -> ControlState:
        return self._state

    @property
    def generation(self) -> int:
        return self._state.generation

    @property
    def changed(self) -> asyncio.Event:
        """Set as soon as the generation this process knows moves on (then replaced): the read
        loop's wait between reads ends on it, so a pause or a lease taken away closes a parked
        Chrome at once rather than when the wait was due to end."""
        return self._changed

    async def load(self) -> ControlState:
        """The row as it is on disk now (another process may have written it)."""
        async with self._lock:
            return self._observe(ControlState.from_json(await self._store.kv_get(KV_CONTROL)))

    @contextlib.asynccontextmanager
    async def pinned(self) -> AsyncIterator[ControlState]:
        """The row as it is on disk now, held still in this process until the block ends.

        No answer of the brain and no local command lands meanwhile: the scheduler checks the
        terms and records a step's budget event inside, so a pace cap or a pause cannot change
        between the two. (A write from another process can land; the next check sees it.)
        """
        async with self._lock:
            yield self._observe(ControlState.from_json(await self._store.kv_get(KV_CONTROL)))

    def _observe(self, state: ControlState) -> ControlState:
        """Take ``state`` as the current one; wake ``wait_stopped`` when its generation moved."""
        moved = state.generation != self._state.generation
        self._state = state
        if moved:
            self._changed.set()
            self._changed = asyncio.Event()
        return state

    async def _change(self, change: Callable[[ControlState], ControlState | None]) -> ControlState:
        """Read-modify-write of the row in one store transaction (``change`` returns ``None``
        to leave it as it is); the generation moves on when the terms changed.

        Durable: this row is what keeps the control gate shut (a lease revoked or lapsed, the
        brain's pause, the owner's own). A power cut must not bring back "granted and unpaused"
        for the read loop to act on before the uplink has asked again.
        """

        def step(raw: str | None) -> str | None:
            before = ControlState.from_json(raw)
            after = change(before)
            return None if after is None else _moved_on(before, after).to_json()

        async with self._lock:
            raw = await self._store.kv_update(KV_CONTROL, step, durable=True)
            return self._observe(ControlState.from_json(raw))

    def _delay(self) -> float:
        return self._rng.uniform(*COMMAND_DELAY)

    # -- pacing and gate -----------------------------------------------------------------------

    def effective_profile(self, local: Profile, state: ControlState | None = None) -> Profile:
        """The local profile under ``state``'s pace cap (the current state by default)."""
        remote = None if self._local_mode else (state or self._state).pace_cap
        return effective_profile(local, remote)

    @property
    def paused(self) -> bool:
        return self.is_paused()

    def is_paused(self, state: ControlState | None = None) -> bool:
        """The owner's pause, or the brain's with an uplink (the current state by default)."""
        current = state or self._state
        return current.local_paused or (not self._local_mode and current.remote_paused)

    def lease_age(self, state: ControlState | None = None) -> float | None:
        """Seconds since the answer that granted the lease arrived; ``None``: not granted.

        The larger of the wall-clock and the monotonic measure: a clock stepped back cannot make
        a grant younger, and after a restart (no monotonic mark) the wall clock still counts.
        """
        current = state or self._state
        at = current.lease_at
        if not current.lease_granted or at is None:
            return None
        age = self._clock.time() - at
        if self._lease_mono is not None:
            age = max(age, self._clock.monotonic() - self._lease_mono)
        return age

    def lease_lapsed(self, state: ControlState | None = None) -> bool:
        current = state or self._state
        valid_for = current.lease_valid_for
        age = self.lease_age(current)
        return valid_for is not None and age is not None and age > valid_for

    def verdict(self, state: ControlState | None = None) -> GateResult:
        """The control gate over ``state`` (the current state by default), without a reload."""
        current = state or self._state
        if current.local_paused:
            return GateResult("control", False, "paused by the owner", CollectorState.PAUSED)
        if self._local_mode:
            return GateResult("control", ok=True)
        if current.remote_paused:
            return GateResult("control", False, "paused by the brain", CollectorState.PAUSED)
        if not (current.lease_granted and current.lease_holder == self._collector_id):
            holder = current.lease_holder or "nobody yet"
            return GateResult(
                "control", False, f"no lease (held by {holder})", CollectorState.PAUSED
            )
        if self.lease_lapsed(current):
            hours = (self.lease_age(current) or 0.0) / 3600
            return GateResult(
                "control",
                False,
                f"the lease lapsed: no answer from the brain for {hours:.1f} h",
                CollectorState.PAUSED,
            )
        return GateResult("control", ok=True)

    async def gate(self, context: GateContext) -> GateResult:  # noqa: ARG002 - one rule for all
        """The control gate over the durable row: a pause written by another process counts."""
        return self.verdict(await self.load())

    async def wait_stopped(self) -> str:
        """Return, with why, as soon as the control state this process knows shuts reading.

        Judged again whenever its generation moves: an answer of the brain, a local command,
        or a reload (any gate check) that found another process's pause. The read loop aborts
        a read and closes Chrome on it, as for a sleep.
        """
        while True:
            changed = self._changed
            verdict = self.verdict()
            if not verdict.ok:
                return verdict.reason
            await changed.wait()

    # -- remote --------------------------------------------------------------------------------

    async def apply(self, control: Control) -> Applied:
        """Apply one ``GET /v1/control`` answer: state first, then each command once."""
        granted = control.lease_held_by(self._collector_id)
        lease = control.lease
        now = self._clock.time()

        def answered(state: ControlState) -> ControlState:
            return replace(
                state,
                lease_granted=granted,
                lease_holder=lease.collector_id if lease.granted else None,
                lease_generation=lease.generation if granted else None,
                lease_valid_for=lease.valid_for if granted else None,
                lease_at=now if granted else None,
                pace_cap=control.pace_cap,
                remote_paused=control.paused,
                polled_at=now,
            )

        await self._change(answered)
        self._lease_mono = self._clock.monotonic() if granted else None
        unknown = await self._store.set_monitored(list(control.monitored))
        mine = control.commands_for(self._collector_id)
        skipped = len(control.commands) - len(mine)
        if skipped:
            log.info("control: %d command(s) for another collector or lease left unused", skipped)
        outcomes = []
        for command in mine:
            if isinstance(command, ResumeCommand):
                # lock_lifted is the owner's word from the bot, as --lock-lifted is locally.
                result = await self._ladder.resume(
                    command.block_id,
                    command.nonce,
                    probe_delay=self._delay(),
                    lock_lifted=command.lock_lifted,
                )
                outcomes.append(CommandOutcome("resume", command.nonce, result.value))
            elif isinstance(command, RefreshGroupsCommand):  # pragma: no branch - closed union
                refresh = await self.refresh(command.nonce)
                outcomes.append(CommandOutcome("refresh_groups", command.nonce, refresh.value))
        for outcome in outcomes:
            log.info("control command %s: %s", outcome.kind, outcome.result)
        return Applied(
            lease=granted,
            profile_cap=control.pace_cap,
            paused=control.paused,
            commands=tuple(outcomes),
            unknown_groups=tuple(unknown),
            skipped=skipped,
        )

    # -- commands (local and remote) -----------------------------------------------------------

    async def pause(self) -> None:
        await self._change(lambda state: replace(state, local_paused=True))

    async def unpause(self) -> None:
        await self._change(lambda state: replace(state, local_paused=False))

    async def resume(
        self, block_id: int, *, lock_lifted: bool = False, replanned: bool = False
    ) -> ResumeResult:
        """A local ``fbcollector resume <block_id>``: same rules as a remote one, own nonce."""
        return await self._ladder.resume(
            block_id,
            secrets.token_urlsafe(16),
            probe_delay=self._delay(),
            lock_lifted=lock_lifted,
            replanned=replanned,
        )

    async def refresh(self, nonce: str | None = None) -> RefreshResult:
        """Ask for a group sync (at most 3 in any 24 h), starting U(30, 300) s from now.

        ``nonce`` is the brain's (a remote command); the sync's snapshot echoes it.
        """
        if not await self._store.claim_nonce(nonce or secrets.token_urlsafe(16), kind="refresh"):
            return RefreshResult.REPLAYED
        now = self._clock.time()
        result = RefreshResult.SCHEDULED

        def schedule(state: ControlState) -> ControlState | None:
            nonlocal result
            if state.refresh_at is not None:
                result = RefreshResult.ALREADY_PENDING
                return None
            recent = tuple(t for t in state.refreshes if now - t < DAY)
            if len(recent) >= REFRESH_PER_DAY:
                result = RefreshResult.LIMIT
                return None
            return replace(
                state,
                refresh_at=now + self._delay(),
                refreshes=(*recent, now),
                refresh_nonce=nonce,
            )

        await self._change(schedule)
        return result

    def refresh_due(self) -> float | None:
        return self._state.refresh_at

    async def refresh_done(self) -> None:
        """The refresh's sync was granted: its nonce now waits for that sync's snapshot."""

        def done(state: ControlState) -> ControlState | None:
            if state.refresh_at is None:
                return None
            return replace(
                state,
                refresh_at=None,
                refresh_nonce=None,
                sync_nonce=state.refresh_nonce or state.sync_nonce,
            )

        await self._change(done)

    async def take_sync_nonce(self) -> str | None:
        """The refresh nonce a groups snapshot should echo, handed out once."""
        taken: str | None = None

        def take(state: ControlState) -> ControlState | None:
            nonlocal taken
            if state.sync_nonce is None:
                return None
            taken = state.sync_nonce
            return replace(state, sync_nonce=None)

        await self._change(take)
        return taken


# -- phase-0 monitored.toml -------------------------------------------------------------------


class MonitoredFileError(Exception):
    """``monitored.toml`` is malformed; the message names the problem, not the file's text."""


@dataclass(frozen=True, slots=True)
class KnownGroup:
    id: str
    state: GroupState
    known_since: float | None = None
    """When the group became known (a sync never moves it)."""
    monitored: bool = False


@dataclass(frozen=True, slots=True)
class MonitoredEntry:
    """One ``[[group]]`` of ``monitored.toml``: the wire's ``MonitoredGroup`` and the owner's
    membership confirmation (phase 0, ``fbcollector.confirm``)."""

    group: MonitoredGroup
    confirmed_member: bool = False
    """The owner says the account is a member (the joined-groups sync cannot say it yet)."""
    confirmed_at: float | None = None
    """When the owner said so (epoch seconds); ``None``: undated, counted from first sight."""

    @property
    def group_id(self) -> str:
        return self.group.group_id


def load_monitored_file(path: Path) -> list[MonitoredGroup] | None:
    """Read ``monitored.toml``; ``None`` when the file does not exist (see ``load_monitored``)."""
    entries = load_monitored(path)
    return None if entries is None else [e.group for e in entries]


def load_monitored(path: Path) -> list[MonitoredEntry] | None:
    """Read ``monitored.toml`` with its confirmations; ``None`` when the file does not exist.

    Format::

        [[group]]
        id = "1234567890"
        tier = 1                               # optional
        listings = false                       # optional
        confirmed_member = true                # optional: the owner's word that it is a member
        confirmed_at = 2026-09-25T09:00:00Z    # optional, with an offset (`groups confirm`)
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    return parse_monitored(raw, name=path.name)


def parse_monitored(raw: bytes, *, name: str = "monitored.toml") -> list[MonitoredEntry]:
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        msg = f"{name}: not valid TOML ({type(exc).__name__})"
        raise MonitoredFileError(msg) from exc
    tables = data.get("group", [])
    if not isinstance(tables, list):
        msg = f"{name}: [[group]] must be an array of tables"
        raise MonitoredFileError(msg)
    entries = [_monitored_entry(table, index, name) for index, table in enumerate(tables)]
    ids = [e.group_id for e in entries]
    if len(ids) != len(set(ids)):
        msg = f"{name}: a group is listed twice"
        raise MonitoredFileError(msg)
    if len(entries) > MAX_MONITORED:
        msg = f"{name}: {len(entries)} groups (the hard limit is {MAX_MONITORED})"
        raise MonitoredFileError(msg)
    return entries


def _monitored_entry(table: object, index: int, name: str) -> MonitoredEntry:
    where = f"{name}: group #{index + 1}"
    if not isinstance(table, dict):
        msg = f"{where} is not a table"
        raise MonitoredFileError(msg)
    try:
        group = MonitoredGroup(
            group_id=str(table.get("id", "")),
            tier=int(table.get("tier", 1)),
            listings=bool(table.get("listings", False)),
        )
    except (ValueError, TypeError) as exc:
        msg = f"{where} is invalid"
        raise MonitoredFileError(msg) from exc
    confirmed = table.get("confirmed_member", False)
    if not isinstance(confirmed, bool):
        # Strict: a quoted "false" would otherwise read as true.
        msg = f"{where}: confirmed_member must be true or false"
        raise MonitoredFileError(msg)
    stamp = table.get("confirmed_at")
    at: float | None = None
    if stamp is not None:
        if not isinstance(stamp, dt.datetime) or stamp.tzinfo is None:
            msg = f"{where}: confirmed_at must be a date-time with an offset (2026-09-25T09:00:00Z)"
            raise MonitoredFileError(msg)
        at = stamp.timestamp()
    return MonitoredEntry(group, confirmed_member=confirmed, confirmed_at=at)


def default_monitored(known: Sequence[KnownGroup]) -> list[MonitoredGroup]:
    """Without a file: every member group, up to 25 — sticky, then oldest-known first.

    Groups monitored already keep their places: with more than 25 members a choice that moved
    between syncs would mark a dropped group's posts seen as not monitored, and give it a new
    baseline when it came back (its posts in between never become items). Free places go to
    the groups known longest; the time a group became known is stable, unlike the time a sync
    last listed it.
    """
    members = [g for g in known if g.state is GroupState.MEMBER]
    members.sort(key=lambda g: (not g.monitored, g.known_since is None, g.known_since or 0.0, g.id))
    return [MonitoredGroup(group_id=g.id) for g in members[:MAX_MONITORED]]
