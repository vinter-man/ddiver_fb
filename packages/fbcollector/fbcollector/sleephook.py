"""Close Chrome before the machine sleeps, and trust nothing until it has woken and re-checked.

DESIGN (Gates, *Sleep*): a wall-clock vs monotonic divergence tells the collector *after* a
wake that it slept (``gates.SleepGate``); an OS hook should let it close Chrome *before* the
sleep, when it can. This module is that hook and the bridge from its thread to the read loop.

**The bridge (``SleepNotice``).** The OS hook runs in a thread of its own. When the OS announces
a sleep it calls ``will_sleep``, which marks a sleep *pending*, wakes the read loop and then
blocks — delaying the sleep — until the loop says Chrome is closed (``ready``) or the grace
runs out. While a sleep is pending the sleep gate stays closed, so nothing launches, navigates
or scrolls; the loop aborts a read in progress (``Reader.read(abort=…)``: nothing the tab shows
while Chrome is being torn down is judged) and closes Chrome. ``did_wake`` ends the pending
sleep; the gate then reports one closed pass and invalidates the home guard's verdict, so the
next pass re-verifies the network before anything else happens. A wake message that never
comes is covered twice: the divergence detector sees the sleep anyway, and a pending sleep
lapses after ``PENDING_MAX`` seconds of monotonic (awake) time, which counts as a wake too.

**macOS: IOKit, not NSWorkspace** (a departure from DESIGN rev 2). DESIGN named
``NSWorkspace``'s ``willSleep`` notification. That is an AppKit service whose notifications
come through the process's main run loop, and the collector's main thread runs asyncio; an
observer on a thread of its own could not be shown to receive them here without sleeping the
owner's Mac. The kernel's power-management messages — ``IORegisterForSystemPower``, Technical
Q&A QA1340 — are what command-line tools and daemons use: the notification port's source goes
on the run loop of the thread that registered (this hook's), the process is told
``kIOMessageSystemWillSleep`` and may delay the sleep for up to 30 s by acknowledging late
(``IOAllowPowerChange``), and ``kIOMessageSystemHasPoweredOn`` follows the wake. The messages
are per process, not per GUI session, so a collector in a *background* fast-user-switching
session registers and is told like any other process. That was not verified by a real sleep
(nothing here sleeps a machine); the divergence detector stays the backstop either way. The
calls go through ``ctypes`` (no new dependency); ``PowerNative`` is the seam the tests fake.

**Linux: a logind delay inhibitor** over the system bus (``jeepney``, pure Python, a Linux-only
dependency). The hook holds a ``delay`` lock on ``sleep``; on ``PrepareForSleep(true)`` it
waits for the loop as above, then releases the lock (logind sleeps at once when every delay
lock is gone, and anyway after ``InhibitDelayMaxSec``, 5 s by default — read from logind and
the grace kept under it); on ``PrepareForSleep(false)`` it reports the wake and takes a new
lock. ``LoginBus`` is the seam the tests fake. The laptop runs with sleep disabled (RUNBOOK),
so this is a backstop there too.

A hook that cannot start (no IOKit, no system bus, logind refusing the lock) logs a warning and
leaves the collector as it was: the divergence detector alone.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol

from fbcollector.clock import Clock

log = logging.getLogger(__name__)

PENDING_MAX: Final = 300.0
"""Seconds of awake time a pending sleep may last without a wake message (then it counts as
one: the OS may have cancelled the sleep, or its wake message was lost)."""
MAC_GRACE: Final = 25.0
"""How long the macOS hook delays a sleep for the loop (IOKit allows up to 30 s)."""
LINUX_GRACE_MAX: Final = 25.0
LINUX_GRACE_DEFAULT: Final = 5.0
"""Logind's own default ``InhibitDelayMaxSec``, used when it cannot be read."""
GRACE_MARGIN: Final = 0.5
POLL: Final = 0.5
"""How often the hook threads look for a stop request between OS messages (seconds)."""
JOIN_TIMEOUT: Final = 5.0

# IOKit/IOMessage.h: sys_iokit | sub_iokit_common | code.
IO_CAN_SYSTEM_SLEEP: Final = 0xE0000270
IO_SYSTEM_WILL_SLEEP: Final = 0xE0000280
IO_SYSTEM_WILL_NOT_SLEEP: Final = 0xE0000290
IO_SYSTEM_HAS_POWERED_ON: Final = 0xE0000300


