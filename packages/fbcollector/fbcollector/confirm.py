"""The owner's word that the account is a member of a group (phase 0, owner decision 2026-09-25).

DESIGN lets the collector open only the groups its latest *complete* sync of ``/groups/joins/``
lists as members, and a sync is complete only once the joined-groups JSON is understood — which
is itself a phase-0 question. Until then no group would ever be read. So the owner may vouch for
a group by hand, in ``monitored.toml``::

    [[group]]
    id = "390000000000001"
    confirmed_member = true
    confirmed_at = 2026-09-25T09:00:00Z    # written by `fbcollector groups confirm`; optional

The rules (``apply_confirmations`` runs each time the collector reads the file):

- **A confirmation makes the group a member** for the scheduler and the reader (state
  ``member``, flag ``confirmed``), known by id alone until its page names it; a pause left by an
  earlier lost verdict is lifted, because a *new* confirmation is the owner saying they checked.
  Its time is ``confirmed_at``, or for an undated one the moment the collector first read it.
- **The group page still has the last word.** A group whose membership rests on a confirmation
  must show ``viewer_join_state == MEMBER`` on its own page (``DetectorInput.needs_member_proof``);
  any other state, or none stated, is ``GROUP_LOST``: the group is paused, the owner alerted, and
  the confirmation **withdrawn** (``withdraw``). The first page that does say ``MEMBER`` is noted
  (``verified_at``).
- **A withdrawn confirmation is never applied again.** The file keeps saying
  ``confirmed_member = true`` — the collector never edits the owner's file — but that
  confirmation (the same ``confirmed_at``, or undated) is spent; ``fbcollector groups confirm``
  dates a new one after the owner checked by hand.
- **A complete sync overrides every confirmation older than it**: it lists the group (a member
  by the sync's own word) or it does not (``unverified``, as for any member missing from it).
  The ``confirmed`` flag goes with the sync; the record is marked ``overridden``. A confirmation
  newer than the latest complete sync counts until the next one.
- **Taking the word back** (``confirmed_member`` removed or false, the entry or the file gone)
  withdraws an active confirmation: a group that is a member only by it becomes ``unverified``.

Records live in kv ``confirmations`` (``{group id: record}``), written durably: a withdrawal is
what keeps a group shut. Only the read loop's process writes them; ``groups confirm`` writes the
file alone, which the loop picks up by its modification time.

The file editor (``confirm_in_file``) changes only the lines it must: the ``confirmed_*`` lines
of the groups it confirms, or a new ``[[group]]`` at the end; comments and everything else stay.
The result is parsed again and compared with what was asked before it replaces the file.
"""

from __future__ import annotations

import datetime as dt
import enum
import logging
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Final

from fbcollector.control import MonitoredEntry, MonitoredFileError, parse_monitored
from fbcollector.model import GroupMeta
from fbcollector.relay import digit_id, facebook_url
from fbcollector.scheduler import FLAG_PAUSED
from fbcollector.store import KV_LAST_COMPLETE_SYNC, Store
from fbwire.models import MAX_MONITORED, GroupState

log = logging.getLogger(__name__)

KV_CONFIRMATIONS: Final = "confirmations"
FLAG_CONFIRMED: Final = "confirmed"
"""The group is a member by the owner's word alone: its page must prove it."""
FILE_HEADER: Final = (
    "# Groups the collector reads in phase 0 (docs/RUNBOOK.md, section 5).\n"
    "# confirmed_member = true: the owner has checked by hand that the account is a member.\n"
)


class ConfirmationState(enum.StrEnum):
    ACTIVE = "active"
    WITHDRAWN = "withdrawn"
    OVERRIDDEN = "overridden"


@dataclass(frozen=True, slots=True)
class Confirmation:
    confirmed_at: float | None
    """The file's ``confirmed_at``: which confirmation this is (``None``: undated)."""
    seen_at: float
    """When the collector first read it."""
    state: ConfirmationState = ConfirmationState.ACTIVE
    verified_at: float | None = None
    """The first group page that said ``MEMBER``."""
    ended_at: float | None = None
    reason: str | None = None

    @property
    def since(self) -> float:
        """The confirmation's moment, never later than when it was read (a future date)."""
        return self.seen_at if self.confirmed_at is None else min(self.confirmed_at, self.seen_at)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Confirmation:
        return cls(
            confirmed_at=_opt_float(data.get("confirmed_at")),
            seen_at=float(data["seen_at"]),
            state=ConfirmationState(data.get("state", ConfirmationState.ACTIVE.value)),
            verified_at=_opt_float(data.get("verified_at")),
            ended_at=_opt_float(data.get("ended_at")),
            reason=data.get("reason"),
        )


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


