from __future__ import annotations

import random
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fbcollector.clock import FakeClock
from fbcollector.control import (
    COMMAND_DELAY,
    KV_CONTROL,
    ControlCenter,
    ControlState,
    KnownGroup,
    MonitoredFileError,
    RefreshResult,
    default_monitored,
    effective_profile,
    load_monitored_file,
)
from fbcollector.gates import Activity, GateContext
from fbcollector.ladder import Ladder, ResumeResult
from fbcollector.model import CollectorState, PageState
from fbcollector.store import Store
from fbwire.models import Control, GroupState, Profile

from .pacing_support import HOUR, seed_groups, verdict

NAV = GateContext(Activity.NAVIGATE)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def store(tmp_path: Path, clock: FakeClock) -> AsyncIterator[Store]:
    s = Store(tmp_path / "state.sqlite", clock=clock)
    await s.open()
    yield s
    await s.close()


@pytest.fixture
def ladder(store: Store, clock: FakeClock) -> Ladder:
    return Ladder(store, clock)


def center(store: Store, ladder: Ladder, clock: FakeClock, *, local_mode: bool) -> ControlCenter:
    return ControlCenter(
        store, ladder, clock, collector_id="home-1", local_mode=local_mode, rng=random.Random(3)
    )


def control(**kw: Any) -> Control:
    data: dict[str, Any] = {
        "issued_at": 1_790_000_000,
        "lease": {"collector_id": "home-1", "granted": True},
    }
    data.update(kw)
    return Control.model_validate(data)


@given(
    local=st.sampled_from(list(Profile)),
    remote=st.one_of(st.none(), st.sampled_from(list(Profile))),
)
def test_remote_normal_never_beats_local_cautious(local: Profile, remote: Profile | None) -> None:
    effective = effective_profile(local, remote)
    if Profile.CAUTIOUS in {local, remote}:
        assert effective is Profile.CAUTIOUS
    else:
        assert effective is Profile.NORMAL


async def test_the_control_row_is_written_durably(
    store: Store, ladder: Ladder, clock: FakeClock
) -> None:
    """Round 2 B-F4: it holds what keeps the gate shut (a revoked lease, a pause); a power cut
    must not bring back "granted and unpaused" for the loop to read under."""
    from .trace_support import durable_writes, traced

    remote = center(store, ladder, clock, local_mode=False)
    await remote.apply(control())
    async with traced(store) as statements:
        await remote.apply(control(lease={"collector_id": "laptop-1", "granted": True}))
        await remote.pause()
    assert f"kv:{KV_CONTROL}" in durable_writes(statements)
    assert not (await remote.gate(NAV)).ok


class TestLocalMode:
    async def test_pause_and_unpause(self, store: Store, ladder: Ladder, clock: FakeClock) -> None:
        c = center(store, ladder, clock, local_mode=True)
        await c.load()
        assert (await c.gate(NAV)).ok
        await c.pause()
        gate = await c.gate(NAV)
        assert (gate.ok, gate.state) == (False, CollectorState.PAUSED)
        assert c.paused
        # the pause is on disk: a restart keeps it
        again = center(store, ladder, clock, local_mode=True)
        await again.load()
        assert again.paused
        await again.unpause()
        assert (await again.gate(NAV)).ok

    async def test_the_brain_has_no_say(
        self, store: Store, ladder: Ladder, clock: FakeClock
    ) -> None:
        c = center(store, ladder, clock, local_mode=True)
        await c.apply(
            control(pace_cap="cautious", paused=True, lease={"collector_id": "x", "granted": True})
        )
        assert c.effective_profile(Profile.NORMAL) is Profile.NORMAL
        assert not c.paused
        assert (await c.gate(NAV)).ok

    async def test_local_resume_gets_its_own_nonce(
        self, store: Store, ladder: Ladder, clock: FakeClock
    ) -> None:
        c = center(store, ladder, clock, local_mode=True)
        block = await ladder.trip(PageState.BLOCKED, reason="x")
        assert await c.resume(block.id) is ResumeResult.TOO_EARLY
        clock.advance(6 * HOUR)
        assert await c.resume(block.id) is ResumeResult.ACCEPTED
        probe = await ladder.probe_request()
        assert probe is not None
        assert COMMAND_DELAY[0] <= probe.not_before - clock.time() <= COMMAND_DELAY[1]