class SleepNotice:
    """What the OS said about sleep, shared by the hook's thread and the read loop.

    Every method is safe from any thread; ``changed`` belongs to the loop ``bind`` was given
    and is only ever set there (``call_soon_threadsafe``).
    """

    def __init__(self, clock: Clock, *, pending_max: float = PENDING_MAX) -> None:
        self._clock = clock
        self._pending_max = pending_max
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._since: float | None = None
        self._woke = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self.changed = asyncio.Event()
        """Set (in the loop) whenever a sleep is announced or a wake reported."""

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _poke(self) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):  # the loop is shutting down
                loop.call_soon_threadsafe(self.changed.set)

    # -- the hook's side -----------------------------------------------------------------------

    def will_sleep(self, grace: float) -> bool:
        """The OS announced a sleep: hold it for up to ``grace`` seconds until Chrome is closed.

        Returns whether the loop said so in time (the hook lets the sleep go on either way).
        """
        with self._lock:
            self._since = self._clock.monotonic()
            self._woke = False
            self._ready.clear()
        self._poke()
        closed = self._ready.wait(max(0.0, grace))
        if not closed:
            log.warning("the machine sleeps before Chrome could be closed (%.0f s)", grace)
        return closed

    def did_wake(self) -> None:
        """The OS reports the wake that follows an announced sleep."""
        with self._lock:
            if self._since is None:
                return  # nothing pending (the divergence detector already saw this sleep)
            self._since = None
            self._woke = True
        self._poke()

    # -- the loop's side -----------------------------------------------------------------------

    @property
    def pending(self) -> bool:
        """A sleep was announced and no wake has followed yet."""
        with self._lock:
            since = self._since
            if since is not None and self._clock.monotonic() - since > self._pending_max:
                self._since = None
                self._woke = True
            return self._since is not None

    def take_wake(self) -> bool:
        """``True`` once after each wake (reported, or a pending sleep that lapsed)."""
        _ = self.pending  # a lapsed pending sleep turns into a wake here
        with self._lock:
            woke, self._woke = self._woke, False
            return woke

    def settle(self) -> None:
        """The divergence detector saw the sleep: nothing is pending any more, no wake owed."""
        with self._lock:
            self._since = None
            self._woke = False

    def ready(self) -> None:
        """Chrome is closed (or was not running): the OS may sleep now."""
        self._ready.set()

    async def wait_pending(self) -> None:
        """Until a sleep is pending (a sleep announced while waiting counts)."""
        while True:
            self.changed.clear()  # then look: an announcement after the look sets it again
            if self.pending:
                return
            await self.changed.wait()


class SleepHook(Protocol):
    def start(self, notice: SleepNotice) -> None:
        """Begin listening in a thread of its own; never raises (a failure is logged)."""
        ...

    def stop(self) -> None:
        """Stop listening and give every OS resource back (bounded)."""
        ...


class NoSleepHook:
    """No OS hook: the divergence detector alone (tests, and platforms without one)."""

    def start(self, notice: SleepNotice) -> None:
        del notice

    def stop(self) -> None:
        return


class _Threaded:
    """A hook body run in a daemon thread until ``stop``."""

    name = "sleep hook"

    def __init__(self) -> None:
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._notice: SleepNotice | None = None

    def start(self, notice: SleepNotice) -> None:
        if self._thread is not None:
            return
        self._notice = notice
        self._stopping.clear()
        self._thread = threading.Thread(target=self._guarded, name="fbc-sleep-hook", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(JOIN_TIMEOUT)

    def _guarded(self) -> None:
        notice = self._notice
        if notice is None:  # pragma: no cover - start() sets it before the thread starts
            return
        try:
            self._run(notice)
        except Exception as exc:  # noqa: BLE001 - the detector remains; the collector goes on
            log.warning(
                "the %s stopped (%s); sleeps are caught after the wake only",
                self.name,
                type(exc).__name__,
            )

    def _run(self, notice: SleepNotice) -> None:
        raise NotImplementedError


# -- macOS: IOKit power messages ---------------------------------------------------------------


class PowerNative(Protocol):
    """The IOKit and CoreFoundation calls the macOS hook makes (``IOKitPower`` in production)."""

    def register(self, callback: Callable[[int, int], None]) -> object:
        """Register for system power messages on this thread's run loop; returns a handle."""
        ...

    def allow(self, handle: object, argument: int) -> None:
        """``IOAllowPowerChange``: the sleep announced with ``argument`` may proceed."""
        ...

    def run(self, handle: object, seconds: float) -> None:
        """Run this thread's run loop for up to ``seconds`` (messages are delivered here)."""
        ...

    def unregister(self, handle: object) -> None: ...


class MacSleepHook(_Threaded):
    name = "macOS sleep hook"

    def __init__(self, native: PowerNative | None = None, *, grace: float = MAC_GRACE) -> None:
        super().__init__()
        self._native = native
        self._grace = grace

    def _run(self, notice: SleepNotice) -> None:
        native = self._native or IOKitPower()
        holder: list[object] = []

        def on_message(message: int, argument: int) -> None:
            if message == IO_SYSTEM_WILL_SLEEP:
                try:
                    notice.will_sleep(self._grace)
                finally:
                    native.allow(holder[0], argument)
            elif message == IO_CAN_SYSTEM_SLEEP:
                native.allow(holder[0], argument)  # never veto: gates keep us safe, not vetoes
            elif message == IO_SYSTEM_HAS_POWERED_ON:
                notice.did_wake()

        holder.append(native.register(on_message))
        log.info("listening for macOS sleep (IOKit power messages)")
        try:
            while not self._stopping.is_set():
                native.run(holder[0], POLL)
        finally:
            native.unregister(holder[0])


_IOKIT: Final = "/System/Library/Frameworks/IOKit.framework/IOKit"
_CORE_FOUNDATION: Final = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_DEFAULT_MODE: Final = "kCFRunLoopDefaultMode"
_CALLBACK = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p
)
"""``IOServiceInterestCallback``: refcon, service, message type, message argument."""


