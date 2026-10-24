"""The ingest transport over a real socket on 127.0.0.1: every limit, before any body is read."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest

from ddiver_fb.api import IngestApi, Response
from ddiver_fb.ingest import IngestServer, bearer_ok, encode, parse_head
from fbwire import ItemsResponse
from tests.brain.helpers import TOKEN, until

AUTH = f"Authorization: Bearer {TOKEN}\r\n"


class StubApi:
    """Stands in for the endpoints, so these tests are about HTTP only."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.both_in = asyncio.Event()
        self.error: BaseException | None = None
        self.cancelled = False

    async def _answer(self, name: str, data: Any) -> Response:
        self.calls.append((name, data))
        self.entered.set()
        if len(self.calls) >= 2:
            self.both_in.set()
        if self.gate is not None:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self.error is not None:
            raise self.error
        return Response(200, {"endpoint": name})

    async def items(self, data: Any) -> Response:
        return await self._answer("items", data)

    async def groups(self, data: Any) -> Response:
        return await self._answer("groups", data)

    async def status(self, data: Any) -> Response:
        return await self._answer("status", data)

    async def events(self, data: Any) -> Response:
        return await self._answer("events", data)

    async def control(self, query: Any) -> Response:
        return await self._answer("control", dict(query))


@pytest.fixture
def stub() -> StubApi:
    return StubApi()


@pytest.fixture
async def server(stub: StubApi) -> AsyncIterator[IngestServer]:
    srv = IngestServer(
        cast("IngestApi", stub),
        host="127.0.0.1",
        port=0,
        token=TOKEN,
        max_body=1024,
        timeout=0.2,
        max_concurrent=2,
    )
    await srv.start()
    yield srv
    await srv.stop()


async def exchange(server: IngestServer, raw: bytes, *, close_write: bool = False) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    try:
        writer.write(raw)
        await writer.drain()
        if close_write:
            writer.write_eof()
        async with asyncio.timeout(5):
            return await reader.read()
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


def split(answer: bytes) -> tuple[int, dict[str, str], Any]:
    head, _, body = answer.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    code = int(lines[0].split(" ")[1])
    headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
    assert int(headers["content-length"]) == len(body)
    assert headers["connection"] == "close"
    return code, headers, json.loads(body)


