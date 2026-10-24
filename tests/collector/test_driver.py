"""The driver's pure parts, offline: allow-list, closing, session modes, launch spec, hygiene,
command line, CDP guard, DOM reads over a fake session, gestures and the capture buffer."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import signal
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from fbcollector.clock import FakeClock
from fbcollector.detectors import FEED_URL, JOINS_URL, group_url
from fbcollector.driver._patchright import PatchrightDriver, Timings
from fbcollector.driver.capture import CaptureBuffer, friendly_name, is_graphql
from fbcollector.driver.cdp import (
    ALLOWED,
    Box,
    CdpNotAllowedError,
    GuardedCdp,
    Layout,
    count_nodes,
    html_lang,
    html_text,
    is_quiet_node,
    joined_count,
    point_is_quiet,
    read_attribute,
    read_dom,
    read_layout,
    screenshot_jpeg,
)
from fbcollector.driver.gestures import (
    margin_candidates,
    margin_span,
    mouse_path,
    wheel_gesture,
)
from fbcollector.driver.launch import (
    MOCK_KEYCHAIN,
    PASSWORD_STORE_BASIC,
    CommandLineMismatchError,
    LaunchError,
    SessionMode,
    apply_preferences,
    check_start_url,
    compare_command,
    find_browser_process,
    installed_chrome_version,
    launch_spec,
    normalise_command,
    parse_version,
    prepare_profile,
    switches,
)
from fbcollector.driver.policy import (
    FacebookSurfaces,
    LocalOnly,
    UrlNotAllowedError,
    require_allowed,
)
from fbcollector.paths import CommandResult

from .driver_fakes import SESSION, FakeContext, FakePlaywright, chrome_ps

# -- URL allow-list ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        FEED_URL,
        JOINS_URL,
        group_url("1234567890"),
        group_url("1234567890", listings=True),
    ],
)
def test_facebook_surfaces_allow_the_three_surfaces(url: str) -> None:
    assert FacebookSurfaces().allows(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.facebook.com/",
        "https://www.facebook.com/?filter=groups&sk=h_chr&x=1",
        "https://www.facebook.com/groups/1234567890/",
        "https://www.facebook.com/groups/somegroup/?sorting_setting=CHRONOLOGICAL",
        "https://www.facebook.com/groups/1234567890/posts/1/",
        "https://www.facebook.com/groups/1234567890/?sorting_setting=RECENT_ACTIVITY",
        "https://www.facebook.com/marketplace/item/1/",
        "https://www.facebook.com/profile.php?id=1",
        "http://www.facebook.com/?filter=groups&sk=h_chr",
        "https://m.facebook.com/groups/joins/",
        "https://www.facebook.com.evil.example/groups/joins/",
        group_url("1234567890") + "\n",
        group_url("1234567890") + "#x",
        FEED_URL + "\n",
        "about:blank",
        "",
    ],
)
def test_facebook_surfaces_refuse_everything_else(url: str) -> None:
    assert not FacebookSurfaces().allows(url)


def test_local_only_admits_one_loopback_port() -> None:
    policy = LocalOnly(8765)
    assert policy.allows("http://127.0.0.1:8765/")
    assert policy.allows("http://127.0.0.1:8765/cookie?x=1")
    assert not policy.allows("http://127.0.0.1:8766/")
    assert not policy.allows("http://localhost:8765/")
    assert not policy.allows("https://127.0.0.1:8765/")
    assert not policy.allows("http://user:pw@127.0.0.1:8765/")
    assert not policy.allows("http://127.0.0.1:99999/")
    assert not policy.allows(FEED_URL)
    with pytest.raises(ValueError, match="port"):
        LocalOnly(0)


def test_require_allowed_does_not_quote_the_url() -> None:
    with pytest.raises(UrlNotAllowedError) as caught:
        require_allowed(FacebookSurfaces(), "https://www.facebook.com/profile.php?id=4")
    assert "profile" not in str(caught.value)


async def test_navigate_refuses_before_touching_the_browser(tmp_path: Path) -> None:
    driver = PatchrightDriver(
        launch_spec(tmp_path, platform="darwin"), policy=LocalOnly(8765), clock=FakeClock()
    )
    with pytest.raises(UrlNotAllowedError):
        await driver.navigate(FEED_URL)  # refused by the policy, not "Chrome is not running"
    with pytest.raises(LaunchError, match="not running"):
        await driver.navigate("http://127.0.0.1:8765/")
    assert not driver.running
    assert driver.cdp_sent == ()
    assert driver.current_url is None
    with pytest.raises(LaunchError, match="not running"):
        await driver.cookie_names()  # "no Chrome" is not "no cookies"
    assert await driver.screenshot(tmp_path / "x.gz") is None
    await driver.park()  # nothing to park
    await driver.close()  # idempotent without a launch


# -- closing: Chrome counts as running until it has exited -------------------------------------


async def launched(
    tmp_path: Path,
    pid: int,
    *,
    mode: SessionMode = SessionMode.READ,
    context: FakeContext | None = None,
    close_timeout: float = 5.0,
    stop_hangs: bool = False,
) -> tuple[PatchrightDriver, FakeContext, FakePlaywright]:
    profile = tmp_path / "profile"
    profile.mkdir(parents=True)
    context = context or FakeContext()
    playwright = FakePlaywright(context, stop_hangs=stop_hangs)
    driver = PatchrightDriver(
        launch_spec(profile, platform="linux"),
        policy=LocalOnly(8765),
        clock=FakeClock(),
        runner=chrome_ps(profile, pid),
        timings=Timings(close_timeout=close_timeout),
        factory=lambda: playwright,
    )
    info = await driver.launch(mode=mode)
    assert info.pid == pid
    return driver, context, playwright


def running(driver: PatchrightDriver) -> bool:
    """Read afresh each time (mypy would narrow ``driver.running`` across the awaits)."""
    return driver.running


async def test_a_second_close_waits_for_the_first_and_chrome_runs_until_it_exited(
    tmp_path: Path, chrome_pid: subprocess.Popen[bytes]
) -> None:
    driver, context, playwright = await launched(tmp_path, chrome_pid.pid)
    assert running(driver)
    assert await driver.cookie_names() == {"c_user", "xs"}
    first = asyncio.create_task(driver.close())
    await context.close_started.wait()
    assert running(driver)  # the tab is gone at once; Chrome is not, yet
    with pytest.raises(LaunchError, match="not running"):
        await driver.cookie_names()  # a closing Chrome has no cookies to show
    with pytest.raises(LaunchError, match="already running"):
        await driver.launch(mode=SessionMode.READ)
    second = asyncio.create_task(driver.close())
    await asyncio.sleep(0.01)
    assert not second.done()  # a second close waits, it does not return at once
    context.exit.set()
    await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert not driver.running
    assert (context.closes, playwright.stopped) == (1, 1)
    assert chrome_pid.poll() is None  # a clean exit needs no SIGKILL
    await asyncio.wait_for(driver.wait_closed(), 1)
    with pytest.raises(LaunchError, match="not running"):
        await driver.cookie_names()


async def test_a_cancelled_close_kills_chrome_and_still_tears_down(
    tmp_path: Path, chrome_pid: subprocess.Popen[bytes]
) -> None:
    driver, context, playwright = await launched(tmp_path, chrome_pid.pid)
    closing = asyncio.create_task(driver.close())
    await context.close_started.wait()
    waiting = asyncio.create_task(driver.close())
    await asyncio.sleep(0.01)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await asyncio.wait_for(waiting, 5)  # released, not cancelled with it
    assert chrome_pid.wait(timeout=5) == -signal.SIGKILL
    assert playwright.stopped == 1
    assert not driver.running
    await asyncio.wait_for(driver.wait_closed(), 1)


@pytest.mark.parametrize("stop_hangs", [False, True])
async def test_a_close_that_times_out_kills_chrome(
    tmp_path: Path, chrome_pid: subprocess.Popen[bytes], stop_hangs: bool
) -> None:
    driver, _context, playwright = await launched(
        tmp_path, chrome_pid.pid, close_timeout=0.05, stop_hangs=stop_hangs
    )
    await asyncio.wait_for(driver.close(), 5)  # a hung Playwright stop is bounded too
    assert chrome_pid.wait(timeout=5) == -signal.SIGKILL
    assert playwright.stopped == 1
    assert not driver.running


class WedgedContext(FakeContext):
    """Playwright's driver process no longer answers: a cancelled close waits for it anyway."""

    async def close(self) -> None:
        self.closes += 1
        self.close_started.set()
        while not self.exit.is_set():
            with contextlib.suppress(asyncio.CancelledError):
                await self.exit.wait()
        self.handlers["close"]()


