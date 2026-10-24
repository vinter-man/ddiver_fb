"""The bot with the fb hooks: owner commands, the refresh button, /status and the export tag."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ddiver_core import portable
from ddiver_core.bot import menus
from ddiver_core.bot.router import Action, Edit, Incoming, Reply, Router, SendDocument, Toast
from ddiver_core.delivery import Delivery
from ddiver_core.i18n import t
from ddiver_core.matcher import Matcher
from ddiver_core.rules import RuleKind
from ddiver_core.storage import Storage
from ddiver_core_testing import FakeBotClient, FakeClock
from ddiver_fb.control import ControlDesk
from ddiver_fb.groups import GroupBook
from ddiver_fb.monitor import CollectorMonitor
from ddiver_fb.pacing import FEED_COUNTER
from ddiver_fb.source import SourceMetrics
from ddiver_fb.store import FbStore
from ddiver_fb.ui import EXPORT_FORMAT, StatusSources, build_fb_router
from fbwire import BudgetKind, BudgetWindow, CollectorState, GroupState, ItemOutcome, Profile
from tests.brain.helpers import (
    CID,
    EPOCH0,
    G1,
    G2,
    MOD,
    OWNER,
    STRANGER,
    T0,
    block,
    info,
    seed_groups,
    snapshot,
    status,
)


@pytest.fixture
async def router(
    storage: Storage, monitor: CollectorMonitor, desk: ControlDesk, fb: FbStore, clock: FakeClock
) -> Router:
    metrics = SourceMetrics()
    metrics.count(ItemOutcome.STORED, 4)
    metrics.count(ItemOutcome.DUPLICATE)
    delivery = Delivery(FakeBotClient(), storage, clock=clock)
    return build_fb_router(
        storage,
        matcher=Matcher(storage, timeout=0.2),
        monitor=monitor,
        desk=desk,
        fb=fb,
        counters=lambda: StatusSources(metrics, delivery, T0),
        regex_timeout=0.2,
        clock=clock,
    )


async def say(router: Router, user: int, text: str, **kw: Any) -> list[Action]:
    return await router.dispatch(Incoming(user_id=user, text=text, **kw))


async def tap(router: Router, user: int, data: str) -> list[Action]:
    return await router.dispatch(Incoming(user_id=user, data=data))


def rendered(action: Action, lang: str = "en") -> str:
    """What the user reads: the literal text, or the catalog string filled in."""
    if getattr(action, "literal", False):
        return key(action)
    return t(lang, key(action), **getattr(action, "params", {}))


def key(action: Action) -> str:
    """The catalog key (or literal text) of any action but a document."""
    assert not isinstance(action, SendDocument)
    return action.key_or_text


def callbacks(actions: list[Action]) -> list[str]:
    return [d for a in actions for row in getattr(a, "buttons", None) or [] for _, d in row]


class TestOwnerOnly:
    @pytest.mark.parametrize("command", ["/pause", "/resume", "/pace cautious", "/lease x"])
    async def test_a_moderator_cannot(self, router: Router, desk: ControlDesk, command: str):
        (answer,) = await say(router, MOD, command)
        assert isinstance(answer, Reply) and key(answer) == "bot.err.owner_only"
        assert not await desk.paused() and await desk.pace_cap() is None

    @pytest.mark.parametrize("command", ["pause", "resume", "pace", "lease"])
    def test_the_router_itself_refuses_them(self, router: Router, command: str):
        # Registered owner-only, so a handler that forgot its own check would still be safe.
        _handler, owner_only = router._commands[command]
        assert owner_only

    async def test_a_stranger_only_meets_the_gate(self, router: Router):
        actions = await say(router, STRANGER, "/pause")
        assert [key(a) for a in actions if isinstance(a, Reply)] == ["bot.gate.pending"]

    async def test_a_forged_resume_button_is_refused(self, router: Router, desk: ControlDesk):
        (answer,) = await tap(router, MOD, "fbresume:17")
        assert isinstance(answer, Toast) and key(answer) == "bot.err.owner_only"


class TestPauseResume:
    async def test_pause_then_resume(self, router: Router, desk: ControlDesk):
        assert [key(a) for a in await say(router, OWNER, "/pause")] == ["fb.pause.done"]
        assert [key(a) for a in await say(router, OWNER, "/pause")] == ["fb.pause.already"]
        assert (await desk.control_for(CID)).paused
        assert [key(a) for a in await say(router, OWNER, "/resume")] == ["fb.resume.unpaused"]
        assert not await desk.paused()
        assert [key(a) for a in await say(router, OWNER, "/resume")] == ["fb.resume.nothing"]

    async def test_resume_shows_the_block_and_binds_the_button_to_it(
        self, router: Router, monitor: CollectorMonitor, desk: ControlDesk
    ):
        await monitor.on_status(status(CollectorState.BLOCKED, block=block(17)))
        actions = await say(router, OWNER, "/resume")
        assert callbacks(actions) == ["fbresume:17"]
        card = rendered(actions[0])
        assert "Block #17: suspect automated behaviour" in card and "rung 0" in card
        assert isinstance(actions[0], Reply)
        label = actions[0].buttons[0][0][0]
        assert menus.unwrap(label) == "▶️ Resume block #17"
        (sent,) = await tap(router, OWNER, "fbresume:17")
        assert isinstance(sent, Edit) and key(sent) == "fb.resume.sent"
        (again,) = await tap(router, OWNER, "fbresume:17")
        assert key(again) == "fb.resume.pending"
        commands = (await desk.control_for(CID)).commands
        assert [(c.kind, getattr(c, "block_id", None)) for c in commands] == [("resume", 17)]

    async def test_a_stale_button_sends_nothing(
        self, router: Router, monitor: CollectorMonitor, desk: ControlDesk
    ):
        await monitor.on_status(status(CollectorState.BLOCKED, block=block(17)))
        await say(router, OWNER, "/resume")
        # the probe failed meanwhile: block 18 is open now, and the old button must not clear it
        await monitor.on_status(status(CollectorState.BLOCKED, block=block(18)))
        (answer,) = await tap(router, OWNER, "fbresume:17")
        assert key(answer) == "fb.resume.stale"
        assert key((await tap(router, OWNER, "fbresume:x"))[0]) == "fb.resume.stale"
        assert (await desk.control_for(CID)).commands == ()

    @pytest.mark.parametrize(
        ("the_block", "expected"),
        [
            (block(until=EPOCH0 + 3600), "Block #17 runs until 2026-09-22 16:00:00 +03:00"),
            (block(until=None, rung=3), "Block #17 is the last rung"),
        ],
    )
    async def test_blocks_a_remote_resume_cannot_lift(
        self, router: Router, monitor: CollectorMonitor, desk, the_block, expected: str
    ):
        await monitor.on_status(status(CollectorState.BLOCKED, block=the_block))
        (answer,) = await say(router, OWNER, "/resume")
        assert rendered(answer).startswith(expected)
        assert callbacks([answer]) == []
        (pressed,) = await tap(router, OWNER, "fbresume:17")  # a button from an earlier card
        assert rendered(pressed).startswith(expected)
        assert (await desk.control_for(CID)).commands == ()

    async def test_an_advanced_protection_lock_is_resumed_with_the_owners_word(
        self, router: Router, monitor: CollectorMonitor, desk: ControlDesk
    ):
        await monitor.on_status(status(CollectorState.BLOCKED, block=block(no_probe=True)))
        (card,) = await say(router, OWNER, "/resume")
        assert "advanced-protection lock" in rendered(card)
        assert "fbcollector login" in rendered(card)
        assert callbacks([card]) == ["fbresume:17:lifted"]
        # a plain button (an old card) cannot resume it
        (pressed,) = await tap(router, OWNER, "fbresume:17")
        assert rendered(pressed).startswith("Block #17 is an advanced-protection lock")
        assert (await desk.control_for(CID)).commands == ()
        (sent,) = await tap(router, OWNER, "fbresume:17:lifted")
        assert key(sent) == "fb.resume.sent"
        (command,) = (await desk.control_for(CID)).commands
        assert getattr(command, "lock_lifted", False)

    async def test_nothing_is_sent_while_the_lease_moves(
        self, router: Router, monitor: CollectorMonitor, desk: ControlDesk, fb: FbStore
    ):
        await monitor.on_status(status(CollectorState.BLOCKED, block=block(17)))
        await fb.touch_collector("laptop", now=EPOCH0)
        await desk.transfer_lease("laptop")
        (answer,) = await tap(router, OWNER, "fbresume:17")
        assert key(answer) == "fb.resume.no_holder"
        (toast,) = await tap(router, OWNER, "refresh")
        assert key(toast) == "fb.refresh.no_holder"

    async def test_resume_also_unpauses(
        self, router: Router, monitor: CollectorMonitor, desk: ControlDesk
    ):
        await desk.pause()
        await monitor.on_status(status(CollectorState.BLOCKED, block=block(until=None)))
        actions = await say(router, OWNER, "/resume")
        assert key(actions[0]) == "fb.resume.unpaused"
        assert "last rung" in rendered(actions[1])


class TestPaceAndLease:
    async def test_pace(self, router: Router, desk: ControlDesk):
        shown = rendered((await say(router, OWNER, "/pace"))[0])
        assert shown.startswith("Pace cap: none")
        await say(router, OWNER, "/pace cautious")
        assert await desk.pace_cap() is Profile.CAUTIOUS
        await say(router, OWNER, "/pace NORMAL")
        assert await desk.pace_cap() is None  # "normal" can never beat a local cautious
        assert rendered((await say(router, OWNER, "/pace fast"))[0]).startswith("Pace cap:")

    async def test_lease(self, router: Router, desk: ControlDesk, fb: FbStore):
        assert "Lease: nobody yet" in rendered((await say(router, OWNER, "/lease"))[0])
        await desk.control_for(CID)
        await fb.touch_collector("laptop", now=EPOCH0)
        shown = rendered((await say(router, OWNER, "/lease"))[0])
        assert "Lease: home-1" in shown and "laptop" in shown
        assert "already holds" in rendered((await say(router, OWNER, f"/lease {CID}"))[0])
        assert "No collector called ghost" in rendered(
            (await say(router, OWNER, "/lease ghost"))[0]
        )
        revoking = rendered((await say(router, OWNER, "/lease laptop"))[0])
        assert revoking.startswith("🔒 home-1 is told to stop at its next check-in")
        assert "/lease laptop now" in revoking
        assert await desk.holder() == CID  # nobody is granted until home-1 has heard
        shown = rendered((await say(router, OWNER, "/lease"))[0])
        assert "Moving to laptop: waiting for home-1" in shown
        cancelled = rendered((await say(router, OWNER, f"/lease {CID}"))[0])
        assert cancelled == "Move cancelled: home-1 keeps the lease."
        moved = rendered((await say(router, OWNER, "/lease laptop NOW"))[0])
        assert moved == "🔒 Lease moved to laptop. home-1 is told to stop at its next check-in."
        assert await desk.holder() == "laptop"

    async def test_lease_to_the_holders_own_id_moves_it_to_its_newer_database(
        self, router: Router, desk: ControlDesk
    ):
        await desk.control_for(CID, "mac-db-7f3a0001")
        await desk.control_for(CID, "laptop-db-c29e002")  # the replacement, under one name
        replacing = rendered((await say(router, OWNER, f"/lease {CID}"))[0])
        assert replacing.startswith("🔒 home-1 with its current state database is told to stop")
        assert "/lease home-1 now" in replacing
        replaced = rendered((await say(router, OWNER, f"/lease {CID} now"))[0])
        assert replaced.startswith("🔒 Lease moved to home-1 with its newer state database.")
        assert (await desk.lease()).incarnation == "laptop-db-c29e002"


class TestListings:
    """Round 2 F10: the per-group listings sort (CHRONOLOGICAL_LISTINGS) had no way to be set:
    Control always said false, and overwrote the phase-0 monitored.toml's choice."""

    async def test_the_owner_turns_it_on_and_control_carries_it(
        self, router: Router, desk: ControlDesk, book: GroupBook, storage: Storage, fb: FbStore
    ):
        await seed_groups(book, G1)
        await storage.subscribe(OWNER, G1)
        shown = rendered((await say(router, OWNER, "/listings"))[0])
        assert "(CHRONOLOGICAL_LISTINGS) as well: none." in shown and f"({G1})" in shown
        done = rendered((await say(router, OWNER, f"/listings {G1} on"))[0])
        assert done.startswith(f"Listings sort for Group {G1}: on.")
        (entry,) = (await desk.control_for(CID)).monitored
        assert entry.listings
        await book.apply_snapshot(snapshot(info(G1)), now=EPOCH0 + 60)  # a sync keeps it
        group = await fb.group(G1)
        assert group is not None and group.listings
        await say(router, OWNER, f"/listings {G1} OFF")
        assert not (await desk.control_for(CID)).monitored[0].listings

    async def test_an_unknown_group_or_a_user_changes_nothing(self, router: Router):
        answer = rendered((await say(router, OWNER, "/listings 999 on"))[0])
        assert answer.startswith("No group with id 999 is known")
        assert key((await say(router, MOD, "/listings 999 on"))[0]) == "bot.err.owner_only"


