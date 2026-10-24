"""The collector process: composition root, the read loop, login, and the shutdown order.

``build`` wires the parts once per process — store, jitter, rhythm, planner, budget, ladder,
control, gates, scheduler, driver, reader, sink and the alert bot — with every I/O port
injectable, so the whole loop runs offline on a ``FakeClock`` with a fake driver in the tests.

The loop (``Collector.run``), each turn: reload control (a CLI ``pause`` or ``resume`` from a
TTY lands in the same database), housekeeping (drain the sink, prune, status snapshot, alert
on long absences), then ask the scheduler — telling it whether Chrome is running. A ``Hold``
closes Chrome when it says so (any closed gate, also one that closed while Chrome sat parked
between reads; quiet hours, a break, a block) and waits. A ``Grant`` made while Chrome is not
running was checked against the launch door and carries the recorded launch — two when this
Chrome major was never tested, since the local self-test is a Chrome launch on the account's
profile of its own: the crash-loop gate admits both together and both are recorded before
either starts (an unreadable version halts instead). The self-test runs, every gate is checked
again, then Chrome starts, with the command-line comparison with the login's and the cookie
gate — and every gate is checked once more before the read navigates, because the launch and
the self-test take long enough for the machine to sleep or change networks. Then the
``Reader`` has the read.

Owner alerts raised during a read (a block, a lost group) are held until the loop has closed
Chrome: the tab must not sit on a checkpoint page through a Telegram round trip, and the alert
says Chrome is closed (DESIGN, invariant 3: block on disk, Chrome closed, owner alerted).

A read is aborted, with Chrome closed under the step in flight, when the OS announces a sleep,
and when this process learns that reading must stop: an answer of the brain that pauses or
takes the lease away, or the owner's ``pause`` from another process, which every gate check
reads from the database (``control``). Between those, every scroll step is paid under the
control state in force then, and a grant is discarded when that state changed before its
navigation.


With an ingest URL the phase-0 sink is replaced by the outbox and the ``Uplink``, a task that
runs beside the loop for the whole process: it polls ``/v1/control`` (the lease, the monitored
set, commands), pushes items and events, sends each finished sync's groups snapshot and reports
the loop's state every minute. The loop wakes it after every read; without the lease the
control gate keeps every read shut, from the first turn on (fail closed).

Shutdown (SIGTERM from launchd/systemd, or Ctrl-C) is ordered: stop reads between steps, park,
close Chrome, stop the uplink and flush the sink (or one bounded push to the brain), close the
database. Its last write says the exit was asked for (``KV_PLANNED_EXIT``), so the next start
is a planned one.

The crash-loop gate counts unplanned process starts and Chrome launches only. A start is
planned when the process before it ended in that orderly shutdown; an exit through an
exception, a ``max_turns`` break, SIGKILL, a power cut or a crash leaves no marker, and
launchd's KeepAlive restarts a process that keeps exiting on its own just the same. A launch
is planned when the Chrome before it ended by the loop's own planned close — a hold (quiet
hours, a break, a closed gate), an announced sleep, reading being stopped, the shutdown — or,
for the first launch of a process, when the start was planned. A read that failed, a page the
detectors did not pass, a launch that failed and a Chrome that ended with no close of ours
make the next launch unplanned.

``login`` stops the supervised service, takes the profile lock and launches Chrome through the
*same* driver launcher on ``about:blank``, in the ``LOGIN`` session mode — and does nothing
else: no step, no capture, and no tab or popup of the owner's is closed (the reads' one-tab
rule would break a 2FA or checkpoint flow). The owner logs in by hand, in as many tabs as it
takes, and quits Chrome; the command line is recorded, and ``login_at`` (which starts the
warm-up and the 14-day cautious ramp) only when the session went from no Facebook cookies to
both. The cookies are read from the profile's store, not from a tab, every ``LOGIN_POLL``
while Chrome runs. A login is the riskiest thing that ever happens on the account (new-network
logins drew the ``advanced_protection`` locks), so it first checks the home network, the
timezone and the power source against ``pin-home`` and refuses anywhere else.

``fbcollector selftest`` also starts Chrome on the logged-in profile, so it passes the launch
door of the read loop — every gate of invariant 2, its launch counted by the crash-loop gate and
recorded before Chrome starts — except the warm-up, the rhythm and the halt gates
(``SELFTEST_EXEMPT``: the owner runs it before the login, after a Linux upgrade in quiet hours,
and to answer a ``SELFTEST`` halt).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import random
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TextIO

import httpx

from fbcollector import alerts as alert_texts
from fbcollector import paths
from fbcollector import status as wire_status
from fbcollector.alerts import AlertBot, Alerts, NullAlerts
from fbcollector.budget import Budget
from fbcollector.clock import Clock, SystemClock
from fbcollector.config import Settings
from fbcollector.confirm import describe as describe_confirmation
from fbcollector.confirm import load_confirmations
from fbcollector.control import ControlCenter
from fbcollector.deadline import run_within
from fbcollector.driver import Driver
from fbcollector.driver.launch import (
    ChromeVersion,
    CommandLineMismatchError,
    SessionMode,
    chrome_resources,
    compare_command,
    installed_chrome_version,
)
from fbcollector.driver.policy import FacebookSurfaces, UrlPolicy
from fbcollector.driver.selftest import SelfTestReport, run_selftest
from fbcollector.events import KV_QUARANTINE, CollectorEvents, outbox_edge, quarantine_edge
from fbcollector.gates import (
    KV_HOME_PIN,
    KV_LOGIN_AT,
    Activity,
    CrashLoopGate,
    EgressLookup,
    FuncGate,
    Gate,
    GateContext,
    GateResult,
    GateSet,
    HomeGuard,
    HomePin,
    HostProbes,
    PowerGate,
    ProfileLock,
    ProfileLockedError,
    SleepGate,
    SleepNotices,
    TimezoneGate,
    WarmupGate,
)
from fbcollector.ladder import Ladder
from fbcollector.model import CollectorState
from fbcollector.paths import Layout, Runner, run_command
from fbcollector.reader import (
    KV_PARSER_BROKEN,
    SLEEP_ABORT,
    Abort,
    Reader,
    ReaderConfig,
    ReadResult,
)
from fbcollector.rhythm import Jitter, Rhythm, RhythmConfig, load_seed
from fbcollector.scheduler import Grant, Hold, Planner, Scheduler, rhythm_result
from fbcollector.sink import ItemSink, JsonlSink, drain
from fbcollector.sleephook import NoSleepHook, SleepHook, SleepNotice
from fbcollector.status import StatusBuilder, note_state
from fbcollector.store import ProcessKind, Store
from fbcollector.sync import local_monitored
from fbcollector.uplink import IngestClient, Uplink
from fbwire.models import EventKind

log = logging.getLogger(__name__)

KV_CMDLINE: Final = "chrome_cmdline"
KV_VERSIONS: Final = wire_status.KV_VERSIONS
KV_HALT: Final = wire_status.KV_HALT
KV_STATUS: Final = wire_status.KV_STATUS
KV_NOT_HOME_SINCE: Final = "not_home_since"
KV_SELFTEST_PREFIX: Final = "selftest:"
KV_PLANNED_EXIT: Final = "planned_exit"
EXIT_CHROME_UNPLANNED: Final = "chrome unplanned"
"""The planned-exit marker's value when the process stopped with an unplanned Chrome launch
owed (its Chrome had failed or ended on its own): the successor's start is planned, its first
launch is not. Any other value (a time, from before) owes nothing."""
"""Written (fsynced) at the very end of a shutdown a signal asked for, once Chrome is closed;
the next process start takes it away and is recorded planned (the crash-loop gate)."""
SESSION_COOKIES: Final = frozenset({"c_user", "xs"})
MAX_WAIT: Final = 60.0
SHUTDOWN_PARK: Final = 30.0
"""Parking the tab at shutdown, before Chrome is closed (``_shutdown``)."""
CLEANUP_EVERY: Final = 3600.0
NOT_HOME_ALERT: Final = 12 * 3600.0
LOGIN_POLL: Final = 10.0
LAUNCH_RETRY: Final = 300.0
LOGIN_REFUSED: Final = 1
HOUR: Final = 3600.0
UPLINK_STOP_GRACE: Final = 15.0
KEY_UPLINK_RESTARTED: Final = "uplink_restarted"
"""How long shutdown waits for the uplink's request in flight (≤ 10 s) before cancelling it."""
STATUS_QUARANTINE: Final = 20
"""Quarantined posts ``fbcollector status`` lists one by one."""

