"""The composition root, end to end over the real ingest server: a post in, a notification out.

The only fakes are the Telegram side (``FakeTelethon`` for the bot's connection, ddiver-core's
``FakeBotClient`` for what it sends) and the clock. The collector is ``httpx`` — what the real
one uses — talking to the server on 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from ddiver_core.i18n import t
from ddiver_core.rules import RuleKind
from ddiver_core.storage import Rule
from ddiver_core_testing import FakeBotClient, FakeClock
from ddiver_fb import schema
from ddiver_fb.health import check, read_beat
from ddiver_fb.runtime import BrainApp
from fbwire import CollectorState, Control, ItemsResponse, to_json
from tests.brain.helpers import (
    CID,
    EPOCH0,
    G1,
    OWNER,
    TOKEN,
    FakeTelethon,
    block,
    info,
    item,
    settings_for,
    snapshot,
    status,
)


@pytest.fixture
def sent() -> FakeBotClient:
    return FakeBotClient()


@pytest.fixture
async def app(tmp_path: Path, clock: FakeClock, sent: FakeBotClient) -> AsyncIterator[BrainApp]:
    brain = BrainApp(settings_for(tmp_path), clock=clock, bot_client=FakeTelethon(), bot=sent)
    await brain.setup()
    yield brain
    await brain.close()


@pytest.fixture
async def client(app: BrainApp) -> AsyncIterator[httpx.AsyncClient]:
    await app.ingest.start()
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{app.ingest.port}",
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=10,
        trust_env=False,
    ) as http:
        yield http


def body(model: Any) -> bytes:
    return to_json(model)


JSON = {"Content-Type": "application/json"}


class TestEndToEnd:
    async def test_a_post_becomes_a_facebook_notification(
        self, app: BrainApp, client: httpx.AsyncClient, sent: FakeBotClient, clock: FakeClock
    ):
        r = await client.post(
            "/v1/groups", content=body(snapshot(info(G1, "Оренда Варна"))), headers=JSON
        )
        assert r.status_code == 200 and r.json()["groups"] == 1
        await app.monitor.idle()  # the owner's summary goes out after the answer
        sync_note = sent.sent.pop()
        assert sync_note.user_id == OWNER and "Оренда Варна" in sync_note.text
        clock.tick(-3600)
        await app.storage.subscribe(OWNER, G1)
        await app.storage.add_rule(OWNER, G1, RuleKind.INCLUDE, "varna")
        await app.invalidate(G1)
        clock.tick(3600)
        r = await client.post("/v1/status", content=body(status()), headers=JSON)
        assert r.status_code == 200

        r = await client.get("/v1/control", params={"collector_id": CID})
        control = Control.model_validate_json(r.content)
        assert control.lease_held_by(CID)
        assert [m.group_id for m in control.monitored] == [str(G1)]

        batch = {"v": 1, "collector_id": CID, "items": [item().model_dump(mode="json")]}
        r = await client.post("/v1/items", json=batch)
        answer = ItemsResponse.model_validate_json(r.content)
        assert [x.result.value for x in answer.results] == ["stored"]
        assert await app.delivery.wait_idle(5)
        (note,) = sent.sent
        assert note.user_id == OWNER
        assert "👥 Group Оренда Варна" in note.text and "t.me" not in note.text
        urls = [getattr(e, "url", "") for e in note.entities]
        assert f"https://www.facebook.com/groups/{G1}/posts/9876543210987654/" in urls
        assert all(u.startswith("https://www.facebook.com/") for u in urls if u)

        r = await client.post("/v1/items", json=batch)  # the collector resends: nothing new
        assert ItemsResponse.model_validate_json(r.content).results[0].result.value == "duplicate"
        assert len(sent.sent) == 1

    async def test_the_wrong_token_is_turned_away(self, client: httpx.AsyncClient):
        r = await client.post(
            "/v1/status", content=body(status()), headers={**JSON, "Authorization": "Bearer x"}
        )
        assert r.status_code == 401


class TestLifecycle:
    async def test_setup_opens_the_fb_track_and_the_owner(self, app: BrainApp):
        assert await app.storage.track_version(schema.TRACK) == len(schema.MIGRATIONS)
        owner = await app.storage.get_user(OWNER)
        assert owner is not None and owner.is_owner
        assert t("en", "notif.chat") == "👥 Group"  # the overlay is installed

    async def test_run_serves_until_asked_to_stop(
        self, app: BrainApp, monkeypatch: pytest.MonkeyPatch
    ):
        telethon = app.bot_client
        started = asyncio.Event()
        original = app.start_tasks

        def start_and_signal() -> None:
            original()
            started.set()

        monkeypatch.setattr(app, "start_tasks", start_and_signal)
        running = asyncio.create_task(app.run())
        async with asyncio.timeout(5):
            await started.wait()
        assert app.ingest.serving
        assert telethon.calls == ["connect", "start 123456789"]
        assert len(telethon.handlers) == 2  # private messages and button presses
        assert len(telethon.requests) == 4  # the command list, for uk/ru/en and the default
        app.request_stop()
        assert await running == 0

    async def test_run_gives_up_when_the_ingest_port_is_taken(
        self, tmp_path: Path, clock: FakeClock
    ):
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            telethon = FakeTelethon()
            brain = BrainApp(
                settings_for(tmp_path, ingest_port=taken.getsockname()[1]),
                clock=clock,
                bot_client=telethon,
                bot=FakeBotClient(),
            )
            await brain.setup()
            try:
                assert await brain.run() == 1  # deaf is worse than down: exit, be restarted
            finally:
                await brain.close()

    async def test_close_is_safe_before_anything_started(self, tmp_path: Path, clock: FakeClock):
        telethon = FakeTelethon()
        brain = BrainApp(
            settings_for(tmp_path), clock=clock, bot_client=telethon, bot=FakeBotClient()
        )
        await brain.setup()
        await brain.close()
        assert telethon.disconnected
        assert brain.api.closing  # anything still in flight was answered 503, not rejected


class TestBackgroundWork:
    async def test_the_heartbeat_says_bot_and_ingest(self, app: BrainApp, clock: FakeClock):
        await app.heartbeat()
        beat = read_beat(app.settings.health_path)
        assert beat is not None and not beat.ingest_up and not beat.bot_connected
        await app.ingest.start()

        class Sender:
            @staticmethod
            def _transport_connected() -> bool:
                return True

        app.bot_client._sender = Sender()
        await app.ping_bot()
        assert app.bot_ping_ok_at == clock()
        await app.heartbeat()
        assert check(app.settings.health_path, now=clock()).healthy

    async def test_a_failed_bot_ping_is_only_logged(self, app: BrainApp):
        app.bot_client.fail_requests = True
        await app.ping_bot()
        assert app.bot_ping_ok_at is None

    async def test_a_broken_sender_is_not_connected(self, app: BrainApp):
        class Broken:
            def _transport_connected(self) -> bool:
                raise RuntimeError("private API changed")

        app.bot_client._sender = Broken()
        assert app.bot_connected() is False

    async def test_prune_keeps_recent_post_ids(self, app: BrainApp, clock: FakeClock):
        for pid, age_days in ((1, 20), (2, 1)):
            received = EPOCH0 - age_days * 86400
            await app.fb.record_item(
                G1,
                pid,
                created_at=received,
                received_at=received,
                via="feed",
                late=False,
                claimed=False,
            )
        await app.prune()
        assert not await app.fb.has_item(G1, 1) and await app.fb.has_item(G1, 2)

    async def test_prune_keeps_post_texts_seven_days(self, app: BrainApp):
        for pid, age_days in ((1, 8), (2, 6)):
            await app.fb.record_text(item(G1, pid), received_at=EPOCH0 - age_days * 86400)
        await app.prune()
        cur = await app.fb.db.execute("SELECT post_id FROM fb_texts")
        assert [r["post_id"] for r in await cur.fetchall()] == [2]

    async def test_prune_keeps_ai_verdicts_as_long_as_the_texts(self, app: BrainApp):
        wish = await app.ai.add_wish(OWNER, G1, "дом", now=EPOCH0 - 30 * 86400, threshold=70)
        for pid, age_days in ((1, 8), (2, 6)):
            await app.fb.record_text(item(G1, pid), received_at=EPOCH0 - age_days * 86400)
            await app.ai.enqueue(G1, pid, created_at=EPOCH0 - 20 * 86400, now=EPOCH0)
        for queued in await app.ai.due(now=EPOCH0, limit=10):
            age = 8 if queued.post_id == 1 else 6
            await app.ai.record(queued, now=EPOCH0 - age * 86400, error="timeout")
        await app.prune()
        cur = await app.fb.db.execute(
            "SELECT post_id FROM fb_ai_verdicts WHERE wish_id=?", (wish.id,)
        )
        assert [r["post_id"] for r in await cur.fetchall()] == [2]

    async def test_the_ai_rule_needs_its_key(self, tmp_path: Path, clock: FakeClock):
        without = BrainApp(settings_for(tmp_path / "a"), clock=clock, bot_client=FakeTelethon())
        assert without.judge is None and without.ai_worker.judge is None
        key = "sk-or-v1-" + "0" * 64
        app = BrainApp(
            settings_for(tmp_path / "b", openrouter_api_key=key, ai_daily_calls=50),
            clock=clock,
            bot_client=FakeTelethon(),
        )
        assert app.judge is not None and app.judge.model == "deepseek/deepseek-v4.1-flash"
        assert app.ai_worker.daily_calls == 50
        assert key not in repr(app.settings)
        await app.judge.close()

    async def test_flush_and_rescan_run(self, app: BrainApp):
        await app.flush_hits()
        await app.rescan()

    async def test_rescan_puts_a_drifted_matcher_right(self, app: BrainApp):
        await app.groups.apply_snapshot(snapshot(info(G1)), now=EPOCH0)
        assert not await app.reconcile_matcher()
        await app.storage.subscribe(OWNER, G1)  # behind the matcher's back
        assert G1 not in app.matcher.monitored
        await app.rescan()
        assert G1 in app.matcher.monitored
        assert not await app.reconcile_matcher()

    async def test_tell_reports_failure(self, app: BrainApp, sent: FakeBotClient):
        sent.fail_next(ConnectionError("down"))
        assert await app.tell(OWNER, "hello") is False
        assert await app.tell(OWNER, "hello") is True

    async def test_a_stalled_telegram_is_waited_for_once_then_not_until_a_ping_succeeds(
        self, app: BrainApp, sent: FakeBotClient
    ):
        """Telethon queues what a caller gave up on and sends it after reconnecting: trying
        again every minute would deliver a copy per minute of the outage."""
        app.tell_timeout = 0.05
        sent.hold()
        async with asyncio.timeout(5):
            assert await app.tell(OWNER, "one") is False  # waited, then gave up
            sent.release()
            assert await app.tell(OWNER, "two") is False  # not even tried
            assert sent.sent == []
            bot = app.bot_client
            assert isinstance(bot, FakeTelethon)
            bot.fail_requests = True
            await app.ping_bot()
            assert await app.tell(OWNER, "three") is False  # a failed ping proves nothing
            bot.fail_requests = False
            await app.ping_bot()
            assert await app.tell(OWNER, "four") is True
        assert [m.text for m in sent.sent] == ["four"]

    async def test_no_request_waits_for_telegram(
        self, app: BrainApp, client: httpx.AsyncClient, sent: FakeBotClient
    ):
        """Round 2 F1/F2: with the bot stuck, every endpoint still answers well inside the
        collector's 10 s, a lease move included."""
        app.tell_timeout = 30.0  # longer than this test waits for any answer
        sent.hold()
        await app.fb.touch_collector("laptop", now=EPOCH0)
        async with asyncio.timeout(5):
            r = await client.post(
                "/v1/status",
                content=body(status(CollectorState.BLOCKED, block=block(3))),
                headers=JSON,
            )
            assert r.status_code == 200
            lost = {
                "v": 1,
                "collector_id": CID,
                "events": [
                    {
                        "v": 1,
                        "collector_id": CID,
                        "event_id": 1,
                        "at": EPOCH0,
                        "kind": "block_opened",
                        "block_id": 3,
                    },
                ],
            }
            r = await client.post("/v1/events", json=lost)
            assert r.status_code == 200 and r.json()["acked"] == [1]
            r = await client.post("/v1/groups", content=body(snapshot(info(G1))), headers=JSON)
            assert r.status_code == 200
            await app.desk.transfer_lease("laptop")
            r = await client.get("/v1/control", params={"collector_id": CID})
            told = Control.model_validate_json(r.content)
            assert not told.lease_held_by(CID)  # the revoked holder hears it: nothing waits
            r = await client.get("/v1/control", params={"collector_id": "laptop"})
            assert Control.model_validate_json(r.content).lease_held_by("laptop")
            r = await client.get("/v1/control", params={"collector_id": CID})
            assert r.status_code == 200  # and again, as a contender
        assert sent.sent == []
        sent.release()
        await app.ping_bot()
        await app.monitor.tick()
        texts = [m.text for m in sent.sent]
        assert any("home-1 has checked in and was told to stop" in text for text in texts)
        assert any("home-1 asked to read, but laptop holds the lease" in text for text in texts)

    async def test_a_disabled_rule_is_explained_to_its_owner(
        self, app: BrainApp, sent: FakeBotClient
    ):
        rule = Rule(1, OWNER, G1, RuleKind.INCLUDE, "re:(a+)+$", None, 0)
        await app._rule_disabled(rule, "timeout")
        assert sent.sent == []  # queued: the matcher may be inside an ingest request
        await app.monitor.idle()
        assert sent.sent[-1].text == "⚠️ Rule re:(a+)+$ was disabled automatically: timeout"

    async def test_metrics_helpers(self, app: BrainApp, clock: FakeClock):
        assert app._status_age() is None
        await app.monitor.on_status(status())
        await app.monitor.tick()
        clock.tick(30)
        assert app._status_age() == 30
        app.source.last_item_at[G1] = EPOCH0 - 60
        assert app._group_ages() == {}  # not monitored: no series for it
        await app.groups.apply_snapshot(snapshot(info(G1)), now=EPOCH0)
        await app.storage.subscribe(OWNER, G1)
        await app.matcher.reload()
        assert app._group_ages() == {G1: 90.0}

    async def test_no_metrics_server_unless_a_port_is_set(self, app: BrainApp):
        await app._start_metrics()
        assert app.metrics_server is None

    async def test_the_metrics_server_serves_the_brain_series(
        self, tmp_path: Path, clock: FakeClock
    ):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        brain = BrainApp(
            settings_for(tmp_path, metrics_port=port),
            clock=clock,
            bot_client=FakeTelethon(),
            bot=FakeBotClient(),
        )
        await brain.setup()
        try:
            await brain._start_metrics()
            assert brain.metrics_server is not None
            async with httpx.AsyncClient(trust_env=False) as http:
                r = await http.get(f"http://127.0.0.1:{port}/metrics")
            assert 'fbb_ingest_items_total{result="stored"} 0' in r.text
        finally:
            await brain.close()


def test_uptime_is_measured_from_construction(tmp_path: Path):
    clock = FakeClock(dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    brain = BrainApp(
        settings_for(tmp_path), clock=clock, bot_client=FakeTelethon(), bot=FakeBotClient()
    )
    clock.tick(90)
    assert brain._uptime() == 90
