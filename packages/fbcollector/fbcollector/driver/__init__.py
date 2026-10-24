"""The ``Driver`` seam: one browser, one tab, one step at a time.

The read loop owns every loop so that gates, the write-ahead budget, the detectors and the stop
rules run between steps (DESIGN, Driver). A driver only ever does one of these at a time:
start Chrome, open an allow-listed URL, send one wheel gesture, take a last look, park on
``about:blank``, close. ``fbcollector.driver._patchright`` is the real implementation and the
only module allowed to import Patchright; the tests use a fake with the same shape.

Every launch names its ``SessionMode``: ``READ`` for the collector's reads (one tab, the steps
above), ``LOGIN`` for ``fbcollector login``, where the owner drives Chrome and the driver only
waits for it to exit (no steps, and no tab or popup of theirs is closed).

``Step`` and its parts are the collector's vocabulary (``fbcollector.model``) and are re-exported
here, so a driver implementation needs nothing from the parser or the scheduler.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from fbcollector.driver.launch import LaunchInfo, SessionMode
from fbcollector.model import DomFacts, GraphqlBody, Step

__all__ = ["DomFacts", "Driver", "GraphqlBody", "LaunchInfo", "SessionMode", "Step"]


class Driver(Protocol):
    @property
    def running(self) -> bool:
        """Chrome is up, or a ``close`` of it has not finished yet (a closing Chrome is not a
        closed one: the OS may sleep only once this is ``False``)."""
        ...

    async def launch(self, *, mode: SessionMode) -> LaunchInfo:
        """Chrome on the profile, one ``about:blank`` page; nothing navigated.

        ``mode`` never changes how Chrome starts, only what the driver does with it afterwards:
        ``READ`` keeps one tab and closes any other; ``LOGIN`` leaves every tab to the owner and
        refuses the steps below.
        """
        ...

    async def navigate(self, url: str) -> Step:
        """One navigation to an allow-listed URL, captured until settled (``READ`` only)."""
        ...

    async def scroll_step(self) -> Step:
        """One human-sized wheel gesture, captured until pagination settles (or does not fire)."""
        ...

    async def dwell(self, seconds: float) -> Step:
        """Wait, then look once more (late dialogs) — the step the detectors see before park."""
        ...

    async def park(self) -> None:
        """Let in-flight bodies land, then ``about:blank`` (nothing in a ``LOGIN`` session)."""
        ...

    async def close(self) -> None:
        """Graceful close; SIGKILL only on timeout (or when the close is cancelled first). A
        close while another one runs waits for that one to finish."""
        ...

    async def cookie_names(self, url: str = ...) -> frozenset[str]:
        """Cookie names for ``url`` in the profile's cookie store, whichever tab set them; raises
        when Chrome is not running (or is closing)."""
        ...

    async def read_attribute(self, selector: str, name: str) -> str | None:
        """One attribute of the first element matching ``selector`` (a DOM read, no script)."""
        ...

    async def screenshot(self, path: Path) -> Path | None: ...

    async def wait_closed(self) -> None:
        """Until Chrome exits by itself (the owner quit it)."""
        ...