DriverFactory = Callable[[UrlPolicy], Driver]
LocalDriverFactory = Callable[[int], Driver]


class Halt:
    """Reasons the collector stops itself until a human acts (kv ``halt``)."""

    NEEDS_LOGIN = "needs_login"
    COMMAND_LINE = "command_line"
    SELFTEST = "selftest"


async def set_halt(store: Store, kind: str, reason: str) -> None:
    await store.kv_set_json(KV_HALT, {"kind": kind, "reason": reason[:300]}, durable=True)


async def get_halt(store: Store) -> dict[str, str] | None:
    value = await store.kv_get_json(KV_HALT)
    return value if isinstance(value, dict) else None


@dataclass
class Ports:
    """Everything that touches the machine or the network, injectable for tests."""

    clock: Clock = field(default_factory=SystemClock)
    platform: str = sys.platform
    runner: Runner = run_command
    probes: HostProbes | None = None
    lookup: EgressLookup | None = None
    driver_factory: DriverFactory | None = None
    local_driver_factory: LocalDriverFactory | None = None
    alerts: Alerts | None = None
    sink: ItemSink | None = None
    uplink_transport: httpx.AsyncBaseTransport | None = None
    """How the uplink reaches the brain (tests); ``None``: the network, i.e. the tunnel."""
    rng: random.Random = field(default_factory=random.Random)
    chrome_version: Callable[[], Any] | None = None
    sleep_hook: SleepHook | None = None
    """The OS sleep hook the running collector starts; ``None``: none (tests). ``fbcollector
    run`` passes ``sleephook.platform_sleep_hook``."""


@dataclass
class Runtime:
    settings: Settings
    layout: Layout
    store: Store
    clock: Clock
    lock: ProfileLock
    ladder: Ladder
    control: ControlCenter
    scheduler: Scheduler
    gates: GateSet
    driver: Driver
    reader: Reader
    events: CollectorEvents
    alerts: Alerts
    sink: ItemSink | None
    """Phase-0 local mode only; with an uplink the outbox is the brain's."""
    uplink: Uplink | None
    ports: Ports
    sleep: SleepNotice
    """What the OS hook said about sleep; the sleep gate and the loop read it."""
    sleep_hook: SleepHook


def default_driver_factory(layout: Layout, ports: Ports) -> DriverFactory:
    from fbcollector.driver._patchright import PatchrightDriver  # noqa: PLC0415 - the one door
    from fbcollector.driver.launch import launch_spec  # noqa: PLC0415

    def make(policy: UrlPolicy) -> Driver:
        return PatchrightDriver(
            launch_spec(layout.profile, platform=ports.platform),
            policy=policy,
            clock=ports.clock,
            rng=ports.rng,
            runner=ports.runner,
        )

    return make


def default_local_factory(layout: Layout, ports: Ports) -> LocalDriverFactory:
    from fbcollector.driver.policy import LocalOnly  # noqa: PLC0415

    make = ports.driver_factory or default_driver_factory(layout, ports)
    return lambda port: make(LocalOnly(port))


def make_alerts(settings: Settings, store: Store, clock: Clock) -> Alerts:
    if settings.alert_bot_token is None or settings.alert_owner_id is None:
        return NullAlerts()
    return AlertBot(
        settings.alert_bot_token.get_secret_value(),
        settings.alert_owner_id,
        clock=clock,
        ledger=store,
    )


