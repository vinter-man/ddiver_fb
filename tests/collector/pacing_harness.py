"""The whole pacing stack of one collector over one state file, on a ``FakeClock``.

Rebuilt on every ``open`` — which is how the tests simulate a crash and a restart: the next
instance knows only what the store (and the fakes standing in for the machine) remember.

The gates are the production list (``runner.production_gates``), profile lock and halt gate
included: a property proven here holds for the gates the collector really runs with.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path

from fbcollector.budget import Budget
from fbcollector.clock import FakeClock
from fbcollector.config import Settings
from fbcollector.control import ControlCenter
from fbcollector.gates import (
    KV_HOME_PIN,
    KV_LOGIN_AT,
    GateContext,
    GateResult,
    GateSet,
    ProfileLock,
)
from fbcollector.ladder import Ladder
from fbcollector.rhythm import Jitter, Rhythm, RhythmConfig
from fbcollector.runner import production_gates
from fbcollector.scheduler import Planner, Scheduler
from fbcollector.store import Store

from .gate_fakes import HOME_PIN, FakeLookup, FakeProbes
from .pacing_support import Events, seed_groups


@dataclass
class Harness:
    path: Path
    clock: FakeClock
    settings: Settings
    seed: str = "harness-seed"
    local_mode: bool = True
    probes: FakeProbes = field(default_factory=FakeProbes)
    lookup: FakeLookup = field(default_factory=FakeLookup)
    store: Store | None = None
    events: Events = field(default_factory=Events)

    async def open(self) -> Scheduler:
        self.store = Store(self.path, clock=self.clock)
        await self.store.open()
        store = self.store
        jitter = Jitter(self.seed)
        rhythm = Rhythm(RhythmConfig.from_settings(self.settings), jitter)
        self.planner = Planner(self.settings, rhythm, jitter)
        self.budget = Budget(
            store,
            self.clock,
            min_gap=self.settings.min_navigation_gap_seconds,
            revisit=self.settings.group_revisit_minutes * 60,
        )
        self.ladder = Ladder(store, self.clock, events=self.events)
        self.control = ControlCenter(
            store,
            self.ladder,
            self.clock,
            collector_id="home-1",
            local_mode=self.local_mode,
            rng=random.Random(7),
        )
        await self.control.load()
        self.lock = ProfileLock(self.path.parent / "collector.lock")
        self.lock.acquire()
        self.gates = GateSet(
            production_gates(
                settings=self.settings,
                store=store,
                clock=self.clock,
                probes=self.probes,
                lookup=self.lookup,
                jitter=jitter,
                lock=self.lock,
                ladder=self.ladder,
                control=self.control,
                rhythm=self._rhythm_gate,
            )
        )
        self.scheduler = Scheduler(
            store=store,
            settings=self.settings,
            clock=self.clock,
            planner=self.planner,
            budget=self.budget,
            gates=self.gates,
            ladder=self.ladder,
            control=self.control,
        )
        await self.scheduler.load()
        return self.scheduler

    async def _rhythm_gate(self, context: GateContext) -> GateResult:
        return await self.scheduler.rhythm_gate(context)

    async def close(self) -> None:
        if self.store is not None:
            await self.store.close()
            self.store = None
        lock = getattr(self, "lock", None)
        if lock is not None:
            lock.release()

    async def prepare(self, *, login_at: float, groups: list[str]) -> None:
        """A pinned home, a login long enough ago to be past warm-up, monitored groups."""
        assert self.store is not None
        await self.store.kv_set(KV_HOME_PIN, HOME_PIN.to_json())
        await self.store.kv_set(KV_LOGIN_AT, repr(login_at))
        await seed_groups(self.store, groups)
        await self.scheduler.load()
