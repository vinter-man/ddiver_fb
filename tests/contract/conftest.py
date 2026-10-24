"""Fixtures for the contract tests: one clock, the brain on 127.0.0.1, one collector."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from ddiver_core import i18n
from ddiver_fb import store as fb_store_module
from fbcollector.clock import FakeClock
from tests.contract.harness import (
    BUSY_MS,
    Brain,
    Collector,
    start_brain,
    start_collector,
    start_time,
    stop_collector,
)


@pytest.fixture(autouse=True)
def short_busy_timeout(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(fb_store_module, "BUSY_TIMEOUT_MS", BUSY_MS)
    yield
    i18n.clear_overlays()  # BrainApp.setup installs the fb overlay


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=start_time())


@pytest.fixture
async def brain(tmp_path: Path, clock: FakeClock) -> AsyncIterator[Brain]:
    started = await start_brain(tmp_path / "brain", clock)
    yield started
    await started.app.close()


@pytest.fixture
async def home(tmp_path: Path, clock: FakeClock, brain: Brain) -> AsyncIterator[Collector]:
    collector = await start_collector(tmp_path / "home", clock, brain)
    yield collector
    await stop_collector(collector)