async def build(
    settings: Settings,
    store: Store,
    layout: Layout,
    ports: Ports,
    *,
    lock: ProfileLock | None = None,
) -> Runtime:
    """Wire one collector over an open store. Nothing here launches Chrome or opens a socket.

    ``lock`` is the profile lock the process already holds (its gate is closed otherwise).
    """
    clock = ports.clock
    alerts = ports.alerts or make_alerts(settings, store, clock)
    events = CollectorEvents(
        store,
        alerts,
        collector_id=settings.collector_id,
        local_mode=settings.local_mode,
        zone=settings.zone,
    )
    jitter = Jitter(await load_seed(store))
    rhythm = Rhythm(RhythmConfig.from_settings(settings), jitter)
    planner = Planner(settings, rhythm, jitter)
    budget = Budget(
        store,
        clock,
        min_gap=settings.min_navigation_gap_seconds,
        revisit=settings.group_revisit_minutes * 60,
    )
    ladder = Ladder(
        store,
        clock,
        events=events,
        cautious_days_after_warning=settings.cautious_days_after_warning,
        cautious_days_after_logout=settings.cautious_days_after_logout,
        strike_decay_days=settings.strike_decay_days,
    )
    control = ControlCenter(
        store,
        ladder,
        clock,
        collector_id=settings.collector_id,
        local_mode=settings.local_mode,
        rng=ports.rng,
    )
    await control.load()
    lock = lock or ProfileLock(layout.lock)
    probes = ports.probes
    if probes is None:
        from fbcollector.gates import host_probes  # noqa: PLC0415

        probes = host_probes(ports.platform, ports.runner)
    lookup = ports.lookup
    if lookup is None:
        from fbcollector.gates import IpifyLookup  # noqa: PLC0415

        lookup = IpifyLookup()

    scheduler_ref: list[Scheduler] = []

    async def rhythm_gate(context: GateContext) -> GateResult:
        return await scheduler_ref[0].rhythm_gate(context)

    notice = SleepNotice(clock)
    gate_set = GateSet(
        production_gates(
            settings=settings,
            store=store,
            clock=clock,
            probes=probes,
            lookup=lookup,
            jitter=jitter,
            lock=lock,
            ladder=ladder,
            control=control,
            rhythm=rhythm_gate,
            sleep=notice,
        )
    )
    scheduler = Scheduler(
        store=store,
        settings=settings,
        clock=clock,
        planner=planner,
        budget=budget,
        gates=gate_set,
        ladder=ladder,
        control=control,
    )
    scheduler_ref.append(scheduler)
    await scheduler.load()
    factory = ports.driver_factory or default_driver_factory(layout, ports)
    driver = factory(FacebookSurfaces())

    async def resources() -> dict[str, Any]:
        return await asyncio.to_thread(chrome_resources, layout.profile, runner=ports.runner)

    reader = Reader(
        store=store,
        driver=driver,
        scheduler=scheduler,
        ladder=ladder,
        events=events,
        clock=clock,
        config=ReaderConfig(
            collector_id=settings.collector_id,
            raw_dir=layout.raw,
            shots_dir=layout.shots,
            novelty_days=settings.novelty_days,
            late_after_hours=settings.late_after_hours,
            outbox_cap=settings.outbox_cap,
        ),
        rng=ports.rng,
        resources=resources,
    )
    sink: ItemSink | None = None
    uplink: Uplink | None = None
    if settings.local_mode:
        sink = ports.sink or JsonlSink(layout.root / "items.jsonl")
    else:
        uplink = make_uplink(
            settings, store, control=control, scheduler=scheduler, alerts=alerts, ports=ports
        )
    return Runtime(
        settings=settings,
        layout=layout,
        store=store,
        clock=clock,
        lock=lock,
        ladder=ladder,
        control=control,
        scheduler=scheduler,
        gates=gate_set,
        driver=driver,
        reader=reader,
        events=events,
        alerts=alerts,
        sink=sink,
        uplink=uplink,
        ports=ports,
        sleep=notice,
        sleep_hook=ports.sleep_hook or NoSleepHook(),
    )


def make_uplink(
    settings: Settings,
    store: Store,
    *,
    control: ControlCenter,
    scheduler: Scheduler,
    alerts: Alerts,
    ports: Ports,
) -> Uplink:
    if settings.ingest_url is None or settings.ingest_token is None:
        msg = "an uplink needs FBC_INGEST_URL and FBC_INGEST_TOKEN"
        raise ValueError(msg)
    client = IngestClient(
        settings.ingest_url,
        settings.ingest_token.get_secret_value(),
        timeout=settings.ingest_timeout,
        transport=ports.uplink_transport,
        socket=None if settings.ingest_socket is None else str(settings.ingest_socket),
    )
    return Uplink(
        client=client,
        store=store,
        control=control,
        status=StatusBuilder(
            store=store, scheduler=scheduler, settings=settings, clock=ports.clock
        ),
        alerts=alerts,
        clock=ports.clock,
        collector_id=settings.collector_id,
    )


GATE_ORDER: Final = (
    "sleep",
    "crash_loop",
    "timezone",
    "power",
    "home",
    "warmup",
    "lock",
    "halt",
    "block",
    "control",
    "rhythm",
)
"""Cheap and state-changing first: the sleep gate invalidates the home guard's verdict."""


async def load_pin(store: Store) -> HomePin | None:
    raw = await store.kv_get(KV_HOME_PIN)
    return None if raw is None else HomePin.from_json(raw)


def production_gates(
    *,
    settings: Settings,
    store: Store,
    clock: Clock,
    probes: HostProbes,
    lookup: EgressLookup,
    jitter: Jitter,
    lock: ProfileLock,
    ladder: Ladder,
    control: ControlCenter,
    rhythm: Callable[[GateContext], Awaitable[GateResult]],
    sleep: SleepNotices | None = None,
) -> list[Gate]:
    """Every gate of invariant 2, in ``GATE_ORDER`` — the one list the collector runs with.

    The pacing tests build their harness from this function too, so a gate dropped here fails
    them instead of passing a hand-built copy. ``sleep`` is what the OS sleep hook reports.
    """

    async def pin() -> HomePin | None:
        return await load_pin(store)

    async def pin_zone() -> str | None:
        pinned = await load_pin(store)
        return None if pinned is None else pinned.timezone

    async def halt_gate(context: GateContext) -> GateResult:  # noqa: ARG001 - one rule
        return await _halt_result(store)

    home = HomeGuard(probes, lookup, pin, clock)
    return [
        SleepGate(clock, on_sleep=[home.invalidate], notices=sleep),
        CrashLoopGate(store, clock),
        TimezoneGate(probes.os_timezone, settings.timezone, home_zone=pin_zone),
        PowerGate(probes.power, clock),
        home,
        WarmupGate(
            store,
            clock,
            jitter,
            min_hours=settings.warmup_min_hours,
            max_hours=settings.warmup_max_hours,
        ),
        lock,
        FuncGate("halt", halt_gate),
        FuncGate("block", ladder.gate),
        FuncGate("control", control.gate),
        FuncGate("rhythm", rhythm),
    ]


async def _halt_result(store: Store) -> GateResult:
    halt = await get_halt(store)
    if halt is not None:
        state = (
            CollectorState.NEEDS_LOGIN
            if halt.get("kind") in {Halt.NEEDS_LOGIN, Halt.COMMAND_LINE}
            else CollectorState.PAUSED
        )
        return GateResult("halt", False, str(halt.get("reason", "halted")), state)
    broken = await store.kv_get(KV_PARSER_BROKEN)
    if broken is not None:
        return GateResult("halt", False, f"parser broken: {broken}", CollectorState.PAUSED)
    return GateResult("halt", ok=True)


