"""GET /v1/control's content: one lease, 25 groups at most, and commands until they land."""

from __future__ import annotations

import pytest

from ddiver_core.storage import Role, Storage
from ddiver_core_testing import FakeClock
from ddiver_fb.control import (
    LEASE_VALID_FOR,
    RESUME_TTL,
    ControlDesk,
    LeaseMove,
    LeaseOutcome,
    RefreshOutcome,
    ResumeOutcome,
    ResumeVerdict,
    resume_verdict,
)
from ddiver_fb.groups import GroupBook
from ddiver_fb.store import FbStore
from fbwire import (
    MAX_MONITORED,
    Control,
    GroupsSnapshot,
    GroupState,
    Profile,
    RefreshGroupsCommand,
    ResumeCommand,
    to_json,
)
from tests.brain.helpers import (
    CID,
    EPOCH0,
    G1,
    G2,
    MOD,
    OWNER,
    block,
    info,
    seed_groups,
    snapshot,
    status,
)

LAPTOP = "laptop"


async def refused_by(fb: FbStore, collector_id: str) -> str | None:
    row = await fb.collector(collector_id)
    assert row is not None
    return row.refused_by


async def two_collectors(desk: ControlDesk, fb: FbStore) -> None:
    await desk.control_for(CID)
    await fb.touch_collector(LAPTOP, now=EPOCH0)


