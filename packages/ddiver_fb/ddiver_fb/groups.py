"""Group sync: the account's memberships become the bot's chat list (D8).

The collector reads "All groups you've joined" and sends the whole list; the brain turns it into
``chats`` rows the core's UI already knows how to show, share and subscribe to. What it may
conclude from a list is limited on purpose:

- A group the collector reports is taken at its word (``state`` is the collector's verdict,
  which for ``left`` includes a group-page read the brain cannot do).
- A group the brain knows that is *absent* from a **complete** snapshot becomes ``unverified``,
  never ``left``: absence from one list is not proof, and only the collector can check. An
  incomplete snapshot proves nothing about absent groups. An entry with ``listed=false`` is
  absent too: the collector sends it for its metadata ("known from earlier syncs only"), and
  its ``member`` is that earlier sync's word, not this one's.
- A group reported ``lost`` (the group page said the account cannot read it) is not re-listed
  by anything but a sync that lists it as a member again (DESIGN "Brain").
- A group the brain has never seen is added only if it is readable; there is nothing to show
  about a group the account already left.
- Nothing older overwrites something newer. A snapshot older than the last one applied from
  its collector (its ``seq``, else its ``taken_at``) changes nothing, and each group remembers
  when, by that collector's clock, the evidence behind its state was seen: a snapshot or an
  event from before then, delayed by a retry, leaves that group alone. Without this a resent
  old sync re-listed a group a newer event had just reported lost.

``state`` decides ``chats.available`` (only ``member`` is readable), and :class:`FbStore` writes
the two together, so a group that just became unreadable drops out of the monitored set in the
same transaction.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Final

from ddiver_core.i18n import DEFAULT_LANG, t
from ddiver_core.storage import Storage
from ddiver_core.timeutil import Clock, utcnow
from ddiver_fb.store import (
    EventKey,
    FbGroup,
    FbStore,
    GroupChange,
    Mark,
    is_newer,
    planned_update,
)
from fbwire import Event, GroupInfo, GroupPrivacy, GroupsSnapshot, GroupState

log = logging.getLogger(__name__)

MAX_TITLE: Final = 128
"""Titles end up in button labels and message lines; Facebook's are far shorter. Telegram's own
limit, which ddiver-core's digest budget is measured against."""
NOTICE_WINDOW: Final = 86400
"""A notice nobody could deliver for a day is dropped, like an event's alert."""

Tell = Callable[[int, str], Awaitable[bool]]
Invalidate = Callable[[int | None], Awaitable[None]]


def next_state(
    current: GroupState | None, incoming: GroupInfo | None, *, complete: bool
) -> GroupState | None:
    """The state a group should have after a snapshot; ``None`` means "no row"."""
    if (
        incoming is not None
        and complete
        and not incoming.listed
        and incoming.state is GroupState.MEMBER
    ):
        # Not in this complete list: the collector's "member" is an earlier sync's verdict.
        # A stronger verdict of its own (left, lost, unverified) still stands below.
        incoming = None
    if incoming is None:
        if current is GroupState.MEMBER and complete:
            return GroupState.UNVERIFIED
        return current
    if current is GroupState.LOST:
        if incoming.state is GroupState.MEMBER and incoming.listed:
            return GroupState.MEMBER
        return GroupState.LEFT if incoming.state is GroupState.LEFT else GroupState.LOST
    if current is None and incoming.state is not GroupState.MEMBER:
        return None
    return incoming.state


def plan_snapshot(
    current: Mapping[int, FbGroup], snapshot: GroupsSnapshot, *, now: int
) -> list[FbGroup]:
    """Every row a snapshot changes, computed without touching the database (pure)."""
    taken = min(snapshot.taken_at, now)  # a collector clock ahead of ours must not date the future
    rows: list[FbGroup] = []
    seen: set[int] = set()
    collector_id, at = snapshot.collector_id, snapshot.taken_at
    for info in snapshot.groups:
        gid = int(info.id)
        seen.add(gid)
        before = current.get(gid)
        if before is not None and before.newer_than(collector_id, at):
            continue
        state = next_state(before.state if before else None, info, complete=snapshot.complete)
        if state is None:
            continue
        title = (info.name.strip() or (before.title if before else "") or info.id)[:MAX_TITLE]
        privacy = info.privacy
        if privacy is GroupPrivacy.UNKNOWN and before is not None:
            privacy = before.privacy
        row = FbGroup(
            chat_id=gid,
            title=title,
            url=info.url,
            state=state,
            state_changed_at=(
                before.state_changed_at if before is not None and before.state is state else now
            ),
            slug=info.slug or (before.slug if before else None),
            privacy=privacy,
            followed=info.followed
            if info.followed is not None
            else (before.followed if before else None),
            last_synced_at=taken,
            last_listed_at=taken if info.listed else (before.last_listed_at if before else None),
            listings=before.listings if before else False,
            tier=before.tier if before else 1,
            sort_ignored=info.sort_ignored
            if info.sort_ignored is not None
            else (before.sort_ignored if before else False),
            shared=before.shared if before else False,
            evidence_at=at,
            evidence_by=collector_id,
        )
        if row != before:
            rows.append(row)
    if snapshot.complete:
        for gid, before in current.items():
            if gid in seen or before.newer_than(collector_id, at):
                continue
            state = next_state(before.state, None, complete=True)
            if state is not None and state is not before.state:
                rows.append(
                    replace(
                        before,
                        state=state,
                        state_changed_at=now,
                        evidence_at=at,
                        evidence_by=collector_id,
                    )
                )
    return rows