async def wait_or_stop(
    clock: Clock, seconds: float, stop: asyncio.Event, *also: asyncio.Event
) -> None:
    """Sleep on the collector's clock, but wake at once when shutdown is asked for (or any of
    ``also`` is set: the OS announcing a sleep, the control terms changing)."""
    if seconds <= 0 or stop.is_set() or any(event.is_set() for event in also):
        return
    sleeper = asyncio.ensure_future(clock.sleep(seconds))
    stoppers = [asyncio.ensure_future(event.wait()) for event in (stop, *also)]
    try:
        await asyncio.wait({sleeper, *stoppers}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (sleeper, *stoppers):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _end_watch(watcher: asyncio.Task[None], abort: asyncio.Event) -> None:
    """End a read's watcher. One that fired is closing Chrome: its close is awaited (bounded
    by the driver's close timeout), since cancelling it half-way would let the OS sleep on a
    Chrome still shutting down, or a stopped collector read on. One that did not fire is
    cancelled."""
    if abort.is_set():
        with contextlib.suppress(Exception):
            await watcher
        return
    watcher.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await watcher


class Collector:
    def __init__(
        self,
        rt: Runtime,
        *,
        max_turns: int | None = None,
        start: tuple[bool, bool] | None = None,
    ) -> None:
        self.rt = rt
        self._max_turns = max_turns
        self._start = start
        """The process start ``run_collector`` recorded before ``build`` (whether it and its
        first Chrome launch are planned, ``record_start``); ``None``: ``run`` records it."""
        self._last_cleanup: float | None = None
        self._monitored_mtime: float | None = None
        self._untested: ChromeVersion | None = None
        """The Chrome whose self-test the latest grant carries (``_selftest_due``)."""
        self._terms_changed = rt.control.changed
        """Set when the control terms move on after this turn's first look at them: the turn's
        wait ends on it (``_hold``)."""
        self.results: list[ReadResult] = []
        self.holds: list[Hold] = []
        self._launch_planned = False
        """Whether the next Chrome launch is planned (the crash-loop gate): set from the process
        start, then by how each Chrome session ended."""
        self._chrome_up = False
        """A Chrome this loop launched and has not seen end yet."""
        self._close_planned = True
        """Every close of the current Chrome session so far was planned."""

    # -- the loop ------------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> int:
        """Loop until ``stop`` is set (the signal handlers of ``run_collector``, or its caller).

        The process start is recorded first, unless ``run_collector`` already did.
        """
        rt = self.rt
        start = self._start if self._start is not None else await record_start(rt.store)
        self._launch_planned = start[1]
        if rt.settings.local_mode:
            # Without the brain's durable event queue the announcement lived only in the alert
            # held for the end of a read, which a crash or a power cut takes with it. The first
            # housekeeping raises it again while posts wait; the alert bot's ledger (24 h) keeps
            # one that did go out from repeating.
            await rt.store.kv_delete(KV_QUARANTINE)
        await self._refresh_monitored(force=True)
        uplink_stop = asyncio.Event()
        uplink = (
            asyncio.create_task(rt.uplink.run(uplink_stop), name="uplink")
            if rt.uplink is not None
            else None
        )
        rt.sleep.bind(asyncio.get_running_loop())
        rt.sleep_hook.start(rt.sleep)
        turns = 0
        asked = False
        try:
            while not stop.is_set():
                if self._max_turns is not None and turns >= self._max_turns:
                    break
                turns += 1
                # Cleared before the gates are looked at: an announcement after that look
                # sets it again and cuts the turn's wait short.
                rt.sleep.changed.clear()
                # An announced sleep is answered before anything slow: the gates below may wait
                # on a network that is going down with the machine, and the OS grants only so
                # long before it sleeps anyway. A parked Chrome is closed first.
                if rt.sleep.pending:
                    if rt.driver.running:
                        await self._close_chrome("the OS announced a sleep", planned=True)
                    self._ack_sleep()
                uplink = await self._keep_uplink(uplink, uplink_stop)
                await rt.control.load()
                # Taken before the plan and the gates look at the terms: a change after that
                # look (the uplink's answer, or a reload that finds another process's pause)
                # sets it and ends this turn's wait.
                self._terms_changed = rt.control.changed
                await self._housekeeping()
                # Right before Chrome's state is read for the plan, with no await in between:
                # a Chrome that died during the housekeeping is seen here, not relaunched first.
                self._chrome_gone()
                running = rt.driver.running
                decision = await rt.scheduler.next(
                    launch=not running,
                    parked=running,
                    selftest=self._selftest_due,
                    launch_planned=self._launch_planned,
                )
                if isinstance(decision, Hold):
                    await self._hold(decision, stop)
                    continue
                if not running and not await self._launch(decision, stop):
                    continue
                result = await self._read(decision, stop)
                if result.sync is not None:
                    if rt.uplink is not None:
                        await rt.uplink.queue_snapshot(result.sync)
                    await self._refresh_monitored(force=True)
                if rt.uplink is not None:
                    rt.uplink.wake()  # items, a block's events: to the brain within the minute
            else:
                asked = True  # the loop ended on ``stop``: not a break, not an exception
        finally:
            await self._shutdown(uplink_stop, uplink)
            await asyncio.to_thread(rt.sleep_hook.stop)
        await self._mark_exit(asked=asked)
        return 0

    async def _mark_exit(self, *, asked: bool) -> None:
        """The last write of an orderly shutdown someone asked for (a SIGTERM from launchd, an
        operator's restart or update), once Chrome is closed: the next start is planned. Its
        first Chrome launch is planned only if this process owed a planned one: a Chrome that
        failed or ended on its own before the stop still counts after it."""
        rt = self.rt
        if asked and not rt.driver.running:
            value = "chrome planned" if self._launch_planned else EXIT_CHROME_UNPLANNED
            await rt.store.kv_set(KV_PLANNED_EXIT, value, durable=True)

    async def _keep_uplink(
        self, task: asyncio.Task[None] | None, stop: asyncio.Event
    ) -> asyncio.Task[None] | None:
        """Restart the uplink task if it ended while the collector runs, and tell the owner.

        ``Uplink.run`` never raises, but if it ended anyway, nothing would poll the brain: no
        remote pause and no lease move would be heard, reads would go on under the last grant
        until it lapsed hours later, and no "brain unreachable" alert would come either.
        """
        rt = self.rt
        if task is None or rt.uplink is None or not task.done() or stop.is_set():
            return task
        error = None if task.cancelled() else task.exception()
        why = type(error).__name__ if error is not None else "it returned"
        log.error("the uplink task ended (%s); restarting it", why, exc_info=error)
        with contextlib.suppress(Exception):
            await rt.alerts.alert(
                KEY_UPLINK_RESTARTED,
                alert_texts.simple(
                    KEY_UPLINK_RESTARTED,
                    f"the task that talks to the brain ended ({why}) and was restarted.",
                    hint="A bug: the log has the traceback. Reading went on meanwhile.",
                ),
                dedup=6 * HOUR,
            )
        return asyncio.create_task(rt.uplink.run(stop), name="uplink")

    async def _read(self, grant: Grant, stop: asyncio.Event) -> ReadResult:
        """One read; owner alerts raised by it go out only after Chrome was closed.

        A sleep the OS announces meanwhile aborts it: Chrome is closed at once, under the step
        in flight (the OS waits for us only so long), and the reader judges nothing the torn
        down tab still returns. The step fails as soon as the tab is gone, long before Chrome
        has exited, so the read ends by waiting for that close — never by cancelling it — and
        only then lets the OS sleep. So does reading being stopped while the read runs: the
        brain's pause or a lease taken away (the uplink's answer), or the owner's ``pause`` from
        another process once a gate check has read it (``ControlCenter.wait_stopped``).
        """
        rt = self.rt
        rt.events.hold_alerts()
        abort = Abort()
        watcher = asyncio.create_task(self._watch(abort), name="read-watch")
        try:
            result = await rt.reader.read(grant, stop=stop, abort=abort)
            self.results.append(result)
            await note_state(
                rt.store,
                CollectorState.OK,
                [f"last read: {result.outcome.value}"],
                now=rt.clock.time(),
                gates=dict.fromkeys(GATE_ORDER, True),  # a granted read passed every gate
            )
            if result.close_chrome:
                await self._close_chrome(result.reason, planned=result.planned_close)
        except BaseException:
            with contextlib.suppress(Exception):
                await self._close_chrome("the read failed", planned=False)
            raise
        finally:
            await _end_watch(watcher, abort)
            self._ack_sleep()
            await rt.events.flush_alerts()
        return result

    async def _watch(self, abort: Abort) -> None:
        """Abort the read and close Chrome under it as soon as the OS announces a sleep or
        this process learns that reading must stop; runs for the length of one read."""
        rt = self.rt
        sleep = asyncio.ensure_future(rt.sleep.wait_pending())
        stopped = asyncio.ensure_future(rt.control.wait_stopped())
        try:
            await asyncio.wait({sleep, stopped}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleep, stopped):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if sleep.cancelled():
            reason = f"control: {stopped.result()}"
            log.warning("reading was stopped mid-read (%s): aborting it, closing Chrome", reason)
        else:
            reason = SLEEP_ABORT
            log.warning("the OS announced a sleep mid-read: aborting it and closing Chrome")
        abort.stop(reason)
        await self._close_chrome(reason, planned=True)
        self._ack_sleep()

    def _ack_sleep(self) -> None:
        """Chrome is closed — not merely closing — so an announced sleep may go ahead.

        ``Driver.running`` stays true until a close has finished.
        """
        if self.rt.sleep.pending and not self.rt.driver.running:
            self.rt.sleep.ready()

    async def _hold(self, hold: Hold, stop: asyncio.Event) -> None:
        rt = self.rt
        self.holds.append(hold)
        await self._note_state(hold)
        if hold.close_chrome and rt.driver.running:
            await self._close_chrome("; ".join(hold.reasons), planned=True)
        self._ack_sleep()
        now = rt.clock.time()
        until = hold.until if hold.until is not None else now + MAX_WAIT
        # A change of the terms this process learns (the brain's pause, the lease taken away)
        # ends the wait: the next turn closes a parked Chrome if the control gate shut.
        await wait_or_stop(
            rt.clock,
            min(max(until - now, 1.0), MAX_WAIT),
            stop,
            rt.sleep.changed,
            self._terms_changed,
        )

    async def _note_state(self, hold: Hold) -> None:
        rt = self.rt
        now = rt.clock.time()
        before = await note_state(
            rt.store, hold.state, hold.reasons, now=now, until=hold.until, gates=hold.gates
        )
        entered = before.get("state") != CollectorState.CRASH_LOOP.value
        if hold.state is CollectorState.CRASH_LOOP and entered:
            # Once when the loop is entered, not on every hold turn while it lasts.
            await rt.events(EventKind.CRASH_LOOP, detail="; ".join(hold.reasons)[:300])
        if hold.state is not CollectorState.NOT_HOME:
            await rt.store.kv_delete(KV_NOT_HOME_SINCE)
            return
        raw = await rt.store.kv_get(KV_NOT_HOME_SINCE)
        if raw is None:
            await rt.store.kv_set(KV_NOT_HOME_SINCE, repr(now))
            return
        local = dt.datetime.fromtimestamp(now, rt.settings.zone)
        if now - float(raw) > NOT_HOME_ALERT and 9 <= local.hour < 21:
            await rt.alerts.alert(
                "not_home",
                alert_texts.simple(
                    "not_home",
                    "not at home for more than 12 h: " + "; ".join(hold.reasons),
                    hint="Chrome stays closed until the home network is back.",
                ),
                dedup=24 * HOUR,
            )

    async def _launch(self, grant: Grant, stop: asyncio.Event) -> bool:
        """Start Chrome for ``grant`` (its launches are already recorded); ``False``: no read."""
        rt = self.rt
        if grant.selftest and not await self._selftest(grant, stop):
            return False
        try:
            info = await rt.driver.launch(mode=SessionMode.READ)
        except Exception as exc:  # noqa: BLE001 - Chrome missing, profile owned elsewhere, ...
            # The launch was already counted, and the next one is unplanned: repeated failures
            # close the crash-loop gate.
            self._launch_planned = False
            log.warning("Chrome did not start: %s", type(exc).__name__)
            with contextlib.suppress(Exception):
                await rt.driver.close()
            await rt.alerts.alert(
                "launch_failed",
                alert_texts.simple("launch_failed", f"Chrome did not start ({type(exc).__name__})"),
                dedup=6 * HOUR,
            )
            await wait_or_stop(rt.clock, LAUNCH_RETRY, stop)
            return False
        # From here only how this Chrome ends decides the next launch: a close of ours sets
        # it, and a Chrome that ends on its own leaves it unplanned.
        self._chrome_up = True
        self._close_planned = True
        self._launch_planned = False
        problem = check_command_line(await rt.store.kv_get(KV_CMDLINE), info.command_line)
        if problem is not None:
            await self._close_chrome(problem, planned=False)
            await set_halt(rt.store, Halt.COMMAND_LINE, problem)
            await rt.alerts.alert(
                "command_line",
                alert_texts.simple(
                    "command_line",
                    problem,
                    hint="Run `fbcollector login` to record the new launch, or roll back.",
                ),
                critical=True,
            )
            return False
        cookies = await rt.driver.cookie_names()
        if not cookies >= SESSION_COOKIES:
            await self._close_chrome("no Facebook session in the profile", planned=False)
            await set_halt(rt.store, Halt.NEEDS_LOGIN, "no Facebook session in the profile")
            await rt.alerts.alert(
                "needs_login",
                alert_texts.simple(
                    "needs_login",
                    "the profile has no Facebook session (c_user/xs missing)",
                    hint="Run `fbcollector login` at home.",
                ),
                critical=True,
            )
            return False
        versions = {
            "chrome": info.chrome_version.full if info.chrome_version else None,
            "patchright": info.patchright_version,
        }
        await rt.store.kv_set_json(KV_VERSIONS, versions)
        # The self-test and the launch take a while: a sleep, a VPN or another network in the
        # meantime must stop the read before its first navigation, not at its first scroll.
        hold = await rt.scheduler.recheck(grant)
        if hold is not None:
            log.warning("a gate closed while Chrome started: %s", "; ".join(hold.reasons))
            await self._hold(hold, stop)
            return False
        return True

    async def _selftest_due(self) -> bool:
        """Whether the read's launch must start with the local self-test (the scheduler asks
        once a read is due, Chrome is not running and every gate is open for its launch).

        An unreadable version halts instead (``False``, and the scheduler's second gate check
        then refuses the launch): an unknown version is not a tested one, and a self-test before
        every launch would spend the crash-loop gate's launches. Asked only past the gates, it
        halts at a launch that would really happen, never over another halt, and once: the
        halt gate refuses every later turn before the question.
        """
        rt = self.rt
        self._untested = None
        version_of = rt.ports.chrome_version or (
            lambda: installed_chrome_version(platform=rt.ports.platform, runner=rt.ports.runner)
        )
        version = await asyncio.to_thread(version_of)
        if version is None:
            await set_halt(rt.store, Halt.SELFTEST, "the installed Chrome's version is unreadable")
            await rt.alerts.alert(
                "selftest:unknown",
                alert_texts.simple(
                    "selftest",
                    "cannot read the installed Chrome's version, so it cannot be checked",
                    hint="Nothing touched Facebook. Check the Chrome install, run "
                    "`fbcollector selftest`, then `fbcollector pause --off`.",
                ),
                critical=True,
            )
            return False
        if version.tested or await rt.store.kv_get(selftest_key(version.major)) == "passed":
            return False
        self._untested = version
        return True

    async def _selftest(self, grant: Grant, stop: asyncio.Event) -> bool:
        """The local self-test the grant's launch starts with (its launch already recorded);
        ``True`` when it passed and every gate is still open for the read's own launch."""
        rt = self.rt
        version = self._untested
        if version is None:  # pragma: no cover - set by _selftest_due for this very grant
            return False
        factory = rt.ports.local_driver_factory or default_local_factory(rt.layout, rt.ports)
        report = await selftest(factory)
        if report.passed:
            await rt.store.kv_set(selftest_key(version.major), "passed")
            # The self-test took a while: the read's own launch passes every gate again first.
            hold = await rt.scheduler.recheck(grant)
            if hold is None:
                return True
            log.warning("a gate closed during the self-test: %s", "; ".join(hold.reasons))
            await self._hold(hold, stop)
            return False
        failed = ", ".join(report.failed())
        await set_halt(rt.store, Halt.SELFTEST, f"Chrome {version.major} failed: {failed}")
        await rt.alerts.alert(
            f"selftest:{version.major}",
            alert_texts.simple(
                "selftest",
                f"Chrome {version.full} failed the local self-test ({failed})",
                hint="Nothing touched Facebook. Check the driver, then `fbcollector pause --off`.",
            ),
            critical=True,
        )
        return False

    async def _close_chrome(self, reason: str, *, planned: bool) -> None:
        """Close Chrome, or wait for a close already under way (the sleep watcher's).

        ``planned``: the loop closes it on purpose (a hold, a sleep, reading stopped, the
        shutdown), so the next launch is planned. One unplanned close of the session (a read
        that failed, a page the detectors did not pass) makes it unplanned, even when another
        close overlaps it or finished first; so does a Chrome that had ended before any close
        of ours.
        """
        driver = self.rt.driver
        if not driver.running:
            self._chrome_gone()
            # Another close of this session finished first (the sleep watcher's): this one
            # still has its say, until the next launch.
            self._launch_planned = self._launch_planned and planned
            return
        self._close_planned = self._close_planned and planned
        log.info("closing Chrome (%s)", reason[:200])
        await driver.close()
        if self._chrome_up and not self.rt.driver.running:
            self._chrome_up = False
            self._launch_planned = self._close_planned

    def _chrome_gone(self) -> None:
        """A Chrome this loop launched is not running, and no close of ours ended it (it
        crashed, or someone quit it): the next launch is unplanned."""
        if self._chrome_up and not self.rt.driver.running:
            log.warning("Chrome ended without the collector closing it")
            self._chrome_up = False
            self._launch_planned = False

    async def _housekeeping(self) -> None:
        rt = self.rt
        if rt.sink is not None:
            await drain(rt.store, rt.sink, rt.settings.collector_id)
        depth = await rt.store.outbox_depth()
        if await outbox_edge(rt.store, depth, rt.settings.outbox_cap):
            cap = rt.settings.outbox_cap
            await rt.events(EventKind.OUTBOX_HIGH, detail=f"outbox holds {depth} items (cap {cap})")
        # A replay may have quarantined posts, or resolved all of them (the flag re-arms).
        invalid = await quarantine_edge(rt.store)
        if invalid is not None:
            await rt.events(EventKind.ITEMS_INVALID, detail=invalid)
        await self._refresh_monitored()
        now = rt.clock.time()
        if self._last_cleanup is None or now - self._last_cleanup >= CLEANUP_EVERY:
            self._last_cleanup = now
            await rt.store.cleanup(
                seen_retention=rt.settings.seen_retention_days * 86400,
                raw_dirs=[rt.layout.raw, rt.layout.shots],
                raw_retention=rt.settings.raw_retention_seconds,
            )

    async def _refresh_monitored(self, *, force: bool = False) -> None:
        rt = self.rt
        if not rt.settings.local_mode:
            return  # the brain's /v1/control owns the monitored set
        path = rt.settings.monitored_file
        try:
            mtime: float | None = path.stat().st_mtime
        except FileNotFoundError:
            mtime = None
        if not force and mtime == self._monitored_mtime:
            return
        self._monitored_mtime = mtime
        await local_monitored(rt.store, path)

    async def _shutdown(
        self, uplink_stop: asyncio.Event, uplink: asyncio.Task[None] | None
    ) -> None:
        rt = self.rt
        with contextlib.suppress(Exception):
            if rt.driver.running:
                # Bounded: a Chrome that no longer answers must not eat launchd's 90 s.
                await run_within(rt.driver.park(), SHUTDOWN_PARK)
        with contextlib.suppress(Exception):
            await self._close_chrome("shutdown", planned=True)
        self._ack_sleep()
        uplink_stop.set()
        if uplink is not None:
            await asyncio.wait({uplink}, timeout=UPLINK_STOP_GRACE)
            if not uplink.done():
                uplink.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await uplink
        with contextlib.suppress(Exception):
            if rt.uplink is not None:
                await rt.uplink.flush()
            elif rt.sink is not None:
                await drain(rt.store, rt.sink, rt.settings.collector_id)


def check_command_line(recorded: str | None, actual: str) -> str | None:
    """Why this launch differs from the login's, or ``None`` when it is the same."""
    if recorded is None:
        return "no Chrome command line was recorded at a login"
    try:
        compare_command(recorded, actual)
    except CommandLineMismatchError as exc:
        return str(exc)
    return None


async def selftest(factory: LocalDriverFactory) -> SelfTestReport:
    return await run_selftest(factory, guard_of=_guard_of)


def _guard_of(driver: Driver) -> Any:
    return getattr(driver, "_cdp", None)


# -- process entry points ---------------------------------------------------------------------


async def run_collector(
    settings: Settings, ports: Ports, *, observe: bool = False, stop: asyncio.Event | None = None
) -> int:
    """``fbcollector run``: prepare the root, take the lock, open the store, loop until stopped."""
    if observe and not settings.observe_mode:
        settings = settings.model_copy(update={"observe_mode": True})
    layout = paths.prepare(settings.root, platform=ports.platform, runner=ports.runner)
    lock = ProfileLock(layout.lock)
    lock.acquire()
    try:
        store = Store(layout.db, clock=ports.clock, outbox_cap=settings.outbox_cap)
        await store.open()
        try:
            # Before ``build``: a process that dies while it starts has taken the marker of
            # the stop before it, so the start after it counts.
            start = await record_start(store)
            rt = await build(settings, store, layout, ports, lock=lock)
            stopper = stop or asyncio.Event()
            if stop is None:
                _install_signals(stopper)
            return await Collector(rt, start=start).run(stopper)
        finally:
            await store.close()
    finally:
        lock.release()


async def record_start(store: Store) -> tuple[bool, bool]:
    """Record this process's start for the crash-loop gate: whether it is planned, and
    whether its first Chrome launch is.

    It is planned when the process before it left the marker of an orderly shutdown someone
    asked for (``KV_PLANNED_EXIT``); its first launch too, unless the marker says a failed
    Chrome was owed an unplanned one (``EXIT_CHROME_UNPLANNED``). The marker is taken in the
    same fsynced commit as the start's row: a crash or a power cut keeps both or neither.
    """
    marker = await store.record_process_start(taking=KV_PLANNED_EXIT)
    return marker is not None, marker is not None and marker != EXIT_CHROME_UNPLANNED


def _install_signals(stop: asyncio.Event) -> None:
    import signal  # noqa: PLC0415

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)


