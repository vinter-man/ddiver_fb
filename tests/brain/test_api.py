"""The ingest endpoints without HTTP: what each answer promises the collector."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from ddiver_core.matcher import Matcher
from ddiver_core.rules import RuleKind
from ddiver_core.storage import OutboxItem, Storage
from ddiver_core_testing import FakeClock
from ddiver_fb.api import MAX_STRIKES, STRIKES_KEPT, IngestApi, Response, Strikes
from ddiver_fb.control import ControlDesk
from ddiver_fb.groups import GroupBook
from ddiver_fb.monitor import CollectorMonitor
from ddiver_fb.source import FacebookSource
from ddiver_fb.store import FbStore
from fbwire import (
    Control,
    EventsResponse,
    GroupsResponse,
    ItemOutcome,
    ItemsResponse,
    StatusResponse,
    to_json,
)
from tests.brain.helpers import (
    CID,
    EPOCH0,
    G1,
    G2,
    OWNER,
    info,
    item,
    seed_groups,
    snapshot,
    status,
)


class Sink:
    def __init__(self) -> None:
        self.rows: list[OutboxItem] = []

    async def __call__(self, row: OutboxItem) -> None:
        self.rows.append(row)


@pytest.fixture
def sink() -> Sink:
    return Sink()


@pytest.fixture
async def api(
    storage: Storage,
    *,
    fb: FbStore,
    monitor: CollectorMonitor,
    desk: ControlDesk,
    book: GroupBook,
    clock: FakeClock,
    sink: Sink,
) -> IngestApi:
    matcher = Matcher(storage, timeout=0.2)
    await seed_groups(book, G1, G2)
    clock.tick(-3600)  # watched for an hour before these posts
    await storage.subscribe(OWNER, G1)
    await storage.add_rule(OWNER, G1, RuleKind.INCLUDE, "varna")
    clock.tick(3600)
    await matcher.reload()
    source = FacebookSource(storage, fb, matcher, deliver=sink, clock=clock)
    return IngestApi(source, monitor, desk, fb, clock=clock)


def wire(*items: Any) -> dict[str, Any]:
    return {"v": 1, "collector_id": CID, "items": [json.loads(to_json(i)) for i in items]}


def results(response: Response) -> list[tuple[str, str | None]]:
    assert isinstance(response.body, ItemsResponse)
    return [(r.result.value, r.reason) for r in response.body.results]


class TestItems:
    async def test_each_item_gets_its_own_result(self, api: IngestApi, sink: Sink):
        bad = json.loads(to_json(item(pid=3)))
        bad["url"] = "https://evil.example/groups/1/posts/3/"
        body = wire(item(pid=1), item(pid=1), item(G2, pid=2))
        body["items"].append(bad)
        response = await api.items(body)
        assert response.status == 200
        got = results(response)
        # the invalid item's rejection comes first: it never reached the source
        assert got[0][0] == "rejected" and "url" in (got[0][1] or "")
        assert got[1:] == [("stored", None), ("duplicate", None), ("not_monitored", None)]
        assert [r.msg_id for r in sink.rows] == [1]

    async def test_a_resend_is_all_duplicates(self, api: IngestApi, sink: Sink):
        body = wire(item(pid=1), item(pid=2, text="nothing to see"))
        await api.items(body)
        again = await api.items(body)
        assert results(again) == [("duplicate", None), ("duplicate", None)]
        assert len(sink.rows) == 1

    async def test_items_without_ids_cannot_be_answered_and_are_counted(self, api: IngestApi):
        body = wire(item(pid=1))
        body["items"].append({"text": "no ids at all"})
        response = await api.items(body)
        assert results(response) == [("stored", None)]
        assert api.metrics.unkeyed_items == 1

    @pytest.mark.parametrize(
        "body",
        [
            {"v": 1, "collector_id": CID},
            {"v": 1, "collector_id": CID, "items": "nope"},
            {"v": 99, "collector_id": CID, "items": []},
            {"v": 1, "collector_id": "../../etc", "items": []},
            [],
        ],
    )
    async def test_a_broken_envelope_is_422(self, api: IngestApi, body: Any):
        response = await api.items(body)
        assert response.status == 422
        assert api.metrics.invalid == 1

    async def test_the_collector_is_seen(self, api: IngestApi, fb: FbStore):
        await api.items(wire(item(pid=1)))
        assert await fb.collector(CID) is not None

    async def test_a_busy_database_mid_batch_is_503_with_what_was_done(
        self, api: IngestApi, fb: FbStore, sink: Sink, monkeypatch: pytest.MonkeyPatch
    ):
        original = fb.has_item
        calls = {"n": 0}

        async def busy_on_second(*args: Any) -> bool:
            calls["n"] += 1
            if calls["n"] == 2:
                raise sqlite3.OperationalError("database is locked")
            return await original(*args)

        monkeypatch.setattr(fb, "has_item", busy_on_second)
        body = wire(item(pid=1), item(pid=2), item(pid=3))
        response = await api.items(body)
        assert response.status == 503 and response.retry_after == 30
        assert results(response) == [("stored", None)]  # pid 1 only; 2 and 3 have no result
        assert api.metrics.busy == 1
        # The collector resends the rows without a result (here: the whole batch, as a 5xx
        # client that ignores the body would). Nothing is delivered twice.
        again = await api.items(body)
        assert results(again) == [("duplicate", None), ("stored", None), ("stored", None)]
        assert sorted(r.msg_id for r in sink.rows) == [1, 2, 3]

    async def test_a_post_whose_own_content_breaks_it_is_rejected_alone(
        self, api: IngestApi, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        import ddiver_fb.source as source_module

        original = source_module.build_payload

        def explode_on_two(one: Any, *args: Any) -> Any:
            if one.post_id == "2":
                raise UnicodeEncodeError("utf-8", "x", 0, 1, "a surrogate")
            return original(one, *args)

        monkeypatch.setattr(source_module, "build_payload", explode_on_two)
        body = wire(item(pid=1), item(pid=2, text="SECRET varna TEXT"), item(pid=3))
        response = await api.items(body)
        assert response.status == 200
        assert results(response) == [
            ("stored", None),
            ("rejected", "brain: cannot process this post"),
            ("stored", None),
        ]
        assert "SECRET" not in caplog.text  # invariant 8: ids in the log, never the text
        assert api._source.metrics.results[ItemOutcome.REJECTED] == 1

    @pytest.mark.parametrize(
        "fault",
        [
            sqlite3.DatabaseError("database disk image is malformed"),
            sqlite3.OperationalError("database is locked"),
            sqlite3.IntegrityError("UNIQUE constraint failed"),
        ],
    )
    async def test_a_storage_fault_mid_batch_keeps_the_rest_for_a_resend(
        self, api: IngestApi, monkeypatch: pytest.MonkeyPatch, fault: Exception
    ):
        """C1-02: a storage error says nothing about the post; rejecting it made the collector
        delete a post the brain never processed."""
        source = api._source
        original = source.process

        async def fail_on_two(one: Any) -> ItemOutcome:
            if one.post_id == "2":
                raise fault
            return await original(one)

        monkeypatch.setattr(source, "process", fail_on_two)
        for _ in range(MAX_STRIKES + 1):  # storage faults never turn into a rejection
            response = await api.items(wire(item(pid=1), item(pid=2), item(pid=3)))
            assert response.status == 503 and response.retry_after == 30
            assert results(response)[1:] == []

    async def test_a_shutdown_mid_batch_is_503_not_rejected(
        self, api: IngestApi, monkeypatch: pytest.MonkeyPatch
    ):
        source = api._source
        original = source.process

        async def close_after_one(one: Any) -> ItemOutcome:
            if one.post_id == "1":
                outcome = await original(one)
                api.closing = True  # the brain starts shutting down meanwhile
                return outcome
            raise ValueError("Connection closed")  # what aiosqlite says once closed

        monkeypatch.setattr(source, "process", close_after_one)
        response = await api.items(wire(item(pid=1), item(pid=2), item(pid=3)))
        assert response.status == 503
        assert results(response) == [("stored", None)]
        response = await api.items(wire(item(pid=4)))
        assert response.status == 503 and results(response) == []

    async def test_a_failure_not_pinned_on_the_post_is_retried_then_given_up(
        self, api: IngestApi, monkeypatch: pytest.MonkeyPatch
    ):
        source = api._source
        original = source.process

        async def bug_on_two(one: Any) -> ItemOutcome:
            if one.post_id == "2":
                raise RuntimeError("a bug")
            return await original(one)

        monkeypatch.setattr(source, "process", bug_on_two)
        body = wire(item(pid=1), item(pid=2), item(pid=3))
        for attempt in range(1, MAX_STRIKES):
            response = await api.items(body)
            assert response.status == 503, attempt
        response = await api.items(body)  # a poison post must not wedge the outbox for good
        assert response.status == 200
        assert results(response) == [
            ("duplicate", None),
            ("rejected", "brain: internal error"),
            ("stored", None),
        ]
        assert api.strikes.counts == {}

    def test_strikes_are_bounded(self):
        strikes = Strikes()
        for n in range(STRIKES_KEPT + 10):
            strikes.note(("1", str(n)))
        assert len(strikes.counts) == STRIKES_KEPT
        assert ("1", "0") not in strikes.counts  # the oldest went first
        assert strikes.note(("1", str(STRIKES_KEPT + 9))) == 2


class TestGroupsStatusEvents:
    async def test_groups(self, api: IngestApi, desk: ControlDesk):
        response = await api.groups(json.loads(to_json(snapshot(info(G1), info(G2)))))
        assert response.status == 200
        assert response.body == GroupsResponse(groups=2, changed=0)  # seeded by the fixture
        older = json.loads(to_json(snapshot(info(G1), taken_at=EPOCH0 - 60)))
        assert (await api.groups(older)).body == GroupsResponse(groups=1, stale=True)
        await desk.control_for(CID)
        stranger = json.loads(to_json(snapshot(info(G1), collector_id="laptop")))
        assert (await api.groups(stranger)).status == 409
        assert (await api.groups({"v": 1, "collector_id": CID})).status == 422

    async def test_status(self, api: IngestApi, monitor: CollectorMonitor):
        response = await api.status(json.loads(to_json(status())))
        assert response.status == 200 and response.body == StatusResponse()
        older = json.loads(to_json(status(sent_at=EPOCH0 - 60)))
        assert (await api.status(older)).body == StatusResponse(stale=True)
        assert (await monitor.holder_row()) is not None
        broken = json.loads(to_json(status()))
        broken["state"] = "offline"  # only the brain may say that
        assert (await api.status(broken)).status == 422

    async def test_events(self, api: IngestApi):
        body: dict[str, Any] = {
            "v": 1,
            "collector_id": CID,
            "events": [
                {"v": 1, "collector_id": CID, "event_id": 5, "at": EPOCH0, "kind": "outbox_high"}
            ],
        }
        response = await api.events(body)
        assert response.status == 200 and response.body == EventsResponse(acked=(5,))
        body["events"][0]["kind"] = "group_lost"  # needs a group_id
        assert (await api.events(body)).status == 422

    async def test_events_must_come_from_the_envelope_collector(
        self, api: IngestApi, desk: ControlDesk, book: GroupBook, storage: Storage
    ):
        """A non-holder cannot borrow the holder's lease by naming it inside its events."""
        await desk.control_for(CID)
        body: dict[str, Any] = {
            "v": 1,
            "collector_id": "laptop",
            "events": [
                {
                    "v": 1,
                    "collector_id": CID,
                    "event_id": 5,
                    "at": EPOCH0,
                    "kind": "group_lost",
                    "group_id": str(G1),
                }
            ],
        }
        response = await api.events(body)
        assert response.status == 422
        assert G1 in await storage.monitored_chat_ids()


