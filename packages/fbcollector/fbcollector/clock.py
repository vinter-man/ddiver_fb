"""Time, injected.

Two clocks matter and they disagree on purpose. Wall time (UTC epoch seconds) is what budget
windows, quiet hours and post ages are measured in, and it survives restarts because it is what
the database stores. Monotonic time never jumps with NTP or the owner changing the clock, and on
both macOS and Linux it does not advance while the machine is suspended — so when wall time moved
much further than monotonic time between two steps, the machine slept, and every gate has to be
checked again before the next action (DESIGN, the sleep gate).

Everything that waits takes a ``Clock`` so tests run on a virtual one and never sleep for real.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Final, Protocol

SUSPEND_THRESHOLD: Final = 30.0
"""Seconds of wall-vs-monotonic divergence that mean the machine slept (DESIGN: > 30 s)."""


class Clock(Protocol):
    def time(self) -> float:
        """UTC epoch seconds."""
        ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


@dataclass(frozen=True, slots=True)
class Mark:
    """Both clocks read at one moment."""

    wall: float
    mono: float

    @classmethod
    def now(cls, clock: Clock) -> Mark:
        return cls(wall=clock.time(), mono=clock.monotonic())

    def divergence(self, later: Mark) -> float:
        """How far the two clocks drifted apart since this mark, in seconds."""
        return abs((later.wall - self.wall) - (later.mono - self.mono))

    def slept_before(self, later: Mark, threshold: float = SUSPEND_THRESHOLD) -> bool:
        return self.divergence(later) > threshold


class FakeClock:
    """A clock the test drives by hand.

    ``sleep`` advances both clocks instantly and records the request, so code under test that
    waits 45 s runs in microseconds. ``suspend`` moves wall time only, as a laptop lid does;
    ``jump_wall`` is an NTP step (either direction).
    """

    def __init__(self, start: float = 1_790_000_000.0, mono_start: float = 1_000.0) -> None:
        self._wall = start
        self._mono = mono_start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self._wall

    def monotonic(self) -> float:
        return self._mono

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(max(0.0, seconds))
        await asyncio.sleep(0)  # still a suspension point, as a real sleep is

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            msg = "time does not run backwards; use jump_wall for a clock step"
            raise ValueError(msg)
        self._wall += seconds
        self._mono += seconds

    def suspend(self, seconds: float) -> None:
        if seconds < 0:
            msg = "a suspend cannot be negative"
            raise ValueError(msg)
        self._wall += seconds

    def jump_wall(self, seconds: float) -> None:
        self._wall += seconds