class TestRemote:
    async def test_no_lease_no_reads(self, store: Store, ladder: Ladder, clock: FakeClock) -> None:
        c = center(store, ladder, clock, local_mode=False)
        await c.load()
        gate = await c.gate(NAV)
        assert (gate.ok, gate.state) == (False, CollectorState.PAUSED)
        assert "nobody yet" in gate.reason
        applied = await c.apply(control())
        assert applied.lease
        assert (await c.gate(NAV)).ok
        await c.apply(control(lease={"collector_id": "laptop-1", "granted": True}))
        gate = await c.gate(NAV)
        assert not gate.ok
        assert "laptop-1" in gate.reason
        await c.apply(control(lease={"collector_id": "home-1", "granted": False}))
        assert "nobody yet" in (await c.gate(NAV)).reason

    async def test_pace_and_pause_are_state(
        self, store: Store, ladder: Ladder, clock: FakeClock
    ) -> None:
        c = center(store, ladder, clock, local_mode=False)
        await c.apply(control(pace_cap="normal"))
        assert c.effective_profile(Profile.CAUTIOUS) is Profile.CAUTIOUS
        assert c.effective_profile(Profile.NORMAL) is Profile.NORMAL
        await c.apply(control(pace_cap="cautious", paused=True))
        assert c.effective_profile(Profile.NORMAL) is Profile.CAUTIOUS
        gate = await c.gate(NAV)
        assert (gate.ok, gate.reason) == (False, "paused by the brain")
        reloaded = center(store, ladder, clock, local_mode=False)
        await reloaded.load()
        assert reloaded.state.pace_cap is Profile.CAUTIOUS
        assert reloaded.state.polled_at == clock.time()
        await c.apply(control())
        assert c.effective_profile(Profile.NORMAL) is Profile.NORMAL  # no opinion: local rules

    async def test_monitored_set_is_applied(
        self, store: Store, ladder: Ladder, clock: FakeClock
    ) -> None:
        await seed_groups(store, ["111", "222"], monitored=False)
        c = center(store, ladder, clock, local_mode=False)
        applied = await c.apply(
            control(monitored=[{"group_id": "111", "tier": 2}, {"group_id": "999"}])
        )
        assert applied.unknown_groups == ("999",)
        assert await store.monitored_ids() == {"111"}

    async def test_commands_are_edges(self, store: Store, ladder: Ladder, clock: FakeClock) -> None:
        c = center(store, ladder, clock, local_mode=False)
        block = await ladder.trip(PageState.BLOCKED, reason="x")
        clock.advance(6 * HOUR)
        commands = [
            {"kind": "resume", "block_id": block.id, "nonce": "resume-0001"},
            {"kind": "refresh_groups", "nonce": "refresh-0001"},
            {"kind": "future_command", "nonce": "whatever-1"},
        ]
        first = await c.apply(control(commands=commands))
        assert [(o.kind, o.result) for o in first.commands] == [
            ("resume", "accepted"),
            ("refresh_groups", "scheduled"),
        ]
        refresh_at = c.refresh_due()
        assert refresh_at is not None
        assert COMMAND_DELAY[0] <= refresh_at - clock.time() <= COMMAND_DELAY[1]
        # the brain re-sends the same commands on the next poll: nothing happens twice
        second = await c.apply(control(commands=commands))
        assert [o.result for o in second.commands] == ["replayed", "replayed"]
        assert c.refresh_due() == refresh_at

    async def test_a_stale_resume_never_clears_a_new_block(
        self, store: Store, ladder: Ladder, clock: FakeClock
    ) -> None:
        c = center(store, ladder, clock, local_mode=False)
        old = await ladder.trip(PageState.BLOCKED, reason="old")
        await ladder.trip(PageState.BLOCKED, reason="new")
        clock.advance(30 * HOUR)
        applied = await c.apply(
            control(commands=[{"kind": "resume", "block_id": old.id, "nonce": "stale-0001"}])
        )
        assert applied.commands[0].result == "stale"
        assert await ladder.probe_request() is None
        current = await store.current_block()
        assert current is not None and current.reason == "new"