def post(
    path: str, body: bytes, *, headers: str = AUTH, content_type: str = "application/json"
) -> bytes:
    return (
        f"POST {path} HTTP/1.1\r\nHost: localhost\r\n{headers}"
        f"Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode() + body


class TestHappyPath:
    async def test_a_post_reaches_its_endpoint_with_the_parsed_body(
        self, server: IngestServer, stub: StubApi
    ):
        code, headers, body = split(await exchange(server, post("/v1/status", b'{"v": 1}')))
        assert (code, body) == (200, {"endpoint": "status"})
        assert headers["content-type"] == "application/json; charset=utf-8"
        assert headers["cache-control"] == "no-store"
        assert stub.calls == [("status", {"v": 1})]

    async def test_control_reads_the_query(self, server: IngestServer, stub: StubApi):
        raw = f"GET /v1/control?collector_id=home-1 HTTP/1.1\r\n{AUTH}\r\n".encode()
        code, _, _ = split(await exchange(server, raw))
        assert code == 200 and stub.calls == [("control", {"collector_id": "home-1"})]

    @pytest.mark.parametrize("path", ["/v1/items", "/v1/groups", "/v1/events"])
    async def test_every_post_route(self, server: IngestServer, stub: StubApi, path: str):
        code, _, body = split(await exchange(server, post(path, b"{}")))
        assert code == 200 and body == {"endpoint": path.rsplit("/", 1)[1]}

    async def test_a_charset_utf8_content_type_is_json_too(self, server: IngestServer):
        raw = post("/v1/status", b"{}", content_type='application/json; charset="UTF-8"')
        assert split(await exchange(server, raw))[0] == 200

    async def test_port_and_serving(self, stub: StubApi):
        srv = IngestServer(cast("IngestApi", stub), host="127.0.0.1", port=0, token=TOKEN)
        assert (srv.port, srv.serving) == (0, False)
        await srv.start()
        assert srv.port != 0
        assert srv.serving
        await srv.stop()
        await srv.stop()  # twice is fine
        assert not srv.serving


class TestAuth:
    @pytest.mark.parametrize(
        "header",
        [
            "",
            f"Authorization: Bearer {'x' * len(TOKEN)}\r\n",
            f"Authorization: Basic {TOKEN}\r\n",
            f"Authorization: Bearer {TOKEN}x\r\n",
            "Authorization: Bearer ключ\r\n",
        ],
    )
    async def test_no_valid_token_no_endpoint(
        self, server: IngestServer, stub: StubApi, header: str
    ):
        raw = post("/v1/items", b"{}", headers=header)
        code, headers, _ = split(await exchange(server, raw))
        assert code == 401 and headers["www-authenticate"] == "Bearer"
        assert stub.calls == []

    async def test_auth_comes_before_the_route(self, server: IngestServer):
        code, _, _ = split(await exchange(server, post("/admin", b"{}", headers="")))
        assert code == 401

    def test_bearer_ok_is_strict(self):
        token = TOKEN.encode()
        assert bearer_ok({"authorization": f"Bearer {TOKEN}"}, token)
        assert bearer_ok({"authorization": f"bearer  {TOKEN} "}, token)
        assert not bearer_ok({"authorization": f"Token {TOKEN}"}, token)
        assert not bearer_ok({}, token)


class TestRejections:
    async def test_unknown_route(self, server: IngestServer):
        assert split(await exchange(server, post("/v1/nope", b"{}")))[0] == 404

    async def test_wrong_method_says_which(self, server: IngestServer):
        raw = f"GET /v1/items HTTP/1.1\r\n{AUTH}\r\n".encode()
        code, headers, _ = split(await exchange(server, raw))
        assert code == 405 and headers["allow"] == "POST"

    async def test_no_length(self, server: IngestServer):
        raw = f"POST /v1/items HTTP/1.1\r\n{AUTH}Content-Type: application/json\r\n\r\n{{}}"
        assert split(await exchange(server, raw.encode()))[0] == 411

    async def test_chunked(self, server: IngestServer):
        raw = (
            f"POST /v1/items HTTP/1.1\r\n{AUTH}Content-Type: application/json\r\n"
            "Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n"
        )
        assert split(await exchange(server, raw.encode()))[0] == 411

    async def test_too_large_is_refused_before_the_body_is_read(
        self, server: IngestServer, stub: StubApi
    ):
        raw = (
            f"POST /v1/items HTTP/1.1\r\n{AUTH}Content-Type: application/json\r\n"
            "Content-Length: 1025\r\n\r\n"
        ).encode()  # and no body at all: the answer must not wait for it
        assert split(await exchange(server, raw))[0] == 413
        assert stub.calls == []

    @pytest.mark.parametrize("length", ["-1", "1e3", "١٢", ""])
    async def test_a_bad_length(self, server: IngestServer, length: str):
        raw = (
            f"POST /v1/items HTTP/1.1\r\n{AUTH}Content-Type: application/json\r\n"
            f"Content-Length: {length}\r\n\r\n"
        ).encode()
        assert split(await exchange(server, raw))[0] == 400

    @pytest.mark.parametrize(
        "content_type", ["text/plain", "application/json; charset=latin-1", "", "application/xml"]
    )
    async def test_json_only(self, server: IngestServer, content_type: str):
        raw = post("/v1/items", b"{}", content_type=content_type)
        assert split(await exchange(server, raw))[0] == 415

    @pytest.mark.parametrize(
        "body",
        [b"{not json", b"\xff\xfe", b"[" * 1000, b"1" * 5000],
        ids=["garbage", "not-utf8", "deep-nesting", "5000-digit-number"],
    )
    async def test_not_json(self, stub: StubApi, body: bytes):
        srv = IngestServer(
            cast("IngestApi", stub), host="127.0.0.1", port=0, token=TOKEN, max_body=10_000
        )
        await srv.start()
        try:
            assert split(await exchange(srv, post("/v1/items", body)))[0] == 400
        finally:
            await srv.stop()
        assert stub.calls == []

    async def test_a_get_with_a_body(self, server: IngestServer):
        raw = f"GET /v1/control?collector_id=a HTTP/1.1\r\n{AUTH}Content-Length: 2\r\n\r\n{{}}"
        assert split(await exchange(server, raw.encode()))[0] == 400

    @pytest.mark.parametrize(
        ("head", "code"),
        [
            (f"POST /v1/items\r\n{AUTH}", 400),
            (f"POST /v1/items HTTP/2.0\r\n{AUTH}", 505),
            (f"POST http://evil/v1/items HTTP/1.1\r\n{AUTH}", 400),
            (f"POST /v1/items HTTP/1.1\r\n{AUTH}Content-Length: 2\r\nContent-Length: 2\r\n", 400),
            (f"POST /v1/items HTTP/1.1\r\n{AUTH}Bad Header: x\r\n", 400),
            (f"POST /v1/items HTTP/1.1\r\n{AUTH}NoColon\r\n", 400),
            (f"GET /v1/control?{'&'.join(f'a{n}=1' for n in range(20))} HTTP/1.1\r\n{AUTH}", 400),
        ],
    )
    async def test_malformed_heads(self, server: IngestServer, head: str, code: int):
        assert split(await exchange(server, f"{head}\r\n".encode()))[0] == code

    async def test_a_huge_head(self, server: IngestServer):
        raw = f"GET /v1/control HTTP/1.1\r\n{AUTH}X-Pad: {'a' * 20_000}\r\n\r\n".encode()
        assert split(await exchange(server, raw))[0] == 431

    async def test_too_many_header_lines(self, server: IngestServer):
        lines = "".join(f"X-{n}: 1\r\n" for n in range(70))
        raw = f"GET /v1/control HTTP/1.1\r\n{AUTH}{lines}\r\n".encode()
        assert split(await exchange(server, raw))[0] == 431


class TestStallsAndLoad:
    async def test_a_head_that_never_ends_is_dropped(self, server: IngestServer):
        answer = await exchange(server, b"GET /v1/control HTTP/1.1\r\nX-Slow: ")
        assert answer == b""  # the 0.2 s head timeout closed it without an answer

    async def test_a_body_that_stops_short_is_dropped(self, server: IngestServer, stub):
        raw = (
            f"POST /v1/items HTTP/1.1\r\n{AUTH}Content-Type: application/json\r\n"
            "Content-Length: 100\r\n\r\n{"
        ).encode()
        assert await exchange(server, raw, close_write=True) == b""
        assert stub.calls == []

    async def test_a_burst_gets_503_instead_of_a_queue(self, server: IngestServer, stub):
        stub.gate = gate = asyncio.Event()
        try:
            first = asyncio.create_task(exchange(server, post("/v1/status", b"{}")))
            second = asyncio.create_task(exchange(server, post("/v1/status", b"{}")))
            async with asyncio.timeout(5):
                await stub.both_in.wait()  # both slots taken, both handlers parked on the gate
            code, headers, _ = split(await exchange(server, post("/v1/status", b"{}")))
            assert code == 503 and headers["retry-after"] == "5"
        finally:
            gate.set()  # never leave handlers parked: stop() would wait on them
        assert split(await first)[0] == split(await second)[0] == 200
        assert server.metrics.overloaded == 1

    async def test_a_refused_body_does_not_cost_the_client_its_answer(self, stub: StubApi):
        """A 413 goes out before the body is read; the client is still sending when it does."""
        srv = IngestServer(
            cast("IngestApi", stub), host="127.0.0.1", port=0, token=TOKEN, max_body=10
        )
        await srv.start()
        try:
            answer = await exchange(srv, post("/v1/items", b"x" * 200_000))
        finally:
            await srv.stop()
        assert split(answer)[0] == 413

    async def test_stop_cancels_a_stuck_request_instead_of_leaving_it(
        self, stub: StubApi, monkeypatch: pytest.MonkeyPatch
    ):
        """C1-02: a handler left running would answer after the database closed, and the items
        it had not stored yet would come back "rejected" — deleted by the collector."""
        import ddiver_fb.ingest as ingest_module

        monkeypatch.setattr(ingest_module, "STOP_TIMEOUT", 0.05)
        srv = IngestServer(cast("IngestApi", stub), host="127.0.0.1", port=0, token=TOKEN)
        await srv.start()
        stub.gate = gate = asyncio.Event()
        stuck = asyncio.create_task(exchange(srv, post("/v1/status", b"{}")))
        try:
            async with asyncio.timeout(5):
                await stub.entered.wait()
            await srv.stop()  # returns although the request was still parked
            assert not srv.serving
            assert stub.cancelled
        finally:
            gate.set()
        assert await stuck == b""  # no answer at all: the collector keeps what it sent

    async def test_a_busy_database_is_503_and_a_bug_is_500(self, server: IngestServer, stub):
        stub.error = sqlite3.OperationalError("database is locked")
        code, headers, _ = split(await exchange(server, post("/v1/status", b"{}")))
        assert code == 503 and headers["retry-after"] == "30"
        stub.error = RuntimeError("bug")
        assert split(await exchange(server, post("/v1/status", b"{}")))[0] == 500
        assert server.metrics.rejected == {503: 1, 500: 1}


class TestUnauthenticatedLoad:
    """Limits that clients without the token could use up."""

    async def test_slow_unauthenticated_heads_do_not_take_the_slots(
        self, server: IngestServer, stub: StubApi
    ):
        slow = [await asyncio.open_connection("127.0.0.1", server.port) for _ in range(3)]
        try:
            for _, writer in slow:
                writer.write(b"POST /v1/items HTTP/1.1\r\nX-Slow: ")
                await writer.drain()
            await until(lambda: len(server._handlers) == 3)
            # more than max_concurrent (2) such clients, and the collector is still served
            code, _, _ = split(await exchange(server, post("/v1/status", b"{}")))
            assert code == 200 and stub.calls == [("status", {})]
        finally:
            for _, writer in slow:
                writer.close()

    async def test_past_the_connection_cap_a_connection_is_closed_at_once(self, stub: StubApi):
        srv = IngestServer(
            cast("IngestApi", stub),
            host="127.0.0.1",
            port=0,
            token=TOKEN,
            timeout=5,
            max_connections=2,
        )
        await srv.start()
        slow = [await asyncio.open_connection("127.0.0.1", srv.port) for _ in range(2)]
        try:
            await until(lambda: len(srv._handlers) == 2)
            try:
                answer = await exchange(srv, post("/v1/status", b"{}"))
            except ConnectionError:
                answer = b""
            assert answer == b"" and srv.metrics.dropped == 1
            assert stub.calls == []
        finally:
            for _, writer in slow:
                writer.close()
            await srv.stop()

    async def test_the_head_has_a_shorter_deadline_than_the_body(self, stub: StubApi):
        srv = IngestServer(
            cast("IngestApi", stub), host="127.0.0.1", port=0, token=TOKEN, head_timeout=0.05
        )
        assert srv._head_timeout == 0.05 and srv._timeout == 10.0
        await srv.start()
        try:
            assert await exchange(srv, b"GET /v1/control HTTP/1.1\r\nX-Slow: ") == b""
        finally:
            await srv.stop()

    async def test_a_content_length_of_thousands_of_digits_is_413_not_a_crash(
        self, server: IngestServer
    ):
        raw = (
            f"POST /v1/items HTTP/1.1\r\n{AUTH}Content-Type: application/json\r\n"
            f"Content-Length: {'9' * 5000}\r\n\r\n"
        ).encode()
        assert split(await exchange(server, raw))[0] == 413
        leading_zeros = (
            f"POST /v1/status HTTP/1.1\r\n{AUTH}Content-Type: application/json\r\n"
            "Content-Length: 00000000000002\r\n\r\n{}"
        ).encode()
        assert split(await exchange(server, leading_zeros))[0] == 200


class TestPieces:
    def test_parse_head_lowercases_names_and_keeps_values(self):
        request = parse_head(b"GET /v1/control?collector_id=x HTTP/1.1\r\nX-Thing:  a b ")
        assert request.headers == {"x-thing": "a b"}
        assert request.query == {"collector_id": "x"} and request.path == "/v1/control"

    def test_encode_a_model_and_a_retry(self):
        raw = encode(Response(503, ItemsResponse(), retry_after=30), {"X-Extra": "1"})
        head, _, body = raw.partition(b"\r\n\r\n")
        assert b"Retry-After: 30" in head and b"X-Extra: 1" in head
        assert json.loads(body) == {"v": 1, "results": []}
