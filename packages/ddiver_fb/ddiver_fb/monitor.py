"""The collector as the brain sees it: its state, its silences, and what the owner is told.

The collector reports every minute (``/v1/status``) and sends one-off events (``/v1/events``).
From those, and from the clock, the brain keeps one question answered for ``/status``: *is
anything being read, and if not, why?* — and it tells the owner when the answer turns bad:

- **Collector state** is the lease holder's last reported state, except ``OFFLINE``, which only
  the brain can see: no report for ``offline_after`` minutes. After a report of a planned idle
  state (``quiet``, ``break``, ``not_home``) the silence may last longer, but not for ever: until
  the idle state's announced end (``Status.resumes_at``), or at most its longest planned length
  (:data:`IDLE_GRACE`), then ``offline_after`` more. The first version waited for ever, so a
  collector that died during a break showed "on a break" — and a delay — indefinitely. It is
  only *alerted* in the daytime, so a laptop that sleeps through the night wakes nobody.
- **Alerts are episodes, not messages.** Each alert has a key (``collector``, ``silence:<id>``,
  …) and an episode (``blocked:17``, ``offline``, the last post's time …); a message goes out
  when the episode changes, and is recorded only once it was delivered, so a failed send is
  retried at the next tick and a restart repeats nothing (``fb_alerts``). A forced logout is
  its block's episode (the ladder opens a block for it). Checking, sending and recording run
  under one lock, so a status and an event announcing the same block page the owner once.
  Events are stored once by their id, their side effects applied once, and their alert retried
  until it is sent (for a day at most: a day-old alert misleads more than it helps).
- **Silence** per group: a readable, watched group with no new post for ``silence_factor`` × its
  p90 gap between posts (at least ``silence_floor``) while the collector says it is reading, in
  the daytime. That is how "the feed quietly stopped showing this group" becomes visible. Both
  are counted in daytime hours only, and the silence from the group's last post or from the
  last time a backstop read its page through (``Status.group_checked``), whichever is later: a
  group that sleeps at night, or whose page the collector read and found nothing new on, is
  quiet, not unseen (two false pages on 2026-10-11, one at 09:59 after the night and one on a
  quiet Sunday evening the backstop had checked twice). Only
  once the collector has read for ``silence_catch_up`` without a pause: what a group posted while
  it was away (the night, a break, a machine asleep) arrives with its first reads after it comes
  back. The first version looked only at the state of the moment, and the minute between the end
  of the night and the first read reported the busiest group silent for 7.8 h (2026-10-10). A
  group the account does not follow is left out: it gets only the slow backstop, hours apart,
  and the owner was told so when it was found.
- **Telegram is never awaited in a collector's request.** ``on_status``, ``on_events`` and
  ``on_snapshot`` do database work only and then :meth:`~CollectorMonitor.nudge` a task of the
  monitor's own, which sends whatever is due (:meth:`~CollectorMonitor.news`); the minute tick
  runs the same pass. The first version sent from the request: while Telegram stalled, every
  request with something to say timed out at the collector, the uplink gave up its cycle before
  posts and status, and stuck handlers held the ingest server's eight slots until even the
  control poll got 503.

Nothing here restarts or "fixes" the collector (DESIGN "Health"): it reports.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import itertools
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from ddiver_core.i18n import DEFAULT_LANG, t
from ddiver_core.storage import Storage
from ddiver_core.timeutil import Clock, format_local, utcnow
from ddiver_fb.control import ControlDesk, identity, short_incarnation
from ddiver_fb.groups import GroupBook, SyncReport
from ddiver_fb.pacing import daytime_seconds, is_daytime, p90
from ddiver_fb.store import CollectorRow, EventKey, EventRow, FbGroup, FbStore
from fbwire import (
    BlockInfo,
    CollectorState,
    Event,
    EventKind,
    EventsRequest,
    GroupsSnapshot,
    GroupState,
    Status,
)
from fbwire.models import MAX_MONITORED

log = logging.getLogger(__name__)

IDLE_GRACE: Final = {
    CollectorState.BREAK: 40 * 60,
    CollectorState.QUIET: 9 * 3600,
    CollectorState.NOT_HOME: 12 * 3600,
}
"""How much longer than ``offline_after`` a collector that announced an idle state may stay
silent, at most: a break is U(15, 40) min; the longest night is 6 h plus 45 min of jitter at each
edge plus the 60 min weekend shift (rounded up); ``not_home`` is alerted after 12 h in the
design. ``Status.resumes_at``, when sent, shortens it to the announced end."""
IDLE_STATES: Final = frozenset(IDLE_GRACE)

ALERT_STATES: Final = frozenset(
    {
        CollectorState.BLOCKED,
        CollectorState.LOGGED_OUT,
        CollectorState.CRASH_LOOP,
        CollectorState.PARSER_BROKEN,
        CollectorState.FEED_NOT_CHRONO,
        CollectorState.NEEDS_LOGIN,
        CollectorState.OFFLINE,
    }
)
"""States that page the owner. ``unknown`` is not one: the collector backs off by itself and
escalates to a block if it repeats, and that block is what the owner needs to hear about."""

READING_STATES: Final = frozenset({CollectorState.OK, CollectorState.FEED_NOT_CHRONO})

EPISODE_EVENTS: Final = {
    EventKind.LOGGED_OUT: CollectorState.LOGGED_OUT,
    EventKind.CRASH_LOOP: CollectorState.CRASH_LOOP,
    EventKind.PARSER_BROKEN: CollectorState.PARSER_BROKEN,
    EventKind.FEED_NOT_CHRONO: CollectorState.FEED_NOT_CHRONO,
}
"""Events that announce a collector state: alerted as that state's episode, so the status that
follows (or preceded) them does not page the owner a second time."""

KEY_COLLECTOR: Final = "collector"
KEY_OVER_CAP: Final = "over_cap"
KEY_LEASE_MOVED: Final = "lease_moved"
EVENT_ALERT_WINDOW: Final = 86400
MIN_GAPS: Final = 5
"""Fewer gaps than this say nothing about a group's rhythm; no silence alert then."""
SILENCE_HISTORY: Final = 14 * 86400
READING_HOLE: Final = 5 * 60
"""A reading stretch ends at a hole this long in the holder's statuses (they come every minute)
or in the brain's own look at them: a machine asleep, a restart, a lost tunnel."""
CHECK_SLACK: Final = 5 * 60
"""How far ahead of the brain's clock a ``Status.group_checked`` time may be and still count."""