@dataclass(frozen=True, slots=True)
class SyncReport:
    total: int
    complete: bool
    changes: tuple[GroupChange, ...]
    stale: bool = False
    """Older than a snapshot already applied from its collector: nothing was changed."""

    def _titles(self, predicate: Callable[[GroupChange], bool]) -> list[str]:
        return [c.after.title for c in self.changes if predicate(c)]

    @property
    def added(self) -> list[str]:
        return self._titles(lambda c: c.before is None)

    def became(self, state: GroupState) -> list[str]:
        """Titles of known groups that moved into ``state`` with this snapshot."""
        return self._titles(
            lambda c: c.before is not None and c.state_changed and c.after.state is state
        )

    @property
    def noteworthy(self) -> bool:
        return bool(self.added or any(self.became(s) for s in GroupState))


def sync_summary(report: SyncReport, lang: str) -> str:
    """What the owner is told about a sync that changed something."""
    lines = [t(lang, "fb.alert.sync", total=report.total)]
    parts = (
        ("fb.alert.sync_added", report.added),
        ("fb.alert.sync_back", report.became(GroupState.MEMBER)),
        ("fb.alert.sync_lost", report.became(GroupState.LOST)),
        ("fb.alert.sync_left", report.became(GroupState.LEFT)),
        ("fb.alert.sync_unverified", report.became(GroupState.UNVERIFIED)),
    )
    lines.extend(t(lang, key, groups=", ".join(titles)) for key, titles in parts if titles)
    return "\n".join(lines)


