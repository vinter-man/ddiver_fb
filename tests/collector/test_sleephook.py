"""The OS sleep hook, its bridge to the read loop and the sleep gate — with fakes, never a sleep.

The hook threads are real threads (the notice is a cross-thread latch), but the OS side is a
fake: a scripted ``PowerNative`` for macOS and a scripted ``LoginBus`` for Linux, plus the real
``jeepney`` message builders against a fake connection. The one exception is a macOS-only smoke
test that registers with IOKit and unregisters again: registering sleeps nothing.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from jeepney import MessageType, new_error, new_method_return, new_signal
from jeepney.wrappers import DBusErrorResponse

from fbcollector.clock import FakeClock
from fbcollector.gates import Activity, GateContext, SleepGate
from fbcollector.sleephook import (
    GRACE_MARGIN,
    IO_CAN_SYSTEM_SLEEP,
    IO_SYSTEM_HAS_POWERED_ON,
    IO_SYSTEM_WILL_NOT_SLEEP,
    IO_SYSTEM_WILL_SLEEP,
    LINUX_GRACE_DEFAULT,
    LINUX_GRACE_MAX,
    PENDING_MAX,
    JeepneyLoginBus,
    LogindSleepHook,
    MacSleepHook,
    NoSleepHook,
    SleepNotice,
    platform_sleep_hook,
)

READ = GateContext(Activity.NAVIGATE)


def pending(notice: SleepNotice) -> bool:
    """``notice.pending`` read through a call: the value changes behind mypy's narrowing."""
    return notice.pending


def eventually(check: Callable[[], bool], timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.005)


async def eventually_async(check: Callable[[], bool], timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.005)


class Announcer:
    """The OS side: ``will_sleep`` from a thread of its own, as a hook calls it."""

    def __init__(self, notice: SleepNotice, grace: float = 5.0) -> None:
        self.result: bool | None = None
        self.thread = threading.Thread(target=self._run, args=(notice, grace), daemon=True)
        self.thread.start()
        eventually(lambda: notice.pending)

    def _run(self, notice: SleepNotice, grace: float) -> None:
        self.result = notice.will_sleep(grace)

    def join(self) -> bool | None:
        self.thread.join(5)
        return self.result


# -- the notice -------------------------------------------------------------------------------


async def test_an_announced_sleep_waits_for_the_loop_and_wakes_it() -> None:
    clock = FakeClock()
    notice = SleepNotice(clock)
    notice.bind(asyncio.get_running_loop())
    assert not pending(notice)
    waiter = asyncio.create_task(notice.wait_pending())
    await asyncio.sleep(0)
    announcer = Announcer(notice)
    await asyncio.wait_for(waiter, 5)  # the loop was woken
    assert pending(notice)
    assert announcer.thread.is_alive()  # still holding the sleep back
    notice.ready()
    assert announcer.join() is True
    assert not notice.take_wake()
    notice.did_wake()
    assert not pending(notice)
    assert notice.take_wake()
    assert not notice.take_wake()  # once per wake


def test_a_loop_that_does_not_answer_lets_the_sleep_go(caplog: pytest.LogCaptureFixture) -> None:
    notice = SleepNotice(FakeClock())  # not bound: nothing to wake, which must not fail
    with caplog.at_level(logging.WARNING):
        assert notice.will_sleep(0.01) is False
    assert "before Chrome could be closed" in caplog.text
    assert pending(notice)  # still: the wake has not come


def test_a_wake_without_an_announced_sleep_is_ignored() -> None:
    notice = SleepNotice(FakeClock())
    notice.did_wake()
    assert not notice.take_wake()


def test_a_pending_sleep_lapses_into_a_wake() -> None:
    clock = FakeClock()
    notice = SleepNotice(clock)
    Announcer(notice, grace=0.0).join()
    clock.advance(PENDING_MAX)
    assert pending(notice)
    clock.advance(1)
    assert not pending(notice)
    assert notice.take_wake()


