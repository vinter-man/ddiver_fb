"""Headless Chrome for the browser tests, through the driver's test-only ``factory`` seam.

The owner works on the machine the tests run on, so a test must never open a window on their
screen or take the keyboard focus. The browser tests therefore run Chrome in its (new) headless
mode by default: with ``channel="chrome"`` that is the installed Google Chrome itself, with its
full browser code, only without a window. What is real Chrome behaviour — capture of streamed
bodies, in-flight tracking, the DOM reads, input and user activation, session restore, cookies,
the CDP traffic — is the same there.

The production driver cannot be made headless: ``LaunchSpec`` has no such field and
``PatchrightDriver`` passes ``headless=False`` as a literal. The switch lives here instead, in
a wrapper around the Playwright entry point the driver takes as ``factory``: its
``launch_persistent_context`` turns the driver's ``headless=False`` into ``True`` and gives the
headless window a desktop size, so the page lays out as in a real window (the pointer and wheel
checks need the margin between the feed and the rail). ``tests/collector/test_headed_only.py``
proves the production composition never goes through a wrapper like this one.

``FBC_BROWSER_HEADED=1`` runs the same tests headed (the owner does that inside the collector's
own macOS user, where the window is invisible to their session), plus the checks that are about
the headed fingerprint itself (``@pytest.mark.headed``).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Final

from patchright.async_api import async_playwright

HEADED_ENV: Final = "FBC_BROWSER_HEADED"
WINDOW_SIZE: Final = (1280, 900)
HEADLESS_ARGS: Final = (f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}",)


def headed() -> bool:
    """``FBC_BROWSER_HEADED=1``: a real window (only where it disturbs nobody)."""
    return os.environ.get(HEADED_ENV, "").strip() == "1"


class _HeadlessChromium:
    def __init__(self, real: Any) -> None:
        self._real = real

    async def launch_persistent_context(self, user_data_dir: str, **options: Any) -> Any:
        if options.get("headless") is not False:  # pragma: no cover - the guard test's job
            msg = "the driver asked for something other than a headed launch"
            raise AssertionError(msg)
        options["headless"] = True
        options["args"] = [*options.get("args", ()), *HEADLESS_ARGS]
        return await self._real.launch_persistent_context(user_data_dir, **options)


class _HeadlessPlaywright:
    def __init__(self, real: Any) -> None:
        self._real = real
        self.chromium = _HeadlessChromium(real.chromium)

    async def stop(self) -> None:
        await self._real.stop()


class _HeadlessManager:
    def __init__(self) -> None:
        self._real = async_playwright()

    async def start(self) -> _HeadlessPlaywright:
        return _HeadlessPlaywright(await self._real.start())


def playwright_factory() -> Callable[[], Any]:
    """What the tests pass as the driver's ``factory``: headless unless asked for headed."""
    return async_playwright if headed() else _HeadlessManager
