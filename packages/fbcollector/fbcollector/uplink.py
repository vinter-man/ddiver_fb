"""The uplink: everything the collector says to the brain, and hears back, over the ssh tunnel.

One task beside the read loop, one request at a time, to ``FBC_INGEST_URL`` through the tunnel
(on the Mac its Unix socket ``FBC_INGEST_SOCKET``; else its local end on 127.0.0.1). A cycle,
at least once a minute and at once when the loop queued something:

- ``GET /v1/control`` every 60 s: the lease, the monitored set, the pace cap, the pause and the
  one-shot commands, applied by ``ControlCenter`` (nonces consumed once, U(30, 300) s before a
  command's Facebook action). An answer that does not parse keeps the last word on disk. The
  poll names the incarnation too: the brain grants the lease to an id and a state database.
- ``POST /v1/groups`` when a sync left a snapshot waiting (kept in kv, so an outage or a restart
  delays it, never loses it). 409 — another collector holds the lease — drops it: resending
  cannot help, and a holder's next sync sends its own. A snapshot that echoes a refresh
  command's nonce closes exactly that command at the brain (wire change 4).
- ``POST /v1/events`` drains the durable queue, oldest first, and deletes the ids the brain
  ``acked``; the request names the incarnation, because event ids count per state database.
  A refused batch (422) is retried event by event: an event the brain refuses while it takes
  its neighbours is dropped and counted, so one bad row cannot hold every later transition
  back — unless it is a lasting one (a block, a logout, a lost group), which stays. When the
  brain takes none, the request is at fault (a version, an id), and every event stays queued.
- ``POST /v1/items`` while the outbox holds rows: batches planned on the stored payload sizes
  (``fbwire.items_body`` sends exactly the planned bytes, ≤ 512 KiB), and every row the brain
  answered for is deleted, whatever the answer. A transport error or any other status keeps
  every row of the batch; a 503 keeps the rows it has no result for (the brain answers the
  results it had before the database got busy) and waits its ``Retry-After``.
- ``POST /v1/status`` every 60 s, built by ``StatusBuilder``. ``seq`` (one fsynced counter for
  statuses and snapshots) and ``incarnation`` (random, created with the state database) let the
  brain ignore a message older than one it applied; ``stale: true`` is a delivery (wire change 2).

Only a failure that the next request would meet too ends a cycle early: no answer at all (a
dead tunnel), a busy brain (503), a refused token or a wrong URL (401, 403, 404). Any other
refusal — a 500 from a bug, a 409 or 422 for one message — is counted
(``uplink_refused_<endpoint>``) and the cycle goes on, so the status still goes out: the first
version ended the cycle at any non-200, and one endpoint's persistent refusal made a healthy,
reading collector look OFFLINE to the brain, without the outbox depth that explains it. Any
failure still counts towards the outage, and a failed cycle is followed by a pause of at least
5 s, doubling to a minute, whatever else is due — a failed push set nothing to wait for, and
the loop once spun on a refused connection at thousands of requests a second.

Collecting goes on meanwhile — everything waits on disk — for as long as the lease is valid.
When the brain has not been usable for 5 minutes (monotonic, so a sleeping laptop does not
count) the owner is told directly through the alert bot (D19: the brain may be what is down),
once per outage and deduplicated for 6 h, and told again when it answers. Each cycle leaves its
outcome in kv ``uplink_last`` (when, whether it went through, the failure's reason and since
when the brain has not been usable) for ``fbcollector uplink``, the operator door's ``uplink``.

The HTTP client never reads proxies from the environment (``trust_env=False``), never follows a
redirect, and gives up after at most 10 s for the whole request — the tunnel's local listener
accepts a connection even when its far side is gone, and httpx's own timeout is per read, which
an answer trickling in never trips. The token travels in a header only; logs name exception
types and status codes, never the token or a post (invariant 8).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import re
import secrets
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from fbcollector import alerts as alert_texts
from fbcollector.alerts import AlertResult, Alerts, quiet_http_logs
from fbcollector.clock import Clock
from fbcollector.config import Settings
from fbcollector.control import KV_CONTROL, Applied, ControlCenter, ControlState
from fbcollector.reader import FLAG_FEED_ABSENT, FLAG_NOT_FOLLOWED, FLAG_SORT_IGNORED
from fbcollector.store import (
    KEPT_EVENTS,
    KV_OUTBOX_REJECTED,
    KV_REFUSED,
    UPLINK_ENDPOINTS,
    GroupRow,
    OutboxRow,
    Store,
)
from fbcollector.sync import SyncOutcome
from fbwire.models import (
    MAX_GROUPS,
    WIRE_VERSION,
    Control,
    Event,
    EventsRequest,
    EventsResponse,
    GroupInfo,
    GroupsResponse,
    GroupsSnapshot,
    ItemsResponse,
    Status,
    StatusResponse,
    items_body,
    to_json,
)

log = logging.getLogger(__name__)

POLL_SECONDS: Final = 60.0
MAX_TIMEOUT: Final = 10.0
DOWN_ALERT_AFTER: Final = 300.0
DOWN_ALERT_DEDUP: Final = 6 * 3600.0
EVENTS_PER_REQUEST: Final = 100
MAX_ITEM_BATCHES: Final = 20
RETRY_AFTER_CAP: Final = 300.0
FLUSH_TIMEOUT: Final = 20.0
FAILURE_PAUSE: Final = 5.0
"""The least a failed cycle waits before the next; it doubles up to the poll period."""
ENDS_CYCLE: Final = frozenset({401, 403, 404, 503})
"""Answers every other request of the cycle would get as well."""
MAX_NAME: Final = 1024
KV_SEQ: Final = "uplink_seq"
KV_INCARNATION: Final = "uplink_incarnation"
KV_SNAPSHOT: Final = "uplink_snapshot"
KV_INTRODUCED: Final = "uplink_introduced"
KV_EVENTS_REJECTED: Final = "events_rejected"
KV_LAST: Final = "uplink_last"
MAX_REASON: Final = 160
KEY_DOWN: Final = "uplink_down"
KEY_BACK: Final = "uplink_back"
SUBSCRIBED: Final = "IS_SUBSCRIBED"
_SLUG: Final = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_INCARNATION: Final = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_HINTS: Final[Mapping[int, str]] = {
    401: "the token was refused (FBC_INGEST_TOKEN)",
    403: "the token was refused (FBC_INGEST_TOKEN)",
    404: "no such endpoint (FBC_INGEST_URL)",
}


class UplinkError(Exception):
    """The brain was not usable for this request; nothing the request concerned is deleted."""

    def __init__(
        self,
        reason: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        answered: bool = False,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.retry_after = retry_after
        self.answered = answered or status is not None
        """The brain answered (the tunnel works); ``False``: a transport failure."""

    @property
    def ends_cycle(self) -> bool:
        """Whether the rest of the cycle would meet the same failure (see the module)."""
        return not self.answered or self.status in ENDS_CYCLE

    @property
    def endpoint(self) -> str:
        return self.reason.partition(":")[0]


@dataclass(frozen=True, slots=True)
class Answer:
    status: int
    body: bytes
    retry_after: float | None = None


def _retry_after(value: str | None) -> float | None:
    if value is None or not value.strip().isdigit():
        return None  # the HTTP-date form is not used by the brain
    return min(float(value.strip()), RETRY_AFTER_CAP)


def _fail(answer: Answer, what: str) -> UplinkError:
    reason = _HINTS.get(answer.status, f"HTTP {answer.status}")
    return UplinkError(
        f"{what}: {reason}", status=answer.status, retry_after=answer.retry_after, answered=True
    )


class _Borrowed(httpx.AsyncBaseTransport):
    """A caller's transport, lent to one ``AsyncClient`` at a time: closing the client must not
    close it (a client per request would otherwise close a shared transport after the first)."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._inner.handle_async_request(request)