class TestLease:
    async def test_the_first_collector_gets_it_and_keeps_it(self, desk: ControlDesk, fb: FbStore):
        first = await desk.control_for(CID)
        assert first.lease_held_by(CID)
        assert (first.lease.generation, first.lease.valid_for) == (1, LEASE_VALID_FOR)
        other = await desk.control_for(LAPTOP)
        assert not other.lease_held_by(LAPTOP)
        assert other.lease.collector_id == CID  # names the holder, so the other can say so
        assert other.lease.valid_for is None
        assert other.monitored == () and other.commands == ()
        # written down with the answer, for the monitor to tell the owner later
        assert (await refused_by(fb, LAPTOP), await refused_by(fb, CID)) == (CID, None)
        # still the holder's, however quiet it goes: the lease never moves by itself
        assert (await desk.control_for(CID)).lease_held_by(CID)
        assert (await desk.lease()).generation == 1

    async def test_status_and_snapshot_also_claim_an_empty_lease(self, desk: ControlDesk):
        assert await desk.holder() is None
        assert await desk.ensure_lease(LAPTOP) == LAPTOP
        assert await desk.ensure_lease(CID) == LAPTOP

    async def test_only_to_a_known_collector(self, desk: ControlDesk, fb: FbStore):
        await desk.control_for(CID)
        assert await desk.transfer_lease("ghost") == (LeaseOutcome.UNKNOWN, CID)
        assert await desk.transfer_lease(CID) == (LeaseOutcome.SAME, CID)

    async def test_a_move_waits_for_the_old_holder_to_hear_it(self, desk: ControlDesk, fb: FbStore):
        """C1-04: granting at once let a holder that cannot reach the brain read on alongside
        the new one, for as long as it stays cut off."""
        await two_collectors(desk, fb)
        await desk.request_refresh()
        assert await desk.transfer_lease(LAPTOP) == (LeaseOutcome.REVOKING, CID)
        assert await fb.open_commands(now=EPOCH0) == []  # they were for the old holder
        # meanwhile nobody reads: not the new one, and not the old one
        waiting = await desk.control_for(LAPTOP)
        assert not waiting.lease_held_by(LAPTOP) and not waiting.lease_held_by(CID)
        assert waiting.monitored == ()
        assert (await desk.lease()).moving_to == LAPTOP
        assert await refused_by(fb, LAPTOP) is None  # the collector it moves to: no contender
        assert await desk.last_move() is None
        # the old holder checks in: this answer tells it to stop, and the move is done
        told = await desk.control_for(CID)
        assert not told.lease_held_by(CID) and told.lease.collector_id == LAPTOP
        assert await desk.last_move() == LeaseMove(CID, LAPTOP, 2)
        assert await refused_by(fb, CID) is None  # hearing it is not contending for it
        granted = await desk.control_for(LAPTOP)
        assert granted.lease_held_by(LAPTOP) and granted.lease.generation == 2
        await desk.control_for(CID)  # still running: now it is a contender
        assert await refused_by(fb, CID) == LAPTOP

    async def test_an_unreadable_recorded_move_is_ignored(self, desk: ControlDesk, fb: FbStore):
        await fb.kv_set("lease_moved", "{not json")
        assert await desk.last_move() is None

    async def test_now_skips_the_wait_for_a_holder_switched_off(
        self, desk: ControlDesk, fb: FbStore
    ):
        await two_collectors(desk, fb)
        assert await desk.transfer_lease(LAPTOP, now=True) == (LeaseOutcome.MOVED, CID)
        assert (await desk.control_for(LAPTOP)).lease_held_by(LAPTOP)

    async def test_a_pending_move_can_be_cancelled_or_redirected(
        self, desk: ControlDesk, fb: FbStore
    ):
        await two_collectors(desk, fb)
        await fb.touch_collector("mini", now=EPOCH0)
        await desk.transfer_lease(LAPTOP)
        assert await desk.transfer_lease("mini") == (LeaseOutcome.REVOKING, CID)
        assert (await desk.lease()).moving_to == "mini"
        assert await desk.transfer_lease(CID) == (LeaseOutcome.CANCELLED, CID)
        state = await desk.lease()
        assert (state.holder, state.moving_to, state.generation) == (CID, None, 1)
        assert (await desk.control_for(CID)).lease_held_by(CID)

    async def test_nobody_holding_it_means_a_move_at_once(self, desk: ControlDesk, fb: FbStore):
        await fb.touch_collector(LAPTOP, now=EPOCH0)
        assert await desk.transfer_lease(LAPTOP) == (LeaseOutcome.MOVED, None)
        assert await desk.holder() == LAPTOP

    async def test_the_old_holders_command_never_reaches_the_new_holder(
        self, desk: ControlDesk, fb: FbStore
    ):
        """Block ids are per collector. A resume for the old holder's block 17 that
        reached the new holder would clear the new holder's own, unrelated block 17."""
        await two_collectors(desk, fb)
        assert await desk.request_resume(17, block(17)) is ResumeOutcome.SENT
        (old,) = await fb.open_commands(now=EPOCH0)
        assert (old.collector_id, old.lease_generation) == (CID, 1)
        await desk.transfer_lease(LAPTOP, now=True)
        # even a command the move somehow left open is addressed to the old holder only
        await fb.db.execute("UPDATE fb_commands SET done_at=NULL, done_reason=NULL")
        assert (await desk.control_for(LAPTOP)).commands == ()
        assert (await desk.control_for(CID)).commands == ()  # nor to the old one, now

    async def test_commands_say_who_and_which_lease_they_are_for(
        self, desk: ControlDesk, fb: FbStore
    ):
        await desk.control_for(CID)
        await desk.request_resume(17, block(17))
        (command,) = (await desk.control_for(CID)).commands
        assert isinstance(command, ResumeCommand)
        assert (command.collector_id, command.lease_generation) == (CID, 1)
        control = await desk.control_for(CID)
        assert control.commands_for(CID) == control.commands
        assert control.commands_for(LAPTOP) == ()

    async def test_no_command_while_the_lease_is_moving(self, desk: ControlDesk, fb: FbStore):
        assert await desk.request_refresh() is RefreshOutcome.NO_HOLDER  # nobody yet
        assert await desk.request_resume(17, block(17)) is ResumeOutcome.NO_HOLDER
        await two_collectors(desk, fb)
        await desk.transfer_lease(LAPTOP)
        assert await desk.request_refresh() is RefreshOutcome.NO_HOLDER
        assert await desk.request_resume(17, block(17)) is ResumeOutcome.NO_HOLDER

    async def test_the_control_answer_validates_and_serialises(self, desk: ControlDesk):
        answer = await desk.control_for(CID)
        again = Control.model_validate_json(to_json(answer))
        assert again == answer and again.issued_at == EPOCH0


