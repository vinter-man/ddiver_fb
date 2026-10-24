"""The Patchright driver: the only module that imports Patchright (invariant 1).

It does exactly what the invariant allows and nothing else: navigate the one tab to an
allow-listed URL (checked here, before anything is sent), move the mouse to a point that reacts
to nothing, send wheel events, read the response bodies the page itself requested, read the DOM
through the guarded CDP session, take a screenshot, read cookie *names*. It never clicks, types,
evaluates script, injects init scripts, routes requests or sets cookies; ``test_invariant1``
holds this file to a fixed list of calls on browser objects.

The driver never loops (DESIGN, Driver): each public method is one step, and the read loop runs
gates, budget, detectors and stop rules between them. Timings follow DESIGN — ``navigate`` waits
for load, dwells U(4, 10) s and polls for stories up to 12 s; ``scroll_step`` waits up to 8 s for
a *new* GraphQL request and up to 20 s for it to finish; ``park`` lets in-flight bodies land (up
to 20 s) before it goes to ``about:blank``; ``close`` is graceful, with SIGKILL of the browser
process only when Chrome does not exit in time (or the close is cancelled before it did), and
``running`` stays true until the close is over.

Every launch names its ``SessionMode``, and both modes start Chrome identically (one launcher,
D15). A **read** session is what the paragraphs above describe: one tab, captured; a tab or
popup anything else opens is closed unread, with a warning. A **login** session (``fbcollector
login``) is the owner's Chrome: the driver attaches no capture and opens no CDP session, takes
no step (``navigate``, ``scroll_step``, ``dwell`` and ``read_attribute`` raise, ``park`` does
nothing) and closes no tab or popup the owner or the page opens, because 2FA, "confirm it's
you" or a link opened in a new tab must survive. It only reads cookie names (the profile's
store, whichever tab set them) and waits for Chrome to exit. Either mode keeps just the one
start tab *at launch*, before anyone could have opened another.

Chrome runs with Patchright's signal handlers off: the collector's own shutdown order (stop
reads, park, close Chrome, flush, close the database) must not race a driver process that
closes the browser on SIGTERM by itself.

Chrome is always headed: ``headless=False`` is a literal below, not a setting. ``factory`` (the
Playwright entry point, ``async_playwright`` in production) is the tests' seam: the browser
tests wrap it to run the same launch headless on their local page, so they never open a window
on the owner's screen, and the production composition never passes it (``test_headed_only``).
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import logging
import os
import random
import signal
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from patchright.async_api import BrowserContext, Page, Playwright, async_playwright
from patchright.async_api import Error as PlaywrightError

from fbcollector.clock import Clock
from fbcollector.deadline import run_within
from fbcollector.driver.capture import CaptureBuffer
from fbcollector.driver.cdp import (
    GuardedCdp,
    count_nodes,
    point_is_quiet,
    read_attribute,
    read_dom,
    read_layout,
    screenshot_jpeg,
)
from fbcollector.driver.gestures import margin_candidates, margin_span, mouse_path, wheel_gesture
from fbcollector.driver.launch import (
    LaunchError,
    LaunchInfo,
    LaunchSpec,
    SessionMode,
    check_start_url,
    find_browser_process,
    installed_chrome_version,
    normalise_command,
    patchright_version,
    prepare_profile,
)
from fbcollector.driver.policy import UrlPolicy, require_allowed
from fbcollector.gates import clear_stale_singleton
from fbcollector.model import DomFacts, Step
from fbcollector.paths import Runner, run_command

log = logging.getLogger(__name__)

BLANK: Final = "about:blank"
FACEBOOK_COOKIE_URL: Final = "https://www.facebook.com/"
FEED_SELECTOR: Final = "[role=feed]"
RAIL_SELECTOR: Final = "[role=complementary]"
STORY_SELECTOR: Final = "[aria-posinset]"


@dataclass(frozen=True, slots=True)
class Timings:
    navigate_timeout: float = 45.0
    dwell: tuple[float, float] = (4.0, 10.0)
    story_poll_max: float = 12.0
    poll_interval: float = 1.0
    pagination_wait: float = 8.0
    pagination_finish: float = 20.0
    scroll_settle: float = 0.5
    park_wait: float = 20.0
    close_timeout: float = 20.0
    launch_timeout: float = 60.0


class PatchrightDriver:
    """One Chrome, one tab, passive capture (a read session; a login session leaves Chrome to
    the owner). Not reusable across processes; one per profile."""

    def __init__(
        self,
        spec: LaunchSpec,
        *,
        policy: UrlPolicy,
        clock: Clock,
        rng: random.Random | None = None,
        runner: Runner = run_command,
        timings: Timings | None = None,
        factory: Callable[[], Any] = async_playwright,
    ) -> None:
        self._spec = spec
        self._policy = policy
        self._clock = clock
        self._rng = rng or random.Random()
        self._runner = runner
        self._t = timings or Timings()
        self._factory = factory
        self._pw: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._cdp: GuardedCdp | None = None
        self._pid: int | None = None
        self._mode: SessionMode | None = None
        """The mode of the current launch; ``None`` before the first one and after a close, so
        a step after a login session's close says "not running", not the login's refusal."""
        self._closed = asyncio.Event()
        self._closing: asyncio.Event | None = None
        """While a close runs: the event its end sets (a second ``close`` waits on it)."""
        self._capture = CaptureBuffer(now=clock.monotonic)
        self._pointer: tuple[float, float] | None = None

    @property
    def running(self) -> bool:
        """Chrome is up — or a close of it has not finished yet.

        A close detaches the tab at once (every step fails from then on), but Chrome may take
        seconds to exit, or need the SIGKILL: until the close is over nothing may take it for
        a closed one (the sleep hook lets the OS sleep only when this is ``False``).
        """
        if self._closing is not None:
            return True
        return self._context is not None and not self._closed.is_set()

    @property
    def current_url(self) -> str | None:
        return self._page.url if self._page is not None else None

    @property
    def pid(self) -> int | None:
        """The Chrome browser process (for the crash tests and the resource samples)."""
        return self._pid

    @property
    def cdp_sent(self) -> tuple[str, ...]:
        """Every method sent on the guarded session so far (the protocol test reads it)."""
        return tuple(self._cdp.sent) if self._cdp is not None else ()

    # -- lifecycle -----------------------------------------------------------------------------

    async def launch(self, *, mode: SessionMode) -> LaunchInfo:
        if self._context is not None or self._closing is not None:
            msg = "Chrome is already running"
            raise LaunchError(msg)
        profile = self._spec.profile
        clear_stale_singleton(profile)
        for change in prepare_profile(profile):
            log.debug("profile hygiene: %s", change)
        self._closed = asyncio.Event()
        self._mode = mode
        manager = self._factory()
        playwright = await manager.start()
        self._pw = playwright
        try:
            return await self._start(playwright, mode)
        except BaseException:
            await self.close()
            raise

    async def _start(self, playwright: Playwright, mode: SessionMode) -> LaunchInfo:
        spec = self._spec
        chromium = playwright.chromium
        context = await chromium.launch_persistent_context(
            str(spec.profile),
            channel=spec.channel,
            headless=False,  # always: no headless reading (DESIGN); tests wrap ``factory``
            no_viewport=spec.no_viewport,
            chromium_sandbox=spec.chromium_sandbox,
            args=list(spec.args),
            ignore_default_args=list(spec.ignore_default_args),
            handle_sigint=False,
            handle_sigterm=False,
            handle_sighup=False,
            timeout=self._t.launch_timeout * 1000,
        )
        self._context = context
        context.on("close", self._on_context_close)
        pages = list(context.pages)
        page = pages[0] if pages else await context.new_page()
        for extra_page in pages[1:]:
            await extra_page.close()
        if mode is SessionMode.LOGIN:
            # The owner drives this Chrome: nothing is captured or read in any tab, and the
            # tabs and popups they (or the page) open stay open.
            context.on("page", self._on_owner_tab)
            self._page = page
        else:
            context.on("page", self._on_extra_page)
            page.on("request", self._capture.on_request)
            page.on("requestfinished", self._capture.on_request_done)
            page.on("requestfailed", self._capture.on_request_done)
            page.on("response", self._capture.on_response)
            self._page = page
            session = await context.new_cdp_session(page)
            self._cdp = GuardedCdp(session)
        process = await asyncio.to_thread(find_browser_process, spec.profile, runner=self._runner)
        if process is None:
            msg = "cannot find the Chrome process running on the profile"
            raise LaunchError(msg)
        self._pid = process.pid
        command = normalise_command(process.command, spec.profile)
        check_start_url(command)
        version = await asyncio.to_thread(installed_chrome_version, runner=self._runner)
        return LaunchInfo(command, process.pid, version, patchright_version())

    def _on_context_close(self, *_args: object) -> None:
        self._closed.set()

    async def _on_extra_page(self, extra_page: Page) -> None:
        # A read session has one tab only: anything the site opens on its own is closed, unread.
        if extra_page is not self._page:
            log.warning("closing a tab the page opened by itself")
            with contextlib.suppress(PlaywrightError):
                await extra_page.close()

    def _on_owner_tab(self, _tab: Page) -> None:
        # A login session: the owner opened it, or the page did on their click. It is theirs.
        log.info("a tab opened in the login session; it stays open")

    async def wait_closed(self) -> None:
        """Until Chrome exits (``fbcollector login``: the owner quits it by hand)."""
        await self._closed.wait()

    async def close(self) -> None:
        """Graceful ``context.close()``; SIGKILL of the browser process only when Chrome does
        not exit in time, or when this close is cancelled before it did.

        A close while another one runs (the sleep watcher's, under the read's step) waits for
        that one to end instead of returning at once. A cancelled close still kills Chrome,
        marks it closed and stops Playwright (bounded) before the cancellation goes on; only
        then is ``running`` false. Both waits stay bounded when Playwright's own driver process
        no longer answers (``run_within``): a hung read ends in this close.
        """
        underway = self._closing
        if underway is not None:
            await underway.wait()
            return
        done = self._closing = asyncio.Event()
        context, playwright, pid = self._context, self._pw, self._pid
        self._context = self._page = None
        self._cdp = None
        self._mode = None
        self._pw = None
        self._pointer = None
        self._capture.discard()
        try:
            await self._shut_down(context, playwright, pid, self._closed)
        finally:
            self._closing = None
            done.set()

    async def _shut_down(
        self,
        context: BrowserContext | None,
        playwright: Playwright | None,
        pid: int | None,
        closed: asyncio.Event,
    ) -> None:
        exited = context is None
        try:
            if context is not None:
                try:
                    await run_within(context.close(), self._t.close_timeout)
                    exited = True
                except (TimeoutError, PlaywrightError) as exc:
                    log.warning("Chrome did not close cleanly (%s)", type(exc).__name__)
        finally:
            # Also when the close is cancelled half-way: Chrome must not outlive it.
            if not exited and pid is not None:
                _kill(pid)
            self._pid = None
            closed.set()
            if playwright is not None:
                with contextlib.suppress(PlaywrightError, TimeoutError):
                    await run_within(playwright.stop(), self._t.close_timeout)

    # -- steps ---------------------------------------------------------------------------------

    def _require(self) -> tuple[Page, GuardedCdp]:
        if self._mode is SessionMode.LOGIN:
            msg = "a login session is the owner's: the driver takes no step in it"
            raise LaunchError(msg)
        if self._page is None or self._cdp is None or self._closed.is_set():
            msg = "Chrome is not running"
            raise LaunchError(msg)
        return self._page, self._cdp

    async def navigate(self, url: str) -> Step:
        require_allowed(self._policy, url)
        page, cdp = self._require()
        self._capture.discard()
        started = self._clock.monotonic()
        status: int | None = None
        document: bytes | None = None
        try:
            response = await page.goto(
                url, wait_until="load", timeout=self._t.navigate_timeout * 1000
            )
        except PlaywrightError as exc:
            # The message quotes the URL; the type is enough for the log.
            log.warning("navigation failed: %s", type(exc).__name__)
            response = None
        latency = int((self._clock.monotonic() - started) * 1000)
        if response is not None:
            status = response.status
            try:
                document = await response.body()
            except PlaywrightError as exc:
                log.warning("document body unavailable: %s", type(exc).__name__)
        await self._await_stories(cdp)
        await self._move_to_margin(page, cdp)
        return await self._step(page, cdp, status=status, document=document, latency=latency)

    async def _await_stories(self, cdp: GuardedCdp) -> None:
        loop = asyncio.get_running_loop()
        start = loop.time()
        await self._clock.sleep(self._rng.uniform(*self._t.dwell))
        while loop.time() - start < self._t.story_poll_max:
            if await self._safe_count(cdp, STORY_SELECTOR) > 0:
                return
            await self._clock.sleep(self._t.poll_interval)

    async def _safe_count(self, cdp: GuardedCdp, selector: str) -> int:
        try:
            return await count_nodes(cdp, selector)
        except PlaywrightError:
            return 0

    async def scroll_step(self) -> Step:
        page, cdp = self._require()
        started = self._clock.monotonic()
        before = self._capture.started
        if self._pointer is None:
            await self._move_to_margin(page, cdp)
        mouse = page.mouse
        for event in wheel_gesture(self._rng):
            await mouse.wheel(0, event.delta_y)
            await self._clock.sleep(event.pause_ms / 1000)
        fired = await self._capture.wait(
            lambda: self._capture.started > before, self._t.pagination_wait
        )
        if fired:
            await self._capture.settle(self._t.pagination_finish)
        await self._clock.sleep(self._t.scroll_settle)
        latency = int((self._clock.monotonic() - started) * 1000)
        return await self._step(page, cdp, pagination_fired=fired, latency=latency)

    async def dwell(self, seconds: float) -> Step:
        """The last look before ``park``: late dialogs, and the bodies still in flight."""
        page, cdp = self._require()
        await self._clock.sleep(max(0.0, seconds))
        await self._capture.settle(self._t.park_wait)
        return await self._step(page, cdp)

    async def park(self) -> None:
        if self._page is None or self._closed.is_set() or self._mode is SessionMode.LOGIN:
            return  # a login session's tabs are the owner's: none is parked
        page = self._page
        await self._capture.settle(self._t.park_wait)
        try:
            await page.goto(BLANK, timeout=self._t.navigate_timeout * 1000)
        except PlaywrightError as exc:
            log.warning("parking failed: %s", type(exc).__name__)
        self._capture.discard()

    async def _step(
        self,
        page: Page,
        cdp: GuardedCdp,
        *,
        status: int | None = None,
        document: bytes | None = None,
        pagination_fired: bool = False,
        latency: int | None = None,
    ) -> Step:
        try:
            dom = await read_dom(cdp)
        except PlaywrightError as exc:
            log.warning("DOM read failed: %s", type(exc).__name__)
            dom = DomFacts()
        bodies, lost = self._capture.take()
        return Step(
            final_url=page.url,
            status=status,
            document=document,
            graphql=bodies,
            dom=dom,
            lost_bodies=lost,
            pagination_fired=pagination_fired,
            latency_ms=latency,
        )

    # -- input ---------------------------------------------------------------------------------

    async def _move_to_margin(self, page: Page, cdp: GuardedCdp) -> bool:
        """Jump to one quiet margin point, then glide to another inside the same column."""
        try:
            layout = await read_layout(cdp, FEED_SELECTOR, RAIL_SELECTOR)
        except PlaywrightError as exc:
            log.debug("layout read failed: %s", type(exc).__name__)
            return False
        span = margin_span(layout)
        quiet = [p for p in margin_candidates(self._rng, layout) if await point_is_quiet(cdp, *p)]
        if span is None or len(quiet) < 2:
            log.info("no quiet margin on this page; the pointer stays put")
            return False
        entry, target = quiet[0], quiet[1]
        mouse = page.mouse
        await mouse.move(*entry, steps=1)
        for x, y in mouse_path(self._rng, entry, target, x_bounds=span):
            await mouse.move(x, y, steps=self._rng.randint(2, 5))
        self._pointer = target
        return True

    # -- reads that are not steps --------------------------------------------------------------

    async def cookie_names(self, url: str = FACEBOOK_COOKIE_URL) -> frozenset[str]:
        """Names of the cookies Chrome would send to ``url``; values never leave Chrome here.

        Raises ``LaunchError`` when Chrome is not running or is being closed: "no Chrome" is
        not "no cookies", which the detectors would read as a logout.
        """
        context = self._context
        if context is None or self._closed.is_set():
            msg = "Chrome is not running"
            raise LaunchError(msg)
        cookies = await context.cookies([url])
        return frozenset(str(c.get("name", "")) for c in cookies if c.get("name"))

    async def read_attribute(self, selector: str, name: str) -> str | None:
        _page, cdp = self._require()
        try:
            return await read_attribute(cdp, selector, name)
        except PlaywrightError as exc:
            log.debug("attribute read failed: %s", type(exc).__name__)
            return None

    async def screenshot(self, path: Path) -> Path | None:
        """A gzip'd JPEG of the tab, 0600 (invariant 8); ``None`` if it could not be taken."""
        if self._cdp is None:
            return None
        try:
            data = await screenshot_jpeg(self._cdp)
        except PlaywrightError as exc:
            log.warning("screenshot failed: %s", type(exc).__name__)
            return None
        await asyncio.to_thread(_write_private_gz, path, data)
        return path


def _write_private_gz(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb") as handle:
        handle.write(data)


def _kill(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)
        log.warning("killed Chrome (pid %d): it had not closed in time", pid)
