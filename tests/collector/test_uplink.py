"""The uplink against a scripted brain (``httpx.MockTransport``): what is sent, what is kept.

Nothing here opens a socket. The brain is a handler that answers like the ingest API by default
and can be told to answer anything else, once, per route; time is the collector's ``FakeClock``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from fbcollector import uplink as uplink_module
from fbcollector.alerts import AlertResult
from fbcollector.clock import FakeClock
from fbcollector.control import ControlCenter
from fbcollector.ladder import Ladder
from fbcollector.model import GroupMeta, PageState, Sighting, Surface
from fbcollector.scheduler import Cadence
from fbcollector.status import StatusBuilder
from fbcollector.store import Store
from fbcollector.sync import SyncOutcome
from fbcollector.uplink import (
    KEY_BACK,
    KEY_DOWN,
    KV_EVENTS_REJECTED,
    KV_INCARNATION,
    KV_INTRODUCED,
    KV_LAST,
    KV_SNAPSHOT,
    IngestClient,
    Uplink,
    UplinkError,
    group_info,
)
from fbwire.models import (
    MAX_BATCH_BYTES,
    Control,
    EventKind,
    GroupPrivacy,
    GroupsSnapshot,
    GroupState,
    Item,
    ItemsRequest,
    Profile,
    Status,
    Via,
)

from .pacing_support import make_settings

CID = "home-1"
TOKEN = "t" * 40
URL = "http://127.0.0.1:18781"
T0 = 1_790_000_000.0
G1, G2 = "1234567890", "2345678901"

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def reply(status: int = 200, body: Any = None, **headers: str) -> httpx.Response:
    return httpx.Response(status, json=body if body is not None else {"v": 1}, headers=headers)


class FakeBrain:
    """Answers like the brain's ingest API; ``then(path, ...)`` scripts the next answers."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.scripted: dict[str, deque[httpx.Response | Exception | Handler]] = defaultdict(deque)
        self.lease: dict[str, Any] = {"collector_id": CID, "granted": True, "generation": 1}
        self.monitored: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []
        self.paused = False

    def then(self, path: str, *answers: httpx.Response | Exception | Handler) -> None:
        self.scripted[path].extend(answers)

    def to(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]

    def bodies(self, path: str) -> list[Any]:
        return [json.loads(r.content) for r in self.to(path)]

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if self.scripted[path]:
            answer = self.scripted[path].popleft()
            if isinstance(answer, Exception):
                raise answer
            if isinstance(answer, httpx.Response):
                return answer
            return await answer(request)
        return self.default(request)

    def default(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/control":
            return reply(
                body={
                    "v": 1,
                    "issued_at": 1_790_000_000,
                    "lease": self.lease,
                    "monitored": self.monitored,
                    "commands": self.commands,
                    "paused": self.paused,
                }
            )
        body = json.loads(request.content)
        if path == "/v1/items":
            results = [
                {"group_id": i["group_id"], "post_id": i["post_id"], "result": "stored"}
                for i in body["items"]
            ]
            return reply(body={"v": 1, "results": results})
        if path == "/v1/events":
            return reply(body={"v": 1, "acked": [e["event_id"] for e in body["events"]]})
        if path == "/v1/groups":
            return reply(body={"v": 1, "ok": True, "groups": len(body["groups"]), "changed": 0})
        return reply(body={"v": 1, "ok": True, "stale": False})


class RecordingAlerts:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.results: deque[AlertResult] = deque()

    async def alert(
        self, key: str, text: str, *, critical: bool = False, dedup: float | None = None
    ) -> AlertResult:
        result = self.results.popleft() if self.results else AlertResult.SENT
        if result is AlertResult.SENT:
            self.sent.append((key, text))
        return result

    def keys(self) -> list[str]:
        return [k for k, _ in self.sent]


class StubScheduler:
    async def cadence(self) -> Cadence:
        return Cadence(Profile.CAUTIOUS, feed_max=840.0, backstop=None, backstop_only=False)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=T0)


@pytest.fixture
async def store(tmp_path: Path, clock: FakeClock) -> AsyncIterator[Store]:
    async with Store(tmp_path / "state.sqlite", clock=clock) as opened:
        yield opened


@pytest.fixture
def brain() -> FakeBrain:
    return FakeBrain()


@pytest.fixture
def alerts() -> RecordingAlerts:
    return RecordingAlerts()


@pytest.fixture
def ladder(store: Store, clock: FakeClock) -> Ladder:
    return Ladder(store, clock)


@pytest.fixture
def control(store: Store, ladder: Ladder, clock: FakeClock) -> ControlCenter:
    return ControlCenter(
        store, ladder, clock, collector_id=CID, local_mode=False, rng=random.Random(5)
    )


def make(
    store: Store,
    clock: FakeClock,
    brain: FakeBrain,
    control: ControlCenter,
    alerts: RecordingAlerts,
    **kw: Any,
) -> Uplink:
    return Uplink(
        client=IngestClient(URL, TOKEN, transport=httpx.MockTransport(brain)),
        store=store,
        control=control,
        status=StatusBuilder(
            store=store,
            scheduler=StubScheduler(),
            settings=make_settings(collector_id=CID),
            clock=clock,
            version="0.1.0",
        ),
        alerts=alerts,
        clock=clock,
        collector_id=CID,
        **kw,
    )


@pytest.fixture
def uplink(
    store: Store,
    clock: FakeClock,
    brain: FakeBrain,
    control: ControlCenter,
    alerts: RecordingAlerts,
) -> Uplink:
    return make(store, clock, brain, control, alerts)


def item(post_id: str, group_id: str = G1, text: str = "flat for rent") -> Item:
    return Item(
        collector_id=CID,
        seq=0,
        group_id=group_id,
        post_id=post_id,
        url=f"https://www.facebook.com/groups/{group_id}/posts/{post_id}/",
        group_name="Group",
        group_url=f"https://www.facebook.com/groups/{group_id}/",
        created_at=1_790_000_000,
        first_seen_at=1_790_000_100,
        via=Via.FEED,
        text=text,
    )


async def queue(store: Store, *post_ids: str, text: str = "flat for rent") -> None:
    for post_id in post_ids:
        sighting = Sighting(post_id=post_id, group_id=G1, surface=Surface.FEED)
        await store.mark_seen(sighting, via=Via.FEED, item=item(post_id, text=text))