# -- the supervised service --------------------------------------------------------------------


LABEL: Final = "com.ddiver.fbcollector"
UNIT: Final = "fbcollector.service"


@dataclass
class ServiceManager:
    """Stops and restarts the supervised collector around a login (launchd or systemd --user)."""

    platform: str
    runner: Runner = run_command
    uid: int = field(default_factory=os.getuid)
    home: Path = field(default_factory=Path.home)

    @property
    def plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    def stop(self) -> bool:
        """Stop the service if it runs; ``True`` when it did (so ``start`` should follow)."""
        if self.platform == "darwin":
            target = f"gui/{self.uid}/{LABEL}"
            if self.runner(["/bin/launchctl", "print", target]).returncode != 0:
                return False
            return self.runner(["/bin/launchctl", "bootout", target]).returncode == 0
        active = self.runner(["systemctl", "--user", "is-active", "--quiet", UNIT])
        if active.returncode != 0:
            return False
        return self.runner(["systemctl", "--user", "stop", UNIT]).returncode == 0

    def start(self) -> bool:
        if self.platform == "darwin":
            args = ["/bin/launchctl", "bootstrap", f"gui/{self.uid}", str(self.plist)]
        else:
            args = ["systemctl", "--user", "start", UNIT]
        return self.runner(args).returncode == 0