class TestVersions:
    async def test_every_answer_is_in_the_version_of_its_request(
        self, api: IngestApi, monkeypatch: pytest.MonkeyPatch
    ):
        """Round 2 F8: the brain deploys first after a wire bump; a collector still on the
        older version must read every answer (receivers refuse anything newer)."""
        import fbwire.models as wire

        monkeypatch.setattr(wire, "WIRE_VERSION", 2)
        old: dict[str, Any] = {"v": 1}
        answers = [
            await api.status({**status().model_dump(mode="json"), **old}),
            await api.groups({**snapshot(info(G1)).model_dump(mode="json"), **old}),
            await api.events(
                {
                    **old,
                    "collector_id": CID,
                    "events": [
                        {
                            "v": 1,
                            "collector_id": CID,
                            "event_id": 1,
                            "at": EPOCH0,
                            "kind": "outbox_high",
                        }
                    ],
                }
            ),
            await api.items(
                {**old, "collector_id": CID, "items": [item().model_dump(mode="json")]}
            ),
            await api.control({"collector_id": CID, "v": "1"}),
            await api.control({"collector_id": "a b", "v": "1"}),  # refused, in v1 too
            await api.status(old),  # an invalid body in v1
        ]
        assert [getattr(a.body, "v", None) for a in answers] == [1] * len(answers)
        assert (await api.status(status().model_dump(mode="json"))).body.v == 2  # type: ignore[union-attr]
        assert (await api.control({"collector_id": CID})).body.v == 2  # type: ignore[union-attr]


class TestControl:
    async def test_control_needs_a_well_formed_collector_id(self, api: IngestApi):
        assert (await api.control({})).status == 422
        assert (await api.control({"collector_id": "a b"})).status == 422
        bad = await api.control({"collector_id": CID, "incarnation": "short"})
        assert bad.status == 422 and "incarnation" in str(bad.body)
        response = await api.control({"collector_id": CID})
        assert response.status == 200 and isinstance(response.body, Control)
        assert [m.group_id for m in response.body.monitored] == [str(G1)]