class TestTargetedCommands:
    """The brain's contract (wire changes 6 and 7): who a command is for."""

    LEASE: ClassVar[dict[str, Any]] = {"collector_id": "home-1", "granted": True, "generation": 3}

    async def test_only_commands_for_this_collector_and_lease_are_applied(
        self, store: Store, ladder: Ladder, clock: FakeClock
    ) -> None:
        c = center(store, ladder, clock, local_mode=False)
        block = await ladder.trip(PageState.BLOCKED, reason="x")
        clock.advance(6 * HOUR)
        commands = [
            {
                "kind": "resume",
                "block_id": block.id,
                "nonce": "for-them-01",
                "collector_id": "laptop-1",
                "lease_generation": 3,
            },
            {
                "kind": "refresh_groups",
                "nonce": "old-lease-01",
                "collector_id": "home-1",
                "lease_generation": 2,
            },
            {
                "kind": "resume",
                "block_id": block.id,
                "nonce": "for-me-0001",
                "collector_id": "home-1",
                "lease_generation": 3,
            },
        ]
        applied = await c.apply(control(lease=self.LEASE, commands=commands))
        assert [(o.nonce, o.result) for o in applied.commands] == [("for-me-0001", "accepted")]
        assert applied.skipped == 2
        assert c.refresh_due() is None
        # a skipped command's nonce is still unused: its own addressee can apply it later
        assert await store.claim_nonce("for-them-01", kind="resume")
        assert await store.claim_nonce("old-lease-01", kind="refresh")

    async def test_without_the_lease_no_command_is_applied(
        self, store: Store, ladder: Ladder, clock: FakeClock
    ) -> None:
        c = center(store, ladder, clock, local_mode=False)
        block = await ladder.trip(PageState.BLOCKED, reason="x")
        clock.advance(6 * HOUR)
        commands = [
            {"kind": "resume", "block_id": block.id, "nonce": "untargeted1"},
            {"kind": "refresh_groups", "nonce": "untargeted2"},
        ]
        for lease in (
            {"collector_id": "laptop-1", "granted": True},
            {"collector_id": "home-1", "granted": False},  # revoked while a move is under way
        ):
            applied = await c.apply(control(lease=lease, commands=commands))
            assert (applied.commands, applied.skipped) == ((), 2)
        assert await ladder.probe_request() is None
        assert c.refresh_due() is None
        # an older brain's untargeted command still works for the holder
        applied = await c.apply(control(commands=commands))
        assert [o.result for o in applied.commands] == ["accepted", "scheduled"]
        assert applied.skipped == 0

    async def test_a_lock_lifted_resume_arms_the_probe_of_an_advanced_protection_block(
        self, store: Store, ladder: Ladder, clock: FakeClock
    ) -> None:
        c = center(store, ladder, clock, local_mode=False)
        block = await ladder.trip(PageState.BLOCKED, reason="x", advanced_protection=True)
        clock.advance(6 * HOUR)
        plain = {"kind": "resume", "block_id": block.id, "nonce": "plain-00001"}
        applied = await c.apply(control(commands=[plain]))
        assert applied.commands[0].result == "needs_confirmation"
        assert await ladder.probe_request() is None
        lifted = {**plain, "nonce": "lifted-0001", "lock_lifted": True}
        applied = await c.apply(control(commands=[lifted]))
        assert applied.commands[0].result == "accepted"
        probe = await ladder.probe_request()
        assert probe is not None and probe.block_id == block.id


class TestRefresh:
    async def test_three_a_day(self, store: Store, ladder: Ladder, clock: FakeClock) -> None:
        c = center(store, ladder, clock, local_mode=True)
        for _ in range(3):
            assert await c.refresh() is RefreshResult.SCHEDULED
            assert await c.refresh() is RefreshResult.ALREADY_PENDING
            await c.refresh_done()
            clock.advance(HOUR)
        assert await c.refresh() is RefreshResult.LIMIT
        clock.advance(22 * HOUR)
        assert await c.refresh() is RefreshResult.SCHEDULED
        assert await c.refresh("same-nonce-1") is RefreshResult.ALREADY_PENDING
        assert await c.refresh("same-nonce-1") is RefreshResult.REPLAYED
        await c.refresh_done()
        await c.refresh_done()  # nothing pending: harmless