async def test_a_close_is_bounded_when_playwright_no_longer_answers(
    tmp_path: Path, chrome_pid: subprocess.Popen[bytes]
) -> None:
    """``asyncio.wait_for`` would wait for the cancelled close to unwind, which never comes."""
    context = WedgedContext()
    driver, _context, playwright = await launched(
        tmp_path, chrome_pid.pid, context=context, close_timeout=0.05
    )
    await asyncio.wait_for(driver.close(), 5)
    assert chrome_pid.wait(timeout=5) == -signal.SIGKILL
    assert playwright.stopped == 1
    assert not driver.running
    context.exit.set()  # let the abandoned close end before the loop closes
    await asyncio.sleep(0.01)


# -- session modes: a read keeps one tab, a login leaves every tab to the owner --------------

DRIVER_LOG = "fbcollector.driver._patchright"
CLOSED_STRAY = "closing a tab the page opened by itself"
LEFT_OPEN = "a tab opened in the login session; it stays open"


async def test_both_modes_launch_the_same_chrome_and_keep_one_start_tab(
    tmp_path: Path, chrome_pid: subprocess.Popen[bytes]
) -> None:
    options: dict[SessionMode, dict[str, Any]] = {}
    for mode in SessionMode:
        stray = FakeContext(pages=2)  # a tab Chrome brought up on its own, before anyone acted
        driver, context, playwright = await launched(
            tmp_path / mode, chrome_pid.pid, mode=mode, context=stray
        )
        start, extra = context.pages
        assert extra.closed
        assert not start.closed
        (launch,) = playwright.launches
        options[mode] = {**launch, "user_data_dir": "<profile>"}
        context.exit.set()
        await driver.close()
    assert options[SessionMode.READ] == options[SessionMode.LOGIN]  # D15: one launcher
    assert options[SessionMode.LOGIN]["headless"] is False