class TestRefreshButton:
    async def test_the_owner_gets_it_and_it_queues_a_command(
        self, router: Router, book: GroupBook, desk: ControlDesk
    ):
        empty = await tap(router, OWNER, "chats:0")
        assert "refresh" in callbacks(empty)
        assert key(empty[0]) == "bot.chats.empty"
        await desk.control_for(CID)
        (toast,) = await tap(router, OWNER, "refresh")
        assert isinstance(toast, Toast) and key(toast) == "fb.refresh.sent"
        assert key((await tap(router, OWNER, "refresh"))[0]) == "fb.refresh.pending"
        assert [c.kind for c in (await desk.control_for(CID)).commands] == ["refresh_groups"]

    async def test_the_limit_is_said(self, router: Router, desk: ControlDesk, clock: FakeClock):
        await desk.control_for(CID)
        for n in range(3):
            await desk.request_refresh()
            done = snapshot(complete=False).model_copy(update={"nonce": f"nonce-{n + 1:04d}"})
            await desk.snapshot_arrived(done)
            clock.tick(60)
        (toast,) = await tap(router, OWNER, "refresh")
        assert isinstance(toast, Toast)
        assert (
            toast.alert and rendered(toast) == "At most 3 group refreshes a day; try again later."
        )

    async def test_a_moderator_has_no_button(self, router: Router, book: GroupBook, storage):
        await seed_groups(book, G1)
        await storage.set_chat_shared(G1, True)
        assert "refresh" not in callbacks(await tap(router, MOD, "chats:0"))
        assert key((await tap(router, MOD, "refresh"))[0]) == "bot.err.owner_only"