class GroupBook:
    """Applies snapshots and group events, and tells whoever a change concerns.

    What a change must cause is written *with* it, so nothing depends on a step after the
    commit that may never run: the messages go into ``fb_notices`` in the change's own
    transaction and are sent (and re-sent, until delivered or a day old) by :meth:`flush`.
    The matcher's cached monitored set is refreshed after the commit as well, but a failure
    there is only logged — the change is durable, and the ingest path and a periodic
    reconciliation both check the cache against the database (C1-03).
    """

    def __init__(
        self,
        storage: Storage,
        fb: FbStore,
        *,
        tell: Tell,
        invalidate: Invalidate,
        clock: Clock = utcnow,
    ) -> None:
        self._storage = storage
        self._fb = fb
        self._tell = tell
        self._invalidate = invalidate
        self._clock = clock
        self._flush_lock = asyncio.Lock()

    def _now(self) -> int:
        return int(self._clock().timestamp())

    async def apply_snapshot(
        self,
        snapshot: GroupsSnapshot,
        *,
        now: int,
        summary_to: int | None = None,
        flush: bool = True,
    ) -> SyncReport:
        """Apply a snapshot; ``summary_to`` (the owner) is told what it changed, if anything.

        ``flush=False`` leaves sending the notices to the caller (one that holds a lock).
        """
        mark = Mark(snapshot.seq, snapshot.incarnation, snapshot.taken_at)
        async with self._fb.groups_lock:
            if not is_newer(await self._fb.snapshot_mark(snapshot.collector_id), mark):
                log.info("group sync from %s is older than one applied; ignored", mark)
                return SyncReport(len(snapshot.groups), snapshot.complete, (), stale=True)
            current = {g.chat_id: g for g in await self._fb.groups()}
            rows = plan_snapshot(current, snapshot, now=now)
            changes = tuple(GroupChange(current.get(r.chat_id), r) for r in rows)
            report = SyncReport(len(snapshot.groups), snapshot.complete, changes)
            notices = await self._notices(changes)
            if report.noteworthy and summary_to is not None:
                text = sync_summary(report, await self._lang(summary_to))
                notices.append((summary_to, text))
            async with self._fb.transaction() as tx:
                await self._fb.write_groups(rows, now=now, db=tx)
                await self._fb.set_snapshot_mark(snapshot.collector_id, mark, now=now, db=tx)
                await self._fb.add_notices(notices, now=now, db=tx)
        await self._after(changes, flush=flush)
        log.info(
            "group sync: %d groups (%s), %d rows changed",
            len(snapshot.groups),
            "complete" if snapshot.complete else "partial",
            len(changes),
        )
        return report

    async def mark(
        self,
        group_id: int,
        *,
        now: int,
        event: Event | None = None,
        key: EventKey | None = None,
        state: GroupState | None = None,
        followed: bool | None = None,
        sort_ignored: bool | None = None,
        flush: bool = True,
    ) -> GroupChange | None:
        """Apply what a group event says about one group; ``None`` when nothing changed.

        With ``event`` (and its ``key``, which names the state database it came from), the
        event is marked applied in the same transaction as the change, so
        it is never acked with its effect missing, and a resend of one whose effect is in
        place changes nothing. An event older than the evidence the group's row rests on (a
        later sync from the same collector) is marked applied and changes nothing.
        """
        if (event is None) != (key is None):
            msg = "an event is marked applied by its key: pass both or neither"
            raise ValueError(msg)
        changes: dict[str, object] = {}
        if event is not None:
            changes["evidence_at"] = event.at
            changes["evidence_by"] = event.collector_id
        if state is not None:
            changes["state"] = state
        if followed is not None:
            changes["followed"] = followed
        if sort_ignored is not None:
            changes["sort_ignored"] = sort_ignored
        async with self._fb.groups_lock:
            group = await self._fb.group(group_id)
            stale = (
                event is not None
                and group is not None
                and group.newer_than(event.collector_id, event.at)
            )
            planned = None
            if group is not None and not stale:
                after = planned_update(group, now=now, **changes)
                planned = None if after == group else GroupChange(group, after)
            notices = await self._notices((planned,) if planned is not None else ())
            async with self._fb.transaction() as tx:
                if planned is not None:
                    await self._fb.write_groups([planned.after], now=now, db=tx)
                    await self._fb.add_notices(notices, now=now, db=tx)
                if key is not None:
                    await self._fb.mark_event_applied(key, now=now, db=tx)
        if planned is not None:
            await self._after((planned,), flush=flush)
        return planned

    async def flush(self) -> int:
        """Send queued notices; returns how many went out. Never raises for a failed send."""
        async with self._flush_lock:
            now = self._now()
            dropped = await self._fb.drop_old_notices(created_before=now - NOTICE_WINDOW)
            if dropped:
                log.warning("%d notices older than a day were dropped", dropped)
            sent = 0
            for notice in await self._fb.pending_notices(since=now - NOTICE_WINDOW):
                if await self._tell(notice.user_id, notice.body):
                    await self._fb.notice_sent(notice.id, now=self._now())
                    sent += 1
                else:
                    await self._fb.notice_failed(notice.id)
            return sent

    async def _notices(self, changes: tuple[GroupChange, ...]) -> list[tuple[int, str]]:
        """What each subscriber of a group that became (un)readable is told."""
        out: list[tuple[int, str]] = []
        for change in changes:
            if change.before is None or not (change.became_available or change.became_unavailable):
                continue  # a new group has no subscribers yet; a rename is nobody's business
            key = (
                "bot.chats.lost_access"
                if change.became_unavailable
                else "bot.chats.regained_access"
            )
            title = change.after.title
            out.extend(
                [
                    (user_id, t(await self._lang(user_id), key, chat=title))
                    for user_id in await self._storage.subscribers_of(change.after.chat_id)
                ]
            )
        return out

    async def _lang(self, user_id: int) -> str:
        user = await self._storage.get_user(user_id)
        return (user.lang if user else None) or DEFAULT_LANG

    async def _after(self, changes: tuple[GroupChange, ...], *, flush: bool = True) -> None:
        """What follows a commit. Nothing here may fail the request: the change is durable."""
        flips = [c for c in changes if c.became_available or c.became_unavailable]
        if any(c.before is None for c in changes) or flips:
            try:
                await self._invalidate(None)
            except Exception:
                log.exception("the matcher was not refreshed; the ingest path will do it")
        if not flush:
            return
        try:
            await self.flush()
        except Exception:
            log.exception("notices were not sent; the next tick sends them")