async def test_a_read_session_closes_any_tab_the_page_opens(
    tmp_path: Path, chrome_pid: subprocess.Popen[bytes], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=DRIVER_LOG)
    driver, context, _playwright = await launched(tmp_path, chrome_pid.pid)
    (start,) = context.pages
    assert set(start.listening) == {"request", "requestfinished", "requestfailed", "response"}
    assert context.cdp_sessions == 1
    popup = await context.open_tab()
    await context.handlers["page"](start)  # the tab the read runs in is never taken for a stray
    assert popup.closed
    assert context.open_tabs() == [start]
    assert caplog.messages.count(CLOSED_STRAY) == 1
    assert LEFT_OPEN not in caplog.messages
    context.exit.set()
    await driver.close()


async def test_a_login_session_leaves_every_tab_and_popup_to_the_owner(
    tmp_path: Path, chrome_pid: subprocess.Popen[bytes], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=DRIVER_LOG)
    no_session = FakeContext(cookies=frozenset())
    driver, context, _playwright = await launched(
        tmp_path, chrome_pid.pid, mode=SessionMode.LOGIN, context=no_session
    )
    (start,) = context.pages
    assert start.listening == []  # nothing captured from the owner's browsing
    assert context.cdp_sessions == 0
    popup = await context.open_tab()  # the page's, on the owner's click: 2FA, "confirm it's you"
    own = await context.open_tab()  # the owner's own (Cmd+T)
    context.jar |= SESSION  # the login finished in the popup
    await start.close()  # the owner closes the tab Chrome started on
    assert context.open_tabs() == [popup, own]
    assert caplog.messages.count(LEFT_OPEN) == 2
    assert CLOSED_STRAY not in caplog.messages
    assert running(driver)
    assert await driver.cookie_names() >= SESSION  # the profile's cookies, not one tab's

    steps: list[Callable[[], Awaitable[object]]] = [
        lambda: driver.navigate("http://127.0.0.1:8765/"),
        driver.scroll_step,
        lambda: driver.dwell(1),
        lambda: driver.read_attribute("html", "lang"),
    ]
    for step in steps:
        with pytest.raises(LaunchError, match="login session"):
            await step()
    await driver.park()
    assert await driver.screenshot(tmp_path / "shot.jpg.gz") is None
    assert all(page.gotos == [] for page in context.pages)
    assert driver.cdp_sent == ()

    context.quit()
    await asyncio.wait_for(driver.wait_closed(), 1)
    assert not running(driver)
    await driver.close()
    assert not popup.closed
    assert not own.closed  # closed by Chrome's exit, never by the driver
    assert chrome_pid.poll() is None  # the owner's quit needs no SIGKILL


