"""The collector and the brain against each other, end to end, offline, on 127.0.0.1.

Every test starts the brain's real ingest server with a real ``fb.sqlite`` and a real collector
runtime whose uplink reaches it over a socket; only Telegram and Facebook are fakes (see
``harness``). Each test is one promise of the contract between the two:

(a) a matching post becomes exactly one notification per subscriber, with Facebook links only;
(b) a lost answer or a second sighting never notifies twice;
(c) a busy database leaves the posts queued at home, and they arrive later;
(d) the lease: a second collector reads nothing until the owner moves it;
(e) ``/resume`` reaches the collector's ladder for the open block only, and only once;
(f) "Refresh groups" is exactly one sync, after the delay, and the brain knows it landed;
(g) the collector's state and events reach ``/status`` and the owner, and an outage or a full
    outbox reaches the owner directly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ddiver_core.bot.router import Action, Edit, Reply, Toast
from ddiver_core.i18n import t
from ddiver_core.rules import RuleKind
from fbcollector.clock import FakeClock
from fbcollector.control import COMMAND_DELAY
from fbcollector.gates import Activity, GateContext
from fbcollector.model import CollectorState, GroupMeta, PageState
from fbcollector.reader import FLAG_FEED_ABSENT
from fbcollector.scheduler import ActionKind
from fbcollector.uplink import KEY_BACK, KEY_DOWN, UplinkError
from fbwire.models import EventKind, GroupState, ItemOutcome
from tests.collector.reader_support import grant
from tests.contract.harness import (
    G1,
    G2,
    MOD,
    OWNER,
    Brain,
    Collector,
    Lossy,
    OlderCollector,
    free_port,
    start_brain,
    start_collector,
    stop_collector,
)

POST = "500000000000001"
POST_URL = f"https://www.facebook.com/groups/{G1}/posts/{POST}/"
HOUR = 3600.0


def now(home: Collector) -> int:
    """The control generation a grant made now would carry (the lease moved it)."""
    return home.rt.control.generation


async def onboard(home: Collector, brain: Brain) -> None:
    """A collector's first minutes with a brain: the lease, a sync, subscribers, the set."""
    applied = await home.uplink.poll_control()
    assert applied.lease  # the first collector to check in holds it
    result = await home.rt.reader.read(grant(ActionKind.SYNC, steps=6, generation=now(home)))
    assert result.sync is not None and result.sync.complete
    await home.uplink.queue_snapshot(result.sync)
    assert await home.uplink.send_snapshot()
    storage = brain.app.storage
    assert await storage.subscribe(OWNER, int(G1))
    await storage.add_rule(OWNER, int(G1), RuleKind.INCLUDE, "varna")
    await storage.set_chat_shared(int(G1), True)
    assert await storage.subscribe(MOD, int(G1))
    await storage.add_rule(MOD, int(G1), RuleKind.INCLUDE, "rent")
    await brain.app.invalidate(int(G1))
    home.clock.advance(60)
    await home.uplink.poll_control()
    assert await home.store.monitored_ids() == {G1}  # the brain's set: subscribed groups only
    brain.sent.sent.clear()  # the owner was told which groups the sync brought


async def read_feed(home: Collector) -> None:
    result = await home.rt.reader.read(grant(ActionKind.FEED, generation=now(home)))
    assert result.page_state is PageState.OK


def urls(message: Any) -> list[str]:
    return [e.url for e in message.entities if getattr(e, "url", None)]


def buttons(actions: list[Action]) -> list[str]:
    return [data for a in actions for row in (getattr(a, "buttons", None) or []) for _, data in row]


def results(brain: Brain) -> dict[ItemOutcome, int]:
    return dict(brain.app.source.metrics.results)


async def test_a_matching_post_is_one_notification_per_subscriber(
    home: Collector, brain: Brain, clock: FakeClock
) -> None:
    await onboard(home, brain)
    clock.advance(600)
    home.site.post(G1, POST, "Flat for rent in Varna, 400 EUR")
    home.site.post(G1, "500000000000002", "Selling a sofa")  # matches nobody
    home.site.post(G2, "500000000000003", "Flat for rent in Varna, 300 EUR")  # not watched

    async def delivered() -> bool:
        return await home.store.outbox_depth() == 0 and results(brain)[ItemOutcome.STORED] == 2

    await home.drive(1800, until=delivered)
    assert await brain.app.delivery.wait_idle(5)
    for user in (OWNER, MOD):
        (note,) = brain.notifications(user)
        assert "Flat for rent in Varna" in note.text
        assert "Flats Varna" in note.text
        assert "t.me" not in note.text
        assert POST_URL in urls(note)
        assert all(u.startswith("https://www.facebook.com/") for u in urls(note))
    assert len(brain.notifications()) == 2
    # the scheduler read the feed under the lease; the unwatched group's post never left home
    assert home.granted(ActionKind.FEED)
    assert results(brain)[ItemOutcome.NOT_MONITORED] == 0