class TestLeaseValidity:
    """Wire change 5: a grant is good for ``valid_for`` seconds without a newer answer."""

    LEASE: ClassVar[dict[str, Any]] = {
        "collector_id": "home-1",
        "granted": True,
        "generation": 4,
        "valid_for": 3600,
    }

    async def test_a_grant_lapses_without_a_newer_answer(
        self, store: Store, ladder: Ladder, clock: FakeClock
    ) -> None:
        c = center(store, ladder, clock, local_mode=False)
        await c.apply(control(lease=self.LEASE))
        assert c.state.lease_generation == 4
        assert c.state.lease_valid_for == 3600
        clock.advance(3600)
        assert (await c.gate(NAV)).ok  # exactly valid_for old: still good
        clock.advance(1)
        gate = await c.gate(NAV)
        assert (gate.ok, gate.state) == (False, CollectorState.PAUSED)
        assert "lease lapsed" in gate.reason
        await c.apply(control(lease=self.LEASE))  # the brain answers again
        assert (await c.gate(NAV)).ok

    async def test_a_clock_stepped_back_does_not_extend_it(
        self, store: Store, ladder: Ladder, clock: FakeClock
    ) -> None:
        c = center(store, ladder, clock, local_mode=False)
        await c.apply(control(lease=self.LEASE))
        clock.jump_wall(-7200)
        clock.advance(3601)  # the wall clock says an hour less has passed
        assert c.lease_lapsed()
        assert not (await c.gate(NAV)).ok

    async def test_a_restart_counts_on_the_wall_clock(
        self, store: Store, ladder: Ladder, clock: FakeClock
    ) -> None:
        await center(store, ladder, clock, local_mode=False).apply(control(lease=self.LEASE))
        clock.advance(1800)
        restarted = center(store, ladder, clock, local_mode=False)
        await restarted.load()
        assert restarted.lease_age() == 1800
        assert (await restarted.gate(NAV)).ok
        clock.suspend(1801)  # asleep: the wall clock moves, the monotonic one does not
        assert restarted.lease_lapsed()

    async def test_an_older_brain_without_validity_never_lapses(
        self, store: Store, ladder: Ladder, clock: FakeClock
    ) -> None:
        c = center(store, ladder, clock, local_mode=False)
        await c.apply(control())
        clock.advance(30 * 24 * HOUR)
        assert not c.lease_lapsed()
        assert (await c.gate(NAV)).ok

    async def test_no_grant_keeps_nothing_of_the_last_one(
        self, store: Store, ladder: Ladder, clock: FakeClock
    ) -> None:
        c = center(store, ladder, clock, local_mode=False)
        await c.apply(control(lease=self.LEASE))
        await c.apply(control(lease={**self.LEASE, "collector_id": "laptop-1"}))
        state = c.state
        assert (state.lease_granted, state.lease_holder) == (False, "laptop-1")
        assert (state.lease_generation, state.lease_valid_for, state.lease_at) == (None,) * 3
        assert c.lease_age() is None


class TestRefreshNonce:
    """Wire change 4: the snapshot of a commanded sync echoes the command's nonce."""

    async def test_the_nonce_travels_from_command_to_snapshot_once(
        self, store: Store, ladder: Ladder, clock: FakeClock
    ) -> None:
        c = center(store, ladder, clock, local_mode=False)
        await c.apply(control(commands=[{"kind": "refresh_groups", "nonce": "refresh-0042"}]))
        assert c.state.refresh_nonce == "refresh-0042"
        assert await c.take_sync_nonce() is None  # the sync has not been granted yet
        await c.refresh_done()
        done = c.state
        assert (done.refresh_at, done.refresh_nonce) == (None, None)
        assert await c.take_sync_nonce() == "refresh-0042"
        assert await c.take_sync_nonce() is None

    async def test_a_local_refresh_has_nothing_to_echo(
        self, store: Store, ladder: Ladder, clock: FakeClock
    ) -> None:
        c = center(store, ladder, clock, local_mode=True)
        assert await c.refresh() is RefreshResult.SCHEDULED
        await c.refresh_done()
        assert await c.take_sync_nonce() is None

    async def test_an_unsent_nonce_waits_for_the_next_sync(
        self, store: Store, ladder: Ladder, clock: FakeClock
    ) -> None:
        c = center(store, ladder, clock, local_mode=False)
        await c.apply(control(commands=[{"kind": "refresh_groups", "nonce": "refresh-0001"}]))
        await c.refresh_done()  # its sync read then failed: no snapshot took the nonce
        clock.advance(HOUR)
        assert await c.refresh() is RefreshResult.SCHEDULED  # a local refresh meanwhile
        await c.refresh_done()
        assert await c.take_sync_nonce() == "refresh-0001"


async def test_an_answer_never_undoes_a_pause_written_by_another_process(
    store: Store, ladder: Ladder, clock: FakeClock
) -> None:
    running = center(store, ladder, clock, local_mode=False)
    await running.load()
    await center(store, ladder, clock, local_mode=False).pause()  # `fbcollector pause` on a TTY
    await running.apply(control())
    assert running.state.local_paused
    stored = ControlState.from_json(await store.kv_get(KV_CONTROL))
    assert stored.local_paused and stored.lease_granted