async def test_a_read_after_a_login_is_a_read_again(
    tmp_path: Path, chrome_pid: subprocess.Popen[bytes]
) -> None:
    driver, context, playwright = await launched(tmp_path, chrome_pid.pid, mode=SessionMode.LOGIN)
    context.quit()
    await driver.close()
    with pytest.raises(LaunchError, match="not running"):
        await driver.scroll_step()  # a closed login session is no Chrome, not the owner's one
    context.exit.clear()
    await driver.launch(mode=SessionMode.READ)
    try:
        assert len(playwright.launches) == 2
        popup = await context.open_tab()
        assert popup.closed  # the launch's own branch: the one-tab rule again
        with pytest.raises(UrlNotAllowedError):
            await driver.navigate(FEED_URL)  # the allow-list, which runs before any mode check
        # The steps read the mode the driver kept, and it is this launch's, not the login's
        # before it: a read parks its tab, where a login session would leave it alone.
        start = context.pages[0]
        assert start.gotos == []
        await driver.park()
        assert start.gotos == ["about:blank"]
    finally:
        context.exit.set()
        await driver.close()


# -- launch spec, hygiene, command line -------------------------------------------------------


def test_launch_spec_per_platform(tmp_path: Path) -> None:
    mac = launch_spec(tmp_path, platform="darwin")
    assert mac.args == ("--no-proxy-server",)
    assert mac.ignore_default_args == (MOCK_KEYCHAIN,)
    assert (mac.channel, mac.no_viewport, mac.chromium_sandbox) == ("chrome", True, True)
    linux = launch_spec(tmp_path, platform="linux")
    assert linux.args == ("--no-proxy-server", PASSWORD_STORE_BASIC)
    assert linux.ignore_default_args == ()
    with pytest.raises(LaunchError):
        launch_spec(tmp_path, platform="win32")


def test_prepare_profile_on_a_fresh_profile(tmp_path: Path) -> None:
    changes = prepare_profile(tmp_path)
    prefs = json.loads((tmp_path / "Default" / "Preferences").read_text())
    assert changes == ["rewrote Preferences"]
    assert prefs["session"]["restore_on_startup"] == 5
    assert prefs["profile"]["exit_type"] == "Normal"
    assert prefs["profile"]["exited_cleanly"] is True
    assert prefs["credentials_enable_service"] is False
    exceptions = prefs["profile"]["content_settings"]["exceptions"]
    for setting in ("notifications", "background_sync"):
        assert exceptions[setting]["https://www.facebook.com:443,*"]["setting"] == 2
        assert exceptions[setting]["[*.]facebook.com,*"]["setting"] == 2
    assert (tmp_path / "Default" / "Preferences").stat().st_mode & 0o777 == 0o600
    assert prepare_profile(tmp_path) == []  # idempotent


def test_prepare_profile_clears_sessions_and_keeps_other_settings(tmp_path: Path) -> None:
    default = tmp_path / "Default"
    (default / "Sessions" / "nested").mkdir(parents=True)
    (default / "Sessions" / "Session_1").write_text("x")
    (default / "Sessions" / "Tabs_1").write_text("x")
    (default / "Current Session").write_text("x")
    (default / "Last Tabs").write_text("x")
    prefs = {
        "session": {"restore_on_startup": 1, "startup_urls": ["https://example.com/"]},
        "profile": {"exit_type": "Crashed", "name": "Person 1", "content_settings": []},
        "intl": {"accept_languages": "en-US,en"},
    }
    (default / "Preferences").write_text(json.dumps(prefs))
    changes = prepare_profile(tmp_path)
    assert list((default / "Sessions").iterdir()) == []
    assert not (default / "Current Session").exists()
    assert "deleted Last Tabs" in changes
    after = json.loads((default / "Preferences").read_text())
    assert after["session"] == {"restore_on_startup": 5, "startup_urls": ["https://example.com/"]}
    assert after["profile"]["name"] == "Person 1"
    assert after["profile"]["exit_type"] == "Normal"
    assert after["intl"] == {"accept_languages": "en-US,en"}


@pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
def test_prepare_profile_refuses_unreadable_preferences(tmp_path: Path, content: str) -> None:
    (tmp_path / "Default").mkdir()
    (tmp_path / "Default" / "Preferences").write_text(content)
    with pytest.raises(LaunchError):
        prepare_profile(tmp_path)


def test_apply_preferences_overrides_a_hostile_shape() -> None:
    prefs: dict[str, Any] = {
        "session": "x",
        "profile": {
            "content_settings": {"exceptions": {"notifications": {"[*.]facebook.com,*": 1}}}
        },
    }
    assert apply_preferences(prefs)
    table = prefs["profile"]["content_settings"]["exceptions"]["notifications"]
    assert table["[*.]facebook.com,*"] == {"setting": 2}
    assert not apply_preferences(prefs)


COMMAND = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --disable-sync "
    "--no-proxy-server --user-data-dir=/Users/x/Library/Application Support/fbcollector/profile "
    "--remote-debugging-pipe about:blank"
)
PROFILE = Path("/Users/x/Library/Application Support/fbcollector/profile")


def ps_runner(*lines: str, code: int = 0) -> Any:
    def run(args: Any) -> CommandResult:
        assert args[0] == "/bin/ps"
        return CommandResult(code, "\n".join(lines))

    return run


def test_find_browser_process_skips_helpers_and_other_profiles() -> None:
    runner = ps_runner(
        "  11 /usr/sbin/cfprefsd agent",
        f"  12 {COMMAND.replace('--disable-sync', '--type=renderer --disable-sync')}",
        f"  13 {COMMAND.replace('profile ', 'profile2 ')}",
        f"  14 {COMMAND}",
    )
    found = find_browser_process(PROFILE, runner=runner)
    assert found is not None
    assert found.pid == 14
    assert find_browser_process(PROFILE, runner=ps_runner(code=1)) is None
    assert find_browser_process(PROFILE, runner=ps_runner("  x junk", "")) is None


def test_command_line_normalisation_and_comparison() -> None:
    normal = normalise_command(COMMAND, PROFILE)
    assert "--user-data-dir=<profile>" in normal
    assert switches(normal)[0] == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    assert switches(normal)[-1] == "--remote-debugging-pipe about:blank"
    compare_command(normal, normal)
    check_start_url(normal)
    changed = normal.replace("--disable-sync ", "--disable-sync --enable-automation ")
    with pytest.raises(CommandLineMismatchError) as caught:
        compare_command(normal, changed)
    assert caught.value.added == ("--enable-automation",)
    with pytest.raises(CommandLineMismatchError) as caught:
        compare_command(changed, normal)
    assert caught.value.removed == ("--enable-automation",)
    reordered = normal.replace(
        "--disable-sync --no-proxy-server", "--no-proxy-server --disable-sync"
    )
    with pytest.raises(CommandLineMismatchError, match="order"):
        compare_command(normal, reordered)
    with pytest.raises(LaunchError, match="about:blank"):
        check_start_url(normal.replace("about:blank", "https://www.facebook.com/"))
    assert switches("chrome") == ["chrome"]


def test_versions(tmp_path: Path) -> None:
    assert parse_version("Google Chrome 153.0.8010.53 ") is not None
    version = parse_version("Google Chrome 153.0.8010.53")
    assert version is not None
    assert (version.full, version.major, version.tested) == ("153.0.8010.53", 153, True)
    old = parse_version("140.0.1.2")
    assert old is not None
    assert not old.tested
    assert parse_version("no version") is None
    app = tmp_path / "Chrome.app"
    (app / "Contents").mkdir(parents=True)
    import plistlib

    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleShortVersionString": "153.0.8010.53"})
    )
    mac = installed_chrome_version(platform="darwin", app=app)
    assert mac is not None
    assert mac.major == 153
    assert installed_chrome_version(platform="darwin", app=tmp_path / "missing") is None

    def linux_runner(args: Any) -> CommandResult:
        return CommandResult(0, "Google Chrome 154.0.1.2\n")

    linux = installed_chrome_version(platform="linux", runner=linux_runner)
    assert linux is not None
    assert linux.major == 154
    failed = installed_chrome_version(platform="linux", runner=lambda _a: CommandResult(127))
    assert failed is None