async def test_a_lost_answer_and_a_second_sighting_do_not_notify_twice(
    tmp_path: Path, clock: FakeClock, brain: Brain
) -> None:
    lossy = Lossy()
    home = await start_collector(tmp_path / "home", clock, brain, transport=lossy)
    try:
        await onboard(home, brain)
        clock.advance(600)
        home.site.post(G1, POST, "Flat for rent in Varna")
        await read_feed(home)
        lossy.lose["/v1/items"] = 1
        with pytest.raises(UplinkError, match="ReadTimeout"):
            await home.uplink.push_items()
        assert await home.store.outbox_depth() == 1  # no answer: kept
        assert await brain.app.delivery.wait_idle(5)
        assert len(brain.notifications()) == 2  # yet the brain had it
        assert await home.uplink.push_items() == 1  # the resend comes back "duplicate"
        assert results(brain)[ItemOutcome.DUPLICATE] == 1
        clock.advance(300)
        await read_feed(home)  # the same post on the next read: seen, not queued again
        assert await home.store.outbox_depth() == 0
        assert await home.uplink.push_items() == 0
        assert await brain.app.delivery.wait_idle(5)
        assert [len(brain.notifications(u)) for u in (OWNER, MOD)] == [1, 1]
    finally:
        await stop_collector(home)


async def test_a_busy_database_keeps_posts_queued_until_they_land(
    home: Collector, brain: Brain, clock: FakeClock
) -> None:
    await onboard(home, brain)
    clock.advance(600)
    home.site.post(G1, POST, "Flat for rent in Varna")
    home.site.post(G1, "500000000000002", "Room for rent near the sea")
    await read_feed(home)
    lock = brain.locked()
    try:
        with pytest.raises(UplinkError, match="busy"):
            await home.uplink.push_items()
        with pytest.raises(UplinkError, match="status: HTTP 503"):
            await home.uplink.send_status()
        assert await home.store.outbox_depth() == 2
        assert brain.notifications() == []
    finally:
        lock.rollback()
        lock.close()
    clock.advance(31)
    assert await home.uplink.push_items() == 2
    assert await brain.app.delivery.wait_idle(5)
    assert len(brain.notifications(OWNER)) == 1  # "varna"
    assert len(brain.notifications(MOD)) == 2  # "rent", twice
    assert await home.uplink.send_status()


async def test_a_second_collector_reads_nothing_until_the_owner_moves_the_lease(
    tmp_path: Path, clock: FakeClock, brain: Brain, home: Collector
) -> None:
    await onboard(home, brain)
    laptop = await start_collector(tmp_path / "laptop", clock, brain, collector_id="laptop-1")
    try:
        applied = await laptop.uplink.poll_control()
        assert not applied.lease
        await laptop.drive(900)  # its feed read comes due, again and again
        refused = [h for h in laptop.holds if h.state is CollectorState.PAUSED]
        assert refused
        assert all(any("no lease (held by home-1)" in r for r in h.reasons) for h in refused)
        assert laptop.grants == [] and laptop.site.calls == []  # not even Chrome
        assert any(
            "laptop-1" in text for text in await brain.alerts()
        )  # the owner sees a contender
        await laptop.store.upsert_group(GroupMeta(id=G2, name="Flats Sofia"), synced=True)
        assert await laptop.uplink.introduce() is not None
        assert not await laptop.uplink.send_snapshot()  # 409: only the holder syncs groups
        assert laptop.uplink.stats.last["groups"] == 409

        (reply,) = await brain.say(OWNER, "/lease laptop-1")
        assert isinstance(reply, Reply) and "laptop-1" in reply.key_or_text
        clock.advance(60)
        applied = await home.uplink.poll_control()  # the holder hears it must stop
        assert not applied.lease
        assert home.rt.control.state.lease_holder == "laptop-1"
        reads = len(home.grants)
        await home.drive(900)
        assert len(home.grants) == reads  # the old holder reads nothing more
        assert any("no lease (held by laptop-1)" in r for h in home.holds for r in h.reasons)

        assert (await laptop.uplink.poll_control()).lease

        async def read() -> bool:
            return "feed" in laptop.site.reads

        await laptop.drive(600, until=read)
        assert laptop.site.launches == 1
    finally:
        await stop_collector(laptop)