CHECKLIST: Final = """\
Before this login (DESIGN, pre-login checklist):
  1. The account's UI language is already English (switched days ago on a trusted device).
  2. A passkey or recovery method exists in Accounts Center.
  3. A trusted device is at hand to approve the login.
  4. No lock wave or recent warning on the account.
  5. This machine is at home, on AC power, on the home network (no VPN, no proxy).
Chrome will open on about:blank with the collector's profile and do nothing else.
Type facebook.com yourself, log in, answer 2FA; browse a little by hand if you like.
Every later human action on this account (checkpoints, joins, follows) happens here too.
Quit Chrome (Cmd+Q / Ctrl+Q) when you are done.
"""


async def login(
    settings: Settings,
    ports: Ports,
    *,
    out: TextIO,
    service: ServiceManager | None = None,
    record_login: bool = False,
) -> int:
    out.write(CHECKLIST)
    out.flush()
    manager = service or ServiceManager(ports.platform, ports.runner)
    stopped = manager.stop()
    if stopped:
        out.write("Stopped the collector service for the login.\n")
    layout = paths.prepare(settings.root, platform=ports.platform, runner=ports.runner)
    lock = ProfileLock(layout.lock)
    try:
        lock.acquire()
    except ProfileLockedError:
        out.write("The collector is still running on this profile; stop it first.\n")
        if stopped:
            manager.start()
        return 4
    try:
        async with Store(layout.db, clock=ports.clock) as store:
            problems = await away_from_home(settings, store, ports)
            if problems:
                out.write(
                    "Refusing to open the account's Chrome here:\n"
                    + "".join(f"  - {p}\n" for p in problems)
                )
                return LOGIN_REFUSED
            return await _login_session(store, layout, ports, out=out, record_login=record_login)
    finally:
        lock.release()
        if stopped and manager.start():
            out.write("Restarted the collector service.\n")