class TestTelegramOnlySettings:
    async def test_no_bots_toggle_and_a_forged_one_changes_nothing(
        self, router: Router, storage: Storage
    ):
        screen = await tap(router, OWNER, "set:main")
        assert "set:bots" not in callbacks(screen)
        await tap(router, OWNER, "set:bots")
        user = await storage.get_user(OWNER)
        assert user is not None and not user.include_bots
        assert "bots" not in rendered(screen[-1]).lower()


class TestNoWeeklySettings:
    """ddiver-core v0.1.0 offers a weekly summary and a weekly backup; the task that sends them
    is ddiver's, not the brain's, so the switches would change nothing while claiming to."""

    async def test_the_settings_screen_has_no_weekly_or_backup_toggle(self, router: Router):
        screen = await tap(router, OWNER, "set:main")
        assert isinstance(screen[-1], Edit)
        assert not {"set:weekly", "set:backup"} & set(callbacks(screen))
        labels = " ".join(label for row in screen[-1].buttons or [] for label, _ in row)
        assert "weekly" not in labels.lower() and "backup" not in labels.lower()
        assert labels  # the screen itself is still there, with language, zone and digest

    @pytest.mark.parametrize("forged", ["set:weekly", "set:backup"])
    @pytest.mark.parametrize("before", [True, False])
    async def test_a_forged_toggle_changes_nothing(
        self, router: Router, storage: Storage, forged: str, before: bool
    ):
        await storage.update_user(OWNER, weekly_report=before, auto_backup=before)
        actions = await tap(router, OWNER, forged)
        user = await storage.get_user(OWNER)
        assert user is not None and (user.weekly_report, user.auto_backup) == (before, before)
        assert not [a for a in actions if isinstance(a, Toast)]  # no "turned on/off" either
        assert not {"set:weekly", "set:backup"} & set(callbacks(actions))

    async def test_a_moderator_gets_the_same_screen(self, router: Router):
        screen = await tap(router, MOD, "set:main")
        assert "set:lang" in callbacks(screen)
        assert not {"set:weekly", "set:backup", "set:bots"} & set(callbacks(screen))