async def test_two_machines_under_one_name_do_not_both_read(
    home: Collector, brain: Brain, clock: FakeClock, tmp_path: Path
) -> None:
    """Round 2 F4: a forgotten Mac and its replacement laptop, both still called home-1, each
    with its own state database. The brain used to grant both; the second is now refused, the
    owner told, and ``/lease home-1`` moves the lease to it in two steps."""
    await onboard(home, brain)
    laptop = await start_collector(tmp_path / "laptop", clock, brain, collector_id="home-1")
    try:
        assert not (await laptop.uplink.poll_control()).lease
        await laptop.drive(900)
        assert laptop.grants == [] and laptop.site.calls == []  # not even Chrome
        assert (await home.uplink.poll_control()).lease  # the first one reads on
        told = await brain.alerts()
        assert sum(text.startswith("🔒 A second state database reports as home-1") for text in told)
        assert not await laptop.uplink.send_snapshot()  # nothing it says drives the brain

        (reply,) = await brain.say(OWNER, "/lease home-1")
        assert isinstance(reply, Reply) and "newer one gets the lease" in reply.key_or_text
        clock.advance(60)
        assert not (await home.uplink.poll_control()).lease  # the old one hears it
        reads = len(home.grants)
        await home.drive(900)
        assert len(home.grants) == reads
        assert (await laptop.uplink.poll_control()).lease

        async def read() -> bool:
            return "feed" in laptop.site.reads

        await laptop.drive(600, until=read)
        assert "feed" in laptop.site.reads
    finally:
        await stop_collector(laptop)


async def test_a_lease_move_completes_while_telegram_hangs(
    home: Collector, brain: Brain, clock: FakeClock, tmp_path: Path
) -> None:
    """Round 2 F1 (critical): the brain told the owner about the move *before* answering the
    old holder. With Telegram stalled that answer never came, the old holder kept its grant,
    and the new one was granted beside it: two machines on one account."""
    await onboard(home, brain)
    laptop = await start_collector(tmp_path / "laptop", clock, brain, collector_id="laptop-1")
    try:
        assert not (await laptop.uplink.poll_control()).lease
        await brain.say(OWNER, "/lease laptop-1")
        brain.app.tell_timeout = 30.0  # longer than this test waits for any answer
        brain.sent.hold()  # Telegram takes nothing from now on
        clock.advance(60)
        async with asyncio.timeout(5):
            applied = await home.uplink.poll_control()  # answered, not stuck behind a send
            assert not applied.lease
            assert (await laptop.uplink.poll_control()).lease
            clock.advance(60)
            assert not (await home.uplink.poll_control()).lease  # a contender now, promptly
        state = home.rt.control.state
        assert (state.lease_granted, state.lease_holder) == (False, "laptop-1")
        assert not (await home.rt.control.gate(GateContext(Activity.NAVIGATE))).ok
        assert (await laptop.rt.control.gate(GateContext(Activity.NAVIGATE))).ok
        await home.drive(600)
        assert home.site.reads.count("feed") == 0  # the old holder reads nothing more

        brain.sent.release()
        await brain.app.ping_bot()  # Telegram answers again: what was held back goes out
        await brain.app.monitor.tick()
        told = await brain.alerts()
        assert sum("home-1 has checked in and was told to stop" in text for text in told) == 1
        assert sum("home-1 asked to read, but laptop-1 holds" in text for text in told) == 1
    finally:
        await stop_collector(laptop)


async def blocked_and_resumable(home: Collector, brain: Brain, clock: FakeClock) -> int:
    """A warning, reported; its 6 h pass; the owner presses the resume button. Its block id."""
    block = await home.rt.ladder.trip(PageState.BLOCKED, reason="suspect automated behaviour")
    assert f"block_opened:{block.id}" in home.alerts.sent_keys()  # told directly, too
    await home.uplink.send_events()
    await home.uplink.send_status()
    told = [text for text in await brain.alerts() if f"#{block.id}: suspect" in text]
    assert len(told) == 1  # the event and the status after it are one episode
    clock.advance(6 * HOUR + 60)
    await home.uplink.send_status()
    card = await brain.say(OWNER, "/resume")
    (data,) = buttons(card)
    assert data == f"fbresume:{block.id}"
    (edit,) = await brain.tap(OWNER, data)
    assert isinstance(edit, Edit) and edit.key_or_text == "fb.resume.sent"
    return block.id


