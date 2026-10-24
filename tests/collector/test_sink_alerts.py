"""The phase-0 sink (durable before acknowledged) and the owner-only alert bot (mock transport)."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from fbcollector import alerts
from fbcollector.alerts import AlertBot, AlertResult, NullAlerts
from fbcollector.clock import FakeClock
from fbcollector.model import Sighting, Surface
from fbcollector.sink import JsonlSink, drain
from fbcollector.store import OutboxRow, Store
from fbwire.models import Item, ItemsResponse, Via

TOKEN = "123456789:" + "A" * 35
OWNER = 424242


def item(post_id: str, group_id: str = "1234567890", text: str = "hello") -> Item:
    return Item(
        collector_id="home-1",
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


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    clock = FakeClock()
    async with Store(tmp_path / "state.sqlite", clock=clock) as opened:
        yield opened


async def queue(store: Store, *post_ids: str) -> None:
    for post_id in post_ids:
        sighting = Sighting(post_id=post_id, group_id="1234567890", surface=Surface.FEED)
        await store.mark_seen(sighting, via=Via.FEED, item=item(post_id))


async def test_jsonl_sink_appends_fsynced_private_lines(store: Store, tmp_path: Path) -> None:
    await queue(store, "11", "12", "13")
    sink = JsonlSink(tmp_path / "items.jsonl")
    result = await drain(store, sink, "home-1")
    assert (result.sent, result.kept, result.failed) == (3, 0, False)
    assert await store.outbox_depth() == 0
    lines = (tmp_path / "items.jsonl").read_text().splitlines()
    assert [json.loads(line)["post_id"] for line in lines] == ["11", "12", "13"]
    assert all(json.loads(line)["seq"] > 0 for line in lines)
    assert (tmp_path / "items.jsonl").stat().st_mode & 0o777 == 0o600
    assert (await drain(store, sink, "home-1")).sent == 0  # nothing left
    await queue(store, "14")
    await drain(store, sink, "home-1")
    assert len((tmp_path / "items.jsonl").read_text().splitlines()) == 4  # appended


async def test_a_short_write_is_finished_before_the_rows_are_answered(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = os.write

    def dribble(fd: int, data: Any) -> int:
        return real_write(fd, bytes(data)[:7])  # a disk that takes a few bytes at a time

    monkeypatch.setattr(os, "write", dribble)
    await queue(store, "31", "32")
    result = await drain(store, JsonlSink(tmp_path / "items.jsonl"), "home-1")
    assert result.sent == 2
    lines = (tmp_path / "items.jsonl").read_text().splitlines()
    assert [json.loads(line)["post_id"] for line in lines] == ["31", "32"]


async def test_a_write_that_stops_keeps_the_rows_and_cuts_the_half_line(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "items.jsonl"
    await queue(store, "40")
    await drain(store, JsonlSink(path), "home-1")
    before = path.read_bytes()
    real_write = os.write
    calls = 0

    def full_disk(fd: int, data: Any) -> int:
        nonlocal calls
        calls += 1
        return real_write(fd, bytes(data)[:10]) if calls == 1 else 0

    monkeypatch.setattr(os, "write", full_disk)
    await queue(store, "41", "42")
    result = await drain(store, JsonlSink(path), "home-1")
    assert result.failed
    assert await store.outbox_depth() == 2  # nothing was answered
    assert path.read_bytes() == before  # the partial line was cut off again


async def test_the_line_is_fsynced_before_the_outbox_row_is_deleted(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        order.append("fsync")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    await store.db.set_trace_callback(
        lambda sql: order.append("delete") if sql.startswith("DELETE FROM outbox") else None
    )
    await queue(store, "51")
    await drain(store, JsonlSink(tmp_path / "items.jsonl"), "home-1")
    await store.db.set_trace_callback(lambda _sql: None)
    assert order == ["fsync", "delete"]


class BrokenSink:
    async def send(self, collector_id: str, rows: Sequence[OutboxRow]) -> ItemsResponse:
        raise OSError("disk full")


class SilentSink:
    async def send(self, collector_id: str, rows: Sequence[OutboxRow]) -> ItemsResponse:
        return ItemsResponse(results=())


async def test_failed_or_unanswered_sends_keep_the_rows(store: Store) -> None:
    await queue(store, "21", "22")
    failed = await drain(store, BrokenSink(), "home-1")
    assert failed.failed
    assert failed.kept == 2
    assert await store.outbox_depth() == 2
    silent = await drain(store, SilentSink(), "home-1")
    assert (silent.sent, silent.kept, silent.failed) == (0, 2, False)  # no spin
    assert await store.outbox_depth() == 2


# -- alerts -----------------------------------------------------------------------------------


class Telegram:
    def __init__(self, answer: Any = None, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.answer = {"ok": True, "result": {}} if answer is None else answer
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return httpx.Response(self.status, json=self.answer)

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


class Ledger:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}

    async def kv_get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def kv_set(self, key: str, value: str) -> None:
        self.kv[key] = value


def bot(api: Telegram, clock: FakeClock, ledger: Ledger | None = None, **kw: Any) -> AlertBot:
    return AlertBot(
        TOKEN, OWNER, clock=clock, ledger=ledger, transport=httpx.MockTransport(api), **kw
    )


async def test_sends_only_send_message_to_the_owner() -> None:
    api, clock = Telegram(), FakeClock()
    assert await bot(api, clock).alert("block:1", "blocked") is AlertResult.SENT
    request = api.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert api.bodies[0]["chat_id"] == OWNER
    assert api.bodies[0]["text"].startswith(alerts.PREFIX)
    assert api.bodies[0]["disable_web_page_preview"] is True


async def test_dedup_survives_a_restart_and_expires() -> None:
    api, clock, ledger = Telegram(), FakeClock(), Ledger()
    assert await bot(api, clock, ledger).alert("k", "one") is AlertResult.SENT
    assert await bot(api, clock, ledger).alert("k", "one again") is AlertResult.DUPLICATE
    clock.advance(6 * 3600)
    assert await bot(api, clock, ledger).alert("k", "later") is AlertResult.SENT
    assert await bot(api, clock, ledger).alert("k", "x", dedup=0) is AlertResult.SENT
    ledger.kv["alert:bad"] = "not json"
    assert await bot(api, clock, ledger).alert("bad", "x") is AlertResult.SENT
    assert len(api.requests) == 4


async def test_hourly_cap_spacing_and_critical_bypass() -> None:
    api, clock = Telegram(), FakeClock()
    client = bot(api, clock, per_hour=2)
    assert await client.alert("a", "a") is AlertResult.SENT
    assert await client.alert("b", "b") is AlertResult.SENT
    assert clock.sleeps == [3.0]  # spaced 3 s apart
    assert await client.alert("c", "c") is AlertResult.RATE_LIMITED
    assert await client.alert("d", "d", critical=True) is AlertResult.SENT
    assert "1 earlier alerts were not sent" in api.bodies[-1]["text"]
    clock.advance(3600)
    assert await client.alert("e", "e") is AlertResult.SENT


@pytest.mark.parametrize(
    "answer",
    [httpx.ConnectError("boom"), {"ok": False, "description": "chat not found"}],
)
async def test_failures_never_log_the_token(answer: Any, caplog: pytest.LogCaptureFixture) -> None:
    api, clock = Telegram(answer), FakeClock()
    caplog.set_level(logging.DEBUG)
    result = await bot(api, clock).alert("k", "x")
    assert result is AlertResult.FAILED
    assert TOKEN not in caplog.text
    assert "AAAA" not in caplog.text
    assert await bot(api, clock).alert("k", "x") is AlertResult.FAILED  # not remembered


async def test_http_error_status_is_a_failure() -> None:
    api = Telegram({"ok": False}, status=403)
    assert await bot(api, FakeClock()).alert("k", "x") is AlertResult.FAILED


async def test_http_request_logging_is_held_at_warning(caplog: pytest.LogCaptureFixture) -> None:
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG)
    api = Telegram()
    await bot(api, FakeClock()).alert("k", "x")
    assert logging.getLogger("httpx").level == logging.WARNING
    assert TOKEN not in caplog.text


async def test_long_texts_are_cut_and_owner_id_is_checked() -> None:
    api = Telegram()
    await bot(api, FakeClock()).alert("k", "x" * 5000)
    assert len(api.bodies[0]["text"]) == alerts.MAX_TEXT
    with pytest.raises(ValueError, match="owner"):
        AlertBot(TOKEN, 0, clock=FakeClock())


async def test_null_alerts_record_without_sending() -> None:
    null = NullAlerts()
    assert await null.alert("k", "text") is AlertResult.DISABLED
    assert null.sent == [("k", "text")]


def test_texts_name_what_to_do() -> None:
    text = alerts.block_opened(7, "blocked", "url:checkpoint", 1_790_000_000.0, no_probe=True)
    assert "block #7" in text
    assert "fbcollector resume 7" in text
    assert "--lock-lifted" in text
    assert "re-plan" in alerts.block_opened(8, "blocked", "x", None)
    assert "lifted" in alerts.probe_result(7, True, "")
    assert "failed" in alerts.probe_result(7, False, "UNKNOWN")
    lost = alerts.group_event("group_lost", "123", "Flats", "join_state:NOT_MEMBER")
    assert "Flats (123)" in lost
    assert "paused" in lost
    assert alerts.group_event("other", "123", None, "x").startswith("⚠️ OTHER: 123")
    assert alerts.simple("crash_loop", "5 launches", hint="check logs").endswith("check logs")
    assert alerts.reasons_text(["a", "b"]) == "a; b"