def test_settle_forgets_the_pending_sleep_and_the_owed_wake() -> None:
    notice = SleepNotice(FakeClock())
    Announcer(notice, grace=0.0).join()
    notice.settle()
    assert not pending(notice)
    notice.did_wake()  # came after the divergence detector already saw the sleep
    assert not notice.take_wake()


async def test_a_closed_loop_is_not_poked() -> None:
    notice = SleepNotice(FakeClock())
    loop = asyncio.new_event_loop()
    loop.close()
    notice.bind(loop)
    assert notice.will_sleep(0.0) is False


# -- the gate ---------------------------------------------------------------------------------


async def test_the_gate_is_shut_from_the_announcement_to_the_pass_after_the_wake() -> None:
    clock = FakeClock()
    notice = SleepNotice(clock)
    forgotten: list[int] = []
    gate = SleepGate(clock, on_sleep=[lambda: forgotten.append(1)], notices=notice)
    assert (await gate.check(READ)).ok
    announcer = Announcer(notice, grace=0.0)
    closed = await gate.check(READ)
    assert not closed.ok
    assert closed.reason == "the OS announced a sleep"
    assert closed.retry_at is None  # needs the OS, not time
    assert forgotten == [1]
    announcer.join()
    notice.did_wake()
    woke = await gate.check(READ)
    assert not woke.ok
    assert woke.reason.startswith("woke from sleep")
    assert woke.retry_at == clock.time()
    assert forgotten == [1, 1]  # the home guard forgets: the next pass re-verifies the network
    assert (await gate.check(READ)).ok


async def test_the_divergence_still_catches_a_sleep_the_os_did_not_announce() -> None:
    clock = FakeClock()
    notice = SleepNotice(clock)
    gate = SleepGate(clock, notices=notice)
    Announcer(notice, grace=0.0).join()
    clock.suspend(3600)  # the lid, and a wake message that never comes
    slept = await gate.check(READ)
    assert not slept.ok
    assert "clocks diverged" in slept.reason
    assert not pending(notice)
    notice.did_wake()  # late: owed nothing
    assert (await gate.check(READ)).ok


# -- macOS ------------------------------------------------------------------------------------


@dataclass
class FakeNative:
    """``PowerNative`` whose run loop delivers scripted messages, one per ``run``."""

    messages: deque[tuple[int, int]] = field(default_factory=deque)
    allowed: list[int] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    callback: Callable[[int, int], None] | None = None
    fail: Exception | None = None

    def register(self, callback: Callable[[int, int], None]) -> object:
        if self.fail is not None:
            raise self.fail
        self.callback = callback
        self.events.append("register")
        return "handle"

    def allow(self, handle: object, argument: int) -> None:
        assert handle == "handle"
        self.allowed.append(argument)
        self.events.append(f"allow {argument}")

    def run(self, handle: object, seconds: float) -> None:
        assert handle == "handle"
        if self.messages and self.callback is not None:
            message, argument = self.messages.popleft()
            self.events.append(f"message {message:#x}")
            self.callback(message, argument)
        else:
            time.sleep(min(seconds, 0.005))

    def unregister(self, handle: object) -> None:
        self.events.append("unregister")


async def test_the_mac_hook_holds_the_sleep_until_chrome_is_closed() -> None:
    notice = SleepNotice(FakeClock())
    notice.bind(asyncio.get_running_loop())
    native = FakeNative(deque([(IO_CAN_SYSTEM_SLEEP, 1), (IO_SYSTEM_WILL_SLEEP, 2)]))
    hook = MacSleepHook(native, grace=5.0)
    hook.start(notice)
    hook.start(notice)  # idempotent
    try:
        await asyncio.wait_for(notice.wait_pending(), 5)
        await asyncio.sleep(0.05)
        assert native.allowed == [1]  # idle sleep is never vetoed; the real one waits
        notice.ready()
        await eventually_async(lambda: native.allowed == [1, 2])
        native.messages.extend([(IO_SYSTEM_WILL_NOT_SLEEP, 0), (IO_SYSTEM_HAS_POWERED_ON, 0)])
        await eventually_async(notice.take_wake)
    finally:
        await asyncio.to_thread(hook.stop)
    assert native.events[0] == "register"
    assert native.events[-1] == "unregister"
    hook.stop()  # twice is fine