MAC_DB, LAPTOP_DB = "mac-db-7f3a0001", "laptop-db-c29e002"


class TestStateDatabases:
    """Round 2 F4: a collector is its id *and* its state database. Every installation
    defaulted to home-1, so a forgotten Mac and its replacement both held the one lease."""

    async def test_a_second_database_under_the_holders_id_is_told_to_stop(
        self, desk: ControlDesk, fb: FbStore
    ):
        assert (await desk.control_for(CID, MAC_DB)).lease_held_by(CID)
        twin = await desk.control_for(CID, LAPTOP_DB)
        assert not twin.lease_held_by(CID)  # the same name, but not granted
        assert twin.lease.collector_id == CID and not twin.lease.granted
        assert twin.monitored == () and twin.commands == ()
        assert (await desk.control_for(CID, MAC_DB)).lease_held_by(CID)  # untouched
        row = await fb.collector(CID)
        assert row is not None
        # the holder's own polls do not wipe out the second database's refusal
        assert (row.refused_by, row.refused_incarnation) == (f"{CID}@{MAC_DB}", LAPTOP_DB)

    async def test_the_owner_moves_it_to_the_second_database_in_two_steps(
        self, desk: ControlDesk, fb: FbStore
    ):
        await desk.control_for(CID, MAC_DB)
        await desk.control_for(CID, LAPTOP_DB)
        assert await desk.transfer_lease(CID) == (LeaseOutcome.REPLACING, CID)
        assert not (await desk.control_for(CID, LAPTOP_DB)).lease_held_by(CID)  # waits
        assert not (await desk.control_for(CID, MAC_DB)).lease_held_by(CID)  # hears: stop
        move = await desk.last_move()
        assert move == LeaseMove(f"{CID} ({MAC_DB[:8]})", f"{CID} ({LAPTOP_DB[:8]})", 2)
        granted = await desk.control_for(CID, LAPTOP_DB)
        assert granted.lease_held_by(CID) and granted.lease.generation == 2
        assert not (await desk.control_for(CID, MAC_DB)).lease_held_by(CID)  # for good
        row = await fb.collector(CID)
        assert row is not None and row.refused_incarnation == MAC_DB

    async def test_now_replaces_at_once_and_a_repeat_cancels_a_pending_one(self, desk: ControlDesk):
        await desk.control_for(CID, MAC_DB)
        assert await desk.transfer_lease(CID) == (LeaseOutcome.SAME, CID)  # nobody else asked
        await desk.control_for(CID, LAPTOP_DB)
        assert (await desk.transfer_lease(CID))[0] is LeaseOutcome.REPLACING
        assert (await desk.transfer_lease(CID))[0] is LeaseOutcome.CANCELLED
        assert (await desk.control_for(CID, MAC_DB)).lease_held_by(CID)
        assert (await desk.transfer_lease(CID))[0] is LeaseOutcome.REPLACING
        assert (await desk.transfer_lease(CID, now=True))[0] is LeaseOutcome.REPLACED
        assert (await desk.control_for(CID, LAPTOP_DB)).lease_held_by(CID)
        assert not (await desk.control_for(CID, MAC_DB)).lease_held_by(CID)

    async def test_a_move_to_another_id_goes_to_the_database_it_checked_in_from(
        self, desk: ControlDesk
    ):
        await desk.control_for(CID, MAC_DB)
        await desk.control_for(LAPTOP, LAPTOP_DB)
        assert (await desk.transfer_lease(LAPTOP, now=True))[0] is LeaseOutcome.MOVED
        assert not (await desk.control_for(LAPTOP, "laptop-db-other01")).lease_held_by(LAPTOP)
        assert (await desk.control_for(LAPTOP, LAPTOP_DB)).lease_held_by(LAPTOP)

    async def test_a_lease_recorded_without_a_database_takes_its_holders_first(
        self, desk: ControlDesk, fb: FbStore
    ):
        """An older brain (or collector) recorded none: the next poll of its holder binds it."""
        await fb.kv_set("lease", CID)
        await fb.kv_set("lease_generation", "1")
        assert (await desk.control_for(CID, MAC_DB)).lease_held_by(CID)
        assert (await desk.lease()).incarnation == MAC_DB
        assert not (await desk.control_for(CID, LAPTOP_DB)).lease_held_by(CID)
        assert not (await desk.control_for(CID)).lease_held_by(CID)  # one that sends none, too
        assert await desk.claim(CID, MAC_DB) and not await desk.claim(CID, LAPTOP_DB)
        # a database that names none cannot be moved to: nothing to bind the lease to
        assert (await desk.transfer_lease(CID))[0] is LeaseOutcome.SAME

    async def test_a_lease_the_first_brain_wrote_has_generation_one(
        self, desk: ControlDesk, fb: FbStore
    ):
        """Round 2 C-F3: v1 recorded the holder only; generation 0 fails the wire's ``ge=1``,
        and every control answer was a 500."""
        await fb.kv_set("lease", CID)
        answer = await desk.control_for(CID, MAC_DB)
        assert answer.lease_held_by(CID) and answer.lease.generation == 1
        await desk.control_for(LAPTOP, LAPTOP_DB)
        await desk.transfer_lease(LAPTOP, now=True)
        assert (await desk.lease()).generation == 2

    async def test_statuses_and_snapshots_claim_by_database_too(self, desk: ControlDesk):
        assert await desk.claim(CID, MAC_DB)  # the first to check in, by status
        assert not await desk.claim(CID, LAPTOP_DB)
        assert await desk.ensure_lease(CID, LAPTOP_DB) == CID  # names the holder