async def test_resume_reaches_the_ladder_once_and_a_probe_lifts_the_block(
    home: Collector, brain: Brain, clock: FakeClock
) -> None:
    await onboard(home, brain)
    block_id = await blocked_and_resumable(home, brain, clock)
    applied = await home.uplink.poll_control()
    assert [(o.kind, o.result) for o in applied.commands] == [("resume", "accepted")]
    probe = await home.rt.ladder.probe_request()
    assert probe is not None and probe.block_id == block_id
    delay = probe.not_before - clock.time()
    assert COMMAND_DELAY[0] <= delay <= COMMAND_DELAY[1]
    clock.advance(60)
    applied = await home.uplink.poll_control()  # re-sent until the block closes: used once
    assert [(o.kind, o.result) for o in applied.commands] == [("resume", "replayed")]
    assert await home.rt.ladder.probe_request() == probe

    async def lifted() -> bool:
        return await home.store.current_block() is None

    await home.drive(900, until=lifted)
    (at, _), *more = home.granted(ActionKind.PROBE)
    assert more == [] and at >= probe.not_before
    await home.uplink.send_events()  # BLOCK_CLOSED
    assert any(
        t("en", "fb.alert.block_closed", block=block_id) == text for text in await brain.alerts()
    )
    clock.advance(60)
    applied = await home.uplink.poll_control()
    assert (applied.commands, applied.skipped) == ((), 0)  # the brain knows it landed


async def test_a_resume_for_a_block_that_is_no_longer_open_does_nothing(
    home: Collector, brain: Brain, clock: FakeClock
) -> None:
    await onboard(home, brain)
    await blocked_and_resumable(home, brain, clock)
    newer = await home.rt.ladder.trip(PageState.BLOCKED, reason="temporarily blocked")
    applied = await home.uplink.poll_control()
    assert [(o.kind, o.result) for o in applied.commands] == [("resume", "stale")]
    assert await home.rt.ladder.probe_request() is None
    current = await home.store.current_block()
    assert current is not None and current.id == newer.id
    await home.uplink.send_status()  # the brain sees the new block: the old command is moot
    clock.advance(60)
    assert (await home.uplink.poll_control()).commands == ()


async def test_refresh_groups_is_one_sync_after_the_delay(
    home: Collector, brain: Brain, clock: FakeClock
) -> None:
    await onboard(home, brain)
    g3 = "400000000000003"
    home.site.groups[g3] = "Rooms Plovdiv"  # joined by hand in the collector's Chrome
    (toast,) = await brain.tap(OWNER, "refresh")
    assert isinstance(toast, Toast) and toast.key_or_text == "fb.refresh.sent"
    applied = await home.uplink.poll_control()
    assert [(o.kind, o.result) for o in applied.commands] == [("refresh_groups", "scheduled")]
    refresh_at = home.rt.control.refresh_due()
    assert refresh_at is not None
    assert COMMAND_DELAY[0] <= refresh_at - clock.time() <= COMMAND_DELAY[1]
    clock.advance(20)
    await home.uplink.poll_control()  # not yet due
    clock.advance(40)
    applied = await home.uplink.poll_control()
    assert [(o.kind, o.result) for o in applied.commands] == [("refresh_groups", "replayed")]

    await home.drive(1800)
    ((at, sync),) = home.granted(ActionKind.SYNC)
    assert sync.action.reason == "refresh" and at >= refresh_at
    assert home.site.reads.count("joins") == 2  # the onboarding sync and this one
    known = await brain.app.fb.group(int(g3))
    assert known is not None and known.state is GroupState.MEMBER
    assert any("Rooms Plovdiv" in text for text in await brain.alerts())  # the owner's sync summary
    clock.advance(60)
    applied = await home.uplink.poll_control()
    assert applied.commands == ()  # the snapshot echoed the nonce: the command is closed


async def test_state_and_events_reach_status_and_the_owner(
    home: Collector, brain: Brain, clock: FakeClock
) -> None:
    await onboard(home, brain)
    await home.drive(300)
    assert await home.uplink.send_status()
    (owner_view,) = await brain.say(OWNER, "/status")
    assert isinstance(owner_view, Reply)
    text = owner_view.key_or_text
    assert "home-1" in text
    assert t("en", "fb.state.ok") in text
    (user_view,) = await brain.say(MOD, "/status")
    assert isinstance(user_view, Reply)
    assert "home-1" not in user_view.key_or_text  # users see no machinery

    await home.rt.events(
        EventKind.GROUP_LOST, group_id=G1, group_name="Flats Varna", detail="members only"
    )
    assert f"group_lost:{G1}" in home.alerts.sent_keys()  # the direct path
    assert await home.uplink.send_events() == 1
    assert any("Flats Varna" in text for text in await brain.alerts())  # and through the brain
    lost = await brain.app.fb.group(int(G1))
    assert lost is not None and lost.state is GroupState.LOST
    clock.advance(60)
    await home.uplink.poll_control()
    assert await home.store.monitored_ids() == set()  # nobody can be served from a lost group


