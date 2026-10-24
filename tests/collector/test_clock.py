from __future__ import annotations

import time

import pytest

from fbcollector.clock import SUSPEND_THRESHOLD, Clock, FakeClock, Mark, SystemClock


class TestFakeClock:
    def test_advance_moves_both_clocks(self) -> None:
        clock = FakeClock(start=100.0, mono_start=5.0)
        clock.advance(10)
        assert (clock.time(), clock.monotonic()) == (110.0, 15.0)

    def test_suspend_moves_wall_time_only(self) -> None:
        clock = FakeClock(start=100.0, mono_start=5.0)
        clock.suspend(3600)
        assert (clock.time(), clock.monotonic()) == (3700.0, 5.0)

    def test_wall_can_jump_either_way(self) -> None:
        clock = FakeClock(start=100.0)
        clock.jump_wall(-50)
        assert clock.time() == 50.0
        clock.jump_wall(20)
        assert clock.time() == 70.0

    def test_time_never_runs_backwards_by_accident(self) -> None:
        clock = FakeClock()
        with pytest.raises(ValueError, match="backwards"):
            clock.advance(-1)
        with pytest.raises(ValueError, match="negative"):
            clock.suspend(-1)

    async def test_sleep_is_virtual_and_recorded(self) -> None:
        clock = FakeClock(start=0.0, mono_start=0.0)
        await clock.sleep(45)
        await clock.sleep(-3)  # asyncio.sleep treats negatives as 0; so does the fake
        assert clock.sleeps == [45, -3]
        assert (clock.time(), clock.monotonic()) == (45.0, 45.0)

    def test_satisfies_the_protocol(self) -> None:
        clocks: list[Clock] = [FakeClock(), SystemClock()]
        assert len(clocks) == 2


class TestSystemClock:
    def test_reads_the_real_clocks(self) -> None:
        clock = SystemClock()
        assert abs(clock.time() - time.time()) < 5
        first = clock.monotonic()
        assert clock.monotonic() >= first

    async def test_sleep_zero_and_negative_return(self) -> None:
        clock = SystemClock()
        await clock.sleep(0)
        await clock.sleep(-1)


class TestSuspendDetection:
    def test_normal_time_does_not_diverge(self) -> None:
        clock = FakeClock()
        before = Mark.now(clock)
        clock.advance(600)
        assert before.divergence(Mark.now(clock)) == 0
        assert not before.slept_before(Mark.now(clock))

    def test_suspend_is_detected_past_the_threshold(self) -> None:
        clock = FakeClock()
        before = Mark.now(clock)
        clock.advance(5)
        clock.suspend(SUSPEND_THRESHOLD)
        assert not before.slept_before(Mark.now(clock))  # exactly 30 s: not yet
        clock.suspend(1)
        assert before.slept_before(Mark.now(clock))
        assert before.divergence(Mark.now(clock)) == SUSPEND_THRESHOLD + 1

    def test_a_backwards_step_also_counts(self) -> None:
        clock = FakeClock()
        before = Mark.now(clock)
        clock.jump_wall(-120)
        assert before.slept_before(Mark.now(clock))

    def test_custom_threshold(self) -> None:
        clock = FakeClock()
        before = Mark.now(clock)
        clock.suspend(10)
        assert before.slept_before(Mark.now(clock), threshold=5)