async def away_from_home(settings: Settings, store: Store, ports: Ports) -> list[str]:
    """Why a human session on the account's profile must not start here (empty: it may).

    The gates of invariant 2 that are about *where* the machine is — the pinned home network
    (router, no VPN route, no proxy, the pinned ASNs), the pinned timezone and AC power — for
    ``fbcollector login``, the owner's own session (``selftest`` has ``selftest_gates``).
    """
    probes = ports.probes
    if probes is None:
        from fbcollector.gates import host_probes  # noqa: PLC0415

        probes = host_probes(ports.platform, ports.runner)
    lookup = ports.lookup
    if lookup is None:
        from fbcollector.gates import IpifyLookup  # noqa: PLC0415

        lookup = IpifyLookup()

    async def pin() -> HomePin | None:
        return await load_pin(store)

    async def pin_zone() -> str | None:
        pinned = await load_pin(store)
        return None if pinned is None else pinned.timezone

    gates = GateSet(
        [
            HomeGuard(probes, lookup, pin, ports.clock),
            TimezoneGate(probes.os_timezone, settings.timezone, home_zone=pin_zone),
            PowerGate(probes.power, ports.clock),
        ]
    )
    report = await gates.evaluate(GateContext(Activity.LAUNCH), stop_early=False)
    return list(report.reasons)


SELFTEST_EXEMPT: Final = frozenset({"warmup", "rhythm", "halt"})
"""The gates ``fbcollector selftest`` does not ask (an explicit exception). The command is a
human's, run at a terminal: it comes before the login in the setup order (no ``login_at`` yet: the
warm-up gate says ``NEEDS_LOGIN``), a Linux Chrome upgrade runs it in quiet hours, and a
``SELFTEST`` halt asks for it. It never navigates off 127.0.0.1. Every other gate holds for it: the
sleep, the crash-loop cap (its launch counted as pending, a planned one: a human's command), the
timezone, the power, the home network, the profile lock, an open block or a back-off, and the
owner's or the brain's pause and the lease."""


def selftest_key(major: int) -> str:
    """The kv key that says a Chrome major passed the local self-test."""
    return f"{KV_SELFTEST_PREFIX}{major}"


