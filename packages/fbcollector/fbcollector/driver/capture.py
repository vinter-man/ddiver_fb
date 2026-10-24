"""Passive capture of what the page itself received: every ``/api/graphql/`` body, in order.

The listener is attached before the first navigation and never removed. For every GraphQL
response it starts fetching the body at once (a streamed ``@defer`` / ``@stream`` body is
evicted from Chrome's buffer soon after the page consumed it), cuts it at 4 MiB, and tags it
with ``fb_api_req_friendly_name`` from the request's form data — for the inventory and the
health counters only; the parser walks every body.

In-flight requests are tracked from ``request`` to ``requestfinished`` / ``requestfailed`` and
body fetches until they complete, so a step can wait for pagination to arrive and ``park`` can
wait for the page to settle before it navigates away (an aborted stream is not what a person's
browser does).

Nothing here imports Patchright: requests and responses are typed by the attributes used, and
the unit tests drive the buffer with plain fakes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol
from urllib.parse import parse_qs, urlsplit

from fbcollector.model import GraphqlBody

log = logging.getLogger(__name__)

GRAPHQL_PATH: Final = "/api/graphql/"
BODY_CAP: Final = 4 * 1024 * 1024
FRIENDLY_FIELD: Final = "fb_api_req_friendly_name"
FRIENDLY_HEADER: Final = "x-fb-friendly-name"
MAX_FRIENDLY_LEN: Final = 120


class RequestLike(Protocol):
    @property
    def url(self) -> str: ...

    @property
    def post_data(self) -> str | None: ...

    @property
    def headers(self) -> dict[str, str]: ...


class ResponseLike(Protocol):
    @property
    def url(self) -> str: ...

    @property
    def status(self) -> int: ...

    @property
    def request(self) -> Any: ...

    async def body(self) -> bytes: ...


def is_graphql(url: str) -> bool:
    try:
        return urlsplit(url).path == GRAPHQL_PATH
    except ValueError:
        return False


def friendly_name(request: RequestLike) -> str | None:
    """The query's friendly name, from the form body or the ``x-fb-friendly-name`` header."""
    name: str | None = None
    data = request.post_data
    if data:
        values = parse_qs(data, keep_blank_values=False).get(FRIENDLY_FIELD)
        name = values[0] if values else None
    if not name:
        headers: Mapping[str, str] = request.headers or {}
        name = next((v for k, v in headers.items() if k.lower() == FRIENDLY_HEADER), None)
    if not name:
        return None
    clean = "".join(c for c in name if c.isalnum() or c in "_.-")
    return clean[:MAX_FRIENDLY_LEN] or None


@dataclass(slots=True)
class _Pending:
    request: Any
    started: float


class CaptureBuffer:
    """Bodies captured since the last ``take()``, plus what is still in flight."""

    def __init__(self, *, now: Callable[[], float], cap: int = BODY_CAP) -> None:
        self._now = now
        self._cap = cap
        self._bodies: list[GraphqlBody] = []
        self._lost = 0
        self._inflight: dict[int, _Pending] = {}
        self._fetches: set[asyncio.Task[None]] = set()
        self._started = 0
        self._changed = asyncio.Event()

    # -- listeners (wired to the page's events by the driver) ----------------------------------

    def on_request(self, request: RequestLike) -> None:
        if is_graphql(request.url):
            self._inflight[id(request)] = _Pending(request, self._now())
            self._started += 1
            self._changed.set()

    def on_request_done(self, request: RequestLike) -> None:
        if self._inflight.pop(id(request), None) is not None:
            self._changed.set()

    def on_response(self, response: ResponseLike) -> None:
        if not is_graphql(response.url):
            return
        task = asyncio.get_running_loop().create_task(self._fetch(response))
        self._fetches.add(task)
        task.add_done_callback(self._fetches.discard)

    async def _fetch(self, response: ResponseLike) -> None:
        name = friendly_name(response.request)
        try:
            body = await response.body()
        except Exception as exc:  # noqa: BLE001 - an evicted or aborted body is counted, not fatal
            self._lost += 1
            log.debug("graphql body lost (%s): %s", name, type(exc).__name__)
        else:
            capped = len(body) > self._cap
            self._bodies.append(
                GraphqlBody(
                    friendly_name=name,
                    body=body[: self._cap],
                    status=response.status,
                    capped=capped,
                )
            )
        finally:
            self._changed.set()

    # -- the driver's view ---------------------------------------------------------------------

    @property
    def started(self) -> int:
        """GraphQL requests seen so far (a step compares before/after for ``pagination_fired``)."""
        return self._started

    @property
    def busy(self) -> bool:
        return bool(self._inflight) or bool(self._fetches)

    def take(self) -> tuple[tuple[GraphqlBody, ...], int]:
        """The bodies and the lost-body count since the last ``take`` (arrival order)."""
        bodies, lost = tuple(self._bodies), self._lost
        self._bodies.clear()
        self._lost = 0
        return bodies, lost

    def discard(self) -> None:
        self.take()
        self._inflight.clear()

    async def wait(self, condition: Callable[[], bool], timeout: float) -> bool:
        """Wait until ``condition()`` holds or ``timeout`` seconds pass; returns whether it held.

        Uses the event loop's own timer: these waits are for Chrome, which runs in real time.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while not condition():
            left = deadline - loop.time()
            if left <= 0:
                return False
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=min(left, 0.25))
            except TimeoutError:
                continue
        return True

    async def settle(self, timeout: float) -> bool:
        """Wait for every in-flight GraphQL request and body fetch to finish."""
        return await self.wait(lambda: not self.busy, timeout)