# -- CDP guard and DOM reads ------------------------------------------------------------------


class FakeSession:
    """Answers the DOM domain for a tiny page."""

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.answers = answers or {}
        self.detached = False

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((method, params))
        answer = self.answers.get(method)
        if callable(answer):
            return answer(params or {})  # type: ignore[no-any-return]
        if isinstance(answer, Exception):
            raise answer
        return answer if answer is not None else {}

    async def detach(self) -> None:
        self.detached = True


def page_answers() -> dict[str, Any]:
    selectors = {
        "[aria-posinset]": [21, 22, 23],
        "[role=dialog]": [31],
        "input[name=pass]": [],
    }

    def select_all(params: dict[str, Any]) -> dict[str, Any]:
        return {"nodeIds": selectors.get(params["selector"], [41])}

    def outer(params: dict[str, Any]) -> dict[str, Any]:
        if params["nodeId"] == 31:
            return {"outerHTML": "<div role=dialog><p>You&#39;re going too fast</p></div>"}
        return {"outerHTML": "<h1>All groups you’ve joined (1,204)</h1>"}

    return {
        "DOM.getDocument": {
            "root": {
                "nodeId": 1,
                "children": [
                    {"nodeName": "html", "nodeType": 10},
                    {"nodeName": "HTML", "attributes": ["lang", "en", "dir", "ltr"]},
                ],
            }
        },
        "DOM.querySelectorAll": select_all,
        "DOM.getOuterHTML": outer,
        "DOM.performSearch": {"searchId": "s1", "resultCount": 2},
    }


async def test_guard_refuses_before_sending() -> None:
    session = FakeSession()
    guard = GuardedCdp(session)
    with pytest.raises(CdpNotAllowedError):
        await guard.send("Runtime.evaluate", {"expression": "1"})
    with pytest.raises(CdpNotAllowedError):
        await guard.send("Input.dispatchKeyEvent")
    assert session.calls == []
    await guard.send("Page.captureScreenshot", {"format": "jpeg"})
    assert guard.sent == ["Page.captureScreenshot"]
    assert session.calls == [("Page.captureScreenshot", {"format": "jpeg"})]
    await guard.detach()
    assert session.detached
    assert {"Network.enable", "Network.getResponseBody", "Page.captureScreenshot"} <= ALLOWED
    assert not {m for m in ALLOWED if m.startswith(("Runtime.", "Input.", "Fetch."))}


async def test_read_dom_over_the_dom_domain() -> None:
    session = FakeSession(page_answers())
    facts = await read_dom(GuardedCdp(session))
    assert facts.html_lang == "en"
    assert facts.posinset_count == 3
    assert facts.dialog_texts == ("You're going too fast",)
    assert facts.joined_header_count == 1204
    assert not facts.has_password_field
    assert facts.see_more == 2
    assert ("DOM.discardSearchResults", {"searchId": "s1"}) in session.calls
    assert {m for m, _ in session.calls} <= ALLOWED


async def test_small_reads() -> None:
    answers = page_answers()
    answers["DOM.querySelector"] = lambda p: {"nodeId": 7 if p["selector"] == "#x" else 0}
    answers["DOM.describeNode"] = {"node": {"attributes": ["data-ua", "Chrome/153"]}}
    guard = GuardedCdp(FakeSession(answers))
    assert await count_nodes(guard, "[aria-posinset]") == 3
    assert await read_attribute(guard, "#x", "data-ua") == "Chrome/153"
    assert await read_attribute(guard, "#missing", "data-ua") is None
    shot = GuardedCdp(FakeSession({"Page.captureScreenshot": {"data": "aGVsbG8="}}))
    assert await screenshot_jpeg(shot) == b"hello"


def test_text_helpers() -> None:
    assert html_text("<div>a <b>b</b><script>x()</script>&amp; c</div>") == "a b & c"
    assert joined_count(["nothing", "All groups you've joined (12)"]) == 12
    assert joined_count(["Groups you’ve joined ( 3 )"]) == 3
    assert joined_count(["joined"]) is None
    assert html_lang({"root": {"children": [{"nodeName": "HTML", "attributes": ["x"]}]}}) is None
    assert html_lang({}) is None


