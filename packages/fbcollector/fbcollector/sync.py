"""Group sync: what ``/groups/joins/`` says the account is a member of (D8, DESIGN Group sync).

The reader drives the page (navigate, scroll while the list grows, detectors, budget) and hands
the result here. The rules that decide what the collector may open next:

- A sync is **complete** only when the list's ``has_next_page`` is false *and* the number of
  groups equals N in the "All groups you've joined (N)" header.
- **Only a complete sync makes a member.** DESIGN: the collector opens only group ids its latest
  complete sync lists as ``member``. An incomplete sync may hold suggestions (group-shaped dicts
  without a join state) or stale entries, and in phase 0 the joins connection is still a guess,
  so it only refreshes the names of groups it lists: a group it is the first to show is stored
  as ``unverified`` (known, never opened) until a complete sync lists it, and a ``lost``,
  ``unverified`` or ``left`` group keeps its state and its ``paused`` flag. It decides nothing
  about the groups it did not list.
- A known group missing from a complete sync becomes ``unverified`` at once (the scheduler
  pauses every non-member group), and the owner is told.
- **``left`` needs two complete syncs without the group and a group page saying the account is
  not a member.** The collector cannot open an unlisted group to check, so the page reading has
  to come first: a group whose page already said so (``lost``) and that then disappears from two
  complete syncs is ``left``. Anything else missing twice stays ``unverified``.
- A group a complete sync lists again is a member again, whatever it was (the owner re-joined,
  or the lost verdict was a glitch); a ``paused`` flag set because it was lost is lifted.

In phase-0 local mode the monitored set follows every sync: ``monitored.toml`` if it exists,
else every member group, up to 25 — the groups monitored already keep their places, and free
places go to the groups known longest.

The owner may vouch for a group in ``monitored.toml`` (``confirmed_member = true``) before any
complete sync exists: it is then a member on the owner's word, which its own page must prove
(``fbcollector.confirm``). A complete sync overrides such a word — the ``confirmed`` flag goes
with it, for the groups it lists and for those it turns ``unverified`` alike.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from fbcollector.confirm import FLAG_CONFIRMED, apply_confirmations
from fbcollector.control import KnownGroup, default_monitored, load_monitored
from fbcollector.joins import JoinedGroups
from fbcollector.model import GroupMeta, GroupState
from fbcollector.parser import PageInfo
from fbcollector.scheduler import FLAG_PAUSED
from fbcollector.store import Store
from fbwire.models import MonitoredGroup

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    complete: bool
    listed: int
    header_count: int | None
    has_next_page: bool | None
    added: tuple[str, ...] = ()
    unverified: tuple[str, ...] = ()
    left: tuple[str, ...] = ()
    relisted: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    """Groups an incomplete sync was the first to show: known, not members until confirmed."""
    listed_ids: tuple[str, ...] = ()
    """Every group this sync listed: the brain's snapshot marks exactly these ``listed``."""

    def summary(self) -> dict[str, object]:
        return {
            "complete": self.complete,
            "listed": self.listed,
            "header_count": self.header_count,
            "has_next_page": self.has_next_page,
            "added": len(self.added),
            "unverified": list(self.unverified),
            "left": list(self.left),
            "relisted": list(self.relisted),
            "pending": list(self.pending),
        }


def is_complete(listed: int, header_count: int | None, page_info: PageInfo | None) -> bool:
    return (
        page_info is not None
        and page_info.has_next_page is False
        and header_count is not None
        and listed == header_count
    )


async def apply_sync(
    store: Store,
    joined: JoinedGroups | None,
    *,
    header_count: int | None,
    page_info: PageInfo | None,
) -> SyncOutcome:
    groups: Sequence[GroupMeta] = joined.groups if joined is not None else ()
    listed = {g.id for g in groups}
    complete = is_complete(len(listed), header_count, page_info)
    added, relisted, pending = await _record_listed(store, groups, complete=complete)
    unverified: list[str] = []
    left: list[str] = []
    if complete:
        await store.note_complete_sync(listed)
        for row in await store.list_groups():
            if row.id in listed or row.missing_syncs == 0:
                continue
            if row.state is GroupState.LOST:
                # Its page already said "not a member"; gone from two complete syncs: left.
                if row.missing_syncs >= 2:
                    await store.upsert_group(GroupMeta(id=row.id), state=GroupState.LEFT)
                    left.append(row.id)
            elif row.state is GroupState.MEMBER:
                await store.upsert_group(GroupMeta(id=row.id), state=GroupState.UNVERIFIED)
                if FLAG_CONFIRMED in row.flags:  # the sync's word overrides the owner's
                    await store.set_group_flags(row.id, row.flags - {FLAG_CONFIRMED}, durable=True)
                unverified.append(row.id)
    return SyncOutcome(
        complete=complete,
        listed=len(listed),
        header_count=header_count,
        has_next_page=page_info.has_next_page if page_info is not None else None,
        added=tuple(added),
        unverified=tuple(unverified),
        left=tuple(left),
        relisted=tuple(relisted),
        pending=tuple(pending),
        listed_ids=tuple(sorted(listed)),
    )


async def _record_listed(
    store: Store, groups: Sequence[GroupMeta], *, complete: bool
) -> tuple[list[str], list[str], list[str]]:
    """Store what the sync listed: ``(added, relisted, pending)``."""
    added: list[str] = []
    relisted: list[str] = []
    pending: list[str] = []
    for meta in groups:
        before = await store.get_group(meta.id)
        if not complete:
            # Names only; membership waits for a complete sync.
            if before is None:
                await store.upsert_group(meta, state=GroupState.UNVERIFIED)
                pending.append(meta.id)
            else:
                await store.upsert_group(meta)
            continue
        row = await store.upsert_group(meta, state=GroupState.MEMBER, synced=True)
        if before is None:
            added.append(meta.id)
        elif before.state is not GroupState.MEMBER:
            relisted.append(meta.id)
        # A member by the sync's word now: no longer paused as lost, no longer on the owner's.
        dropped = row.flags & ({FLAG_CONFIRMED} | ({FLAG_PAUSED} if meta.id in relisted else set()))
        if dropped:
            await store.set_group_flags(meta.id, row.flags - dropped)
    return added, relisted, pending


async def local_monitored(store: Store, monitored_file: Path) -> list[MonitoredGroup]:
    """Phase 0: ``monitored.toml``, or every member group up to 25; applied to the store.

    Groups that are not members (lost, left, unverified) are never monitored, whatever the
    file says: the collector only opens groups its latest complete sync listed — or, before a
    complete sync says otherwise, the groups the owner confirmed in the file.
    """
    in_file = load_monitored(monitored_file)
    await apply_confirmations(store, in_file or ())
    rows = await store.list_groups()
    members = {r.id for r in rows if r.state is GroupState.MEMBER}
    if in_file is None:
        known = [KnownGroup(r.id, r.state, r.baseline_at, monitored=r.monitored) for r in rows]
        wanted = default_monitored(known)
    else:
        wanted = [e.group for e in in_file]
    entries = [m for m in wanted if m.group_id in members]
    skipped = [m.group_id for m in wanted if m.group_id not in members]
    if skipped:
        log.warning("monitored groups that are not members are skipped: %s", ", ".join(skipped))
    await store.set_monitored(entries)
    return entries