@dataclass(frozen=True, slots=True)
class ConfirmationReport:
    applied: tuple[str, ...] = ()
    overridden: tuple[str, ...] = ()
    withdrawn: tuple[str, ...] = ()


async def load_confirmations(store: Store) -> dict[str, Confirmation]:
    raw = await store.kv_get_json(KV_CONFIRMATIONS)
    if not isinstance(raw, dict):
        return {}
    return {str(k): Confirmation.from_json(v) for k, v in raw.items() if isinstance(v, dict)}


async def _save(store: Store, records: dict[str, Confirmation]) -> None:
    data = {gid: record.to_json() for gid, record in sorted(records.items())}
    await store.kv_set_json(KV_CONFIRMATIONS, data, durable=True)


async def last_complete_sync(store: Store) -> float | None:
    raw = await store.kv_get(KV_LAST_COMPLETE_SYNC)
    return None if raw is None else float(raw)


async def apply_confirmations(
    store: Store, entries: Iterable[MonitoredEntry]
) -> ConfirmationReport:
    """Bring the store in line with the file's confirmations (see the module's rules)."""
    now = store.now()
    records = await load_confirmations(store)
    before = dict(records)
    synced = await last_complete_sync(store)
    wanted = {e.group_id: e for e in entries if e.confirmed_member}
    applied: list[str] = []
    overridden: list[str] = []
    withdrawn: list[str] = []
    for gid, entry in wanted.items():
        record = records.get(gid)
        if record is None or record.confirmed_at != entry.confirmed_at:
            record = Confirmation(confirmed_at=entry.confirmed_at, seen_at=now)
            if synced is not None and synced >= record.since:
                # Dated before the latest complete sync, which already said what it knew.
                records[gid] = _ended(record, ConfirmationState.OVERRIDDEN, now, "a newer sync")
                await _drop_flag(store, gid)
                overridden.append(gid)
                continue
            records[gid] = record
            await _make_member(store, gid)
            applied.append(gid)
            continue
        if record.state is not ConfirmationState.ACTIVE:
            continue  # spent: only a new date brings it back
        if synced is not None and synced >= record.since:
            records[gid] = _ended(record, ConfirmationState.OVERRIDDEN, now, "a complete sync")
            await _drop_flag(store, gid)
            overridden.append(gid)
            continue
        row = await store.get_group(gid)
        if row is not None and row.state in {GroupState.LOST, GroupState.LEFT}:
            # A lost verdict that did not get to withdraw it (a crash in between).
            records[gid] = _ended(record, ConfirmationState.WITHDRAWN, now, "the page said lost")
            await _drop_flag(store, gid)
            withdrawn.append(gid)
    for gid, record in list(records.items()):
        if record.state is ConfirmationState.ACTIVE and gid not in wanted:
            records[gid] = _ended(record, ConfirmationState.WITHDRAWN, now, "taken back")
            await _unconfirm(store, gid)
            withdrawn.append(gid)
    if records != before:
        await _save(store, records)
    for gid in applied:
        log.info("group %s: a member by the owner's confirmation (its page will check)", gid)
    for gid in overridden:
        log.info("group %s: its confirmation is overridden by a complete sync", gid)
    for gid in withdrawn:
        log.info("group %s: its confirmation is withdrawn", gid)
    return ConfirmationReport(tuple(applied), tuple(overridden), tuple(withdrawn))


def _ended(record: Confirmation, state: ConfirmationState, now: float, reason: str) -> Confirmation:
    return replace(record, state=state, ended_at=now, reason=reason)


async def _make_member(store: Store, group_id: str) -> None:
    row = await store.upsert_group(GroupMeta(id=group_id), state=GroupState.MEMBER)
    flags = (row.flags | {FLAG_CONFIRMED}) - {FLAG_PAUSED}
    if flags != row.flags:
        await store.set_group_flags(group_id, flags, durable=True)