async def test_a_group_the_feed_stopped_showing_stays_unfollowed_after_a_sync(
    home: Collector, brain: Brain, clock: FakeClock
) -> None:
    """Round 2 F5: the next snapshot said "followed" again (the group is subscribed by
    definition) and undid the event: /status promised the feed's delay for a group whose
    posts only came through backstops, hours apart."""
    await onboard(home, brain)
    # a backstop read the group page: the account is subscribed
    await home.store.upsert_group(GroupMeta(id=G1, subscribe_status="IS_SUBSCRIBED"))
    row = await home.store.get_group(G1)
    assert row is not None
    # what the reader does when the feed has not shown G1 for 48 h while backstops find posts
    await home.store.set_group_flags(G1, {*row.flags, FLAG_FEED_ABSENT})
    await home.rt.events(EventKind.NOT_FOLLOWED, group_id=G1, detail="absent from the feed")
    assert await home.uplink.send_events() == 1
    before = await brain.app.fb.group(int(G1))
    assert before is not None and before.followed is False
    result = await home.rt.reader.read(
        grant(ActionKind.SYNC, steps=6, generation=now(home))
    )  # the daily sync
    assert result.sync is not None
    await home.uplink.queue_snapshot(result.sync)
    assert await home.uplink.send_snapshot()
    after = await brain.app.fb.group(int(G1))
    assert after is not None and after.followed is False


async def test_an_outage_and_a_full_outbox_reach_the_owner_directly(
    tmp_path: Path, clock: FakeClock
) -> None:
    brain = await start_brain(tmp_path / "brain", clock, port=free_port())
    home = await start_collector(tmp_path / "home", clock, brain, outbox_cap=100)
    try:
        await onboard(home, brain)
        await brain.app.ingest.stop()  # the tunnel is up, the brain is not
        clock.advance(600)
        for n in range(80):
            home.site.post(G1, f"5000000000{n:05d}", f"Room {n} for rent")
        await read_feed(home)  # reading goes on: the lease is valid for hours
        assert "outbox_high:" in home.alerts.sent_keys()
        for _ in range(7):
            assert not await home.uplink.cycle()
            clock.advance(60)
        assert home.alerts.sent_keys().count(KEY_DOWN) == 1
        (down,) = [text for key, text in home.alerts.sent if key == KEY_DOWN]
        assert "80 post(s) wait" in down
        await brain.app.ingest.start()
        assert await home.uplink.cycle()
        assert home.alerts.sent_keys()[-1] == KEY_BACK
        assert await home.store.outbox_depth() == 0
        outbox_told = t("en", "fb.alert.outbox_high", id="home-1")
        assert outbox_told in await brain.alerts()  # the event outlived the outage
    finally:
        await stop_collector(home)
        await brain.app.close()


async def test_a_brain_one_wire_version_ahead_still_serves_an_older_collector(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 2 F8: the brain answered in its own version, which a collector still on the
    older one refuses: no lease, posts stored but never deleted at home, events never acked.
    It deploys first after a bump, so it answers every request in that request's version."""
    import fbwire.models as wire

    monkeypatch.setattr(wire, "WIRE_VERSION", wire.WIRE_VERSION + 1)
    older = OlderCollector(wire.WIRE_VERSION - 1)
    brain = await start_brain(tmp_path / "brain", clock, port=free_port())
    home = await start_collector(tmp_path / "home", clock, brain, transport=older)
    try:
        await onboard(home, brain)
        clock.advance(600)
        home.site.post(G1, POST, "Flat in Varna for rent, 400 EUR")
        await read_feed(home)
        await home.rt.events(EventKind.OUTBOX_HIGH, detail="test")
        assert await home.uplink.cycle()
        assert await home.store.outbox_depth() == 0 and await home.store.pending_events() == []
        assert await brain.app.delivery.wait_idle(5)
        assert len(brain.notifications(OWNER)) == 1
        assert older.answered and set(older.answered) == {wire.WIRE_VERSION - 1}
    finally:
        await stop_collector(home)
        await brain.app.close()


async def test_a_proxy_in_the_environment_is_never_used(
    home: Collector, brain: Brain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bearer token goes to the tunnel's end and nowhere else, whatever the shell exports."""
    closed = f"http://127.0.0.1:{free_port()}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "all_proxy"):
        monkeypatch.setenv(name, closed)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    assert (await home.uplink.poll_control()).lease
