"""Fake Patchright objects for the driver's offline tests (no Chrome, no browser at all).

A ``FakeContext`` is a persistent context whose tabs the test opens and closes, standing in for
the owner or for the page (``open_tab``: Chrome announces the new tab with ``page``), whose
cookie jar is the profile's (``jar``: cookie names, shared by every tab), and which the owner can
quit (``quit``). ``FakePlaywright`` hands it out and records every launch's options.
``chrome_ps`` answers the driver's ``ps`` lookup for a stand-in Chrome process.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from fbcollector.paths import CommandResult

SESSION = frozenset({"c_user", "xs"})


class FakePage:
    """One tab: what listens to it, where it was sent, whether it was closed."""

    def __init__(self, url: str = "about:blank") -> None:
        self.url = url
        self.listening: list[str] = []
        self.gotos: list[str] = []
        self.closed = False

    def on(self, event: str, handler: Any) -> None:
        del handler
        self.listening.append(event)

    async def goto(self, url: str, **options: Any) -> None:
        del options
        self.gotos.append(url)
        self.url = url

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    """A persistent context whose ``close`` returns only once Chrome has exited (``exit``)."""

    def __init__(self, *, pages: int = 1, cookies: frozenset[str] = SESSION) -> None:
        self.pages = [FakePage() for _ in range(pages)]
        self.jar = set(cookies)
        """The profile's cookie names: whichever tab set a cookie, every tab sees it."""
        self.handlers: dict[str, Any] = {}
        self.close_started = asyncio.Event()
        self.exit = asyncio.Event()
        self.closes = 0
        self.cdp_sessions = 0

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    async def new_cdp_session(self, page: Any) -> Any:
        del page
        self.cdp_sessions += 1
        return object()

    async def cookies(self, urls: list[str]) -> list[dict[str, str]]:
        del urls
        return [{"name": name, "value": "x"} for name in sorted(self.jar)]

    async def open_tab(self, url: str = "about:blank") -> FakePage:
        """A tab or popup the owner or the page opens; the driver hears of it as ``page``."""
        tab = FakePage(url)
        self.pages.append(tab)
        handled = self.handlers["page"](tab)
        if inspect.isawaitable(handled):
            await handled
        return tab

    def open_tabs(self) -> list[FakePage]:
        return [page for page in self.pages if not page.closed]

    def quit(self) -> None:
        """The owner quits Chrome (Cmd+Q): it exits, and the context says so."""
        self.exit.set()
        self.handlers["close"]()

    async def close(self) -> None:
        self.closes += 1
        self.close_started.set()
        await self.exit.wait()
        self.handlers["close"]()


class FakePlaywright:
    def __init__(self, context: FakeContext, *, stop_hangs: bool = False) -> None:
        self.context = context
        self.chromium = self
        self.stop_hangs = stop_hangs
        self.stopped = 0
        self.launches: list[dict[str, Any]] = []

    async def launch_persistent_context(self, user_data_dir: str, **options: Any) -> Any:
        self.launches.append({"user_data_dir": user_data_dir, **options})
        return self.context

    async def start(self) -> FakePlaywright:
        return self

    async def stop(self) -> None:
        self.stopped += 1
        if self.stop_hangs:
            await asyncio.Event().wait()


def chrome_ps(profile: Path, pid: int) -> Callable[[Sequence[str]], CommandResult]:
    """A runner whose ``ps`` shows Chrome (``pid``) on ``profile``, started on about:blank."""
    command = f"{pid} /opt/chrome --no-proxy-server --user-data-dir={profile} about:blank\n"

    def runner(args: Sequence[str]) -> CommandResult:
        return CommandResult(0, command) if args[0] == "/bin/ps" else CommandResult(1, "")

    return runner
