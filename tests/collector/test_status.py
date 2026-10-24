"""The loop's state record and the wire ``Status`` built from it (no network, a virtual clock)."""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fbcollector.clock import FakeClock
from fbcollector.model import CollectorState, Coverage, PageState, ReadOutcome, Surface
from fbcollector.reader import KV_PARSER_BROKEN
from fbcollector.scheduler import Cadence
from fbcollector.status import (
    BACKSTOP_COUNTER,
    FEED_COUNTER,
    KV_HALT,
    KV_STATUS,
    KV_VERSIONS,
    StatusBuilder,
    note_state,
    package_version,
)
from fbcollector.store import Store
from fbwire.models import BudgetKind, Profile, Status

from .pacing_support import make_settings, seed_groups

T0 = 1_790_000_000.0


class Scheduler:
    def __init__(self, cadence: Cadence) -> None:
        self.value = cadence

    async def cadence(self) -> Cadence:
        return self.value


NORMAL = Cadence(Profile.NORMAL, feed_max=570.0, backstop=5625.4, backstop_only=False)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=T0)


@pytest.fixture
async def store(tmp_path: Path, clock: FakeClock) -> AsyncIterator[Store]:
    async with Store(tmp_path / "state.sqlite", clock=clock) as opened:
        yield opened


def builder(store: Store, clock: FakeClock, cadence: Cadence = NORMAL) -> StatusBuilder:
    return StatusBuilder(
        store=store,
        scheduler=Scheduler(cadence),
        settings=make_settings(collector_id="home-7"),
        clock=clock,
        version="0.1.0",
    )


class TestNoteState:
    async def test_since_survives_the_same_state_and_moves_with_a_new_one(
        self, store: Store
    ) -> None:
        assert await note_state(store, CollectorState.OK, ["x"], now=T0) == {}
        before = await note_state(store, CollectorState.OK, ["y"], now=T0 + 60)
        assert before["reasons"] == ["x"]
        record = await store.kv_get_json(KV_STATUS)
        assert (record["since"], record["at"], record["reasons"]) == (T0, T0 + 60, ["y"])
        await note_state(store, CollectorState.QUIET, [], now=T0 + 120, until=T0 + 3600)
        record = await store.kv_get_json(KV_STATUS)
        assert (record["state"], record["since"], record["until"]) == ("quiet", T0 + 120, T0 + 3600)

    async def test_gates_are_kept_through_turns_that_checked_none(self, store: Store) -> None:
        await note_state(store, CollectorState.NOT_HOME, [], now=T0, gates={"home": False})
        await note_state(store, CollectorState.OK, [], now=T0 + 1)  # a planner wait
        assert (await store.kv_get_json(KV_STATUS))["gates"] == {"home": False}
        await note_state(store, CollectorState.OK, [], now=T0 + 2, gates={"home": True})
        record = await store.kv_get_json(KV_STATUS)
        assert record["gates"] == {"home": True}
        assert "until" not in record