class TestMonitoredSet:
    async def test_readable_watched_groups_only(
        self, desk: ControlDesk, book: GroupBook, storage: Storage
    ):
        await seed_groups(book, G1, G2)
        await storage.subscribe(OWNER, G1)
        assert [m.group_id for m in (await desk.control_for(CID)).monitored] == [str(G1)]
        await storage.subscribe(OWNER, G2)
        await book.mark(G2, now=EPOCH0, state=GroupState.LOST)
        assert [m.group_id for m in (await desk.monitored()).sent] == [str(G1)]

    async def test_a_paused_user_does_not_keep_a_group_monitored(
        self, desk: ControlDesk, book: GroupBook, storage: Storage
    ):
        await seed_groups(book, G1)
        await storage.set_chat_shared(G1, True)
        await storage.subscribe(MOD, G1)
        await storage.pause_delivery(MOD, "UserIsBlockedError")
        assert (await desk.monitored()).sent == ()
        await storage.resume_delivery(MOD)
        await storage.update_user(MOD, role=Role.DENIED)
        assert (await desk.monitored()).sent == ()

    async def test_over_the_cap_the_most_wanted_win_and_the_rest_are_reported(
        self, desk: ControlDesk, book: GroupBook, storage: Storage
    ):
        ids = [1_000_000 + n for n in range(MAX_MONITORED + 3)]
        await book.apply_snapshot(snapshot(*(info(g) for g in ids)), now=EPOCH0)
        for gid in ids:
            await storage.subscribe(OWNER, gid)
        wanted = ids[-2:]  # the moderator also watches the last two
        for gid in wanted:
            await storage.set_chat_shared(gid, True)
            await storage.subscribe(MOD, gid)
        chosen = await desk.monitored()
        sent = [int(m.group_id) for m in chosen.sent]
        assert len(sent) == MAX_MONITORED
        assert sent[:2] == wanted
        assert set(sent) | set(chosen.over_cap) == set(ids)
        assert len(chosen.over_cap) == 3
        Control.model_validate_json(to_json(await desk.control_for(CID)))  # still valid

    async def test_tier_and_listings_travel(
        self, desk: ControlDesk, book: GroupBook, storage: Storage, fb: FbStore
    ):
        await seed_groups(book, G1)
        await storage.subscribe(OWNER, G1)
        await fb.db.execute("UPDATE fb_groups SET tier=3, listings=1")
        (entry,) = (await desk.monitored()).sent
        assert (entry.tier, entry.listings) == (3, True)