def test_control_state_json() -> None:
    state = ControlState(
        lease_granted=True,
        lease_holder="home-1",
        lease_generation=7,
        lease_valid_for=21600,
        lease_at=4.0,
        pace_cap=Profile.CAUTIOUS,
        remote_paused=True,
        local_paused=True,
        refresh_at=5.0,
        refresh_nonce="refresh-0001",
        sync_nonce="refresh-0000",
        refreshes=(1.0, 2.0),
        polled_at=3.0,
    )
    assert ControlState.from_json(state.to_json()) == state
    assert ControlState.from_json(None) == ControlState()
    assert ControlState.from_json(ControlState().to_json()) == ControlState()
    # a row written before the lease fields existed still loads
    old = '{"lease_granted": true, "lease_holder": "home-1", "polled_at": 3.0}'
    assert ControlState.from_json(old) == ControlState(
        lease_granted=True, lease_holder="home-1", polled_at=3.0
    )


async def test_state_is_persisted_under_its_key(
    store: Store, ladder: Ladder, clock: FakeClock
) -> None:
    c = center(store, ladder, clock, local_mode=True)
    await c.pause()
    raw = await store.kv_get(KV_CONTROL)
    assert raw is not None and '"local_paused": true' in raw
    # a verdict has nothing to do with control
    assert (await ladder.on_verdict(verdict(PageState.OK))).reason == ""


class TestMonitoredFile:
    def write(self, tmp_path: Path, text: str | bytes) -> Path:
        path = tmp_path / "monitored.toml"
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text)
        return path

    def test_missing_file(self, tmp_path: Path) -> None:
        assert load_monitored_file(tmp_path / "monitored.toml") is None

    def test_valid(self, tmp_path: Path) -> None:
        path = self.write(
            tmp_path,
            '[[group]]\nid = "111"\n\n[[group]]\nid = "222"\ntier = 3\nlistings = true\n',
        )
        groups = load_monitored_file(path)
        assert groups is not None
        assert [(g.group_id, g.tier, g.listings) for g in groups] == [
            ("111", 1, False),
            ("222", 3, True),
        ]
        assert load_monitored_file(self.write(tmp_path, "")) == []

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("[[group]\n", "not valid TOML"),
            (b"\xff\xfe", "not valid TOML"),
            ('group = "111"\n', "array of tables"),
            ("group = [1]\n", "not a table"),
            ('[[group]]\nid = "abc"\n', "is invalid"),
            ('[[group]]\nid = "111"\ntier = "x"\n', "is invalid"),
            ('[[group]]\nid = "111"\n[[group]]\nid = "111"\n', "twice"),
            ("".join(f'[[group]]\nid = "{100 + i}"\n' for i in range(26)), "hard limit"),
        ],
    )
    def test_invalid(self, tmp_path: Path, text: str | bytes, message: str) -> None:
        with pytest.raises(MonitoredFileError, match=message):
            load_monitored_file(self.write(tmp_path, text))

    def test_default_is_every_member_group_up_to_25(self) -> None:
        known = [
            KnownGroup(str(100 + i), GroupState.MEMBER, known_since=float(30 - i))
            for i in range(30)
        ]
        known.append(KnownGroup("999", GroupState.LEFT, known_since=0.0))
        known.append(KnownGroup("998", GroupState.MEMBER))
        chosen = default_monitored(known)
        assert len(chosen) == 25
        assert chosen[0].group_id == "129"  # known longest
        assert "999" not in {g.group_id for g in chosen}
        assert "998" not in {g.group_id for g in chosen}

    def test_the_default_set_is_sticky(self) -> None:
        """Groups monitored already keep their places, whatever a later sync lists first."""
        known = [
            KnownGroup(str(100 + i), GroupState.MEMBER, known_since=float(i), monitored=i >= 5)
            for i in range(30)
        ]
        chosen = {g.group_id for g in default_monitored(known)}
        assert chosen == {str(100 + i) for i in range(5, 30)}
        known[7] = KnownGroup("107", GroupState.UNVERIFIED, known_since=7.0, monitored=True)
        refilled = {g.group_id for g in default_monitored(known)}
        assert "107" not in refilled
        assert "100" in refilled  # the free place goes to the group known longest