Tell = Callable[[int, str], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class MonitorSettings:
    offline_after: int = 20 * 60
    day_start_hour: int = 8
    day_end_hour: int = 23
    tz: str | None = None
    """The collector's day; ``None``: the owner's own timezone."""
    silence_factor: float = 3.0
    silence_floor: int = 2 * 3600
    silence_catch_up: int = 3600
    """How long the collector reads without a pause before a group's silence counts."""


@dataclass(frozen=True, slots=True)
class ReadingStretch:
    """The lease holder reading without a pause the brain could see."""

    holder: tuple[str, str]
    """The collector and its state database (``incarnation``): a new one reads from scratch."""
    since: int
    seen: int
    """The last tick that saw it reading."""


@dataclass(frozen=True, slots=True)
class CollectorView:
    """What ``/status`` needs to know about the collector side, read in one go."""

    holder: CollectorRow | None
    state: CollectorState | None
    """Effective state; ``None`` when no collector has ever reported."""
    paused: bool
    others: tuple[CollectorRow, ...]
    groups: tuple[FbGroup, ...]
    monitored: tuple[int, ...]
    over_cap: tuple[int, ...]

    @property
    def status(self) -> Status | None:
        return self.holder.status if self.holder is not None else None

    @property
    def block(self) -> BlockInfo | None:
        status = self.status
        return status.block if status is not None else None


def idle_grace(status: Status) -> int:
    """Seconds of silence, beyond ``offline_after``, that ``status`` announced."""
    grace = IDLE_GRACE.get(status.state, 0)
    if grace and status.resumes_at is not None:
        # Both times are the collector's own clock: how long it still meant to be idle.
        grace = min(grace, max(0, status.resumes_at - status.sent_at))
    return grace


def effective_state(
    row: CollectorRow | None, *, now: int, offline_after: int
) -> CollectorState | None:
    if row is None or row.status is None or row.last_status_at is None:
        return None
    if now - row.last_status_at >= offline_after + idle_grace(row.status):
        return CollectorState.OFFLINE
    return row.status.state


def block_episode(block_id: int, incarnation: str | None) -> str:
    """A block's alert episode. Block ids count per state database, so the database is part of
    it: block 1 of a collector's new database is not the old one's, and must page the owner."""
    base = f"{CollectorState.BLOCKED.value}:{block_id}"
    return f"{base}@{incarnation}" if incarnation else base


def episode_of(state: CollectorState, block: BlockInfo | None, incarnation: str | None) -> str:
    """The alert episode of a state. A logout is its block's: the collector opens one for it
    and announces it too, and the owner should be paged once, not twice (C1-09)."""
    blocked = state in (CollectorState.BLOCKED, CollectorState.LOGGED_OUT)
    if blocked and block is not None:
        return block_episode(block.id, incarnation)
    return state.value


def format_duration(seconds: float, lang: str, *, bound: bool = False) -> str:
    """``45 min`` / ``2.3 h`` / ``3 d``: short, at the precision a person reads at a glance.

    ``bound=True`` rounds up, for a promise ("up to 11 min"): a bound rounded down is a bound
    the system then breaks. Elapsed time ("silent for 2 h") rounds to the nearest.
    """
    step = math.ceil if bound else round
    minutes = max(1, int(step(seconds / 60)))
    if minutes < 90:
        return t(lang, "fb.dur.min", n=minutes)
    hours = seconds / 3600
    if hours < 48:
        tenths = int(step(hours * 10))
        text = f"{tenths / 10:.1f}".removesuffix(".0")
        return t(lang, "fb.dur.hours", n=text)
    return t(lang, "fb.dur.days", n=int(step(hours / 24)))


class CollectorMonitor:
    def __init__(
        self,
        storage: Storage,
        fb: FbStore,
        desk: ControlDesk,
        groups: GroupBook,
        *,
        owner_id: int,
        tell: Tell,
        clock: Clock = utcnow,
        settings: MonitorSettings | None = None,
    ) -> None:
        self._storage = storage
        self._fb = fb
        self._desk = desk
        self._groups = groups
        self._owner_id = owner_id
        self._tell = tell
        self._clock = clock
        self.settings = settings or MonitorSettings()
        self.last_holder_status_at: int | None = None
        """When the lease holder last reported (brain clock), for the metrics scrape."""
        self.last_holder_state: CollectorState | None = None
        self.last_holder_status: Status | None = None
        """The holder's effective state and last status, for the (synchronous) metrics scrape."""
        self._alert_lock = asyncio.Lock()
        self._reading: ReadingStretch | None = None
        self._sender: asyncio.Task[None] | None = None
        self._again = False
        self._closed = False

    def _now(self) -> int:
        return int(self._clock().timestamp())

    # -- the owner's language and day ------------------------------------------------------

    async def _owner(self) -> tuple[str, str]:
        """The owner's language and the zone to show them times in (their own)."""
        user = await self._storage.get_user(self._owner_id)
        lang = (user.lang if user else None) or DEFAULT_LANG
        tz = (user.tz if user else None) or self.settings.tz or "UTC"
        return lang, tz

    async def _day_zone(self) -> str:
        """Where the collector lives: COLLECTOR_TZ, else the owner's zone."""
        user = await self._storage.get_user(self._owner_id)
        return self.settings.tz or (user.tz if user else None) or "UTC"

    async def daytime(self) -> bool:
        """Whether it is daytime where the collector lives."""
        s = self.settings
        return is_daytime(self._clock(), await self._day_zone(), s.day_start_hour, s.day_end_hour)

    async def _alert(self, key: str, episode: str, text: str) -> bool:
        """Send ``text`` unless this episode of ``key`` was already sent; record it when sent."""
        async with self._alert_lock:
            return await self._alert_now(key, episode, text)

    async def _alert_now(self, key: str, episode: str, text: str) -> bool:
        """:meth:`_alert` for a caller that holds :attr:`_alert_lock`."""
        if await self._fb.alert_episode(key) == episode:
            return False
        if not await self._tell(self._owner_id, text):
            return False  # not recorded: the next tick tries again
        await self._fb.set_alert(key, episode, now=self._now())
        return True

    # -- the sender ------------------------------------------------------------------------

    def nudge(self) -> None:
        """Something the owner may need to hear was stored: send it soon, from our own task.

        Called by the ingest path, which must never wait for Telegram. At most one pass runs;
        a nudge during a pass asks for one more after it.
        """
        if self._closed:
            return
        if self._sender is not None and not self._sender.done():
            self._again = True
            return
        self._sender = asyncio.create_task(self._send_while_asked(), name="fb-monitor-news")

    async def _send_while_asked(self) -> None:
        while True:
            self._again = False
            try:
                await self.news()
            except Exception:
                log.exception("the collector's news were not all sent; the next tick retries")
            if not self._again or self._closed:
                return

    async def idle(self) -> None:
        """Wait until no nudged pass is running (tests, and shutdown)."""
        while self._sender is not None and not self._sender.done():
            await asyncio.wait({self._sender})

    async def close(self) -> None:
        """Stop sending; what was not sent stays queued in the database for the next start."""
        self._closed = True
        sender, self._sender = self._sender, None
        if sender is not None and not sender.done():
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender

    async def news(self) -> None:
        """Send everything the collector's messages left to say, and nothing else."""
        await self._groups.flush()
        await self._check_state()
        await self._send_event_alerts()
        await self._check_lease_move()
        await self._check_contenders()

    # -- inputs from the collector ---------------------------------------------------------

    async def on_status(self, status: Status) -> bool:
        """Store and act on a status; ``False`` when it is older than one already applied
        (a delayed resend must not put back a block or a state the collector has left), or
        when it comes from a second state database under the lease holder's id: stored, it
        would overwrite the holder's own report (the owner hears of it from its control poll).
        """
        cid, incarnation = status.collector_id, status.incarnation
        async with self._desk.lease_lock:
            state = await self._desk.lease()
            if state.holder == cid and not state.holds(cid, incarnation):
                return False
            if not await self._fb.save_status(status, now=self._now()):
                return False
            leased = await self._desk.claim(cid, incarnation)
            if leased:
                await self._desk.settle(status)
        if leased:  # an unleased collector is shown in /status, but drives nothing
            self.nudge()
        return True

    async def on_events(self, request: EventsRequest) -> list[int]:
        """Store each event once and apply what it says; returns the ids now safe to delete.

        An id is returned only once the event's effect is in place. The first version stored
        an event and then applied it: a busy database between the two answered 503, the resend
        found the event stored, skipped the effect and acked it — a lost group stayed monitored
        for good. Now a stored but unapplied event is applied again on the resend (every effect
        is idempotent), and the request, not each event, names the collector: the envelope is
        what the lease is checked against (the wire refuses events from another collector).
        """
        now = self._now()
        collector_id = request.collector_id
        incarnation = request.incarnation or ""
        acked: list[int] = []
        async with self._desk.lease_lock:
            state = await self._desk.lease()
            leased = state.holder is None or state.holds(collector_id, request.incarnation)
            for event in request.events:
                key = (collector_id, incarnation, event.event_id)
                await self._fb.add_event(event, incarnation=incarnation, now=now)
                if not await self._fb.event_applied(key):
                    await self._apply_event(event, key, now=now, leased=leased)
                acked.append(event.event_id)
        self.nudge()
        return acked

    async def _apply_event(self, event: Event, key: EventKey, *, now: int, leased: bool) -> None:
        gid = int(event.group_id) if event.group_id is not None else None
        mark = self._groups.mark
        if not leased:
            # Stored and alerted, but a collector without the lease changes nothing.
            await self._fb.mark_event_applied(key, now=now)
        elif event.kind is EventKind.GROUP_LOST and gid is not None:
            await mark(gid, now=now, event=event, key=key, state=GroupState.LOST, flush=False)
        elif event.kind is EventKind.NOT_FOLLOWED and gid is not None:
            await mark(gid, now=now, event=event, key=key, followed=False, flush=False)
        elif event.kind is EventKind.SORT_IGNORED and gid is not None:
            await mark(gid, now=now, event=event, key=key, sort_ignored=True, flush=False)
        elif event.kind is EventKind.BLOCK_CLOSED and event.block_id is not None:
            await self._desk.block_closed(event.collector_id, event.block_id, event=key)
        else:
            await self._fb.mark_event_applied(key, now=now)  # nothing to apply but its alert

    async def on_snapshot(self, snapshot: GroupsSnapshot) -> SyncReport | None:
        """Apply a groups snapshot from the lease holder; ``None`` from anybody else."""
        async with self._desk.lease_lock:
            if not await self._desk.claim(snapshot.collector_id, snapshot.incarnation):
                return None
            report = await self._groups.apply_snapshot(
                snapshot, now=self._now(), summary_to=self._owner_id, flush=False
            )
            # A stale one too: when the answer to a snapshot failed after its commit (a busy
            # database), the resend is answered stale, and a step skipped for stale snapshots
            # never ran for it — the refresh stayed open for 12 h. Closing by nonce, or by a
            # time the command must predate, is idempotent.
            await self._desk.snapshot_arrived(snapshot)
        await self._rearm_group_alerts()
        self.nudge()
        return report

    async def _rearm_group_alerts(self) -> None:
        """Forget the per-group alerts whose condition the stored group no longer shows, so
        the next loss, unfollow or ignored sort is alerted again. From the stored rows, not
        from a snapshot's changes: a resent snapshot changes nothing, and a clear lost after
        its commit left the group's alert muted for good."""
        keys: list[str] = []
        for group in await self._fb.groups():
            gid = group.chat_id
            if group.state is GroupState.MEMBER:
                keys.append(f"{EventKind.GROUP_LOST.value}:{gid}")
            if group.followed:
                keys.append(f"{EventKind.NOT_FOLLOWED.value}:{gid}")
            if not group.sort_ignored:
                keys.append(f"{EventKind.SORT_IGNORED.value}:{gid}")
        await self._fb.clear_alerts(keys)

    # -- the periodic look -----------------------------------------------------------------

    async def tick(self) -> None:
        # First: a step below that fails must not cost the stretch a tick it did not see.
        reading_since = self._reading_since(await self.holder_row(), self._now())
        await self._fb.expire_commands(now=self._now())
        await self.news()
        await self._check_over_cap()
        await self._check_silence(reading_since)

    async def _check_lease_move(self) -> None:
        """The revoked holder has heard it must stop; the new one reads from its next poll."""
        move = await self._desk.last_move()
        if move is None:
            return
        lang, _ = await self._owner()
        text = t(lang, "fb.alert.lease_moved", old=move.old, id=move.new)
        await self._alert(KEY_LEASE_MOVED, str(move.generation), text)

    async def _check_contenders(self) -> None:
        """Once per contender and holder: a collector was told another one holds the lease.

        One under the holder's own id is a second state database (a second machine, or one
        whose state was recreated): its own message, and ``/lease <id>`` moves the lease to it.
        """
        lease = await self._desk.lease()
        label = lease.label
        if lease.holder is None or label is None:
            return
        lang, _ = await self._owner()
        for row in await self._fb.collectors():
            if row.refused_by != label:
                continue
            cid = row.collector_id
            if cid == lease.holder:
                key = f"contender:{identity(cid, row.refused_incarnation)}"
                text = t(
                    lang,
                    "fb.alert.twin",
                    id=cid,
                    old=short_incarnation(lease.incarnation),
                    new=short_incarnation(row.refused_incarnation),
                )
            else:
                key = f"contender:{cid}"
                text = t(lang, "fb.alert.contender", id=cid, holder=lease.holder)
            await self._alert(key, label, text)

    async def holder_row(self) -> CollectorRow | None:
        holder = await self._desk.holder()
        return await self._fb.collector(holder) if holder is not None else None

    async def state(self) -> CollectorState | None:
        return effective_state(
            await self.holder_row(), now=self._now(), offline_after=self.settings.offline_after
        )

    async def _check_state(self) -> None:
        row = await self.holder_row()
        state = effective_state(row, now=self._now(), offline_after=self.settings.offline_after)
        self.last_holder_status_at = row.last_status_at if row is not None else None
        self.last_holder_state = state
        self.last_holder_status = row.status if row is not None else None
        if row is None or state is None:
            return
        lang, tz = await self._owner()
        status = row.status
        block = status.block if status is not None else None
        if state in ALERT_STATES:
            if state is CollectorState.OFFLINE and not await self.daytime():
                return
            incarnation = status.incarnation if status is not None else None
            episode = episode_of(state, block, incarnation)
            await self._alert(KEY_COLLECTOR, episode, self._state_text(row, state, lang, tz))
            return
        if state is CollectorState.OK:
            async with self._alert_lock:
                sent = await self._fb.alert_episode(KEY_COLLECTOR)
                if sent is not None and sent != CollectorState.OK.value:
                    await self._alert_now(
                        KEY_COLLECTOR,
                        CollectorState.OK.value,
                        t(lang, "fb.alert.back", id=row.collector_id),
                    )

    def _state_text(self, row: CollectorRow, state: CollectorState, lang: str, tz: str) -> str:
        status = row.status
        block = status.block if status is not None else None
        if state is CollectorState.BLOCKED and block is not None:
            return self._block_text(row.collector_id, block, lang, tz)
        if state is CollectorState.OFFLINE:
            silent = self._now() - (row.last_status_at or self._now())
            return t(
                lang,
                "fb.alert.offline",
                id=row.collector_id,
                silent=format_duration(silent, lang),
            )
        text = t(lang, "fb.alert.state", id=row.collector_id, state=t(lang, f"fb.state.{state}"))
        if status is not None and status.detail and status.state is state:
            text = f"{text}\n{status.detail}"
        return text

    def _block_text(self, collector_id: str, block: BlockInfo, lang: str, tz: str) -> str:
        if block.until is None:
            return t(
                lang, "fb.alert.blocked_final", id=collector_id, block=block.id, reason=block.reason
            )
        until = dt.datetime.fromtimestamp(block.until, dt.UTC)
        return t(
            lang,
            "fb.alert.blocked",
            id=collector_id,
            block=block.id,
            reason=block.reason,
            until=format_local(until, tz),
        )

    async def _send_event_alerts(self) -> None:
        # One lock from selecting to marking: two requests (or a request and the tick) would
        # otherwise both select the same unalerted row and both send it.
        async with self._alert_lock:
            now = self._now()
            await self._fb.expire_unalerted(received_before=now - EVENT_ALERT_WINDOW, now=now)
            pending = await self._fb.unalerted_events(since=now - EVENT_ALERT_WINDOW)
            if not pending:
                return
            lang, tz = await self._owner()
            done = [event.key for event in pending if await self._event_alert(event, lang, tz)]
            await self._fb.mark_events_alerted(done, now=now)

    async def _event_alert(self, event: EventRow, lang: str, tz: str) -> bool:
        """Deliver (or deliberately skip) one event's alert; ``False`` means try again later.

        The caller holds :attr:`_alert_lock`."""
        try:
            kind = EventKind(event.kind)
        except ValueError:
            kind = None
        cid = event.collector_id
        if kind is EventKind.BLOCK_OPENED and event.block_id is not None:
            row = await self._fb.collector(cid)
            status = row.status if row is not None else None
            block = status.block if status is not None else None
            episode = block_episode(event.block_id, event.incarnation)
            if await self._fb.alert_episode(KEY_COLLECTOR) == episode:
                return True
            same_db = status is not None and (status.incarnation or "") == event.incarnation
            if block is not None and block.id == event.block_id and same_db:
                text = self._block_text(cid, block, lang, tz)
            else:
                reason = event.detail or "?"
                text = t(lang, "fb.alert.state", id=cid, state=t(lang, "fb.state.blocked"))
                text = f"{text} (#{event.block_id}: {reason})"
            return await self._alert_or_seen(KEY_COLLECTOR, episode, text)
        if kind in EPISODE_EVENTS:
            state = EPISODE_EVENTS[kind]
            text = t(lang, "fb.alert.state", id=cid, state=t(lang, f"fb.state.{state}"))
            if event.detail:
                text = f"{text}\n{event.detail}"
            episode = state.value
            if kind is EventKind.LOGGED_OUT and event.block_id is not None:
                episode = block_episode(event.block_id, event.incarnation)
            return await self._alert_or_seen(KEY_COLLECTOR, episode, text)
        if kind is EventKind.BLOCK_CLOSED:
            sent = await self._tell(
                self._owner_id, t(lang, "fb.alert.block_closed", block=event.block_id)
            )
            if sent:
                # Recovered: the "reading again" that the next OK status would send says less.
                await self._fb.set_alert(KEY_COLLECTOR, CollectorState.OK.value, now=self._now())
            return sent
        if kind in (EventKind.GROUP_LOST, EventKind.NOT_FOLLOWED, EventKind.SORT_IGNORED):
            gid = event.group_id
            group = await self._fb.group(gid) if gid is not None else None
            title = group.title if group is not None else str(gid)
            key = {
                EventKind.GROUP_LOST: "fb.alert.group_lost",
                EventKind.NOT_FOLLOWED: "fb.alert.not_followed",
                EventKind.SORT_IGNORED: "fb.alert.sort_ignored",
            }[kind]
            return await self._alert_or_seen(f"{kind.value}:{gid}", "1", t(lang, key, group=title))
        if kind is EventKind.OUTBOX_HIGH:
            return await self._tell(self._owner_id, t(lang, "fb.alert.outbox_high", id=cid))
        return await self._tell(self._owner_id, t(lang, "fb.alert.event", id=cid, kind=event.kind))

    async def _alert_or_seen(self, key: str, episode: str, text: str) -> bool:
        """For event alerts (the caller holds the lock): ``True`` when told now or before."""
        if await self._fb.alert_episode(key) == episode:
            return True  # already told, by the status or an earlier event
        return await self._alert_now(key, episode, text)

    async def _check_over_cap(self) -> None:
        monitored = await self._desk.monitored()
        if not monitored.over_cap:
            await self._fb.clear_alert(KEY_OVER_CAP)
            return
        lang, _ = await self._owner()
        titles = await self._titles(monitored.over_cap)
        await self._alert(
            KEY_OVER_CAP,
            ",".join(str(gid) for gid in sorted(monitored.over_cap)),
            t(
                lang,
                "fb.alert.over_cap",
                n=len(monitored.over_cap),
                cap=MAX_MONITORED,
                groups=", ".join(titles),
            ),
        )

    async def _check_silence(self, reading_since: int | None) -> None:
        now = self._now()
        if reading_since is None or now - reading_since < self.settings.silence_catch_up:
            return
        if not await self.daytime():
            return
        lang, _ = await self._owner()
        s = self.settings
        zone = await self._day_zone()

        def day(start: int, end: int) -> float:
            return daytime_seconds(start, end, zone, s.day_start_hour, s.day_end_hour)

        row = await self.holder_row()
        checked = row.status.group_checked if row is not None and row.status is not None else {}
        sent = await self._desk.monitored()
        for entry in sent.sent:
            gid = int(entry.group_id)
            group = await self._fb.group(gid)
            if group is not None and group.followed is False:
                continue
            times = await self._fb.item_times(gid, since=now - SILENCE_HISTORY)
            # Gaps all within a night say nothing about the day: a group that posts only at
            # night has no daytime rhythm to be silent against.
            gaps = [g for a, b in itertools.pairwise(times) if (g := day(a, b)) > 0]
            if len(gaps) < MIN_GAPS:
                continue
            gap = p90(gaps)
            threshold = max(s.silence_factor * gap, s.silence_floor)
            last = times[-1]
            check = checked.get(str(gid))
            # A check from the future is a clock gone wrong, not a page read: it would hold
            # the alert off for as long as that clock stays wrong.
            fresh = check is not None and check <= now + CHECK_SLACK
            quiet_since = max(last, min(check, now)) if fresh and check is not None else last
            watched_since = await self._watched_since(gid)
            if day(quiet_since, now) < threshold or (
                watched_since is not None and day(watched_since, now) < threshold
            ):
                continue
            title = group.title if group is not None else str(gid)
            await self._alert(
                f"silence:{gid}",
                str(last),
                t(
                    lang,
                    "fb.alert.silence",
                    group=title,
                    silent=format_duration(day(last, now), lang),
                    gap=format_duration(gap, lang),
                ),
            )

    def _reading_since(self, row: CollectorRow | None, now: int) -> int | None:
        """Since when the holder has been reading without a pause the brain could see.

        Seen by the minute tick: any state but a reading one, a hole in the holder's statuses or
        in the ticks themselves, or another holder (or state database) starts a new stretch.
        Kept in memory: a restart of the brain starts one too, which only delays an alert.
        """
        state = effective_state(row, now=now, offline_after=self.settings.offline_after)
        last = self._reading
        if (
            row is None
            or row.status is None
            or row.last_status_at is None
            or state not in READING_STATES
        ):
            self._reading = None
            return None
        quiet = now - row.last_status_at
        if quiet > READING_HOLE:
            if last is not None:
                log.info("the reading stretch ends: no status for %d s", quiet)
            self._reading = None
            return None
        holder = (row.collector_id, row.status.incarnation or "")
        if last is not None and last.holder == holder and now - last.seen <= READING_HOLE:
            self._reading = ReadingStretch(holder, last.since, now)
        else:
            if last is not None:
                why = "another holder" if last.holder != holder else "no tick"
                log.info("the reading stretch starts again (%s)", why)
            self._reading = ReadingStretch(holder, now, now)
        return self._reading.since

    async def _watched_since(self, gid: int) -> int | None:
        """When the current watchers started watching: an old last post from before then is
        the gap in *our* reading, not the group's silence."""
        since = await self._storage.subscribed_since(gid)
        active = set(await self._storage.subscribers_of(gid))
        moments = [int(m.timestamp()) for uid, m in since.items() if uid in active]
        return min(moments) if moments else None

    async def _titles(self, ids: tuple[int, ...] | list[int]) -> list[str]:
        titles = []
        for gid in ids:
            group = await self._fb.group(gid)
            titles.append(group.title if group is not None else str(gid))
        return titles

    # -- /status ---------------------------------------------------------------------------

    async def view(self) -> CollectorView:
        holder_id = await self._desk.holder()
        rows = await self._fb.collectors()
        holder = next((r for r in rows if r.collector_id == holder_id), None)
        monitored = await self._desk.monitored()
        return CollectorView(
            holder=holder,
            state=effective_state(
                holder, now=self._now(), offline_after=self.settings.offline_after
            ),
            paused=await self._desk.paused(),
            others=tuple(r for r in rows if r.collector_id != holder_id),
            groups=tuple(await self._fb.groups()),
            monitored=tuple(int(m.group_id) for m in monitored.sent),
            over_cap=monitored.over_cap,
        )