async def test_layout_and_hit_test() -> None:
    def query(params: dict[str, Any]) -> dict[str, Any]:
        return {"nodeId": {"[role=feed]": 5, "[role=complementary]": 6}.get(params["selector"], 0)}

    def box(params: dict[str, Any]) -> dict[str, Any]:
        quads = {5: [260, 0, 760, 0, 760, 900, 260, 900], 6: [880, 0, 1080, 0, 1080, 900, 880, 900]}
        return {"model": {"border": quads[params["nodeId"]]}}

    answers = {
        "Page.getLayoutMetrics": {"cssVisualViewport": {"clientWidth": 1088, "clientHeight": 600}},
        "DOM.getDocument": {"root": {"nodeId": 1}},
        "DOM.querySelector": query,
        "DOM.getBoxModel": box,
        "DOM.getNodeForLocation": {"backendNodeId": 9},
        "DOM.describeNode": {"node": {"nodeName": "DIV", "attributes": ["role", "main"]}},
    }
    guard = GuardedCdp(FakeSession(answers))
    layout = await read_layout(guard, "[role=feed]", "[role=complementary]")
    assert layout == Layout(1088, 600, Box(260, 0, 760, 900), Box(880, 0, 1080, 900))
    assert await point_is_quiet(guard, 800, 300)
    broken = GuardedCdp(FakeSession({"DOM.getNodeForLocation": RuntimeError("gone")}))
    assert not await point_is_quiet(broken, 1, 1)
    answers["DOM.querySelector"] = {"nodeId": 0}
    empty = await read_layout(GuardedCdp(FakeSession(answers)), "a", "b")
    assert empty.feed is None
    assert empty.rail is None


@pytest.mark.parametrize(
    ("node", "quiet"),
    [
        ({"nodeName": "DIV"}, True),
        ({"nodeName": "BODY", "attributes": ["class", "x"]}, True),
        ({"nodeName": "A", "attributes": ["href", "/x"]}, False),
        ({"nodeName": "IMG"}, False),
        ({"nodeName": "svg"}, False),
        ({"nodeName": "DIV", "attributes": ["role", "button"]}, False),
        ({"nodeName": "DIV", "attributes": ["role", "link"]}, False),
        ({"nodeName": "DIV", "attributes": ["tabindex", "0"]}, False),
        ({"nodeName": "SPAN", "attributes": ["aria-label", "Profile"]}, False),
    ],
)
def test_quiet_nodes(node: dict[str, Any], quiet: bool) -> None:
    assert is_quiet_node(node) is quiet


# -- gestures ---------------------------------------------------------------------------------


def test_wheel_gestures_have_human_sizes() -> None:
    rng = random.Random(3)
    back_ups = 0
    for _ in range(400):
        events = wheel_gesture(rng)
        forward = [e for e in events if e.delta_y > 0]
        backward = [e for e in events if e.delta_y < 0]
        assert 3 <= len(forward) <= 8
        assert len(backward) <= 2
        assert all(100 <= abs(e.delta_y) <= 120 for e in events)
        assert all(16 <= e.pause_ms <= 60 for e in events)
        assert events[: len(forward)] == forward  # back up only at the end
        back_ups += bool(backward)
    assert 20 < back_ups < 100  # about one in eight


def test_margin_between_feed_and_rail() -> None:
    layout = Layout(1088, 600, Box(260, 0, 760, 900), Box(880, 0, 1080, 900))
    assert margin_span(layout) == (768, 872)
    points = margin_candidates(random.Random(1), layout, count=50)
    assert all(768 <= x <= 872 and 180 <= y <= 420 for x, y in points)
    no_rail = Layout(1000, 600, Box(260, 0, 760, 900), None)
    assert margin_span(no_rail) == (768, 992)
    narrow = Layout(1000, 600, Box(260, 0, 760, 900), Box(770, 0, 990, 900))
    assert margin_span(narrow) is None
    assert margin_candidates(random.Random(1), narrow) == []
    no_feed = Layout(1000, 600, None, None)
    assert margin_span(no_feed) == (952, 992)
    assert margin_span(Layout(0, 0, None, None)) is None
    assert margin_span(Layout(20, 600, None, None)) is None
    # A window narrower than the feed column plus a margin: no span, never an inverted one
    # whose points would land on the feed (a headless 800x600 window showed it).
    cramped = Layout(756, 469, Box(260, 0, 760, 6096), Box(880, 0, 1080, 6096))
    assert margin_span(cramped) is None
    assert margin_candidates(random.Random(1), cramped) == []
    clipped = Layout(830, 600, Box(260, 0, 760, 900), Box(880, 0, 1080, 900))
    assert margin_span(clipped) == (768, 822)  # the window's edge, not the rail's, bounds it