@dataclass
class _Registration:
    root: int
    port: ctypes.c_void_p
    notifier: ctypes.c_uint32
    loop: int
    source: int
    callback: Any
    """The ctypes trampoline: kept alive for as long as IOKit may call it."""


class IOKitPower:
    """``PowerNative`` over IOKit and CoreFoundation through ``ctypes`` (Technical Q&A QA1340)."""

    def __init__(self, iokit: Any = None, cf: Any = None, mode: int | None = None) -> None:
        """``iokit``, ``cf`` and ``mode`` stand in for the libraries in tests."""
        iokit = iokit if iokit is not None else ctypes.CDLL(_IOKIT)
        cf = cf if cf is not None else ctypes.CDLL(_CORE_FOUNDATION)
        iokit.IORegisterForSystemPower.restype = ctypes.c_uint32
        iokit.IORegisterForSystemPower.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            _CALLBACK,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        iokit.IONotificationPortGetRunLoopSource.restype = ctypes.c_void_p
        iokit.IONotificationPortGetRunLoopSource.argtypes = [ctypes.c_void_p]
        iokit.IOAllowPowerChange.restype = ctypes.c_int
        iokit.IOAllowPowerChange.argtypes = [ctypes.c_uint32, ctypes.c_long]
        iokit.IODeregisterForSystemPower.restype = ctypes.c_int
        iokit.IODeregisterForSystemPower.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
        iokit.IOServiceClose.restype = ctypes.c_int
        iokit.IOServiceClose.argtypes = [ctypes.c_uint32]
        iokit.IONotificationPortDestroy.restype = None
        iokit.IONotificationPortDestroy.argtypes = [ctypes.c_void_p]
        cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
        cf.CFRunLoopGetCurrent.argtypes = []
        cf.CFRunLoopAddSource.restype = None
        cf.CFRunLoopAddSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        cf.CFRunLoopRemoveSource.restype = None
        cf.CFRunLoopRemoveSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        cf.CFRunLoopRunInMode.restype = ctypes.c_int32
        cf.CFRunLoopRunInMode.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]
        self._iokit = iokit
        self._cf = cf
        self._mode = mode if mode is not None else ctypes.c_void_p.in_dll(cf, _DEFAULT_MODE).value

    def register(self, callback: Callable[[int, int], None]) -> object:
        def trampoline(_refcon: Any, _service: Any, message: int, argument: Any) -> None:
            try:
                callback(int(message), int(argument or 0))
            except Exception:  # an exception must not unwind into IOKit's C frames
                log.exception("the sleep hook's handler failed")

        port = ctypes.c_void_p()
        notifier = ctypes.c_uint32()
        callback_ref = _CALLBACK(trampoline)
        root = self._iokit.IORegisterForSystemPower(
            None, ctypes.byref(port), callback_ref, ctypes.byref(notifier)
        )
        if not root:
            msg = "IORegisterForSystemPower refused"
            raise OSError(msg)
        source = self._iokit.IONotificationPortGetRunLoopSource(port)
        loop = self._cf.CFRunLoopGetCurrent()
        self._cf.CFRunLoopAddSource(loop, source, self._mode)
        return _Registration(root, port, notifier, loop, source, callback_ref)

    def allow(self, handle: object, argument: int) -> None:
        assert isinstance(handle, _Registration)  # noqa: S101 - our own handle
        self._iokit.IOAllowPowerChange(handle.root, argument)

    def run(self, handle: object, seconds: float) -> None:
        del handle
        self._cf.CFRunLoopRunInMode(self._mode, seconds, True)  # returnAfterSourceHandled

    def unregister(self, handle: object) -> None:
        assert isinstance(handle, _Registration)  # noqa: S101 - our own handle
        self._cf.CFRunLoopRemoveSource(handle.loop, handle.source, self._mode)
        self._iokit.IODeregisterForSystemPower(ctypes.byref(handle.notifier))
        self._iokit.IOServiceClose(handle.root)
        self._iokit.IONotificationPortDestroy(handle.port)