class IngestClient:
    """HTTP to the brain's ingest API: bearer token, JSON, bounded, no proxies from the env."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = MAX_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
        socket: str | None = None,
    ) -> None:
        if not 0 < timeout <= MAX_TIMEOUT:
            msg = f"the ingest timeout must be in (0, {MAX_TIMEOUT:.0f}] seconds"
            raise ValueError(msg)
        self._base = base_url.rstrip("/")
        self._auth = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self._timeout = timeout
        self._transport = _Borrowed(transport) if transport is not None else None
        self._socket = socket
        quiet_http_logs()

    def _transport_for_request(self) -> httpx.AsyncBaseTransport | None:
        """The caller's transport; else the tunnel's Unix socket when there is one (the client
        closes it with itself); else ``None``, TCP to the URL."""
        if self._transport is not None or self._socket is None:
            return self._transport
        return httpx.AsyncHTTPTransport(uds=self._socket)

    async def post(self, path: str, body: bytes) -> Answer:
        return await self._send("POST", path, content=body)

    async def get(self, path: str, params: Mapping[str, str]) -> Answer:
        return await self._send("GET", path, params=params)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None = None,
        params: Mapping[str, str] | None = None,
    ) -> Answer:
        headers = dict(self._auth)
        if content is not None:
            headers["Content-Type"] = "application/json"
        try:
            # A deadline for the whole request: httpx's timeout is per connect, read or write,
            # so an answer that trickles in byte by byte would hold the uplink indefinitely.
            async with asyncio.timeout(self._timeout):
                # A client per request: the brain closes every connection after its answer.
                async with httpx.AsyncClient(
                    transport=self._transport_for_request(),
                    timeout=self._timeout,
                    trust_env=False,
                    follow_redirects=False,
                ) as client:
                    response = await client.request(
                        method, self._base + path, content=content, params=params, headers=headers
                    )
        except TimeoutError as exc:
            raise UplinkError("transport: no answer in time") from exc
        except httpx.HTTPError as exc:
            raise UplinkError(f"transport: {type(exc).__name__}") from exc
        return Answer(
            response.status_code,
            response.content,
            _retry_after(response.headers.get("retry-after")),
        )


class StatusSource(Protocol):
    async def build(self, *, seq: int | None = None, incarnation: str | None = None) -> Status: ...


@dataclass(slots=True)
class UplinkStats:
    """Counted per process, for the log and the tests."""

    requests: int = 0
    failures: int = 0
    items_sent: int = 0
    events_sent: int = 0
    statuses: int = 0
    snapshots: int = 0
    polls: int = 0
    last: dict[str, int] = field(default_factory=dict)
    """The last status code per endpoint."""


class Uplink:
    def __init__(
        self,
        *,
        client: IngestClient,
        store: Store,
        control: ControlCenter,
        status: StatusSource,
        alerts: Alerts,
        clock: Clock,
        collector_id: str,
        poll_seconds: float = POLL_SECONDS,
        down_alert_after: float = DOWN_ALERT_AFTER,
    ) -> None:
        self._client = client
        self._store = store
        self._control = control
        self._status = status
        self._alerts = alerts
        self._clock = clock
        self._id = collector_id
        self._poll = poll_seconds
        self._down_after = down_alert_after
        self._wake = asyncio.Event()
        self._snapshot_lock = asyncio.Lock()
        self._control_at: float | None = None
        self._status_at: float | None = None
        self._items_after: float | None = None
        self._more = False
        self._nothing_answered = False
        self._answered = False
        self._down_since: float | None = None
        self._down_since_wall: float | None = None
        self._down_told = False
        self._down_handled = False
        self._failed_cycles = 0
        self.stats = UplinkStats()

    # -- the task ------------------------------------------------------------------------------

    def wake(self) -> None:
        """Something was queued: run a cycle now rather than at the next poll."""
        self._wake.set()

    @property
    def down_since(self) -> float | None:
        """Monotonic time of the first failure of the current outage (``None``: reachable)."""
        return self._down_since

    async def run(self, stop: asyncio.Event) -> None:
        """Cycle until ``stop``; never raises (the read loop must not die with the tunnel)."""
        try:
            await self.introduce()
        except Exception:  # a snapshot problem must not stop the uplink
            log.warning("could not queue the introductory groups snapshot", exc_info=True)
        while not stop.is_set():
            try:
                ok = await self.cycle()
            except Exception:  # cycle() never raises; this keeps the task alive if it does
                log.exception("uplink cycle raised")
                ok = False
            self._failed_cycles = 0 if ok else self._failed_cycles + 1
            await self._wait(stop, floor=self._failure_pause())

    def _failure_pause(self) -> float:
        """How long to wait at least after this many failed cycles in a row."""
        if self._failed_cycles == 0:
            return 0.0
        doublings = min(self._failed_cycles - 1, 10)
        return min(FAILURE_PAUSE * float(2**doublings), self._poll)

    async def cycle(self) -> bool:
        """One round; ``True`` when every request made in it was answered and taken."""
        self._wake.clear()
        self._answered = False
        failure: UplinkError | None = None
        for step in (
            self._control_step,
            self.send_snapshot,
            self.send_events,
            self._items_step,
            self._status_step,
        ):
            try:
                await step()
            except UplinkError as exc:
                failure = failure or exc
                if exc.ends_cycle:
                    break
                await self._count_refusal(exc)
            except Exception:  # a bug here must not take the read loop down with it
                log.exception("uplink cycle failed")
                failure = failure or UplinkError("uplink: an error in the collector")
                break
        if failure is not None:
            self._more = False  # never straight back after a failure (run() pauses first)
            await self._guarded(self._down(failure))
        elif self._answered:
            await self._guarded(self._up())
        if failure is not None or self._answered:
            # A cycle that made no request (woken inside a back-off) says nothing about the
            # brain: it must not turn a failed outcome into "ok" in the middle of an outage.
            await self._guarded(self._record(failure))
        return failure is None

    async def _record(self, failure: UplinkError | None) -> None:
        """The cycle's outcome for ``fbcollector uplink``: when, whether it went through, why
        not (the reason, made of this module's own words and a status code) and since when the
        brain has not been usable (wall clock)."""
        await self._store.kv_set_json(
            KV_LAST,
            {
                "at": self._clock.time(),
                "ok": failure is None,
                "reason": None if failure is None else clean_reason(failure.reason),
                "down_since": self._down_since_wall,
            },
        )

    async def _control_step(self) -> None:
        if self._due(self._control_at):
            self._control_at = self._mono()
            await self.poll_control()

    async def _items_step(self) -> None:
        if self._items_after is None or self._mono() >= self._items_after:
            await self.push_items()

    async def _status_step(self) -> None:
        if self._due(self._status_at):
            self._status_at = self._mono()
            await self.send_status()

    async def _count_refusal(self, exc: UplinkError) -> None:
        log.warning("the brain refused a request: %s", exc.reason)
        await self._guarded(self._store.kv_incr(KV_REFUSED + exc.endpoint))

    @staticmethod
    async def _guarded(step: Awaitable[object]) -> None:
        """Bookkeeping after the requests (outage alerts, counters) must not end the task."""
        try:
            await step
        except Exception:
            log.exception("uplink bookkeeping failed")

    async def flush(self, timeout: float = FLUSH_TIMEOUT) -> None:
        """At shutdown: one bounded attempt to hand over what is queued."""
        try:
            async with asyncio.timeout(timeout):
                await self.send_snapshot()
                await self.send_events()
                await self.push_items()
        except (UplinkError, TimeoutError) as exc:
            log.info("uplink flush stopped: %s", getattr(exc, "reason", type(exc).__name__))

    def _mono(self) -> float:
        return self._clock.monotonic()

    def _due(self, last: float | None) -> bool:
        return last is None or self._mono() - last >= self._poll

    def _next_delay(self) -> float:
        now = self._mono()
        if self._more:
            return 0.0
        dues = [
            now if at is None else at + self._poll for at in (self._control_at, self._status_at)
        ]
        if self._items_after is not None:
            dues.append(self._items_after)
        return min(max(0.0, min(dues) - now), self._poll)

    async def _wait(self, stop: asyncio.Event, *, floor: float = 0.0) -> None:
        """Until the next request is due, a wake-up or ``stop``; after a failed cycle, at
        least ``floor`` first, which only ``stop`` cuts short (not a wake-up: every read
        queues something, and it must not bring the next attempt forward during an outage)."""
        if floor > 0:
            await self._sleep_or(floor, stop)
        delay = self._next_delay()
        if delay <= 0 or stop.is_set() or self._wake.is_set():
            return
        await self._sleep_or(delay, stop, self._wake)

    async def _sleep_or(self, seconds: float, *events: asyncio.Event) -> None:
        if any(event.is_set() for event in events):
            return
        waiters = {
            asyncio.ensure_future(self._clock.sleep(seconds)),
            *(asyncio.ensure_future(event.wait()) for event in events),
        }
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waiters:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _request(self, endpoint: str, answer: Awaitable[Answer]) -> Answer:
        self.stats.requests += 1
        try:
            result = await answer
        except UplinkError:
            self.stats.failures += 1
            raise
        self.stats.last[endpoint] = result.status
        if result.status < 500 and result.status not in _HINTS:
            self._answered = True
        return result

    async def _post(self, endpoint: str, path: str, body: bytes) -> Answer:
        return await self._request(endpoint, self._client.post(path, body))

    # -- outage alerts -------------------------------------------------------------------------

    async def _down(self, exc: UplinkError) -> None:
        now = self._mono()
        if self._down_since is None:
            self._down_since = now
            self._down_since_wall = self._clock.time()
            log.warning("the brain is not usable: %s", exc.reason)
        if self._down_handled or now - self._down_since < self._down_after:
            return
        minutes = int((now - self._down_since) // 60)
        depth = await self._store.outbox_depth()
        text = alert_texts.simple(
            KEY_DOWN,
            f"the brain has not been usable for {minutes} min ({exc.reason}); "
            f"{depth} post(s) wait in the outbox.",
            hint=(
                "Reading goes on while the lease is valid. Check the tunnel (the operator "
                "door's `tunnel`: ssh fbc-local tunnel) and the brain."
            ),
        )
        result = await self._alerts.alert(KEY_DOWN, text, dedup=DOWN_ALERT_DEDUP)
        if result in {AlertResult.SENT, AlertResult.DISABLED}:
            self._down_told = self._down_handled = True
        elif result is AlertResult.DUPLICATE:
            self._down_handled = True  # told within the last hours; not again, and no "back"

    async def _up(self) -> None:
        since = self._down_since
        if since is None:
            return
        told = self._down_told
        self._down_since = self._down_since_wall = None
        self._down_told = self._down_handled = False
        minutes = int((self._mono() - since) // 60)
        log.info("the brain answers again after %d min", minutes)
        if told:
            await self._alerts.alert(
                KEY_BACK,
                f"✅ UPLINK: the brain answers again after {minutes} min.",
                dedup=0.0,
            )

    # -- control -------------------------------------------------------------------------------

    async def poll_control(self) -> Applied:
        # The lease is granted to this id *and* this state database: a second machine (or a
        # recreated state) under the same id is told to stop rather than granted too.
        params = {
            "collector_id": self._id,
            "incarnation": await self.incarnation(),
            "v": str(WIRE_VERSION),  # the brain answers in the version it is asked in
        }
        answer = await self._request("control", self._client.get("/v1/control", params))
        if answer.status != 200:
            raise _fail(answer, "control")
        try:
            control = Control.model_validate_json(answer.body)
        except ValidationError as exc:
            raise UplinkError(
                "control: an answer this collector cannot read", answered=True
            ) from exc
        before = self._control.state
        applied = await self._control.apply(control)
        self.stats.polls += 1
        after = self._control.state
        if (before.lease_granted, before.lease_holder) != (after.lease_granted, after.lease_holder):
            holder = after.lease_holder or "nobody"
            log.warning("lease %s (holder: %s)", "held" if applied.lease else "not held", holder)
        return applied

    # -- status --------------------------------------------------------------------------------

    async def incarnation(self) -> str:
        """The name of the ``seq`` counter, created once with the state database."""
        raw = await self._store.kv_get(KV_INCARNATION)
        if raw is not None and _INCARNATION.match(raw):
            return raw
        fresh = secrets.token_urlsafe(12)  # 16 characters of [A-Za-z0-9_-]
        await self._store.kv_set(KV_INCARNATION, fresh, durable=True)
        return fresh

    async def _stamp(self) -> tuple[int, str]:
        incarnation = await self.incarnation()
        return await self._store.kv_incr(KV_SEQ, durable=True), incarnation

    async def send_status(self) -> bool:
        seq, incarnation = await self._stamp()
        try:
            status = await self._status.build(seq=seq, incarnation=incarnation)
        except ValidationError:
            log.exception("the status could not be built; skipped")
            return False
        answer = await self._post("status", "/v1/status", to_json(status))
        if answer.status == 422:
            log.error("the brain refused the status: %s", _error_text(answer))
            return False
        if answer.status != 200:
            raise _fail(answer, "status")
        with contextlib.suppress(ValidationError):
            if StatusResponse.model_validate_json(answer.body).stale:
                log.debug("status %d was older than one the brain has", seq)
        self.stats.statuses += 1
        return True

    # -- events --------------------------------------------------------------------------------

    async def send_events(self) -> int:
        """Drain the durable events queue; returns how many the brain acknowledged."""
        sent = 0
        while True:
            pending = await self._store.pending_events(EVENTS_PER_REQUEST)
            if not pending:
                return sent
            foreign = [e.event_id for e in pending if e.collector_id != self._id]
            if foreign:
                # Queued under an earlier collector id; the brain would refuse the whole batch.
                log.warning("dropping %d queued events of another collector id", len(foreign))
                await self._store.ack_events(foreign)
                continue
            acked = await self._post_events(pending)
            sent += len(acked)
            self.stats.events_sent += len(acked)
            if not acked:
                return sent  # nothing acknowledged: do not spin on the same rows

    async def _post_events(self, events: Sequence[Event]) -> list[int]:
        """Send one request; returns the ids now off the queue (acknowledged, or dropped)."""
        answer = await self._send_events(events)
        if answer.status == 422:
            if len(events) == 1:
                raise _fail(answer, "events")  # alone, a refusal does not say whose it is
            return await self._one_by_one(events)
        return await self._taken(answer, events)

    async def _send_events(self, events: Sequence[Event]) -> Answer:
        # The incarnation names the database whose counter numbered these events (wire
        # change 12): a new state database starts again at 1.
        request = EventsRequest(
            collector_id=self._id, incarnation=await self.incarnation(), events=tuple(events)
        )
        return await self._post("events", "/v1/events", to_json(request))

    async def _taken(self, answer: Answer, events: Sequence[Event]) -> list[int]:
        if answer.status != 200:
            raise _fail(answer, "events")
        try:
            response = EventsResponse.model_validate_json(answer.body)
        except ValidationError as exc:
            raise UplinkError(
                "events: an answer this collector cannot read", answered=True
            ) from exc
        ours = {e.event_id for e in events}
        acked = [i for i in response.acked if i in ours]
        await self._store.ack_events(acked)
        return acked

    async def _one_by_one(self, events: Sequence[Event]) -> list[int]:
        """A refused batch, event by event, to find what the brain refuses.

        An event is the culprit only when the brain takes others of the same batch; then it
        is dropped and counted, unless it is a lasting transition (``KEPT_EVENTS``: a block,
        a logout, a lost group), which stays queued whatever the brain says. When it takes
        none, the refusal is the request's — the version, the collector id — not the events',
        and every one of them stays: the first version dropped each one refused alone, so a
        version skew emptied the queue in one cycle.
        """
        taken: list[int] = []
        refused: list[tuple[Event, Answer]] = []
        accepted = False
        for event in events:
            answer = await self._send_events([event])
            if answer.status == 422:
                refused.append((event, answer))
            else:
                taken += await self._taken(answer, [event])
                accepted = True
        if not accepted:
            raise _fail(refused[0][1], "events")
        dropped = [event for event, _ in refused if event.kind not in KEPT_EVENTS]
        for event, answer in refused:
            log.error(
                "the brain refused event %d (%s): %s (%s)",
                event.event_id,
                event.kind.value,
                "dropped" if event in dropped else "kept",
                _error_text(answer),
            )
        if dropped:
            await self._store.ack_events([event.event_id for event in dropped])
            await self._store.kv_incr(KV_EVENTS_REJECTED, len(dropped))
        return taken + [event.event_id for event in dropped]

    # -- items ---------------------------------------------------------------------------------

    async def push_items(self) -> int:
        """Send queued items until the outbox is empty; returns how many rows were answered.

        Every failure sets when to try again (``Retry-After``, else a poll period): one that
        left an earlier moment in place made every later cycle due at once.
        """
        sent = 0
        self._more = False
        for _ in range(MAX_ITEM_BATCHES):
            batch = await self._store.next_batch(self._id)
            if not batch:
                self._items_after = None
                return sent
            try:
                sent += await self._push_batch(batch)
            except UplinkError as exc:
                wait = exc.retry_after if exc.retry_after is not None else self._poll
                self._items_after = self._mono() + wait
                raise
            self._items_after = None
            if self._nothing_answered:
                return sent  # nothing was answered: do not spin on the same rows
        self._more = True
        return sent

    async def _push_batch(self, batch: Sequence[OutboxRow]) -> int:
        """One request; returns the rows deleted. Rows queued under an earlier collector id go
        under that id (the brain takes items from any collector): sent under today's id, the
        brain refused each one, and a refused row is deleted."""
        body = items_body(batch[0].collector_id or self._id, [row.payload for row in batch])
        try:
            answer = await self._post("items", "/v1/items", body)
        except UplinkError as exc:
            await self._store.note_send_failure(batch, exc.reason)
            raise
        response = _items_response(answer) if answer.status in {200, 503} else None
        if response is None:
            error = (
                UplinkError("items: an answer this collector cannot read", answered=True)
                if answer.status == 200
                else _fail(answer, "items")
            )
            await self._store.note_send_failure(batch, error.reason)
            raise error
        applied = await self._store.apply_results(batch, response)
        self.stats.items_sent += applied.deleted
        self._nothing_answered = applied.deleted == 0
        if answer.status == 503:
            # Busy mid-batch: what it answered is done, the rest waits for Retry-After.
            wait = answer.retry_after if answer.retry_after is not None else self._poll
            raise UplinkError("items: the brain is busy", status=503, retry_after=wait)
        return applied.deleted

    # -- groups --------------------------------------------------------------------------------

    async def queue_snapshot(self, sync: SyncOutcome) -> GroupsSnapshot:
        """Keep the snapshot of a finished sync for the next cycle (and wake it)."""
        nonce = await self._control.take_sync_nonce()
        async with self._snapshot_lock:
            waiting = await self._store.kv_get(KV_SNAPSHOT)
            if nonce is None and waiting is not None:
                # A newer snapshot replaces an unsent one, and inherits the refresh it answers.
                with contextlib.suppress(ValidationError):
                    nonce = GroupsSnapshot.model_validate_json(waiting).nonce
            snapshot = await self._snapshot(
                complete=sync.complete,
                header_count=sync.header_count,
                listed=frozenset(sync.listed_ids),
                nonce=nonce,
            )
            await self._store.kv_set(KV_SNAPSHOT, snapshot.model_dump_json())
        self.wake()
        return snapshot

    async def introduce(self) -> GroupsSnapshot | None:
        """Tell a brain that never had a snapshot from here which groups this collector knows.

        After phase 0 the collector knows its groups long before the next daily sync; without
        this the brain would monitor nothing until then. The snapshot is incomplete and lists
        nothing (``listed=false``): it adds member groups, and concludes nothing about others.
        """
        store = self._store
        if await store.kv_get(KV_INTRODUCED) is not None:
            return None
        async with self._snapshot_lock:
            if await store.kv_get(KV_SNAPSHOT) is not None or not await store.list_groups():
                return None
            snapshot = await self._snapshot(
                complete=False, header_count=None, listed=frozenset(), nonce=None
            )
            await store.kv_set(KV_SNAPSHOT, snapshot.model_dump_json())
        return snapshot

    async def send_snapshot(self) -> bool:
        raw = await self._store.kv_get(KV_SNAPSHOT)
        if raw is None:
            return False
        answer = await self._post("groups", "/v1/groups", raw.encode("utf-8"))
        if answer.status == 409:
            log.warning("groups snapshot refused: another collector holds the lease")
            await self._forget_snapshot(raw)
            return False
        if answer.status == 422:
            log.error("the brain refused the groups snapshot: %s", _error_text(answer))
            await self._forget_snapshot(raw)
            return False
        if answer.status != 200:
            raise _fail(answer, "groups")
        with contextlib.suppress(ValidationError):
            reply = GroupsResponse.model_validate_json(answer.body)
            log.info("groups snapshot: %d groups, %d changed", reply.groups, reply.changed)
        await self._forget_snapshot(raw)
        await self._store.kv_set(KV_INTRODUCED, "1")
        self.stats.snapshots += 1
        return True

    async def _forget_snapshot(self, sent: str) -> None:
        """Delete the snapshot just sent — unless a newer one replaced it meanwhile."""
        async with self._snapshot_lock:
            if await self._store.kv_get(KV_SNAPSHOT) == sent:
                await self._store.kv_delete(KV_SNAPSHOT)

    async def _snapshot(
        self, *, complete: bool, header_count: int | None, listed: frozenset[str], nonce: str | None
    ) -> GroupsSnapshot:
        groups = [
            info for row in await self._store.list_groups() if (info := group_info(row, listed))
        ]
        if len(groups) > MAX_GROUPS:
            groups, complete = groups[:MAX_GROUPS], False
        shown = sum(1 for g in groups if g.listed)
        # A group whose row does not fit the wire was left out: then the list is not the page's.
        complete = complete and header_count is not None and shown == header_count
        seq, incarnation = await self._stamp()
        return GroupsSnapshot(
            collector_id=self._id,
            taken_at=int(self._clock.time()),
            complete=complete,
            header_count=header_count,
            groups=tuple(groups),
            seq=seq,
            incarnation=incarnation,
            nonce=nonce,
        )


def group_info(row: GroupRow, listed: frozenset[str]) -> GroupInfo | None:
    """A stored group as the brain's snapshot entry; ``None`` when it cannot be one.

    ``followed`` is what the brain promises delays by, so a group the feed has stopped showing
    (``feed_absent``) is not followed, whatever the group page says: it is subscribed by
    definition, and the next snapshot used to tell the brain so, undoing the NOT_FOLLOWED event
    and promising those users the feed's delay again.
    """
    if FLAG_NOT_FOLLOWED in row.flags or FLAG_FEED_ABSENT in row.flags:
        followed: bool | None = False
    elif row.subscribe_status is not None:
        followed = row.subscribe_status == SUBSCRIBED
    else:
        followed = None
    if FLAG_SORT_IGNORED in row.flags:
        sort_ignored: bool | None = True
    else:  # known once a group page was read: the flag is cleared there when the sort holds
        sort_ignored = False if row.sorting_setting is not None else None
    try:
        return GroupInfo(
            id=row.id,
            name=(row.name or row.id)[:MAX_NAME],
            slug=row.slug if row.slug and _SLUG.match(row.slug) else None,
            url=f"https://www.facebook.com/groups/{row.id}/",
            privacy=row.privacy,
            state=row.state,
            followed=followed,
            listed=row.id in listed,
            sort_ignored=sort_ignored,
        )
    except ValidationError:
        log.warning("group %r does not fit the wire; left out of the snapshot", row.id[:32])
        return None


def _items_response(answer: Answer) -> ItemsResponse | None:
    try:
        return ItemsResponse.model_validate_json(answer.body)
    except ValidationError:
        return None


def clean_reason(reason: str) -> str:
    """A failure's reason as ``uplink_last`` keeps it: printable ASCII, bounded."""
    text = "".join(c if c.isascii() and c.isprintable() else "?" for c in reason)
    return text[:MAX_REASON]


def _error_text(answer: Answer) -> str:
    """The brain's ``error`` field, bounded (it never echoes input, by contract)."""
    return answer.body[:300].decode("utf-8", errors="replace")


# -- `fbcollector uplink` -----------------------------------------------------------------------


async def uplink_lines(settings: Settings, store: Store, now: float) -> list[str]:
    """What ``fbcollector uplink`` prints (the operator door's ``uplink``): the mode, where the
    brain is (host and port), whether a token is set, the collector id, the lease, the last
    answer and the last cycle, what waits, and the refusal counters.

    Read from the settings and the database only. No group or post id, no post text, nothing of
    the token (not even its length): the door's output leaves the collector's account.
    """
    zone = settings.zone
    lines = [f"mode: {'local (phase 0)' if settings.local_mode else 'uplink'}"]
    if settings.ingest_url is None:
        lines.append("brain: none configured (FBC_INGEST_URL unset)")
    else:
        parts = urlsplit(settings.ingest_url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        where = f"brain: {parts.hostname}:{port}"
        if settings.ingest_socket is not None:
            where += f" through the socket {settings.ingest_socket}"
        lines.append(where)
    lines.append(f"token: {'set' if settings.ingest_token is not None else 'unset'}")
    named = "collector_id" in settings.model_fields_set
    lines.append(f"collector id: {settings.collector_id}{'' if named else ' (the default)'}")
    state = ControlState.from_json(await store.kv_get(KV_CONTROL))
    lines.append(_lease_line(state, settings.collector_id, now))
    lines.append(f"last control answer: {_when(state.polled_at, now, zone)}")
    lines.append(_cycle_line(await store.kv_get_json(KV_LAST), now, zone))
    lines.append(
        f"waiting: {await store.outbox_depth()} item(s) in the outbox, "
        f"{await store.events_depth()} event(s)"
    )
    snapshot = "waiting" if await store.kv_get(KV_SNAPSHOT) is not None else "none waiting"
    introduced = "yes" if await store.kv_get(KV_INTRODUCED) is not None else "not yet"
    lines.append(f"groups snapshot: {snapshot}; groups introduced to the brain: {introduced}")
    refused = [
        f"{endpoint} {await store.kv_int(KV_REFUSED + endpoint)}" for endpoint in UPLINK_ENDPOINTS
    ]
    lines.append(
        f"refused by the brain: {', '.join(refused)}; items rejected "
        f"{await store.kv_int(KV_OUTBOX_REJECTED)}, events rejected "
        f"{await store.kv_int(KV_EVENTS_REJECTED)}"
    )
    return lines


def _lease_line(state: ControlState, collector_id: str, now: float) -> str:
    if state.polled_at is None:
        return "lease: no answer from the brain yet"
    if not (state.lease_granted and state.lease_holder == collector_id):
        return f"lease: not held (holder: {state.lease_holder or 'nobody'})"
    text = f"lease: held by {collector_id}, generation {state.lease_generation}"
    if state.lease_at is not None:
        age = max(0.0, now - state.lease_at)
        text += f", granted {_span(age)} ago"
        if state.lease_valid_for is not None:
            left = state.lease_valid_for - age
            text += f", lapses in {_span(left)}" if left > 0 else ", LAPSED"
    return text


def _cycle_line(last: Any, now: float, zone: dt.tzinfo) -> str:
    if not isinstance(last, dict) or not isinstance(last.get("at"), int | float):
        return "last cycle: none recorded"
    text = f"last cycle: {_when(float(last['at']), now, zone)}, "
    if last.get("ok"):
        return text + "ok"
    text += f"failed: {clean_reason(str(last.get('reason') or 'unknown'))}"
    since = last.get("down_since")
    if isinstance(since, int | float):
        text += f"; the brain not usable since {_when(float(since), now, zone)}"
    return text


def _when(at: float | None, now: float, zone: dt.tzinfo) -> str:
    if at is None:
        return "never"
    stamp = dt.datetime.fromtimestamp(at, zone).strftime("%Y-%m-%d %H:%M:%S")
    return f"{stamp} ({_span(max(0.0, now - at))} ago)"


def _span(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:.0f} s"
    if seconds < 2 * 3600:
        return f"{seconds / 60:.0f} min"
    if seconds < 2 * 86400:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} d"