class TestPauseAndPace:
    async def test_pause_is_state_on_every_answer(self, desk: ControlDesk):
        assert await desk.pause()
        assert not await desk.pause()
        assert (await desk.control_for(CID)).paused
        assert await desk.unpause()
        assert not await desk.unpause()
        assert not (await desk.control_for(CID)).paused

    async def test_pace_cap(self, desk: ControlDesk, fb: FbStore):
        assert await desk.pace_cap() is None
        await desk.set_pace_cap(Profile.CAUTIOUS)
        assert (await desk.control_for(CID)).pace_cap is Profile.CAUTIOUS
        await desk.set_pace_cap(None)
        assert (await desk.control_for(CID)).pace_cap is None
        await fb.kv_set("pace_cap", "reckless")  # a value from nowhere is no cap, not a crash
        assert await desk.pace_cap() is None


def synced(taken_at: int, nonce: str | None = None) -> GroupsSnapshot:
    return snapshot(complete=False, taken_at=taken_at).model_copy(update={"nonce": nonce})


class TestRefresh:
    @pytest.fixture(autouse=True)
    async def holder(self, desk: ControlDesk) -> None:
        await desk.control_for(CID)

    async def test_sent_then_pending_then_resent_until_a_snapshot_lands(
        self, desk: ControlDesk, clock: FakeClock
    ):
        assert await desk.request_refresh() is RefreshOutcome.SENT
        assert await desk.request_refresh() is RefreshOutcome.PENDING
        first = await desk.control_for(CID)
        second = await desk.control_for(CID)
        refresh = RefreshGroupsCommand(nonce="nonce-0001", collector_id=CID, lease_generation=1)
        assert first.commands == second.commands == (refresh,)
        clock.tick(120)
        await desk.snapshot_arrived(synced(EPOCH0 + 100))
        assert (await desk.control_for(CID)).commands == ()

    async def test_a_snapshot_taken_before_the_request_does_not_settle_it(
        self, desk: ControlDesk, clock: FakeClock
    ):
        clock.tick(600)
        await desk.request_refresh()
        await desk.snapshot_arrived(synced(EPOCH0))  # an older sync that was still in flight
        assert len((await desk.control_for(CID)).commands) == 1

    async def test_a_collector_clock_a_little_ahead_does_not_settle_it_early(
        self, desk: ControlDesk, clock: FakeClock
    ):
        """The command's time is the brain's, ``taken_at`` the collector's. A sync that
        was in flight when the command was made, from a clock 20 s ahead, looks 10 s newer."""
        await desk.request_refresh()
        await desk.snapshot_arrived(synced(EPOCH0 + 10))
        assert len((await desk.control_for(CID)).commands) == 1

    async def test_an_echoed_nonce_settles_exactly_its_command(self, desk: ControlDesk):
        await desk.request_refresh()
        await desk.snapshot_arrived(synced(EPOCH0 + 3600, nonce="nonce-9999"))  # another one
        assert len((await desk.control_for(CID)).commands) == 1
        await desk.snapshot_arrived(synced(EPOCH0 - 3600, nonce="nonce-0001"))  # any clock
        assert (await desk.control_for(CID)).commands == ()

    async def test_three_a_day(self, desk: ControlDesk, clock: FakeClock):
        for n in range(3):
            assert await desk.request_refresh() is RefreshOutcome.SENT
            await desk.snapshot_arrived(synced(EPOCH0, nonce=f"nonce-{n + 1:04d}"))
            clock.tick(60)
        assert await desk.request_refresh() is RefreshOutcome.LIMIT
        clock.tick(86400)
        assert await desk.request_refresh() is RefreshOutcome.SENT