# -- Linux: a logind delay inhibitor -----------------------------------------------------------


class Closeable(Protocol):
    def close(self) -> None: ...


class LoginBus(Protocol):
    """The logind calls the Linux hook makes (``JeepneyLoginBus`` in production)."""

    def inhibit(self) -> Closeable:
        """Take a ``delay`` inhibitor lock on ``sleep``; closing it releases the lock."""
        ...

    def delay_max(self) -> float | None:
        """Logind's ``InhibitDelayMaxSec`` (seconds), or ``None`` when it cannot be read."""
        ...

    def next_signal(self, timeout: float) -> bool | None:
        """The next ``PrepareForSleep`` value, or ``None`` when none came within ``timeout``."""
        ...

    def close(self) -> None: ...


class LogindSleepHook(_Threaded):
    name = "logind sleep hook"

    def __init__(self, bus: Callable[[], LoginBus] | None = None) -> None:
        super().__init__()
        self._bus = bus

    def _run(self, notice: SleepNotice) -> None:
        bus = (self._bus or JeepneyLoginBus)()
        lock: Closeable | None = None
        try:
            limit = bus.delay_max() or LINUX_GRACE_DEFAULT
            grace = max(0.0, min(limit, LINUX_GRACE_MAX) - GRACE_MARGIN)
            lock = bus.inhibit()
            log.info("listening for sleep through logind (delay up to %.1f s)", grace)
            while not self._stopping.is_set():
                start = bus.next_signal(POLL)
                if start is None:
                    continue
                if start:
                    notice.will_sleep(grace)
                    if lock is not None:
                        lock.close()  # the last delay lock gone: logind sleeps now
                        lock = None
                else:
                    notice.did_wake()
                    if lock is None:
                        lock = bus.inhibit()
        finally:
            if lock is not None:
                lock.close()
            bus.close()


LOGIN1_PATH: Final = "/org/freedesktop/login1"
LOGIN1_NAME: Final = "org.freedesktop.login1"
LOGIN1_MANAGER: Final = "org.freedesktop.login1.Manager"
BUS_TIMEOUT: Final = 5.0


class JeepneyLoginBus:
    """``LoginBus`` over a blocking ``jeepney`` connection to the system bus."""

    def __init__(self, connect: Callable[[], Any] | None = None) -> None:
        # jeepney is a Linux-only dependency of the collector (and a development one).
        from jeepney import DBusAddress, MatchRule, message_bus  # noqa: PLC0415
        from jeepney.io.blocking import open_dbus_connection  # noqa: PLC0415

        self._conn = (
            connect() if connect is not None else open_dbus_connection("SYSTEM", enable_fds=True)
        )
        self._login1 = DBusAddress(LOGIN1_PATH, bus_name=LOGIN1_NAME, interface=LOGIN1_MANAGER)
        rule = MatchRule(
            type="signal",
            sender=LOGIN1_NAME,
            interface=LOGIN1_MANAGER,
            member="PrepareForSleep",
            path=LOGIN1_PATH,
        )
        self._filter = self._conn.filter(rule, bufsize=16)
        self._queue = self._filter.__enter__()
        self._call(message_bus.AddMatch(rule))

    def _call(self, message: Any) -> Any:
        from jeepney.wrappers import unwrap_msg  # noqa: PLC0415

        return unwrap_msg(self._conn.send_and_get_reply(message, timeout=BUS_TIMEOUT))

    def inhibit(self) -> Closeable:
        from jeepney import new_method_call  # noqa: PLC0415

        why = "close the collector's Chrome before sleep"
        body = self._call(
            new_method_call(self._login1, "Inhibit", "ssss", ("sleep", "fbcollector", why, "delay"))
        )
        lock: Closeable = body[0]
        return lock

    def delay_max(self) -> float | None:
        from jeepney import Properties  # noqa: PLC0415

        try:
            body = self._call(Properties(self._login1).get("InhibitDelayMaxUSec"))
            _signature, value = body[0]
            return float(value) / 1_000_000
        except Exception:  # noqa: BLE001 - an old logind: use its documented default
            return None

    def next_signal(self, timeout: float) -> bool | None:
        try:
            message = self._conn.recv_until_filtered(self._queue, timeout=timeout)
        except TimeoutError:
            return None
        return bool(message.body[0])

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._filter.__exit__(None, None, None)
        self._conn.close()


def platform_sleep_hook(platform: str) -> SleepHook:
    """The production hook for ``platform`` (started by the running collector only)."""
    if platform == "darwin":
        return MacSleepHook()
    if platform.startswith("linux"):
        return LogindSleepHook()
    return NoSleepHook()