class TestFromRules:
    """Adding ``from:`` rules and checking them, in the bot, with the Facebook wording."""

    async def test_an_id_is_added_and_a_phone_or_a_username_is_explained(
        self, router: Router, book: GroupBook, storage: Storage
    ):
        await seed_groups(book, G1)
        await storage.subscribe(OWNER, G1)
        await tap(router, OWNER, f"radd:{G1}:include")
        lines = "from:100012345678901\nfrom:+359888123456\nfrom:@olena.k\nfrom:ol"
        report = rendered((await say(router, OWNER, lines))[0])
        rules = await storage.list_rules(OWNER, G1, RuleKind.INCLUDE)
        assert [r.pattern for r in rules] == ["from:100012345678901"]
        assert "Facebook does not show an author's phone number" in report
        assert "Facebook shows no @usernames" in report
        assert "fires on half the group" in report
        assert "Telegram" not in report

    async def test_the_rule_check_counts_them_as_rules_on_the_author(
        self, router: Router, book: GroupBook, storage: Storage
    ):
        await seed_groups(book, G1)
        await storage.subscribe(OWNER, G1)
        await storage.add_rule(OWNER, G1, RuleKind.INCLUDE, "from:100012345678901")
        answer = " ".join(rendered(a) for a in await say(router, OWNER, "/test flat in Varna"))
        assert "(plus 1 from: rules — those match on who wrote a post" in answer


