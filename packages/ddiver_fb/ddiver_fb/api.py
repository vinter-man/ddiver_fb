"""The ingest API's endpoints, apart from HTTP: validated JSON in, a status code and JSON out.

Kept separate from the transport (``http.py``) so every rule about *what* is answered can be
tested without a socket, and the socket tests only have to prove the transport's own limits.

The answers follow invariant 5 (a post is durable before it is acknowledged):

- ``POST /v1/items`` answers per item — ``stored``, ``duplicate``, ``not_monitored`` or
  ``rejected(reason)`` — and the collector deletes every item that got one. An invalid item is
  rejected on its own, never with its whole batch, or the collector would resend the batch for
  ever. A busy database stops the batch: the answer is **503** and carries the results of the
  items already processed; the rest have no result, so the collector keeps them and resends,
  and whatever it resends twice comes back ``duplicate``.
- Only an error that belongs to the item rejects it: building its notification from its own
  content failed (:class:`~ddiver_fb.source.PoisonItem`). Anything else — a storage error, a
  connection closed by a shutdown, a bug not yet pinned on the item — is answered like a busy
  database, 503 with the results so far, so the collector keeps the post; the first version
  rejected those too, and the collector deleted posts the brain had never processed. A post
  that keeps failing for reasons other than storage is rejected after ``MAX_STRIKES`` attempts,
  so one poison post cannot wedge the outbox behind it for good. Logs carry ids, never text.
- ``POST /v1/events`` answers ``acked`` only after every event is stored.

Every answer is a ``fbwire`` model (``ItemsResponse``, ``GroupsResponse``, ``StatusResponse``,
``EventsResponse``, ``ErrorResponse``), written in the version of the request it answers
(``fbwire.answer_version``; a control poll names its version as ``?v=``): after a wire bump the
brain deploys first, and every collector still on the older version must read what it is told.
A status or snapshot older than one already applied is answered ``stale`` with 200: it was
received, and resending it would change nothing.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic import BaseModel, TypeAdapter, ValidationError

from ddiver_core.timeutil import Clock, utcnow
from ddiver_fb.control import ControlDesk
from ddiver_fb.monitor import CollectorMonitor
from ddiver_fb.source import FacebookSource, PoisonItem
from ddiver_fb.store import FbStore
from fbwire import (
    ErrorResponse,
    EventsRequest,
    EventsResponse,
    GroupsResponse,
    GroupsSnapshot,
    Item,
    ItemOutcome,
    ItemResult,
    ItemsResponse,
    Status,
    StatusResponse,
    answer_version,
    describe_validation,
    parse_items_request,
)
from fbwire.models import CollectorId, Incarnation

log = logging.getLogger(__name__)

RETRY_AFTER: Final = 30
"""Seconds a collector should wait after a 503; its own poll period is about this long."""
MAX_STRIKES: Final = 5
"""Attempts before an item that fails for no reason of storage's is given up as poison."""
STRIKES_KEPT: Final = 1024

_COLLECTOR_ID: Final = TypeAdapter(CollectorId)
_INCARNATION: Final[TypeAdapter[str | None]] = TypeAdapter(Incarnation | None)


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: BaseModel | Mapping[str, Any]
    retry_after: int | None = None


@dataclass(slots=True)
class Strikes:
    """Failed attempts per item, bounded (oldest forgotten): the memory of a poison post."""

    counts: OrderedDict[tuple[str, str], int] = field(default_factory=OrderedDict)

    def note(self, key: tuple[str, str]) -> int:
        count = self.counts.pop(key, 0) + 1
        self.counts[key] = count
        while len(self.counts) > STRIKES_KEPT:
            self.counts.popitem(last=False)
        return count

    def clear(self, key: tuple[str, str]) -> None:
        self.counts.pop(key, None)


@dataclass(slots=True)
class ApiMetrics:
    requests: dict[str, int] = field(default_factory=dict)
    busy: int = 0
    invalid: int = 0
    item_errors: int = 0
    unkeyed_items: int = 0

    def note(self, endpoint: str, status: int) -> None:
        key = f"{endpoint}:{status}"
        self.requests[key] = self.requests.get(key, 0) + 1


def error(status: int, message: str, v: int | None = None) -> Response:
    return Response(status, ErrorResponse(v=answer_version(v), error=message))


def _invalid(exc: Exception, v: int) -> Response:
    if isinstance(exc, ValidationError):
        return error(422, describe_validation(exc), v)
    return error(422, str(exc)[:300] or "invalid", v)


def _version_of(data: Any) -> int:
    """The version to answer ``data`` in (its own ``v`` when this side speaks it)."""
    return answer_version(data.get("v") if isinstance(data, Mapping) else None)