async def outbox_keys(store: Store) -> list[str]:
    rows = await store.next_batch(CID)
    return [row.post_id for row in rows]


def synced(
    *,
    complete: bool,
    listed: int,
    header_count: int | None,
    listed_ids: tuple[str, ...] = (),
) -> SyncOutcome:
    return SyncOutcome(
        complete=complete,
        listed=listed,
        header_count=header_count,
        has_next_page=not complete,
        listed_ids=listed_ids,
    )


def results(*pairs: tuple[str, str], reason: str | None = None) -> dict[str, Any]:
    out = []
    for post_id, result in pairs:
        entry: dict[str, Any] = {"group_id": G1, "post_id": post_id, "result": result}
        if result == "rejected":
            entry["reason"] = reason or "invalid"
        out.append(entry)
    return {"v": 1, "results": out}


# -- the HTTP client ------------------------------------------------------------------------------


class TestClient:
    async def test_bearer_json_and_no_redirects(self) -> None:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(302, headers={"Location": "http://elsewhere.example/"})

        client = IngestClient(URL + "/", TOKEN, transport=httpx.MockTransport(handler))
        answer = await client.post("/v1/status", b"{}")
        assert answer.status == 302  # not followed
        request = seen[0]
        assert str(request.url) == f"{URL}/v1/status"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["content-type"] == "application/json"
        answer = await client.get("/v1/control", {"collector_id": CID})
        assert seen[1].url.params["collector_id"] == CID
        assert "content-type" not in seen[1].headers

    async def test_through_a_unix_socket_when_one_is_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[httpx.Request] = []
        made: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return reply()

        def transport(**kwargs: Any) -> httpx.AsyncBaseTransport:
            made.append(kwargs)
            return httpx.MockTransport(handler)

        monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)
        socket = "/Users/fbc/.ssh/fbtunnel.sock"
        client = IngestClient("http://127.0.0.1:8781", TOKEN, socket=socket)
        await client.post("/v1/status", b"{}")
        await client.get("/v1/control", {"collector_id": CID})
        assert made == [{"uds": socket}, {"uds": socket}]
        assert str(seen[0].url) == "http://127.0.0.1:8781/v1/status"

    @pytest.mark.parametrize("timeout", [0.0, 10.5, 60.0])
    def test_the_timeout_is_bounded(self, timeout: float) -> None:
        with pytest.raises(ValueError, match="timeout"):
            IngestClient(URL, TOKEN, timeout=timeout)

    async def test_a_transport_error_names_its_type_never_the_token(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        client = IngestClient(URL, TOKEN, transport=httpx.MockTransport(handler))
        with caplog.at_level(logging.DEBUG), pytest.raises(UplinkError) as caught:
            await client.post("/v1/items", b"{}")
        assert caught.value.reason == "transport: ConnectError"
        assert TOKEN not in caplog.text

    async def test_the_whole_request_has_a_deadline(self) -> None:
        """Round 2 B-F3: httpx's timeout is per read, and an answer trickling in never trips it."""

        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.Event().wait()  # the far side never finishes
            raise AssertionError("unreachable")

        client = IngestClient(URL, TOKEN, timeout=0.05, transport=httpx.MockTransport(handler))
        async with asyncio.timeout(5):
            with pytest.raises(UplinkError) as caught:
                await client.get("/v1/control", {"collector_id": CID})
        assert caught.value.reason == "transport: no answer in time"
        assert caught.value.ends_cycle

    async def test_retry_after_is_read_in_seconds_and_capped(self) -> None:
        answers = deque(
            [
                reply(503, **{"Retry-After": "30"}),
                reply(503, **{"Retry-After": "99999"}),
                reply(503, **{"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            ]
        )
        client = IngestClient(
            URL, TOKEN, transport=httpx.MockTransport(lambda r: answers.popleft())
        )
        assert (await client.post("/v1/items", b"{}")).retry_after == 30
        assert (await client.post("/v1/items", b"{}")).retry_after == 300
        assert (await client.post("/v1/items", b"{}")).retry_after is None


# -- items ----------------------------------------------------------------------------------------


class TestItems:
    async def test_every_answered_row_is_deleted_whatever_the_answer(
        self, uplink: Uplink, store: Store, brain: FakeBrain
    ) -> None:
        await queue(store, "11", "12", "13", "14")
        brain.then(
            "/v1/items",
            reply(
                body=results(
                    ("11", "stored"),
                    ("12", "duplicate"),
                    ("13", "not_monitored"),
                    ("14", "rejected"),
                )
            ),
        )
        assert await uplink.push_items() == 4
        assert await store.outbox_depth() == 0
        assert await store.kv_int("outbox_rejected") == 1
        (body,) = brain.bodies("/v1/items")
        request = ItemsRequest.model_validate(body)
        assert [i.post_id for i in request.items] == ["11", "12", "13", "14"]
        assert request.collector_id == CID

    async def test_a_row_without_a_result_stays_queued(
        self, uplink: Uplink, store: Store, brain: FakeBrain
    ) -> None:
        await queue(store, "11", "12")
        brain.then("/v1/items", reply(body=results(("11", "stored"))))
        brain.then("/v1/items", reply(body=results()))  # the resend: still nothing for 12
        assert await uplink.push_items() == 1
        (row,) = await store.next_batch(CID)
        assert (row.post_id, row.attempts) == ("12", 2)

    async def test_a_busy_brain_keeps_what_it_did_not_answer(
        self, uplink: Uplink, store: Store, brain: FakeBrain, clock: FakeClock
    ) -> None:
        await queue(store, "11", "12", "13")
        brain.then("/v1/items", reply(503, body=results(("11", "stored")), **{"Retry-After": "30"}))
        with pytest.raises(UplinkError, match="busy") as caught:
            await uplink.push_items()
        assert caught.value.retry_after == 30
        assert await outbox_keys(store) == ["12", "13"]
        assert await uplink.cycle()  # inside Retry-After: items are not tried again
        assert len(brain.to("/v1/items")) == 1
        clock.advance(31)
        await uplink.cycle()
        assert await store.outbox_depth() == 0
        assert len(brain.to("/v1/items")) == 2

    @pytest.mark.parametrize(
        ("answer", "reason"),
        [
            (reply(500, body={"v": 1, "error": "internal error"}), "items: HTTP 500"),
            (reply(401, body={"v": 1, "error": "unauthorized"}), "token was refused"),
            (reply(404), "no such endpoint"),
            (reply(422, body={"v": 1, "error": "v: unsupported"}), "items: HTTP 422"),
            (reply(503, body={"v": 1, "error": "busy"}), "busy"),
            (httpx.Response(200, content=b"not json"), "cannot read"),
            (httpx.ReadTimeout("slow"), "transport: ReadTimeout"),
        ],
    )
    async def test_nothing_is_deleted_without_an_answer(
        self,
        uplink: Uplink,
        store: Store,
        brain: FakeBrain,
        answer: httpx.Response | Exception,
        reason: str,
    ) -> None:
        await queue(store, "11", "12")
        brain.then("/v1/items", answer)
        with pytest.raises(UplinkError, match=reason):
            await uplink.push_items()
        rows = await store.next_batch(CID)
        assert [r.post_id for r in rows] == ["11", "12"]
        assert all(r.attempts == 1 for r in rows)
        assert TOKEN not in json.dumps([r.payload for r in rows])

    async def test_batches_are_the_bytes_that_were_planned(
        self, uplink: Uplink, store: Store, brain: FakeBrain
    ) -> None:
        text = "квартира " * 1800  # ~16 KiB of UTF-8: capped, flagged, still one post
        await queue(store, *(str(100 + n) for n in range(70)), text=text)
        assert await uplink.push_items() == 70
        requests = brain.to("/v1/items")
        assert len(requests) >= 3
        posted: list[str] = []
        for request in requests:
            assert len(request.content) <= MAX_BATCH_BYTES
            posted += [i.post_id for i in ItemsRequest.model_validate_json(request.content).items]
        assert posted == [str(100 + n) for n in range(70)]

    async def test_an_empty_outbox_sends_nothing(self, uplink: Uplink, brain: FakeBrain) -> None:
        assert await uplink.push_items() == 0
        assert brain.requests == []

    async def test_a_long_queue_goes_out_over_several_cycles_without_waiting(
        self, uplink: Uplink, store: Store, brain: FakeBrain, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(uplink_module, "MAX_ITEM_BATCHES", 1)
        await queue(store, *(str(100 + n) for n in range(40)), text="ж" * 9000)
        first = await uplink.push_items()
        assert 0 < first < 40
        assert uplink._next_delay() == 0  # more is waiting: the next cycle comes at once
        assert await uplink.push_items() == 40 - first
        await uplink.cycle()
        assert uplink._next_delay() == 60

    async def test_the_next_cycle_waits_for_retry_after(
        self, uplink: Uplink, store: Store, brain: FakeBrain
    ) -> None:
        await uplink.cycle()  # control and status: next due in 60 s
        await queue(store, "11")
        brain.then("/v1/items", reply(503, body=results(), **{"Retry-After": "20"}))
        assert not await uplink.cycle()
        assert uplink._next_delay() == 20

    async def test_every_failure_sets_when_to_try_again(
        self, uplink: Uplink, store: Store, brain: FakeBrain, clock: FakeClock
    ) -> None:
        """Round 2 B-F1: after a 503, a transport error left the 503's moment in place, and
        every cycle after it was due at once."""
        await uplink.cycle()
        await queue(store, "11")
        brain.then("/v1/items", reply(503, body=results(), **{"Retry-After": "1"}))
        assert not await uplink.cycle()
        clock.advance(2)
        for failure in (
            httpx.ConnectError("refused"),
            reply(500),
            httpx.Response(200, content=b"x"),
        ):
            brain.then("/v1/items", failure)
            assert not await uplink.cycle()
            assert uplink._items_after is not None
            wait = uplink._items_after - clock.monotonic()
            assert wait >= 60  # a moment ahead to wait for, not the 503's, already past
            clock.advance(wait)
        assert await store.outbox_depth() == 1

    async def test_a_failed_cycle_forgets_that_more_was_waiting(
        self,
        uplink: Uplink,
        store: Store,
        *,
        brain: FakeBrain,
        clock: FakeClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(uplink_module, "MAX_ITEM_BATCHES", 1)
        await queue(store, *(str(100 + n) for n in range(40)), text="ж" * 9000)
        assert await uplink.cycle()
        assert uplink._next_delay() == 0  # more is waiting
        brain.then("/v1/items", httpx.ConnectError("gone"))
        clock.advance(1)
        assert not await uplink.cycle()
        assert uplink._next_delay() > 0  # not straight back, whatever was waiting

    async def test_rows_queued_under_an_earlier_id_go_out_under_it(
        self, uplink: Uplink, store: Store, brain: FakeBrain
    ) -> None:
        """Round 2 F9: sent under today's id, the brain refused each of them as coming from
        another collector, and a refused row is deleted: the posts were lost."""
        old = item("21").model_copy(update={"collector_id": "mac-1"})
        await store.mark_seen(
            Sighting(post_id="21", group_id=G1, surface=Surface.FEED), via=Via.FEED, item=old
        )
        await queue(store, "22")
        assert await uplink.push_items() == 2
        bodies = brain.bodies("/v1/items")
        assert [(b["collector_id"], [i["post_id"] for i in b["items"]]) for b in bodies] == [
            ("mac-1", ["21"]),
            (CID, ["22"]),
        ]
        assert await store.outbox_depth() == 0


# -- events ---------------------------------------------------------------------------------------


class TestEvents:
    async def test_acked_events_are_deleted_and_others_kept(
        self, uplink: Uplink, store: Store, brain: FakeBrain
    ) -> None:
        first = await store.enqueue_event(EventKind.BLOCK_OPENED, collector_id=CID, block_id=1)
        await store.enqueue_event(EventKind.CRASH_LOOP, collector_id=CID)
        brain.then(
            "/v1/events",
            reply(body={"v": 1, "acked": [first.event_id, 999]}),  # 999 was never sent
            reply(body={"v": 1, "acked": []}),
        )
        assert await uplink.send_events() == 1
        (left,) = await store.pending_events()
        assert left.kind is EventKind.CRASH_LOOP
        assert await uplink.send_events() == 1
        assert await store.pending_events() == []

    async def test_one_refused_event_does_not_hold_back_the_others(
        self, uplink: Uplink, store: Store, brain: FakeBrain, caplog: pytest.LogCaptureFixture
    ) -> None:
        for kind in (EventKind.CRASH_LOOP, EventKind.PARSER_BROKEN, EventKind.OUTBOX_HIGH):
            await store.enqueue_event(kind, collector_id=CID, detail="x")
        refused = reply(422, body={"v": 1, "error": "events.0.kind: enum"})
        brain.then("/v1/events", refused)  # the batch
        brain.then("/v1/events", refused)  # the crash loop, alone
        with caplog.at_level(logging.ERROR):
            assert await uplink.send_events() == 3
        assert await store.pending_events() == []
        assert await store.kv_int(KV_EVENTS_REJECTED) == 1
        assert "crash_loop" in caplog.text
        assert len(brain.to("/v1/events")) == 4

    async def test_events_name_the_database_that_numbered_them(
        self, uplink: Uplink, store: Store, brain: FakeBrain
    ) -> None:
        await store.enqueue_event(EventKind.CRASH_LOOP, collector_id=CID)
        await uplink.send_events()
        await uplink.send_status()
        (events,) = brain.bodies("/v1/events")
        (status,) = brain.bodies("/v1/status")
        assert events["incarnation"] == status["incarnation"] == await uplink.incarnation()

    async def test_a_refusal_of_every_event_is_the_requests_and_keeps_them(
        self, uplink: Uplink, store: Store, brain: FakeBrain
    ) -> None:
        """Round 2 B-F6: each event refused alone was dropped, so a version skew (the
        request refused, whatever it holds) emptied the queue in one cycle."""
        await store.enqueue_event(EventKind.CRASH_LOOP, collector_id=CID)
        await store.enqueue_event(EventKind.GROUP_LOST, collector_id=CID, group_id=G1)
        refused = reply(422, body={"v": 1, "error": "v: unsupported wire version"})
        brain.then("/v1/events", refused, refused, refused)  # the batch, then each alone
        with pytest.raises(UplinkError) as caught:
            await uplink.send_events()
        assert caught.value.status == 422 and not caught.value.ends_cycle
        assert len(await store.pending_events()) == 2
        assert await store.kv_int(KV_EVENTS_REJECTED) == 0

    async def test_one_event_refused_alone_is_kept_until_others_say_whose_fault_it_is(
        self, uplink: Uplink, store: Store, brain: FakeBrain
    ) -> None:
        await store.enqueue_event(EventKind.CRASH_LOOP, collector_id=CID)
        brain.then("/v1/events", reply(422, body={"v": 1, "error": "x"}))
        with pytest.raises(UplinkError):
            await uplink.send_events()
        assert len(await store.pending_events()) == 1

    async def test_a_lasting_transition_is_never_dropped_for_a_refusal(
        self, uplink: Uplink, store: Store, brain: FakeBrain
    ) -> None:
        lost = await store.enqueue_event(EventKind.GROUP_LOST, collector_id=CID, group_id=G1)
        await store.enqueue_event(EventKind.CRASH_LOOP, collector_id=CID)
        refused = reply(422, body={"v": 1, "error": "events.0.group_id: string_pattern"})

        async def take(request: httpx.Request) -> httpx.Response:
            return brain.default(request)

        brain.then("/v1/events", refused, refused)  # the batch, the lost group alone
        brain.then("/v1/events", take)  # the crash loop: taken
        brain.then("/v1/events", refused)  # the lost group again, alone in the queue now
        with pytest.raises(UplinkError):
            await uplink.send_events()
        (left,) = await store.pending_events()
        assert left.event_id == lost.event_id  # still queued: the brain may be the one wrong
        assert await store.kv_int(KV_EVENTS_REJECTED) == 0

    async def test_events_of_another_collector_id_are_dropped(
        self, uplink: Uplink, store: Store, brain: FakeBrain
    ) -> None:
        await store.enqueue_event(EventKind.CRASH_LOOP, collector_id="old-name")
        await store.enqueue_event(EventKind.OUTBOX_HIGH, collector_id=CID)
        assert await uplink.send_events() == 1
        (body,) = brain.bodies("/v1/events")
        assert [e["kind"] for e in body["events"]] == ["outbox_high"]

    @pytest.mark.parametrize(
        "answer", [reply(500), httpx.Response(200, content=b"[]"), httpx.ConnectError("x")]
    )
    async def test_a_failure_keeps_the_queue(
        self, uplink: Uplink, store: Store, brain: FakeBrain, answer: Any
    ) -> None:
        await store.enqueue_event(EventKind.CRASH_LOOP, collector_id=CID)
        brain.then("/v1/events", answer)
        with pytest.raises(UplinkError):
            await uplink.send_events()
        assert len(await store.pending_events()) == 1

    async def test_nothing_acked_does_not_spin(
        self, uplink: Uplink, store: Store, brain: FakeBrain
    ) -> None:
        await store.enqueue_event(EventKind.CRASH_LOOP, collector_id=CID)
        brain.then("/v1/events", reply(body={"v": 1, "acked": []}))
        assert await uplink.send_events() == 0
        assert len(brain.to("/v1/events")) == 1


# -- control --------------------------------------------------------------------------------------


class TestControl:
    async def test_the_answer_reaches_the_control_center(
        self,
        *,
        uplink: Uplink,
        store: Store,
        brain: FakeBrain,
        control: ControlCenter,
        ladder: Ladder,
        clock: FakeClock,
    ) -> None:
        await store.upsert_group(GroupMeta(id=G1, name="One", join_state="MEMBER"), synced=True)
        block = await ladder.trip(PageState.BLOCKED, reason="x")
        clock.advance(6 * 3600)
        brain.lease = {"collector_id": CID, "granted": True, "generation": 2, "valid_for": 21600}
        brain.monitored = [{"group_id": G1}]
        brain.commands = [
            {
                "kind": "resume",
                "block_id": block.id,
                "nonce": "resume-0001",
                "collector_id": CID,
                "lease_generation": 2,
            }
        ]
        applied = await uplink.poll_control()
        assert applied.lease
        assert [o.result for o in applied.commands] == ["accepted"]
        assert control.state.lease_generation == 2
        assert await store.monitored_ids() == {G1}
        (request,) = brain.to("/v1/control")
        assert request.method == "GET" and request.url.params["collector_id"] == CID
        # the lease is granted to this state database too, not just to the name
        assert request.url.params["incarnation"] == await store.kv_get(KV_INCARNATION)
        applied = await uplink.poll_control()  # re-sent until the block closes: used once
        assert [o.result for o in applied.commands] == ["replayed"]

    @pytest.mark.parametrize(
        "answer",
        [
            reply(500),
            httpx.Response(200, content=b"{}"),
            reply(body={"v": 9, "issued_at": 1_790_000_000, "lease": {"collector_id": CID}}),
        ],
    )
    async def test_an_unusable_answer_keeps_the_last_word(
        self, uplink: Uplink, brain: FakeBrain, control: ControlCenter, answer: httpx.Response
    ) -> None:
        await uplink.poll_control()
        brain.then("/v1/control", answer)
        with pytest.raises(UplinkError):
            await uplink.poll_control()
        assert control.state.lease_granted

    async def test_a_lost_lease_is_logged(
        self, uplink: Uplink, brain: FakeBrain, caplog: pytest.LogCaptureFixture
    ) -> None:
        await uplink.poll_control()
        brain.lease = {"collector_id": "laptop-1", "granted": True, "generation": 2}
        with caplog.at_level(logging.WARNING):
            applied = await uplink.poll_control()
        assert not applied.lease
        assert "lease not held (holder: laptop-1)" in caplog.text


# -- status ---------------------------------------------------------------------------------------


class TestStatus:
    async def test_seq_orders_statuses_and_snapshots_under_one_incarnation(
        self,
        *,
        uplink: Uplink,
        store: Store,
        brain: FakeBrain,
        clock: FakeClock,
        control: ControlCenter,
        alerts: RecordingAlerts,
    ) -> None:
        await store.upsert_group(GroupMeta(id=G1, name="One"), synced=True)
        assert await uplink.send_status()
        await uplink.queue_snapshot(synced(complete=False, listed=1, header_count=None))
        await uplink.send_snapshot()
        restarted = make(store, clock, brain, control, alerts)
        assert await restarted.send_status()
        first, second = (Status.model_validate(b) for b in brain.bodies("/v1/status"))
        (snapshot,) = (GroupsSnapshot.model_validate(b) for b in brain.bodies("/v1/groups"))
        assert (first.seq, snapshot.seq, second.seq) == (1, 2, 3)
        assert first.incarnation == snapshot.incarnation == second.incarnation
        assert first.incarnation == await store.kv_get(KV_INCARNATION)
        assert first.collector_id == CID and first.profile is Profile.CAUTIOUS

    async def test_a_broken_incarnation_is_replaced(self, uplink: Uplink, store: Store) -> None:
        await store.kv_set(KV_INCARNATION, "no")
        fresh = await uplink.incarnation()
        assert fresh != "no" and len(fresh) == 16
        assert await uplink.incarnation() == fresh

    async def test_stale_is_delivered_and_a_refusal_is_only_logged(
        self, uplink: Uplink, brain: FakeBrain, caplog: pytest.LogCaptureFixture
    ) -> None:
        brain.then("/v1/status", reply(body={"v": 1, "ok": True, "stale": True}))
        assert await uplink.send_status()
        brain.then("/v1/status", reply(422, body={"v": 1, "error": "state: enum"}))
        with caplog.at_level(logging.ERROR):
            assert not await uplink.send_status()
        assert "state: enum" in caplog.text
        brain.then("/v1/status", reply(503))
        with pytest.raises(UplinkError):
            await uplink.send_status()
        assert uplink.stats.statuses == 1

    async def test_a_status_that_cannot_be_built_is_skipped(
        self,
        store: Store,
        clock: FakeClock,
        brain: FakeBrain,
        control: ControlCenter,
        alerts: RecordingAlerts,
    ) -> None:
        class Broken:
            async def build(self, *, seq: int | None = None, incarnation: str | None = None) -> Any:
                return Status.model_validate({"collector_id": CID})

        uplink = make(store, clock, brain, control, alerts)
        uplink._status = Broken()
        assert not await uplink.send_status()
        assert brain.to("/v1/status") == []


# -- groups snapshots -----------------------------------------------------------------------------


async def seed_groups(store: Store) -> None:
    await store.upsert_group(
        GroupMeta(
            id=G1,
            name="Flats Varna",
            slug="flats.varna",
            privacy=GroupPrivacy.PRIVATE,
            join_state="MEMBER",
            subscribe_status="IS_SUBSCRIBED",
        ),
        state=GroupState.MEMBER,
        synced=True,
    )
    await store.upsert_group(
        GroupMeta(id=G2, name="Old one", slug="bad slug!"), state=GroupState.LOST
    )


class TestSnapshots:
    async def test_a_sync_becomes_the_brains_snapshot(
        self, uplink: Uplink, store: Store, brain: FakeBrain, control: ControlCenter
    ) -> None:
        await seed_groups(store)
        await control.apply(
            Control.model_validate(
                {
                    "issued_at": 1_790_000_000,
                    "lease": {"collector_id": CID, "granted": True},
                    "commands": [{"kind": "refresh_groups", "nonce": "refresh-0007"}],
                }
            )
        )
        await control.refresh_done()  # the scheduler granted the refresh's sync
        sync = synced(complete=True, listed=1, header_count=1, listed_ids=(G1,))
        snapshot = await uplink.queue_snapshot(sync)
        assert snapshot.complete and snapshot.header_count == 1
        assert snapshot.nonce == "refresh-0007"
        one, two = snapshot.groups
        assert (one.id, one.listed, one.slug, one.followed, one.privacy) == (
            G1,
            True,
            "flats.varna",
            True,
            GroupPrivacy.PRIVATE,
        )
        assert (two.id, two.listed, two.slug, two.state) == (G2, False, None, GroupState.LOST)
        assert await uplink.send_snapshot()
        assert await store.kv_get(KV_SNAPSHOT) is None
        assert await store.kv_get(KV_INTRODUCED) == "1"
        (body,) = brain.bodies("/v1/groups")
        assert GroupsSnapshot.model_validate(body) == snapshot
        assert not await uplink.send_snapshot()  # nothing left to send

    @pytest.mark.parametrize("status", [409, 422])
    async def test_a_refused_snapshot_is_dropped(
        self, uplink: Uplink, store: Store, brain: FakeBrain, status: int
    ) -> None:
        await seed_groups(store)
        await uplink.queue_snapshot(synced(complete=False, listed=0, header_count=None))
        brain.then("/v1/groups", reply(status, body={"v": 1, "error": "x"}))
        assert not await uplink.send_snapshot()
        assert await store.kv_get(KV_SNAPSHOT) is None
        assert await store.kv_get(KV_INTRODUCED) is None

    async def test_an_unreachable_brain_keeps_it(
        self, uplink: Uplink, store: Store, brain: FakeBrain
    ) -> None:
        await seed_groups(store)
        await uplink.queue_snapshot(synced(complete=False, listed=0, header_count=None))
        brain.then("/v1/groups", reply(502))
        with pytest.raises(UplinkError, match="groups: HTTP 502"):
            await uplink.send_snapshot()
        assert await store.kv_get(KV_SNAPSHOT) is not None

    async def test_a_newer_snapshot_replaces_an_unsent_one_and_keeps_its_nonce(
        self, uplink: Uplink, store: Store, control: ControlCenter
    ) -> None:
        await seed_groups(store)
        await control.apply(
            Control.model_validate(
                {
                    "issued_at": 1_790_000_000,
                    "lease": {"collector_id": CID, "granted": True},
                    "commands": [{"kind": "refresh_groups", "nonce": "refresh-0008"}],
                }
            )
        )
        await control.refresh_done()
        first = await uplink.queue_snapshot(synced(complete=False, listed=0, header_count=3))
        second = await uplink.queue_snapshot(synced(complete=False, listed=0, header_count=3))
        assert first.nonce == second.nonce == "refresh-0008"
        assert second.seq is not None and first.seq is not None and second.seq > first.seq
        stored = await store.kv_get(KV_SNAPSHOT)
        assert stored is not None
        assert GroupsSnapshot.model_validate_json(stored) == second

    async def test_one_queued_meanwhile_is_not_forgotten(
        self, uplink: Uplink, store: Store, brain: FakeBrain
    ) -> None:
        await seed_groups(store)
        await uplink.queue_snapshot(synced(complete=False, listed=0, header_count=None))

        async def a_sync_finishes_meanwhile(request: httpx.Request) -> httpx.Response:
            await uplink.queue_snapshot(synced(complete=False, listed=0, header_count=None))
            return reply(body={"v": 1, "ok": True, "groups": 2, "changed": 0})

        brain.then("/v1/groups", a_sync_finishes_meanwhile)
        assert await uplink.send_snapshot()
        assert await store.kv_get(KV_SNAPSHOT) is not None
        assert await uplink.send_snapshot()
        assert await store.kv_get(KV_SNAPSHOT) is None

    async def test_a_listed_group_left_out_makes_it_incomplete(
        self, uplink: Uplink, store: Store
    ) -> None:
        await seed_groups(store)
        await store.upsert_group(GroupMeta(id="0123", name="not a wire id"), synced=True)
        sync = synced(complete=True, listed=2, header_count=2, listed_ids=(G1, "0123"))
        snapshot = await uplink.queue_snapshot(sync)
        assert not snapshot.complete
        assert [g.id for g in snapshot.groups] == [G1, G2]

    async def test_a_snapshot_over_the_group_cap_is_cut_and_incomplete(
        self, uplink: Uplink, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(uplink_module, "MAX_GROUPS", 1)
        await seed_groups(store)
        sync = synced(complete=True, listed=2, header_count=2, listed_ids=(G1, G2))
        snapshot = await uplink.queue_snapshot(sync)
        assert [g.id for g in snapshot.groups] == [G1]
        assert not snapshot.complete

    async def test_introduction_happens_once_and_lists_nothing(
        self, uplink: Uplink, store: Store, brain: FakeBrain
    ) -> None:
        assert await uplink.introduce() is None  # no groups yet: nothing to say
        await seed_groups(store)
        snapshot = await uplink.introduce()
        assert snapshot is not None
        assert not snapshot.complete and not any(g.listed for g in snapshot.groups)
        assert await uplink.introduce() is None  # one is waiting already
        assert await uplink.send_snapshot()
        assert await uplink.introduce() is None  # the brain has had one


def test_a_reason_is_kept_printable_and_bounded() -> None:
    assert uplink_module.clean_reason("transport: ConnectError") == "transport: ConnectError"
    assert uplink_module.clean_reason("a\x1b[0m\nb\u00e9") == "a?[0m?b?"
    assert len(uplink_module.clean_reason("x" * 1000)) == uplink_module.MAX_REASON


def test_followed_comes_from_the_page_and_the_flag() -> None:
    from fbcollector.store import GroupRow

    def row(
        subscribe: str | None, flags: frozenset[str] = frozenset(), sorting: str | None = None
    ) -> GroupRow:
        return GroupRow(
            id=G1,
            name=None,
            slug=None,
            privacy=GroupPrivacy.UNKNOWN,
            state=GroupState.MEMBER,
            join_state=None,
            subscribe_status=subscribe,
            sorting_setting=sorting,
            monitored=True,
            tier=1,
            listings=False,
            baseline_at=T0,
            missing_syncs=0,
            flags=flags,
            last_synced_at=None,
            updated_at=T0,
        )

    listed = frozenset({G1})
    assert [
        getattr(group_info(r, listed), "followed", "x")
        for r in (
            row("IS_SUBSCRIBED"),
            row("CAN_SUBSCRIBE"),
            row(None),
            row("IS_SUBSCRIBED", frozenset({"not_followed"})),
            # Round 2 F5: subscribed, but the feed has not shown it for 48 h: the brain must
            # keep promising the backstop's delay, not the feed's, after the next sync
            row("IS_SUBSCRIBED", frozenset({"feed_absent"})),
        )
    ] == [True, False, None, False, False]
    # Round 2 F7: the brain learns the sort is honoured again only from snapshots
    assert [
        getattr(group_info(r, listed), "sort_ignored", "x")
        for r in (
            row(None),
            row(None, sorting="CHRONOLOGICAL"),
            row(None, frozenset({"sort_ignored"}), sorting="TOP_POSTS"),
        )
    ] == [None, False, True]
    info = group_info(row(None), listed)
    assert info is not None and info.name == G1  # no name known yet: the id stands in


# -- the cycle, outages and the owner -----------------------------------------------------------


class TestCycle:
    async def test_one_cycle_does_everything_due_in_order(
        self, uplink: Uplink, store: Store, brain: FakeBrain
    ) -> None:
        await queue(store, "11")
        await store.enqueue_event(EventKind.CRASH_LOOP, collector_id=CID)
        await seed_groups(store)
        await uplink.introduce()
        assert await uplink.cycle()
        assert [r.url.path for r in brain.requests] == [
            "/v1/control",
            "/v1/groups",
            "/v1/events",
            "/v1/items",
            "/v1/status",
        ]
        assert await uplink.cycle()  # a second cycle at once: nothing is due
        assert len(brain.requests) == 5

    async def test_polls_come_every_minute(
        self, uplink: Uplink, brain: FakeBrain, clock: FakeClock
    ) -> None:
        await uplink.cycle()
        clock.advance(59)
        await uplink.cycle()
        assert len(brain.to("/v1/control")) == 1
        clock.advance(1)
        await uplink.cycle()
        assert len(brain.to("/v1/control")) == len(brain.to("/v1/status")) == 2

    async def test_the_first_failure_ends_the_cycle(
        self, uplink: Uplink, store: Store, brain: FakeBrain
    ) -> None:
        await queue(store, "11")
        brain.then("/v1/control", httpx.ConnectError("tunnel down"))
        assert not await uplink.cycle()
        assert [r.url.path for r in brain.requests] == ["/v1/control"]
        assert await store.outbox_depth() == 1

    async def test_a_refusal_does_not_stop_the_posts_or_the_status(
        self, uplink: Uplink, store: Store, brain: FakeBrain
    ) -> None:
        """Round 2 F6: any non-200 ended the cycle before the status, so one endpoint's
        refusal (a brain bug, a version skew) made a reading collector look offline."""
        await queue(store, "11")
        await store.enqueue_event(EventKind.CRASH_LOOP, collector_id=CID)
        brain.then("/v1/events", reply(500))
        assert not await uplink.cycle()
        assert [r.url.path for r in brain.requests] == [
            "/v1/control",
            "/v1/events",
            "/v1/items",
            "/v1/status",
        ]
        assert await store.outbox_depth() == 0
        assert await store.kv_int("uplink_refused_events") == 1
        assert uplink.down_since is not None  # still no way for events: an outage
        (status,) = brain.bodies("/v1/status")
        assert status["counters"]["uplink_refused_events"] == 1

    async def test_a_failure_in_the_outage_bookkeeping_does_not_escape(
        self,
        uplink: Uplink,
        store: Store,
        *,
        brain: FakeBrain,
        clock: FakeClock,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Round 2 B-F3: ``_down`` and ``_up`` ran outside the cycle's guard."""

        async def broken() -> int:
            raise RuntimeError("disk I/O error")

        monkeypatch.setattr(store, "outbox_depth", broken)
        with caplog.at_level(logging.ERROR):
            for _ in range(7):
                brain.then("/v1/control", httpx.ConnectError("x"))
                assert not await uplink.cycle()
                clock.advance(60)
        assert "uplink bookkeeping failed" in caplog.text

    async def test_the_owner_hears_of_an_outage_after_five_minutes_once(
        self,
        uplink: Uplink,
        store: Store,
        brain: FakeBrain,
        clock: FakeClock,
        alerts: RecordingAlerts,
    ) -> None:
        await queue(store, "11", "12")
        for _ in range(12):  # ten minutes of a dead tunnel, a cycle every 60 s
            brain.then("/v1/control", httpx.ConnectError("tunnel down"))
            await uplink.cycle()
            clock.advance(60)
        assert alerts.keys() == [KEY_DOWN]
        (_, text) = alerts.sent[0]
        assert "5 min" in text and "transport: ConnectError" in text and "2 post(s)" in text
        assert TOKEN not in text
        assert await uplink.cycle()  # the brain answers again
        assert alerts.keys() == [KEY_DOWN, KEY_BACK]
        assert "after 12 min" in alerts.sent[1][1]
        assert uplink.down_since is None
        assert await store.outbox_depth() == 0

    async def test_the_outage_alert_points_at_the_tunnel(
        self, uplink: Uplink, brain: FakeBrain, clock: FakeClock, alerts: RecordingAlerts
    ) -> None:
        for _ in range(6):
            brain.then("/v1/control", httpx.ConnectError("tunnel down"))
            await uplink.cycle()
            clock.advance(60)
        (_, text) = alerts.sent[0]
        assert "ssh fbc-local tunnel" in text
        assert "autossh" not in text

    async def test_each_cycle_leaves_its_outcome_for_the_uplink_command(
        self, uplink: Uplink, store: Store, brain: FakeBrain, clock: FakeClock
    ) -> None:
        assert await store.kv_get(KV_LAST) is None
        assert await uplink.cycle()
        assert await store.kv_get_json(KV_LAST) == {
            "at": T0,
            "ok": True,
            "reason": None,
            "down_since": None,
        }
        clock.advance(60)
        brain.then("/v1/control", httpx.ConnectError("tunnel down"))
        assert not await uplink.cycle()
        started = clock.time()
        clock.suspend(3600)  # a sleep: the wall clock runs on, the outage's start stays
        clock.advance(60)
        brain.then("/v1/control", reply(401))
        assert not await uplink.cycle()
        assert await store.kv_get_json(KV_LAST) == {
            "at": clock.time(),
            "ok": False,
            "reason": "control: the token was refused (FBC_INGEST_TOKEN)",
            "down_since": started,
        }
        clock.advance(60)
        assert await uplink.cycle()
        last = await store.kv_get_json(KV_LAST)
        assert (last["ok"], last["reason"], last["down_since"]) == (True, None, None)

    async def test_a_cycle_that_sent_nothing_leaves_the_last_outcome_in_place(
        self, uplink: Uplink, store: Store, brain: FakeBrain, clock: FakeClock
    ) -> None:
        assert await uplink.cycle()
        clock.advance(10)
        await queue(store, "11")
        brain.then("/v1/items", httpx.ConnectError("tunnel down"))
        assert not await uplink.cycle()
        failed = await store.kv_get_json(KV_LAST)
        assert failed["ok"] is False
        assert failed["down_since"] is not None
        # A woken cycle inside the items back-off: nothing due, no request made. It must not
        # report "ok" in the middle of the outage.
        clock.advance(5)
        await queue(store, "12")
        requests = len(brain.to("/v1/control")) + len(brain.to("/v1/items"))
        assert await uplink.cycle()
        assert len(brain.to("/v1/control")) + len(brain.to("/v1/items")) == requests
        assert await store.kv_get_json(KV_LAST) == failed

    async def test_a_failure_to_record_the_cycle_does_not_fail_it(
        self,
        uplink: Uplink,
        store: Store,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def broken(key: str, value: Any, *, durable: bool = False) -> None:
            raise RuntimeError("disk I/O error")

        monkeypatch.setattr(store, "kv_set_json", broken)
        with caplog.at_level(logging.ERROR):
            assert await uplink.cycle()
        assert "uplink bookkeeping failed" in caplog.text

    async def test_a_short_outage_is_not_worth_a_message(
        self, uplink: Uplink, brain: FakeBrain, clock: FakeClock, alerts: RecordingAlerts
    ) -> None:
        for _ in range(4):
            brain.then("/v1/control", reply(503))
            await uplink.cycle()
            clock.advance(60)
        assert await uplink.cycle()
        assert alerts.sent == []

    async def test_a_sleeping_laptop_is_not_an_outage(
        self, uplink: Uplink, brain: FakeBrain, clock: FakeClock, alerts: RecordingAlerts
    ) -> None:
        brain.then("/v1/control", httpx.ConnectError("x"), httpx.ConnectError("x"))
        await uplink.cycle()
        clock.suspend(3600)  # the lid was closed: wall time only
        clock.advance(60)
        await uplink.cycle()
        assert alerts.sent == []

    async def test_an_alert_that_failed_is_tried_again_and_a_duplicate_is_not(
        self, uplink: Uplink, brain: FakeBrain, clock: FakeClock, alerts: RecordingAlerts
    ) -> None:
        alerts.results.extend([AlertResult.FAILED, AlertResult.DUPLICATE])
        for _ in range(8):
            brain.then("/v1/control", httpx.ConnectError("x"))
            await uplink.cycle()
            clock.advance(60)
        assert alerts.sent == []  # failed once, then deduplicated by the bot: never "sent"
        assert alerts.results == deque()
        await uplink.cycle()
        assert alerts.sent == []  # no "back" for an outage the owner was not told about

    async def test_a_bug_in_a_cycle_is_logged_not_raised(
        self, uplink: Uplink, brain: FakeBrain, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def broken(request: httpx.Request) -> httpx.Response:
            raise RuntimeError("bug")

        brain.then("/v1/control", broken)
        with caplog.at_level(logging.ERROR):
            assert not await uplink.cycle()
        assert "uplink cycle failed" in caplog.text


class TestRun:
    async def test_the_task_polls_every_minute_and_wakes_for_new_items(
        self, uplink: Uplink, store: Store, brain: FakeBrain, clock: FakeClock
    ) -> None:
        stop = asyncio.Event()
        polls: list[float] = []

        async def control(request: httpx.Request) -> httpx.Response:
            polls.append(clock.time())
            if len(polls) == 2:
                await queue(store, "11")
                uplink.wake()
            if len(polls) == 4:
                stop.set()
            return brain.default(request)

        brain.then("/v1/control", *([control] * 4))
        await seed_groups(store)
        async with asyncio.timeout(5):
            await uplink.run(stop)
        assert [p - polls[0] for p in polls] == [0, 60, 120, 180]
        assert await store.outbox_depth() == 0
        assert len(brain.to("/v1/groups")) == 1  # the introduction

    async def test_a_dead_brain_after_a_busy_one_is_tried_a_few_times_a_minute(
        self, uplink: Uplink, store: Store, brain: FakeBrain, clock: FakeClock
    ) -> None:
        """Round 2 B-F1: a 503 and then a refused connection made the task spin, a request
        and a database write per turn, thousands a second, for the whole outage."""
        await uplink.cycle()
        await queue(store, "12")
        stop = asyncio.Event()
        times: list[float] = []

        async def dead(request: httpx.Request) -> httpx.Response:
            times.append(clock.monotonic())
            if len(times) >= 40:
                stop.set()
            raise httpx.ConnectError("refused", request=request)

        brain.then("/v1/items", reply(503, body=results(), **{"Retry-After": "1"}))
        for path in ("/v1/control", "/v1/items", "/v1/status"):
            brain.then(path, *([dead] * 100))
        async with asyncio.timeout(5):
            await uplink.run(stop)
        busiest = max(sum(1 for t in times if at <= t < at + 60) for at in times)
        assert busiest <= 4
        assert clock.sleeps and min(s for s in clock.sleeps if s > 0) >= 5  # never straight back

    async def test_a_cycle_that_raises_does_not_end_the_task(
        self, uplink: Uplink, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        stop = asyncio.Event()
        calls = 0

        async def cycle() -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("bug")
            stop.set()
            return True

        monkeypatch.setattr(uplink, "cycle", cycle)
        with caplog.at_level(logging.ERROR):
            async with asyncio.timeout(5):
                await uplink.run(stop)
        assert calls == 2 and "uplink cycle raised" in caplog.text

    async def test_an_introduction_that_fails_does_not_stop_the_task(
        self, uplink: Uplink, brain: FakeBrain, caplog: pytest.LogCaptureFixture
    ) -> None:
        stop = asyncio.Event()

        async def broken() -> None:
            raise RuntimeError("bug")

        async def control(request: httpx.Request) -> httpx.Response:
            stop.set()
            return brain.default(request)

        uplink.introduce = broken  # type: ignore[method-assign]
        brain.then("/v1/control", control)
        with caplog.at_level(logging.WARNING):
            async with asyncio.timeout(5):
                await uplink.run(stop)
        assert "introductory" in caplog.text

    async def test_flush_hands_over_what_is_queued_and_gives_up_quietly(
        self, uplink: Uplink, store: Store, brain: FakeBrain
    ) -> None:
        await queue(store, "11")
        await store.enqueue_event(EventKind.CRASH_LOOP, collector_id=CID)
        await uplink.flush()
        assert await store.outbox_depth() == 0 and await store.pending_events() == []
        assert brain.to("/v1/control") == []  # nothing new is asked for on the way out
        await queue(store, "12")
        brain.then("/v1/items", httpx.ConnectError("gone"))
        await uplink.flush()
        assert await store.outbox_depth() == 1

    async def test_flush_is_bounded(self, uplink: Uplink, store: Store, brain: FakeBrain) -> None:
        async def hang(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

        await queue(store, "11")
        brain.then("/v1/items", hang)
        async with asyncio.timeout(5):
            await uplink.flush(timeout=0.05)
        assert await store.outbox_depth() == 1