class TestExport:
    async def test_the_tag_and_the_file_name(
        self, router: Router, book: GroupBook, storage: Storage
    ):
        await seed_groups(book, G1)
        await storage.subscribe(OWNER, G1)
        await storage.add_rule(OWNER, G1, RuleKind.INCLUDE, "varna")
        (doc,) = await say(router, OWNER, "/export")
        assert isinstance(doc, SendDocument)
        assert doc.filename == "ddiver-fb-2026-09-22.json"
        data = json.loads(doc.content)
        assert data["format"] == EXPORT_FORMAT == "ddiver-fb.config"
        assert data["chats"] == [
            {"id": G1, "username": None, "title": f"Group {G1}", "subscribed": True}
        ]

    async def test_a_ddiver_export_is_refused(self, router: Router):
        await say(router, OWNER, "/import")
        ddiver_file = json.dumps({"format": portable.FORMAT, "version": 1, "chats": []})
        (answer,) = await say(router, OWNER, ddiver_file)
        assert rendered(answer) == "Could not read it: not a ddiver-fb export"

    async def test_our_own_export_round_trips(
        self, router: Router, book: GroupBook, storage: Storage
    ):
        await seed_groups(book, G1)
        await storage.subscribe(OWNER, G1)
        await storage.add_rule(OWNER, G1, RuleKind.INCLUDE, "varna")
        (doc,) = await say(router, OWNER, "/export")
        await storage.unsubscribe(OWNER, G1)
        await say(router, OWNER, "/import")
        assert isinstance(doc, SendDocument)
        (answer,) = await say(router, OWNER, "x", document=doc.content)
        assert rendered(answer).startswith("✅ Imported: 1 chats, 0 rules")