async def selftest_gates(
    settings: Settings, store: Store, ports: Ports, *, lock: ProfileLock
) -> GateSet:
    """The launch door of ``fbcollector selftest``: ``production_gates`` without the exempt ones.

    Built from the production list, so a gate added there holds for the self-test too unless it
    is exempted here by name. ``lock`` is the profile lock the command holds.
    """
    clock = ports.clock
    probes = ports.probes
    if probes is None:
        from fbcollector.gates import host_probes  # noqa: PLC0415

        probes = host_probes(ports.platform, ports.runner)
    lookup = ports.lookup
    if lookup is None:
        from fbcollector.gates import IpifyLookup  # noqa: PLC0415

        lookup = IpifyLookup()
    jitter = Jitter(await load_seed(store))
    rhythm = Rhythm(RhythmConfig.from_settings(settings), jitter)
    ladder = Ladder(
        store,
        clock,
        cautious_days_after_warning=settings.cautious_days_after_warning,
        cautious_days_after_logout=settings.cautious_days_after_logout,
        strike_decay_days=settings.strike_decay_days,
    )
    control = ControlCenter(
        store,
        ladder,
        clock,
        collector_id=settings.collector_id,
        local_mode=settings.local_mode,
        rng=ports.rng,
    )
    await control.load()

    async def rhythm_gate(context: GateContext) -> GateResult:  # noqa: ARG001 - one rule
        return rhythm_result(rhythm, clock.time())

    gates = production_gates(
        settings=settings,
        store=store,
        clock=clock,
        probes=probes,
        lookup=lookup,
        jitter=jitter,
        lock=lock,
        ladder=ladder,
        control=control,
        rhythm=rhythm_gate,
    )
    return GateSet([gate for gate in gates if gate.name not in SELFTEST_EXEMPT])


async def _login_session(
    store: Store, layout: Layout, ports: Ports, *, out: TextIO, record_login: bool
) -> int:
    factory = ports.driver_factory or default_driver_factory(layout, ports)
    driver = factory(FacebookSurfaces())
    # The owner's own session: a launch a human asked for is planned.
    await store.record_process_event(ProcessKind.CHROME_LAUNCH, "login", planned=True)
    info = await driver.launch(mode=SessionMode.LOGIN)
    try:
        before = await driver.cookie_names()
        last = before
        await store.kv_set(KV_CMDLINE, info.command_line)
        out.write("Chrome is open. Log in by hand, then quit Chrome.\n")
        out.flush()
        while driver.running:
            waiter = asyncio.ensure_future(driver.wait_closed())
            done, _ = await asyncio.wait({waiter}, timeout=LOGIN_POLL)
            if waiter in done:
                break
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
            with contextlib.suppress(Exception):
                last = await driver.cookie_names()
    finally:
        await driver.close()
    fresh = not before >= SESSION_COOKIES and last >= SESSION_COOKIES
    halt = await get_halt(store)
    if halt is not None and halt.get("kind") in {Halt.NEEDS_LOGIN, Halt.COMMAND_LINE}:
        await store.kv_delete(KV_HALT)
    if fresh or record_login:
        now = ports.clock.time()
        await store.kv_set(KV_LOGIN_AT, repr(now))
        out.write("Login recorded: warm-up (24-72 h) and the 14-day cautious ramp start now.\n")
    elif last >= SESSION_COOKIES:
        out.write("The profile was already logged in; login_at is unchanged.\n")
    else:
        out.write("No Facebook session found in the profile; nothing recorded.\n")
    return 0


def fmt_time(epoch: float | None, zone: dt.tzinfo) -> str:
    if epoch is None:
        return "-"
    return dt.datetime.fromtimestamp(epoch, zone).strftime("%Y-%m-%d %H:%M")


async def status_lines(settings: Settings, store: Store, clock: Clock) -> list[str]:
    """What ``fbcollector status`` prints: read from the database, no Chrome, no network."""
    zone = settings.zone
    lines: list[str] = []
    status = await store.kv_get_json(KV_STATUS) or {}
    lines.append(
        f"state: {status.get('state', 'unknown')} ({'; '.join(status.get('reasons', []))})"
    )
    block = await store.current_block()
    if block is not None:
        lines.append(
            f"block #{block.id} rung {block.rung} ({block.state.value}): {block.reason}; "
            f"earliest resume {fmt_time(block.until, zone)}"
        )
    halt = await get_halt(store)
    if halt is not None:
        lines.append(f"halted: {halt.get('kind')}: {halt.get('reason')}")
    broken = await store.kv_get(KV_PARSER_BROKEN)
    if broken is not None:
        lines.append(f"parser broken: {broken}")
    login_raw = await store.kv_get(KV_LOGIN_AT)
    lines.append(f"login: {fmt_time(float(login_raw), zone) if login_raw else 'never'}")
    control = await store.kv_get_json("control") or {}
    if control.get("local_paused"):
        lines.append("paused by the owner (fbcollector pause --off to resume reading)")
    if not settings.local_mode:
        holder = control.get("lease_holder") or "nobody"
        mine = control.get("lease_granted") and holder == settings.collector_id
        lease = "held" if mine else f"not held (holder: {holder})"
        polled = fmt_time(control.get("polled_at"), zone)
        lines.append(f"brain: lease {lease}; last answer {polled}")
    if await store.kv_get("feed_not_chrono") == "1":
        lines.append("backstop-only mode: the Groups feed was not chronological")
    lines.append(f"outbox: {await store.outbox_depth()} items")
    quarantined = await store.list_quarantined()
    if quarantined:
        lines.append(
            f"quarantine: {len(quarantined)} post(s) whose item does not fit the wire model "
            "(not seen: retried when seen again, and by `fbcollector replay`)"
        )
        lines += [
            f"  {q.group_id}/{q.post_id} x{q.attempts} {fmt_time(q.last_at, zone)}: "
            + ", ".join(q.errors)
            for q in quarantined[:STATUS_QUARANTINE]
        ]
        if len(quarantined) > STATUS_QUARANTINE:
            lines.append(f"  ... and {len(quarantined) - STATUS_QUARANTINE} more")
    groups = await store.list_groups()
    monitored = [g for g in groups if g.monitored]
    lines.append(f"groups: {len(groups)} known, {len(monitored)} monitored")
    for group in monitored:
        flags = ",".join(sorted(group.flags)) or "-"
        lines.append(f"  {group.id} {group.name or ''} [{group.state.value}] flags={flags}")
    confirmations = await load_confirmations(store)
    if confirmations:
        lines.append("membership confirmed by hand (monitored.toml):")
        lines += [describe_confirmation(g, c, zone) for g, c in sorted(confirmations.items())]
    versions = await store.kv_get_json(KV_VERSIONS) or {}
    if versions:
        lines.append(f"versions: {json.dumps(versions, sort_keys=True)}")
    del clock
    return lines


def parse_since(text: str, now: float) -> float:
    """``6h``, ``2d``, ``30m`` or an ISO date/time (UTC when no offset)."""
    units = {"m": 60, "h": 3600, "d": 86400}
    if text and text[-1] in units and text[:-1].isdigit():
        return now - int(text[:-1]) * units[text[-1]]
    moment = dt.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.timestamp()


def require_tty(stream: Any) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(callable(isatty) and isatty())