class TestBuild:
    async def test_a_fresh_collector_reports_ok_with_its_budget_and_cadence(
        self, store: Store, clock: FakeClock
    ) -> None:
        await store.kv_set_json(KV_VERSIONS, {"chrome": "153.0.1.2", "patchright": None})
        status = await builder(store, clock).build(seq=5, incarnation="abcdefgh1234")
        assert isinstance(status, Status)
        assert (status.collector_id, status.state, status.profile) == (
            "home-7",
            CollectorState.OK,
            Profile.NORMAL,
        )
        assert (status.seq, status.incarnation, status.sent_at) == (5, "abcdefgh1234", int(T0))
        assert status.block is None and status.detail is None and status.resumes_at is None
        windows = {(w.kind, w.window_seconds): w.cap for w in status.budget}
        assert windows[(BudgetKind.NAVIGATION, 3600)] == 20
        assert windows[(BudgetKind.SCROLL, 86400)] == 500
        assert status.counters[FEED_COUNTER] == 570
        assert status.counters[BACKSTOP_COUNTER] == 5626  # rounded up: a promise
        assert status.counters["outbox_depth"] == 0
        assert status.versions == {
            "chrome": "153.0.1.2",
            "fbcollector": "0.1.0",
            "wire": "1",
        }

    async def test_each_monitored_group_says_when_its_page_was_last_read_through(
        self, store: Store, clock: FakeClock
    ) -> None:
        await seed_groups(store, ["111", "222", "333"])
        await seed_groups(store, ["444"], monitored=False)

        async def read(surface: Surface, group_id: str | None, *, reached: bool) -> None:
            read_id = await store.start_read(surface, group_id=group_id, target=T0 - 3600)
            clock.advance(60)
            outcome = ReadOutcome.COMPLETE if reached else ReadOutcome.GAP
            coverage = Coverage(newest=int(T0), oldest=int(T0 - 7200), target_reached=reached)
            await store.finish_read(read_id, coverage=coverage, steps=3, outcome=outcome)
            clock.advance(600)

        await read(Surface.GROUP, "111", reached=True)
        first = int(clock.time())
        await read(Surface.GROUP, "111", reached=True)  # the latest that reached its target
        await read(Surface.GROUP, "111", reached=False)  # stopped short: not a read-through
        await read(Surface.GROUP, "222", reached=False)
        await read(Surface.FEED, None, reached=True)  # the feed is not the group's own page
        await read(Surface.GROUP, "444", reached=True)  # not monitored
        status = await builder(store, clock).build()
        assert status.group_checked == {"111": first}
        # A page that ignores the sort: old posts bumped by comments can end the read early.
        await store.set_group_flags("111", {"sort_ignored"})
        assert (await builder(store, clock).build()).group_checked == {}

    async def test_no_backstop_interval_no_counter(self, store: Store, clock: FakeClock) -> None:
        cadence = Cadence(Profile.CAUTIOUS, feed_max=840.0, backstop=None, backstop_only=False)
        status = await builder(store, clock, cadence).build()
        assert BACKSTOP_COUNTER not in status.counters
        assert status.profile is Profile.CAUTIOUS
        infinite = Cadence(Profile.NORMAL, feed_max=570.0, backstop=math.inf, backstop_only=False)
        assert BACKSTOP_COUNTER not in (await builder(store, clock, infinite).build()).counters

    async def test_the_open_block_is_the_state(self, store: Store, clock: FakeClock) -> None:
        await note_state(store, CollectorState.OK, ["last read: complete"], now=T0)
        clock.advance(30)
        block = await store.open_block(
            state=PageState.BLOCKED,
            reason="suspect automated behaviour",
            until=T0 + 6 * 3600,
            rung=0,
        )
        status = await builder(store, clock).build()
        assert status.state is CollectorState.BLOCKED
        assert status.block is not None
        assert (status.block.id, status.block.until, status.block.rung) == (
            block.id,
            int(T0 + 6 * 3600),
            0,
        )
        assert status.state_since == int(T0 + 30)
        assert status.detail == "suspect automated behaviour"

    async def test_a_forced_logout_says_so(self, store: Store, clock: FakeClock) -> None:
        await store.open_block(
            state=PageState.LOGGED_OUT, reason="logged out", until=None, rung=3, no_probe=True
        )
        status = await builder(store, clock).build()
        assert status.state is CollectorState.LOGGED_OUT
        assert status.block is not None and status.block.until is None and status.block.no_probe

    async def test_a_stale_blocked_record_is_reported_as_reading(
        self, store: Store, clock: FakeClock
    ) -> None:
        await note_state(store, CollectorState.BLOCKED, ["block: block 1"], now=T0)
        status = await builder(store, clock).build()
        assert (status.state, status.detail, status.block) == (CollectorState.OK, None, None)

    async def test_a_broken_parser_is_named(self, store: Store, clock: FakeClock) -> None:
        await note_state(store, CollectorState.PAUSED, ["halt: parser broken: x"], now=T0)
        await store.kv_set(KV_PARSER_BROKEN, '["accepted 0 of 9"]')
        status = await builder(store, clock).build()
        assert status.state is CollectorState.PARSER_BROKEN
        assert status.detail == "halt: parser broken: x"
        await store.kv_set_json(KV_HALT, {"kind": "selftest", "reason": "Chrome 154 failed"})
        assert (await builder(store, clock).build()).state is CollectorState.PAUSED

    async def test_backstop_only_reading_is_feed_not_chrono(
        self, store: Store, clock: FakeClock
    ) -> None:
        cadence = Cadence(Profile.NORMAL, feed_max=570.0, backstop=3600.0, backstop_only=True)
        await note_state(store, CollectorState.OK, [], now=T0)
        status = await builder(store, clock, cadence).build()
        assert status.state is CollectorState.FEED_NOT_CHRONO

    @pytest.mark.parametrize(
        ("state", "announced"),
        [
            (CollectorState.QUIET, True),
            (CollectorState.BREAK, True),
            (CollectorState.UNKNOWN, True),
            (CollectorState.OK, False),
            (CollectorState.NOT_HOME, False),
        ],
    )
    async def test_planned_pauses_announce_their_end(
        self, store: Store, clock: FakeClock, state: CollectorState, announced: bool
    ) -> None:
        await note_state(store, state, ["rhythm: x"], now=T0, until=T0 + 1800.7)
        status = await builder(store, clock).build()
        assert status.state is state
        assert status.resumes_at == (int(T0 + 1800) if announced else None)
        assert status.state_since == int(T0)

    async def test_garbage_in_the_record_is_not_reported(
        self, store: Store, clock: FakeClock
    ) -> None:
        await store.kv_set_json(
            KV_STATUS,
            {
                "state": "offline",  # the brain's verdict, never the collector's
                "reasons": ["", "x" * 400],
                "since": "yesterday",
                "until": 1.0e20,
                "gates": {"home": False, "Bad-Key": True, "x" * 41: True},
            },
        )
        status = await builder(store, clock).build()
        assert status.state is CollectorState.OK
        assert status.state_since is None and status.resumes_at is None
        assert status.detail == "x" * 300
        assert status.gates == {"home": False}
        await store.kv_set_json(KV_STATUS, ["not", "a", "record"])
        assert (await builder(store, clock).build()).state is CollectorState.OK
        await store.kv_set_json(KV_STATUS, {"state": "no-such-state", "gates": "open"})
        status = await builder(store, clock).build()
        assert status.state is CollectorState.OK and status.gates == {}

    async def test_counters_follow_the_queues(self, store: Store, clock: FakeClock) -> None:
        await store.kv_incr("outbox_dropped", 3)
        await store.kv_incr("events_dropped", 2)
        counters = (await builder(store, clock).build()).counters
        assert (counters["outbox_dropped"], counters["events_dropped"]) == (3, 2)
        assert counters["outbox_rejected"] == 0


def test_the_package_version_is_known(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.metadata

    assert package_version() not in {"", "unknown"}

    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    assert package_version() == "unknown"
