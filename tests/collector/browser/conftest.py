"""Browser tests: the real Patchright driver against ``LocalSite`` on 127.0.0.1, never Facebook.

They run where Google Chrome is installed and are skipped elsewhere (CI) or with
``FBC_SKIP_BROWSER=1``. Every driver here is built with ``LocalOnly(port)``: a facebook.com URL
raises before anything is sent. Each test launches Chrome for a few seconds on a throwaway
profile under ``tmp_path``.

**Headless by default.** The installed Google Chrome runs in its new headless mode (real
Chrome, no window, no focus taken), through the driver's test-only ``factory`` seam
(``headless.py``); the production launch spec cannot be headless (``test_headed_only``). The
few checks that are about the headed fingerprint itself — no ``HeadlessChrome`` in the user
agent, no ``--headless`` switch, window metrics a real screen gives — are marked ``headed`` and
run only with ``FBC_BROWSER_HEADED=1``, which also runs every other test headed. Run that where
a window disturbs nobody: inside the collector's own macOS user (RUNBOOK, section 11),
next to ``fbcollector selftest``.
"""

from __future__ import annotations

import os
import random
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from fbcollector.clock import SystemClock
from fbcollector.driver._patchright import PatchrightDriver, Timings
from fbcollector.driver.launch import LINUX_CHROME_BINARY, MAC_CHROME_BINARY, launch_spec
from fbcollector.driver.policy import LocalOnly
from fbcollector.driver.selftest import LocalSite

from .headless import HEADED_ENV, headed, playwright_factory

FAST = Timings(
    dwell=(0.2, 0.3),
    story_poll_max=1.0,
    poll_interval=0.2,
    pagination_wait=3.0,
    pagination_finish=5.0,
    scroll_settle=0.2,
    park_wait=8.0,
    close_timeout=10.0,
)


def chrome_installed() -> bool:
    binary = MAC_CHROME_BINARY if sys.platform == "darwin" else LINUX_CHROME_BINARY
    return binary.exists()


HEADED_ONLY = pytest.mark.skip(
    reason=f"headed-fingerprint check: runs only with {HEADED_ENV}=1, inside the collector's "
    "macOS user (RUNBOOK, section 11)"
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    skip = None
    if os.environ.get("FBC_SKIP_BROWSER"):
        skip = pytest.mark.skip(reason="FBC_SKIP_BROWSER is set")
    elif not chrome_installed():
        skip = pytest.mark.skip(reason="Google Chrome is not installed")
    here = Path(__file__).parent
    for item in items:
        if Path(str(item.fspath)).parent == here:
            item.add_marker(pytest.mark.browser)
            if skip is not None:
                item.add_marker(skip)
            elif item.get_closest_marker("headed") is not None and not headed():
                item.add_marker(HEADED_ONLY)


@pytest.fixture
def site() -> Iterator[LocalSite]:
    with LocalSite() as running:
        yield running


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    path = tmp_path / "profile"
    path.mkdir(mode=0o700)
    return path


DriverFactory = Callable[..., PatchrightDriver]


@pytest.fixture
def make_driver(profile: Path, site: LocalSite) -> DriverFactory:
    def build(
        *, seed: int = 1, timings: Timings = FAST, port: int | None = None
    ) -> PatchrightDriver:
        return PatchrightDriver(
            launch_spec(profile),
            policy=LocalOnly(port or site.port),
            clock=SystemClock(),
            rng=random.Random(seed),
            timings=timings,
            factory=playwright_factory(),
        )

    return build