async def test_the_mac_hook_lets_the_sleep_go_after_its_grace() -> None:
    notice = SleepNotice(FakeClock())
    native = FakeNative(deque([(IO_SYSTEM_WILL_SLEEP, 7)]))
    hook = MacSleepHook(native, grace=0.05)
    hook.start(notice)
    try:
        await eventually_async(lambda: native.allowed == [7])
    finally:
        await asyncio.to_thread(hook.stop)


def test_a_hook_that_cannot_register_leaves_the_detector(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hook = MacSleepHook(FakeNative(fail=OSError("no IOKit")))
    with caplog.at_level(logging.WARNING):
        hook.start(SleepNotice(FakeClock()))
        hook.stop()
    assert "macOS sleep hook stopped (OSError)" in caplog.text


class FakeLibrary:
    """A ctypes library whose functions record their calls (``restype``/``argtypes`` settable)."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        for name, answer in answers.items():
            setattr(self, name, self._function(name, answer))

    def _function(self, name: str, answer: Any) -> Any:
        def call(*args: Any) -> Any:
            self.calls.append((name, args))
            return answer

        return call


IOKIT_FUNCTIONS = {
    "IORegisterForSystemPower": 0,
    "IONotificationPortGetRunLoopSource": 11,
    "IOAllowPowerChange": 0,
    "IODeregisterForSystemPower": 0,
    "IOServiceClose": 0,
    "IONotificationPortDestroy": None,
}
CF_FUNCTIONS = {
    "CFRunLoopGetCurrent": 22,
    "CFRunLoopAddSource": None,
    "CFRunLoopRemoveSource": None,
    "CFRunLoopRunInMode": 3,
}


def test_iokit_calls_in_order_and_a_refused_registration() -> None:
    from fbcollector.sleephook import IOKitPower

    refused = IOKitPower(FakeLibrary(IOKIT_FUNCTIONS), FakeLibrary(CF_FUNCTIONS), mode=33)
    with pytest.raises(OSError, match="refused"):
        refused.register(lambda _m, _a: None)
    iokit = FakeLibrary({**IOKIT_FUNCTIONS, "IORegisterForSystemPower": 44})
    cf = FakeLibrary(CF_FUNCTIONS)
    native = IOKitPower(iokit, cf, mode=33)
    handle = native.register(lambda _m, _a: None)
    native.allow(handle, 9)
    native.run(handle, 0.5)
    native.unregister(handle)
    assert [name for name, _ in iokit.calls] == [
        "IORegisterForSystemPower",
        "IONotificationPortGetRunLoopSource",
        "IOAllowPowerChange",
        "IODeregisterForSystemPower",
        "IOServiceClose",
        "IONotificationPortDestroy",
    ]
    assert iokit.calls[2][1] == (44, 9)
    assert cf.calls[1] == ("CFRunLoopAddSource", (22, 11, 33))
    assert cf.calls[2] == ("CFRunLoopRunInMode", (33, 0.5, True))
    assert cf.calls[3] == ("CFRunLoopRemoveSource", (22, 11, 33))


@pytest.mark.skipif(sys.platform != "darwin", reason="IOKit is macOS only")
def test_iokit_registration_round_trip(caplog: pytest.LogCaptureFixture) -> None:
    """Registers for power messages on a thread's run loop and gives everything back."""
    from fbcollector.sleephook import IOKitPower

    got: list[tuple[int, int]] = []
    seen: dict[str, Any] = {}

    def body() -> None:
        native = IOKitPower()
        handle = native.register(lambda m, a: got.append((m, a)))
        seen["handle"] = handle
        native.run(handle, 0.05)
        # The C trampoline, called by hand: what IOKit would deliver after a wake.
        handle.callback(None, 0, IO_SYSTEM_HAS_POWERED_ON, None)  # type: ignore[attr-defined]
        native.unregister(handle)

    thread = threading.Thread(target=body)
    thread.start()
    thread.join(10)
    assert got == [(IO_SYSTEM_HAS_POWERED_ON, 0)]
    assert seen["handle"].root != 0

    def broken(_message: int, _argument: int) -> None:
        raise RuntimeError("bug")

    def raising() -> None:
        native = IOKitPower()
        handle = native.register(broken)
        with caplog.at_level(logging.ERROR):
            handle.callback(None, 0, IO_SYSTEM_HAS_POWERED_ON, None)  # type: ignore[attr-defined]
        native.unregister(handle)

    thread = threading.Thread(target=raising)
    thread.start()
    thread.join(10)
    assert "handler failed" in caplog.text


# -- Linux ------------------------------------------------------------------------------------


class FakeLock:
    def __init__(self, log: list[str], n: int) -> None:
        self.log = log
        self.n = n

    def close(self) -> None:
        self.log.append(f"release {self.n}")


@dataclass
class FakeBus:
    signals: deque[bool] = field(default_factory=deque)
    delay: float | None = 5.0
    log: list[str] = field(default_factory=list)
    locks: int = 0
    notice: SleepNotice | None = None

    def inhibit(self) -> FakeLock:
        self.locks += 1
        self.log.append(f"inhibit {self.locks}")
        return FakeLock(self.log, self.locks)

    def delay_max(self) -> float | None:
        return self.delay

    def next_signal(self, timeout: float) -> bool | None:
        if not self.signals:
            time.sleep(min(timeout, 0.005))
            return None
        start = self.signals.popleft()
        self.log.append(f"PrepareForSleep({start})")
        return start

    def close(self) -> None:
        self.log.append("close")


@pytest.mark.parametrize(
    ("delay", "grace"),
    [
        (5.0, 5.0 - GRACE_MARGIN),
        (None, LINUX_GRACE_DEFAULT - GRACE_MARGIN),
        (90.0, LINUX_GRACE_MAX - GRACE_MARGIN),
        (0.2, 0.0),
    ],
)
async def test_the_logind_hook_releases_its_lock_once_chrome_is_closed(
    delay: float | None, grace: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    notice = SleepNotice(FakeClock())
    notice.bind(asyncio.get_running_loop())
    bus = FakeBus(deque([True]), delay=delay)
    graces: list[float] = []
    real = notice.will_sleep

    def spy(value: float) -> bool:
        graces.append(value)
        return real(value)

    monkeypatch.setattr(notice, "will_sleep", spy)
    hook = LogindSleepHook(lambda: bus)
    hook.start(notice)
    try:
        await eventually_async(lambda: bool(graces))
        if grace > 0:
            await asyncio.wait_for(notice.wait_pending(), 5)
            assert "release 1" not in bus.log  # held while the loop closes Chrome
            notice.ready()
        await eventually_async(lambda: "release 1" in bus.log)
        bus.signals.append(False)
        await eventually_async(lambda: bus.locks == 2)  # a new lock for the next sleep
        assert notice.take_wake()
    finally:
        await asyncio.to_thread(hook.stop)
    assert graces == [pytest.approx(grace)]
    assert bus.log[-2:] == ["release 2", "close"]


async def test_the_logind_hook_tolerates_repeated_and_unmatched_signals() -> None:
    notice = SleepNotice(FakeClock())
    # A short logind delay: nobody answers here, so each sleep waits out its 0.1 s grace.
    bus = FakeBus(deque([False, True, True, False, True]), delay=0.6)
    hook = LogindSleepHook(lambda: bus)
    hook.start(notice)
    try:
        await eventually_async(lambda: not bus.signals and "release 2" in bus.log)
    finally:
        await asyncio.to_thread(hook.stop)
    assert bus.log == [
        "inhibit 1",
        "PrepareForSleep(False)",  # a wake without a sleep: the lock is kept
        "PrepareForSleep(True)",
        "release 1",
        "PrepareForSleep(True)",  # no lock left to release
        "PrepareForSleep(False)",
        "inhibit 2",
        "PrepareForSleep(True)",
        "release 2",
        "close",  # stopped with no lock held
    ]


def test_the_logind_hook_without_a_bus_leaves_the_detector(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def no_bus() -> FakeBus:
        raise ConnectionRefusedError

    hook = LogindSleepHook(no_bus)
    with caplog.at_level(logging.WARNING):
        hook.start(SleepNotice(FakeClock()))
        hook.stop()
    assert "logind sleep hook stopped" in caplog.text


def test_platform_hooks() -> None:
    assert isinstance(platform_sleep_hook("darwin"), MacSleepHook)
    assert isinstance(platform_sleep_hook("linux"), LogindSleepHook)
    other = platform_sleep_hook("win32")
    assert isinstance(other, NoSleepHook)
    other.start(SleepNotice(FakeClock()))
    other.stop()


# -- jeepney against a fake connection --------------------------------------------------------


class FakeFilter:
    def __init__(self, conn: FakeConnection, rule: Any, bufsize: int) -> None:
        self.conn = conn
        self.rule = rule
        self.bufsize = bufsize
        self.queue: deque[Any] = deque()

    def __enter__(self) -> deque[Any]:
        return self.queue

    def __exit__(self, *_exc: object) -> None:
        self.conn.log.append("filter off")


class FakeConnection:
    """The part of ``jeepney.io.blocking.DBusConnection`` the bus uses; logind answers."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.sent: list[Any] = []
        self.filters: list[FakeFilter] = []
        self.refuse: set[str] = set()
        self.delay_usec: int = 5_000_000

    def filter(self, rule: Any, *, bufsize: int = 1) -> FakeFilter:
        made = FakeFilter(self, rule, bufsize)
        self.filters.append(made)
        return made

    def send_and_get_reply(self, message: Any, *, timeout: float | None = None) -> Any:
        assert timeout is not None
        self.sent.append(message)
        member = message.header.fields.get(3)  # HeaderFields.member
        if member in self.refuse:
            return new_error(message, "org.freedesktop.DBus.Error.AccessDenied", "s", ("no",))
        if member == "Inhibit":
            return new_method_return(message, "h", (FakeFd(self.log),))
        if member == "Get":
            return new_method_return(message, "v", (("t", self.delay_usec),))
        return new_method_return(message)

    def recv_until_filtered(self, queue: deque[Any], *, timeout: float) -> Any:
        if not queue:
            raise TimeoutError
        return queue.popleft()

    def close(self) -> None:
        self.log.append("closed")


class FakeFd:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def close(self) -> None:
        self.log.append("fd closed")


def prepare_for_sleep(start: bool) -> Any:
    from jeepney import DBusAddress

    login1 = DBusAddress(
        "/org/freedesktop/login1",
        bus_name="org.freedesktop.login1",
        interface="org.freedesktop.login1.Manager",
    )
    return new_signal(login1, "PrepareForSleep", "b", (start,))


def test_the_jeepney_bus_speaks_logind() -> None:
    conn = FakeConnection()
    bus = JeepneyLoginBus(lambda: conn)
    rule = conn.filters[0].rule
    assert conn.filters[0].bufsize > 1  # a quick true/false pair must not overwrite itself
    assert rule.header_fields["member"] == "PrepareForSleep"
    assert rule.header_fields["sender"] == "org.freedesktop.login1"
    add_match = conn.sent[0]
    assert add_match.header.fields.get(3) == "AddMatch"
    assert "member='PrepareForSleep'" in add_match.body[0]

    lock = bus.inhibit()
    inhibit = conn.sent[-1]
    assert inhibit.header.fields.get(3) == "Inhibit"
    assert inhibit.body[0] == "sleep"
    assert inhibit.body[3] == "delay"
    lock.close()
    assert conn.log == ["fd closed"]

    assert bus.delay_max() == 5.0
    conn.refuse.add("Get")
    assert bus.delay_max() is None
    conn.refuse.add("Inhibit")
    with pytest.raises(DBusErrorResponse):
        bus.inhibit()

    assert bus.next_signal(0.01) is None
    conn.filters[0].queue.extend([prepare_for_sleep(True), prepare_for_sleep(False)])
    assert conn.filters[0].queue[0].header.message_type is MessageType.signal
    assert bus.next_signal(0.01) is True
    assert bus.next_signal(0.01) is False
    bus.close()
    assert conn.log[-2:] == ["filter off", "closed"]