class TestStatus:
    async def test_before_any_collector(self, router: Router):
        text = rendered((await say(router, OWNER, "/status"))[0])
        assert "Collector: none has reported yet" in text
        assert "Delay: nothing is being read right now" in text

    async def test_the_owner_sees_the_machinery(
        self,
        *,
        router: Router,
        monitor: CollectorMonitor,
        book: GroupBook,
        storage: Storage,
        desk: ControlDesk,
        fb: FbStore,
    ):
        await monitor.on_snapshot(snapshot(info(G1, "Оренда"), info(G2, "Robota")))
        await storage.subscribe(OWNER, G1)
        await storage.subscribe(OWNER, G2)
        await book.mark(G2, now=EPOCH0, followed=False)
        await book.mark(G1, now=EPOCH0, sort_ignored=True)
        await desk.set_pace_cap(Profile.CAUTIOUS)
        await fb.touch_collector("laptop", now=EPOCH0)
        budget = (
            BudgetWindow(kind=BudgetKind.NAVIGATION, window_seconds=3600, cap=20, used=5),
            BudgetWindow(kind=BudgetKind.SCROLL, window_seconds=86400, cap=500, used=100),
        )
        await monitor.on_status(status(budget=budget, detail="reading the feed"))
        text = rendered((await say(router, OWNER, "/status"))[0])
        assert "Collector home-1: ✅ reading" in text
        assert "pace: cautious" in text  # the cap makes the effective profile cautious
        assert "Details: reading the feed" in text
        assert "page loads 15 of 20 per 60 min; scrolls 400 of 500 per 24 h" in text
        assert "Pace capped at cautious" in text
        assert "Without the lease (told to stop): laptop" in text
        assert "Groups: 2 read · 2 known · 0 unreadable" in text
        assert "👁 Not followed, slow backstop only: Robota" in text
        assert "↕️ Ignore newest-first sorting: Оренда" in text
        assert "Delay: up to 19 min for new posts" in text  # cautious: U(10, 18) + 1 min
        assert "Up to 4 h in: Robota" in text  # cautious: max(3 h, 2 / 3.2) × 1.3 + 1 min
        assert "Since start: 4 new posts · 1 repeats" in text
        assert "Notifications: 0 sent" in text
        assert "/pause · /resume · /pace · /lease" in text

    async def test_a_block_and_a_pause_on_the_owner_status(
        self, router: Router, monitor: CollectorMonitor, desk: ControlDesk, book: GroupBook
    ):
        await seed_groups(book, G1)
        await book.mark(G1, now=EPOCH0, state=GroupState.LOST)
        await monitor.on_status(status(CollectorState.BLOCKED, block=block(until=EPOCH0 + 60)))
        await desk.pause()
        text = rendered((await say(router, OWNER, "/status"))[0])
        assert "⛔ Block #17: suspect automated behaviour — until 2026-09-22 15:01:00" in text
        assert "⏸ Paused by you" in text and "🚫 Unreadable: Group" in text
        assert "Delay: nothing is being read right now" in text

    async def test_a_final_block(self, router: Router, monitor: CollectorMonitor):
        await monitor.on_status(status(CollectorState.BLOCKED, block=block(until=None)))
        text = rendered((await say(router, OWNER, "/status"))[0])
        assert "until re-planned on the collector host" in text

    async def test_a_moderator_sees_their_groups_and_their_delay_only(
        self,
        *,
        router: Router,
        monitor: CollectorMonitor,
        book: GroupBook,
        storage: Storage,
        desk: ControlDesk,
    ):
        await seed_groups(book, G1, G2)
        await storage.set_chat_shared(G1, True)
        await storage.subscribe(MOD, G1)
        await storage.subscribe(OWNER, G2)
        await book.mark(G2, now=EPOCH0, followed=False)
        await monitor.on_status(status())
        await desk.pause()
        text = rendered((await say(router, MOD, "/status"))[0], "uk")
        assert "Колектор: ✅ читає" in text and "home-1" not in text
        assert "Ваших груп: 1" in text
        assert "⏸ Власник поставив читання на паузу." in text
        assert "/pause" not in text and "Групи:" not in text
        await desk.unpause()
        text = rendered((await say(router, MOD, "/status"))[0], "uk")
        # normal profile: the feed interval's top (9.5 min) plus a minute, rounded up, not down
        assert "Затримка: до 11 хв для нових дописів" in text
        assert "повільний" not in text  # G2 is not theirs

    @pytest.mark.parametrize(
        ("state", "resumes", "expected"),
        [
            (
                CollectorState.QUIET,
                EPOCH0 + 3600,
                (
                    "Delay: nothing is read right now (🌙 quiet hours); reading resumes at "
                    "2026-09-22 16:00:00 +03:00"
                ),
            ),
            (
                CollectorState.BREAK,
                None,
                "Delay: nothing is read right now (☕ on a break); reading resumes by itself",
            ),
            (
                CollectorState.UNKNOWN,
                EPOCH0 + 600,
                (
                    "Delay: nothing is read right now (❓ backing off after a page it did not "
                    "recognise); reading resumes at 2026-09-22 15:10:00 +03:00"
                ),
            ),
        ],
    )
    async def test_a_planned_pause_is_said_as_one_not_as_a_delay(
        self, router: Router, monitor: CollectorMonitor, state, resumes, expected: str
    ):
        """C1-08: quiet hours, a break or a back-off promised "up to 11 min"."""
        await monitor.on_status(status(state, resumes_at=resumes))
        text = rendered((await say(router, OWNER, "/status"))[0])
        assert expected in text
        assert "for new posts" not in text

    async def test_the_shoulder_bound_unless_the_collector_says_its_interval(
        self, router: Router, monitor: CollectorMonitor
    ):
        await monitor.on_status(status(sent_at=EPOCH0))
        text = rendered((await say(router, OWNER, "/status"))[0])
        assert "Delay: up to 11 min for new posts" in text
        assert "Up to 26 min in the hour after and the 1.5 h before quiet hours" in text
        await monitor.on_status(status(sent_at=EPOCH0 + 60, counters={FEED_COUNTER: 25 * 60}))
        text = rendered((await say(router, OWNER, "/status"))[0])
        assert "Delay: up to 26 min for new posts" in text  # it is in the shoulder now
        assert "quiet hours" not in text

    async def test_a_user_is_told_which_of_their_groups_are_over_the_cap(
        self, router: Router, monitor: CollectorMonitor, book: GroupBook, storage: Storage
    ):
        ids = [7_000_000 + n for n in range(26)]
        await book.apply_snapshot(snapshot(*(info(g) for g in ids)), now=EPOCH0)
        for gid in ids:
            await storage.subscribe(OWNER, gid)
        await storage.set_chat_shared(ids[-1], True)
        await storage.subscribe(MOD, ids[-1])  # still the least wanted but one: 2 subscribers
        await storage.set_chat_shared(ids[0], True)
        await storage.subscribe(MOD, ids[0])
        await storage.unsubscribe(OWNER, ids[-2])
        await storage.set_chat_shared(ids[-2], True)
        await storage.subscribe(MOD, ids[-2])  # one subscriber, highest id: over the cap
        await monitor.on_status(status())
        text = rendered((await say(router, MOD, "/status"))[0], "uk")
        assert f"⚠️ Не читаються (понад ліміт у 25 груп): Group {ids[-2]}" in text

    async def test_feed_not_chronological_means_the_backstop_delay_for_all(
        self, router: Router, monitor: CollectorMonitor, book: GroupBook, storage: Storage
    ):
        await seed_groups(book, G1)
        await storage.subscribe(OWNER, G1)
        await monitor.on_status(status(CollectorState.FEED_NOT_CHRONO))
        text = rendered((await say(router, OWNER, "/status"))[0])
        assert "Delay: up to 2 h — groups are read one by one" in text

    async def test_over_the_cap_is_listed(
        self, router: Router, monitor: CollectorMonitor, book: GroupBook, storage: Storage
    ):
        ids = [7_000_000 + n for n in range(27)]
        await book.apply_snapshot(snapshot(*(info(g) for g in ids)), now=EPOCH0)
        for gid in ids:
            await storage.subscribe(OWNER, gid)
        await monitor.on_status(status())
        text = rendered((await say(router, OWNER, "/status"))[0])
        assert "⚠️ Over the 25-group limit, not read: Group 7000025, Group 7000026" in text