async def _drop_flag(store: Store, group_id: str) -> bool:
    row = await store.get_group(group_id)
    if row is None or FLAG_CONFIRMED not in row.flags:
        return False
    await store.set_group_flags(group_id, row.flags - {FLAG_CONFIRMED}, durable=True)
    return True


async def _unconfirm(store: Store, group_id: str) -> None:
    """The owner took the word back: a member by it alone is unverified again."""
    row = await store.get_group(group_id)
    if row is None or FLAG_CONFIRMED not in row.flags:
        return
    if row.state is GroupState.MEMBER:
        await store.upsert_group(GroupMeta(id=group_id), state=GroupState.UNVERIFIED)
    await _drop_flag(store, group_id)


async def withdraw(store: Store, group_id: str, *, reason: str) -> bool:
    """The group page did not prove membership: spend the confirmation. ``True`` if one was.

    The caller has already marked the group lost and paused (fail closed first).
    """
    records = await load_confirmations(store)
    record = records.get(group_id)
    dropped = await _drop_flag(store, group_id)
    if record is None or record.state is not ConfirmationState.ACTIVE:
        return dropped
    records[group_id] = _ended(record, ConfirmationState.WITHDRAWN, store.now(), reason)
    await _save(store, records)
    return True


async def note_verified(store: Store, group_id: str) -> None:
    """The group page said ``MEMBER``: record the first time it did."""
    records = await load_confirmations(store)
    record = records.get(group_id)
    if record is None or record.state is not ConfirmationState.ACTIVE or record.verified_at:
        return
    records[group_id] = replace(record, verified_at=store.now())
    await _save(store, records)


def describe(group_id: str, record: Confirmation, zone: dt.tzinfo) -> str:
    """One status line for a confirmation."""

    def at(epoch: float | None) -> str:
        if epoch is None:
            return "-"
        return dt.datetime.fromtimestamp(epoch, zone).strftime("%Y-%m-%d %H:%M")

    head = f"  {group_id} confirmed {at(record.since)}"
    if record.state is ConfirmationState.ACTIVE:
        checked = "not yet verified on its page"
        if record.verified_at is not None:
            checked = f"verified on its page {at(record.verified_at)}"
        return f"{head}: active, {checked}"
    return f"{head}: {record.state.value} {at(record.ended_at)} ({record.reason or '-'})"


# -- group references -------------------------------------------------------------------------


_SLUG: Final = re.compile(r"[A-Za-z0-9._-]{1,128}")


class GroupRefError(ValueError):
    """Not a group id or a facebook.com group URL."""


@dataclass(frozen=True, slots=True)
class GroupRef:
    group_id: str | None
    slug: str | None = None
    """A vanity name, to be resolved through the groups the collector already knows."""


def parse_group_ref(text: str) -> GroupRef:
    """A numeric group id, or a ``facebook.com/groups/<id or name>/…`` URL."""
    value = text.strip()
    if digit_id(value) is not None:
        return GroupRef(value)
    if "://" not in value:
        value = "https://" + value
    parsed = facebook_url(value)
    ref = parsed.group_ref if parsed is not None else None
    if ref is None:
        msg = f"{text!r} is neither a group id nor a facebook.com/groups/ URL"
        raise GroupRefError(msg)
    if digit_id(ref) is not None:
        return GroupRef(ref)
    if not _SLUG.fullmatch(ref):
        msg = f"{text!r} does not name a group"
        raise GroupRefError(msg)
    return GroupRef(None, ref)


# -- editing monitored.toml -------------------------------------------------------------------


_GROUP_TABLE: Final = re.compile(r"^\s*\[\[\s*group\s*\]\]\s*(?:#.*)?$")
_ANY_TABLE: Final = re.compile(r"^\s*\[")
_ID_LINE: Final = re.compile(r"""^\s*id\s*=\s*(?:"(\d+)"|'(\d+)'|(\d+))\s*(?:#.*)?$""")
_CONFIRM_LINE: Final = re.compile(r"^\s*(?:confirmed_member|confirmed_at)\s*=")


