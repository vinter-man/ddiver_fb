"""The ingest server: a small hand-rolled HTTP/1.1 endpoint for the collector, and nothing else.

Hand-rolled like ddiver-core's ``MetricsServer``, for the same reasons: the surface is five
routes, one client and one content type, and a framework would bring more code than this file
and a dependency's worth of behaviour nobody here chose. What it enforces, in this order, before
any byte of a body is read:

0. at most ``max_connections`` connections open at all (reading a head, waiting, lingering);
   one more is closed at once, without an answer or a task left behind;
1. the head (request line and headers) arrives within ``head_timeout`` and fits 16 KiB, with at
   most 64 header lines — a slow or endless head is dropped, not waited on;
2. ``Authorization: Bearer <token>``, compared in constant time (``hmac.compare_digest``);
3. a known route and its method;
4. at most ``max_concurrent`` authenticated requests in flight — a burst gets 503, not a queue
   of megabytes. Only now is a slot taken: clients that never authenticate cannot hold them;
5. a body only as ``Content-Length`` (no chunked encoding), at most 1 MiB, ``application/json``.

Every answer is JSON and closes the connection. The container binds ``0.0.0.0:8781`` and compose
publishes it on ``127.0.0.1`` only, where the collector's ssh tunnel ends (D6).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel

from ddiver_fb.api import IngestApi, Response, error
from fbwire import MAX_BODY_BYTES

log = logging.getLogger(__name__)

MAX_HEAD_BYTES: Final = 16 * 1024
MAX_HEADERS: Final = 64
READ_TIMEOUT: Final = 10.0
HEAD_TIMEOUT: Final = 5.0
"""Shorter than the body's: a head is a few hundred bytes, and a client dripping one is either
broken or holding a connection on purpose."""
MAX_CONCURRENT: Final = 8
MAX_CONNECTIONS: Final = 64
BIND_BACKLOG: Final = 16
MAX_QUERY_FIELDS: Final = 8
LINGER_SECONDS: Final = 1.0
LINGER_BYTES: Final = 2 * MAX_BODY_BYTES
"""What a refused request may still push at us before we stop listening: a legitimate body is
at most ``MAX_BODY_BYTES``; past this (or past the time) the rest is not worth the wait."""
STOP_TIMEOUT: Final = 5.0
CANCEL_TIMEOUT: Final = 1.0

ROUTES: Final[Mapping[str, str]] = {
    "/v1/items": "POST",
    "/v1/groups": "POST",
    "/v1/status": "POST",
    "/v1/events": "POST",
    "/v1/control": "GET",
}

REASONS: Final[Mapping[int, str]] = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    411: "Length Required",
    413: "Content Too Large",
    415: "Unsupported Media Type",
    422: "Unprocessable Content",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    503: "Service Unavailable",
    505: "HTTP Version Not Supported",
}

_SINGLE: Final = frozenset({"authorization", "content-length", "content-type", "transfer-encoding"})


class _Reject(Exception):  # noqa: N818 - an answer, not an error condition
    def __init__(self, response: Response, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(response.status)
        self.response = response
        self.headers = dict(headers or {})


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]


@dataclass(slots=True)
class ServerMetrics:
    requests: int = 0
    unauthorized: int = 0
    rejected: dict[int, int] = field(default_factory=dict)
    overloaded: int = 0
    dropped: int = 0
    """Connections closed at once because ``max_connections`` were already open."""


def parse_head(head: bytes) -> Request:
    """Parse a request head (without the final blank line). Raises ``_Reject`` on anything off."""
    lines = head.split(b"\r\n")
    if len(lines) - 1 > MAX_HEADERS:
        raise _Reject(error(431, "too many header lines"))
    try:
        method, target, version = lines[0].decode("ascii").split(" ")
    except (UnicodeDecodeError, ValueError) as exc:
        raise _Reject(error(400, "malformed request line")) from exc
    if not version.startswith("HTTP/1."):
        raise _Reject(error(505, "HTTP/1.x only"))
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(b":")
        if not sep or not name or name != name.strip() or b" " in name:
            raise _Reject(error(400, "malformed header"))
        key = name.decode("latin-1").lower()
        if key in _SINGLE and key in headers:
            raise _Reject(error(400, f"{key} given twice"))
        headers[key] = value.decode("latin-1").strip()
    parts = urlsplit(target)
    if not parts.path.startswith("/") or parts.scheme or parts.netloc:
        raise _Reject(error(400, "origin-form targets only"))
    try:
        pairs = parse_qsl(parts.query, max_num_fields=MAX_QUERY_FIELDS, strict_parsing=False)
    except ValueError as exc:
        raise _Reject(error(400, "too many query fields")) from exc
    return Request(method=method, path=parts.path, query=dict(pairs), headers=headers)


def bearer_ok(headers: Mapping[str, str], token: bytes) -> bool:
    scheme, _, given = headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return False
    # Both sides as bytes: compare_digest on str refuses non-ASCII, which a hostile header can be.
    return hmac.compare_digest(given.strip().encode("latin-1", "replace"), token)


def _json_ok(content_type: str) -> bool:
    media, _, params = content_type.partition(";")
    if media.strip().lower() != "application/json":
        return False
    charset = params.strip().lower()
    return not charset or charset.replace(" ", "") in {"charset=utf-8", 'charset="utf-8"'}


def encode(response: Response, extra: Mapping[str, str] | None = None) -> bytes:
    body = response.body
    if isinstance(body, BaseModel):
        payload = body.model_dump_json().encode("utf-8")
    else:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    lines = [
        f"HTTP/1.1 {response.status} {REASONS.get(response.status, 'Unknown')}",
        "Content-Type: application/json; charset=utf-8",
        f"Content-Length: {len(payload)}",
        "Cache-Control: no-store",
        "Connection: close",
    ]
    if response.retry_after is not None:
        lines.append(f"Retry-After: {response.retry_after}")
    lines.extend(f"{name}: {value}" for name, value in (extra or {}).items())
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + payload


class IngestServer:
    def __init__(
        self,
        api: IngestApi,
        *,
        host: str,
        port: int,
        token: str,
        max_body: int = MAX_BODY_BYTES,
        timeout: float = READ_TIMEOUT,
        head_timeout: float = HEAD_TIMEOUT,
        max_concurrent: int = MAX_CONCURRENT,
        max_connections: int = MAX_CONNECTIONS,
    ) -> None:
        self._api = api
        self._host = host
        self._port = port
        self._token = token.encode("latin-1")
        self._max_body = max_body
        self._timeout = timeout
        self._head_timeout = min(head_timeout, timeout)
        self._slots = asyncio.Semaphore(max_concurrent)
        self._max_connections = max_connections
        self._handlers: set[asyncio.Task[None]] = set()
        self._server: asyncio.Server | None = None
        self._auth_warned = False
        self.metrics = ServerMetrics()
        self._routes: dict[str, Callable[[Request, Any], Awaitable[Response]]] = {
            "/v1/items": lambda _r, data: api.items(data),
            "/v1/groups": lambda _r, data: api.groups(data),
            "/v1/status": lambda _r, data: api.status(data),
            "/v1/events": lambda _r, data: api.events(data),
            "/v1/control": lambda r, _data: api.control(r.query),
        }

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        if self._server is None or not self._server.sockets:
            return self._port
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def serving(self) -> bool:
        return self._server is not None and self._server.is_serving()

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self._host, self._port, backlog=BIND_BACKLOG, limit=MAX_HEAD_BYTES
        )
        log.info("ingest on http://%s:%d/v1/", self._host, self.port)

    async def stop(self) -> None:
        """Stop accepting, then give requests in flight a bounded time to finish.

        ``wait_closed`` waits for every open connection, and one stuck on a locked database
        would otherwise hold the shutdown until the container is killed. What is cut off here
        was not acknowledged, so the collector resends it.
        """
        if self._server is None:
            return
        server, self._server = self._server, None
        server.close()
        try:
            async with asyncio.timeout(STOP_TIMEOUT):
                await server.wait_closed()
        except TimeoutError:
            # Cancelled, not left running: the database closes next, and a handler that
            # outlived it would answer "rejected" for posts it could no longer store.
            stuck = [task for task in self._handlers if not task.done()]
            log.warning(
                "cancelling %d ingest requests still running after %.0fs", len(stuck), STOP_TIMEOUT
            )
            for task in stuck:
                task.cancel()
            if stuck:
                await asyncio.wait(stuck, timeout=CANCEL_TIMEOUT)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.metrics.requests += 1
        if len(self._handlers) >= self._max_connections:
            self.metrics.dropped += 1
            writer.transport.abort()  # no answer, no linger: a flood gets nothing to hold on to
            return
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        try:
            response, extra = await self._answer(reader)
            if response.status >= 400:
                self.metrics.rejected[response.status] = (
                    self.metrics.rejected.get(response.status, 0) + 1
                )
            writer.write(encode(response, extra))
            await writer.drain()
            await self._linger(reader, writer)
        except (ConnectionError, TimeoutError, asyncio.IncompleteReadError):
            pass  # a client that hung up or stalled gets nothing more
        except Exception:
            log.warning("ingest request failed", exc_info=True)
        finally:
            if task is not None:
                self._handlers.discard(task)
            writer.close()
            with contextlib.suppress(ConnectionError, TimeoutError):
                await writer.wait_closed()

    async def _linger(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Half-close, and swallow what the client is still sending, briefly and boundedly.

        An answer can go out before the request was read in full: a 413 for a body left unread,
        a 503 when no slot was free. Closing a socket with unread input makes the kernel answer
        with a reset, and a reset can destroy the answer in the client's buffer before it is
        read — the collector would see a transport error where it was told "too large".
        """
        if writer.can_write_eof():
            writer.write_eof()
        left = LINGER_BYTES
        with contextlib.suppress(TimeoutError, ConnectionError):
            async with asyncio.timeout(LINGER_SECONDS):
                while left > 0:
                    chunk = await reader.read(min(64 * 1024, left))
                    if not chunk:
                        return
                    left -= len(chunk)

    async def _answer(self, reader: asyncio.StreamReader) -> tuple[Response, dict[str, str]]:
        try:
            request = await self._read_head(reader)
            self._check(request)
        except _Reject as rejected:
            return rejected.response, rejected.headers
        if self._slots.locked():
            self.metrics.overloaded += 1
            return error(503, "busy"), {"Retry-After": "5"}
        async with self._slots:
            try:
                data = await self._read_body(reader, request)
            except _Reject as rejected:
                return rejected.response, rejected.headers
            handler = self._routes[request.path]
            try:
                return await handler(request, data), {}
            except sqlite3.OperationalError:
                log.warning("database busy on %s; answered 503", request.path)
                return error(503, "database busy"), {"Retry-After": "30"}
            except Exception:
                log.exception("ingest handler for %s failed", request.path)
                return error(500, "internal error"), {}

    async def _read_head(self, reader: asyncio.StreamReader) -> Request:
        try:
            async with asyncio.timeout(self._head_timeout):
                head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.LimitOverrunError as exc:
            raise _Reject(error(431, "request head too large")) from exc
        return parse_head(head[:-4])

    def _check(self, request: Request) -> None:
        if not bearer_ok(request.headers, self._token):
            self.metrics.unauthorized += 1
            if not self._auth_warned:
                # Once per process: a misconfigured collector retries every minute, and one
                # line says everything the next thousand would.
                log.warning("ingest request without a valid bearer token (further ones counted)")
                self._auth_warned = True
            raise _Reject(error(401, "unauthorized"), {"WWW-Authenticate": "Bearer"})
        method = ROUTES.get(request.path)
        if method is None:
            raise _Reject(error(404, "no such endpoint"))
        if request.method != method:
            raise _Reject(error(405, f"use {method}"), {"Allow": method})

    async def _read_body(self, reader: asyncio.StreamReader, request: Request) -> Any:
        headers = request.headers
        if "transfer-encoding" in headers:
            raise _Reject(error(411, "send Content-Length, not a chunked body"))
        raw_length = headers.get("content-length")
        if request.method == "GET":
            if raw_length not in (None, "0"):
                raise _Reject(error(400, "GET takes no body"))
            return None
        if raw_length is None:
            raise _Reject(error(411, "Content-Length required"))
        if not raw_length.isascii() or not raw_length.isdigit():
            raise _Reject(error(400, "bad Content-Length"))
        if len(raw_length.lstrip("0")) > len(str(self._max_body)):
            # Before int(): a length of thousands of digits makes int() itself refuse (and a
            # long one costs time), and it is too large whatever it says.
            raise _Reject(error(413, f"body over {self._max_body} bytes"))
        length = int(raw_length)
        if length > self._max_body:
            raise _Reject(error(413, f"body over {self._max_body} bytes"))
        if not _json_ok(headers.get("content-type", "")):
            raise _Reject(error(415, "application/json only"))
        async with asyncio.timeout(self._timeout):
            body = await reader.readexactly(length)
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            # ValueError covers JSONDecodeError and a number too long for int(); RecursionError
            # is a megabyte of "[" — both would otherwise escape as a 500.
            raise _Reject(error(400, "body is not JSON")) from exc