class IngestApi:
    def __init__(
        self,
        source: FacebookSource,
        monitor: CollectorMonitor,
        desk: ControlDesk,
        fb: FbStore,
        *,
        clock: Clock = utcnow,
    ) -> None:
        self._source = source
        self._monitor = monitor
        self._desk = desk
        self._fb = fb
        self._clock = clock
        self.metrics = ApiMetrics()
        self.strikes = Strikes()
        self.closing = False
        """Set when shutdown begins: every item from then on is answered 503, never rejected,
        because what fails now fails for the shutdown's sake, not the post's."""

    def _now(self) -> int:
        return int(self._clock().timestamp())

    def stopping(self) -> bool:
        # A method, not the attribute: shutdown sets it while an item is being awaited.
        return self.closing

    async def items(self, data: Any) -> Response:
        v = _version_of(data)
        try:
            parsed = parse_items_request(data)
        except (ValidationError, ValueError) as exc:
            self.metrics.invalid += 1
            return _invalid(exc, v)
        await self._fb.touch_collector(parsed.collector_id, now=self._now())
        results: list[ItemResult] = list(parsed.rejected)
        self._source.metrics.count(ItemOutcome.REJECTED, len(parsed.rejected))
        if parsed.unkeyed:
            self.metrics.unkeyed_items += parsed.unkeyed
            log.warning("%d items without usable ids were dropped", parsed.unkeyed)
        total = len(parsed.items) + len(parsed.rejected)
        for item in parsed.items:
            if self.stopping():
                return self._retry_later(results, total, "the brain is stopping", v)
            try:
                outcome = await self._source.process(item)
            except PoisonItem:
                self.metrics.item_errors += 1
                log.exception("item %s/%s cannot be processed", item.group_id, item.post_id)
                results.append(self._rejected(item, "brain: cannot process this post"))
                continue
            except sqlite3.Error:
                return self._retry_later(results, total, "database busy or failing", v)
            except Exception:
                if self.stopping():
                    return self._retry_later(results, total, "the brain is stopping", v)
                self.metrics.item_errors += 1
                strikes = self.strikes.note(item.key)
                log.exception(
                    "item %s/%s failed (attempt %d of %d)",
                    item.group_id,
                    item.post_id,
                    strikes,
                    MAX_STRIKES,
                )
                if strikes < MAX_STRIKES:
                    return self._retry_later(results, total, "an item failed; retry", v)
                self.strikes.clear(item.key)
                results.append(self._rejected(item, "brain: internal error"))
                continue
            self.strikes.clear(item.key)
            results.append(ItemResult(group_id=item.group_id, post_id=item.post_id, result=outcome))
        return Response(200, ItemsResponse(v=v, results=tuple(results)))

    def _retry_later(self, results: list[ItemResult], total: int, why: str, v: int) -> Response:
        """503 with the results so far: the collector keeps (and resends) the rest."""
        self.metrics.busy += 1
        log.warning("%s after %d of %d items; the rest will be resent", why, len(results), total)
        return Response(503, ItemsResponse(v=v, results=tuple(results)), RETRY_AFTER)

    def _rejected(self, item: Item, reason: str) -> ItemResult:
        self._source.metrics.count(ItemOutcome.REJECTED)
        return ItemResult(
            group_id=item.group_id, post_id=item.post_id, result=ItemOutcome.REJECTED, reason=reason
        )

    async def groups(self, data: Any) -> Response:
        v = _version_of(data)
        try:
            snapshot = GroupsSnapshot.model_validate(data)
        except ValidationError as exc:
            self.metrics.invalid += 1
            return _invalid(exc, v)
        report = await self._monitor.on_snapshot(snapshot)
        if report is None:
            return error(409, "this collector does not hold the lease", v)
        return Response(
            200,
            GroupsResponse(
                v=v, groups=report.total, changed=len(report.changes), stale=report.stale
            ),
        )

    async def status(self, data: Any) -> Response:
        v = _version_of(data)
        try:
            status = Status.model_validate(data)
        except ValidationError as exc:
            self.metrics.invalid += 1
            return _invalid(exc, v)
        applied = await self._monitor.on_status(status)
        return Response(200, StatusResponse(v=v, stale=not applied))

    async def events(self, data: Any) -> Response:
        v = _version_of(data)
        try:
            request = EventsRequest.model_validate(data)
        except ValidationError as exc:
            self.metrics.invalid += 1
            return _invalid(exc, v)
        acked = await self._monitor.on_events(request)
        return Response(200, EventsResponse(v=v, acked=tuple(acked)))

    async def control(self, query: Mapping[str, str]) -> Response:
        raw_v = query.get("v", "")
        v = answer_version(int(raw_v) if raw_v.isascii() and raw_v.isdigit() else None)
        try:
            collector_id = _COLLECTOR_ID.validate_python(query.get("collector_id"))
        except ValidationError:
            self.metrics.invalid += 1
            return error(422, "collector_id: a query parameter like ?collector_id=home-1", v)
        try:
            # The collector's state database: the lease is bound to it (``None``: an older
            # collector, which sends none).
            incarnation = _INCARNATION.validate_python(query.get("incarnation"))
        except ValidationError:
            self.metrics.invalid += 1
            return error(422, "incarnation: 8 to 64 of A-Z, a-z, 0-9, _ and -", v)
        answer = await self._desk.control_for(collector_id, incarnation)
        self._monitor.nudge()  # a move or a contender to report: after this answer, not before
        return Response(200, answer.model_copy(update={"v": v}))