def stamp_of(at: dt.datetime) -> str:
    """A TOML offset date-time in UTC, to the second."""
    return at.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def confirm_text(text: str, group_ids: Sequence[str], at: dt.datetime) -> str:
    """``text`` with ``confirmed_member = true`` and ``confirmed_at`` on every group of
    ``group_ids`` (added at the end when not listed). Raises ``MonitoredFileError`` when the
    file is invalid, would go over 25 groups, or has a shape this editor cannot change safely.
    """
    ids = list(dict.fromkeys(group_ids))
    old = parse_monitored(text.encode("utf-8"))
    stamp = stamp_of(at)
    lines = text.splitlines()
    located = _locate(lines)
    for start, end, id_line in sorted((located[g] for g in ids if g in located), reverse=True):
        kept: list[str] = []
        for n in range(start, end):
            if n == id_line:
                kept += [lines[n], "confirmed_member = true", f"confirmed_at = {stamp}"]
            elif not _CONFIRM_LINE.match(lines[n]):
                kept.append(lines[n])
        lines[start:end] = kept
    added = [g for g in ids if g not in located]
    if not lines and added:
        lines = FILE_HEADER.rstrip("\n").split("\n")
    for gid in added:
        if lines and lines[-1].strip():
            lines.append("")
        lines += ["[[group]]", f'id = "{gid}"', "confirmed_member = true"]
        lines.append(f"confirmed_at = {stamp}")
    result = "\n".join(lines) + "\n"
    try:
        new = parse_monitored(result.encode("utf-8"))
    except MonitoredFileError as exc:
        raise _unsafe("the result would not be a valid monitored.toml") from exc
    _check_edit(old, new, ids, at)
    return result


def _locate(lines: Sequence[str]) -> dict[str, tuple[int, int, int]]:
    """``{group id: (block start, block end, id line)}`` for every ``[[group]]`` block."""
    starts = [n for n, line in enumerate(lines) if _GROUP_TABLE.match(line)]
    found: dict[str, tuple[int, int, int]] = {}
    for start in starts:
        end = next(
            (n for n in range(start + 1, len(lines)) if _ANY_TABLE.match(lines[n])), len(lines)
        )
        for n in range(start + 1, end):
            match = _ID_LINE.match(lines[n])
            if match is not None:
                gid = next(g for g in match.groups() if g is not None)
                found.setdefault(gid, (start, end, n))
                break
    return found


def _check_edit(
    old: Sequence[MonitoredEntry],
    new: Sequence[MonitoredEntry],
    ids: Sequence[str],
    at: dt.datetime,
) -> None:
    """Refuse an edit whose parsed result is not exactly the old file plus the confirmations."""
    listed = [e.group_id for e in old]
    expected_ids = listed + [g for g in ids if g not in set(listed)]
    problem = None
    if [e.group_id for e in new] != expected_ids:
        problem = "the groups would change order or number"
    else:
        was = {e.group_id: e for e in old}
        confirmed_at = float(int(at.timestamp()))
        for entry in new:
            if entry.group_id in ids:
                good = entry.confirmed_member and entry.confirmed_at == confirmed_at
                if entry.group_id in was:
                    good = good and entry.group == was[entry.group_id].group
            else:
                good = entry == was[entry.group_id]
            if not good:
                problem = f"group {entry.group_id} would not come out as asked"
                break
    if problem is not None:
        raise _unsafe(problem)


def _unsafe(problem: str) -> MonitoredFileError:
    return MonitoredFileError(
        f"monitored.toml has a shape this command cannot edit safely ({problem}); "
        "add `confirmed_member = true` by hand"
    )


def confirm_in_file(path: Path, group_ids: Sequence[str], at: dt.datetime) -> list[str]:
    """Confirm ``group_ids`` in ``path`` (created if missing); returns the ids newly added."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except UnicodeDecodeError as exc:
        msg = f"{path.name}: not valid TOML (UnicodeDecodeError)"
        raise MonitoredFileError(msg) from exc
    known = {e.group_id for e in parse_monitored(text.encode("utf-8"))}
    ids = list(dict.fromkeys(group_ids))
    if len(known | set(ids)) > MAX_MONITORED:
        msg = f"{path.name}: more than {MAX_MONITORED} groups (the hard limit)"
        raise MonitoredFileError(msg)
    result = confirm_text(text, ids, at)
    try:
        _write_private(path, result)
    except OSError as exc:
        msg = f"{path} cannot be written ({exc.strerror or type(exc).__name__})"
        raise MonitoredFileError(msg) from exc
    return [g for g in ids if g not in known]


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".fbc-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