def test_mouse_path_ends_at_the_target_and_stays_in_the_column() -> None:
    rng = random.Random(9)
    for _ in range(200):
        start = (rng.uniform(770, 870), rng.uniform(100, 500))
        end = (rng.uniform(770, 870), rng.uniform(100, 500))
        path = mouse_path(rng, start, end, x_bounds=(768, 872))
        assert 3 <= len(path) <= 6
        assert path[-1] == end
        assert all(768 <= x <= 872 for x, _ in path)
    assert mouse_path(random.Random(1), (5, 5), (5, 5))[-1] == (5, 5)


# -- capture ----------------------------------------------------------------------------------


class FakeRequest:
    def __init__(self, url: str, post_data: str | None = None, headers: Any = None) -> None:
        self.url = url
        self.post_data = post_data
        self.headers = headers or {}


class FakeResponse:
    def __init__(
        self, request: FakeRequest, body: bytes | Exception, status: int = 200, delay: float = 0
    ) -> None:
        self.url = request.url
        self.request = request
        self.status = status
        self._body = body
        self._delay = delay

    async def body(self) -> bytes:
        await asyncio.sleep(self._delay)
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


GQL = "https://www.facebook.com/api/graphql/"


def test_graphql_detection_and_friendly_names() -> None:
    assert is_graphql(GQL)
    assert is_graphql("http://127.0.0.1:1/api/graphql/")
    assert not is_graphql("https://www.facebook.com/api/graphqlbatch/")
    assert not is_graphql("http://[bad")
    form = "av=1&fb_api_req_friendly_name=CometNewsFeedPaginationQuery&variables=%7B%7D"
    assert friendly_name(FakeRequest(GQL, form)) == "CometNewsFeedPaginationQuery"
    header = FakeRequest(GQL, "x=1", {"X-FB-Friendly-Name": "GroupsCometFeedRegularStoriesQuery"})
    assert friendly_name(header) == "GroupsCometFeedRegularStoriesQuery"
    assert friendly_name(FakeRequest(GQL, "fb_api_req_friendly_name=<script>")) == "script"
    assert friendly_name(FakeRequest(GQL)) is None
    assert friendly_name(FakeRequest(GQL, "fb_api_req_friendly_name=%3C%3E")) is None


def state(buffer: CaptureBuffer) -> tuple[int, bool]:
    return buffer.started, buffer.busy


async def test_capture_buffer_tracks_bodies_and_in_flight() -> None:
    buffer = CaptureBuffer(now=lambda: 0.0, cap=10)
    other = FakeRequest("https://static.xx.fbcdn.net/x.js")
    buffer.on_request(other)
    buffer.on_response(FakeResponse(other, b"js"))
    assert state(buffer) == (0, False)

    first = FakeRequest(GQL, "fb_api_req_friendly_name=A")
    buffer.on_request(first)
    assert state(buffer) == (1, True)
    buffer.on_response(FakeResponse(first, b"0123456789ABCDEF", delay=0.01))
    lost = FakeRequest(GQL, "fb_api_req_friendly_name=B")
    buffer.on_request(lost)
    buffer.on_response(FakeResponse(lost, RuntimeError("No resource with given identifier")))
    buffer.on_request_done(first)
    buffer.on_request_done(lost)
    buffer.on_request_done(FakeRequest(GQL))  # unknown: ignored
    assert await buffer.settle(2.0)
    bodies, lost_count = buffer.take()
    assert lost_count == 1
    assert len(bodies) == 1
    assert bodies[0].friendly_name == "A"
    assert bodies[0].body == b"0123456789"
    assert bodies[0].capped
    assert bodies[0].status == 200
    assert buffer.take() == ((), 0)


async def test_capture_wait_times_out() -> None:
    buffer = CaptureBuffer(now=lambda: 0.0)
    buffer.on_request(FakeRequest(GQL))
    assert not await buffer.settle(0.05)
    assert await buffer.wait(lambda: True, 0.0)
    assert not await buffer.wait(lambda: False, -1.0)
    buffer.discard()
    assert not buffer.busy