class TestResume:
    @pytest.fixture(autouse=True)
    async def holder(self, desk: ControlDesk) -> None:
        await desk.control_for(CID)

    def test_verdicts_mirror_the_collector_in_its_order(self):
        assert resume_verdict(None, now=EPOCH0) is ResumeVerdict.NO_BLOCK
        assert resume_verdict(block(until=None), now=EPOCH0) is ResumeVerdict.FINAL
        assert resume_verdict(block(no_probe=True), now=EPOCH0) is ResumeVerdict.NO_PROBE
        assert resume_verdict(block(until=EPOCH0 + 1), now=EPOCH0) is ResumeVerdict.TOO_EARLY
        assert resume_verdict(block(until=EPOCH0), now=EPOCH0) is ResumeVerdict.OK
        # the ladder checks the time before the lock, and lifts the lock only when told
        early_lock = block(until=EPOCH0 + 1, no_probe=True)
        assert resume_verdict(early_lock, now=EPOCH0) is ResumeVerdict.TOO_EARLY
        lifted = resume_verdict(block(no_probe=True), now=EPOCH0, lock_lifted=True)
        assert lifted is ResumeVerdict.OK

    async def test_a_resume_is_bound_to_the_open_block(self, desk: ControlDesk):
        current = block(17)
        assert await desk.request_resume(16, current) is ResumeOutcome.STALE
        assert await desk.request_resume(17, None) is ResumeOutcome.STALE
        assert await desk.request_resume(17, block(17, until=EPOCH0 + 60)) is ResumeOutcome.REFUSED
        assert await desk.request_resume(17, current) is ResumeOutcome.SENT
        assert await desk.request_resume(17, current) is ResumeOutcome.PENDING
        commands = (await desk.control_for(CID)).commands
        assert commands == (
            ResumeCommand(block_id=17, nonce="nonce-0001", collector_id=CID, lease_generation=1),
        )

    async def test_an_advanced_protection_lock_needs_the_owners_word(self, desk: ControlDesk):
        locked = block(17, no_probe=True)
        assert await desk.request_resume(17, locked) is ResumeOutcome.REFUSED
        assert await desk.request_resume(17, locked, lock_lifted=True) is ResumeOutcome.SENT
        (command,) = (await desk.control_for(CID)).commands
        assert isinstance(command, ResumeCommand) and command.lock_lifted

    async def test_lock_lifted_means_nothing_for_an_ordinary_block(self, desk: ControlDesk):
        assert await desk.request_resume(17, block(17), lock_lifted=True) is ResumeOutcome.SENT
        (command,) = (await desk.control_for(CID)).commands
        assert isinstance(command, ResumeCommand) and not command.lock_lifted

    async def test_it_is_resent_until_the_block_is_gone_or_new(self, desk: ControlDesk):
        await desk.request_resume(17, block(17))
        await desk.settle(status(block=block(17)))
        assert len((await desk.control_for(CID)).commands) == 1
        await desk.settle(status(block=block(17), collector_id=LAPTOP))  # not its command
        assert len((await desk.control_for(CID)).commands) == 1
        await desk.settle(status(block=block(18)))  # the probe failed: a new rung, a new id
        assert (await desk.control_for(CID)).commands == ()

    async def test_a_closed_block_or_time_ends_it(self, desk: ControlDesk, clock: FakeClock, fb):
        await desk.request_resume(17, block(17))
        await desk.block_closed(CID, 16)
        await desk.block_closed(LAPTOP, 17)  # another collector's block 17 is another block
        assert len((await desk.control_for(CID)).commands) == 1
        await desk.block_closed(CID, 17)
        assert (await desk.control_for(CID)).commands == ()
        await desk.request_resume(17, block(17))
        clock.tick(RESUME_TTL)
        assert (await desk.control_for(CID)).commands == ()
        assert await fb.expire_commands(now=int(clock().timestamp())) == 1

    async def test_a_status_without_a_block_settles_every_resume(self, desk: ControlDesk):
        await desk.request_resume(17, block(17))
        await desk.settle(status())
        assert (await desk.control_for(CID)).commands == ()
