"""The Patchright driver on a local page: fingerprint, capture, input, parking, relaunches.

Headless by default (``conftest``); the ``headed`` tests are the headed fingerprint itself and
run only with ``FBC_BROWSER_HEADED=1``.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from fbcollector.driver.cdp import CdpNotAllowedError
from fbcollector.driver.launch import MOCK_KEYCHAIN, PROFILE_TOKEN, SessionMode, compare_command
from fbcollector.driver.selftest import (
    FEED_RIGHT,
    LOAD_QUERY,
    PAGE_QUERY,
    RAIL_LEFT,
    LocalSite,
    probe,
    run_selftest,
    whole_stream,
)

from .conftest import DriverFactory
from .headless import headed


async def test_capture_dom_reads_and_input(make_driver: DriverFactory, site: LocalSite) -> None:
    driver = make_driver()
    info = await driver.launch(mode=SessionMode.READ)
    try:
        assert "--no-proxy-server" in info.command_line
        assert f"--user-data-dir={PROFILE_TOKEN}" in info.command_line
        assert info.command_line.endswith(" about:blank")
        assert "--enable-automation" not in info.command_line
        if sys.platform == "darwin":
            assert MOCK_KEYCHAIN not in info.command_line
        assert ("--headless" in info.command_line) is not headed()  # the mode the test asked
        assert driver.current_url == "about:blank"

        step = await driver.navigate(site.url("/"))
        assert step.status == 200
        assert step.document is not None
        assert b"fbc-probe" in step.document
        loads = [b for b in step.graphql if b.friendly_name == LOAD_QUERY]
        assert len(loads) == 1
        assert whole_stream(loads[0].body)  # every NDJSON part of the streamed body
        assert loads[0].status == 200
        assert step.dom.html_lang == "en"
        assert step.dom.posinset_count == 16
        assert await probe(driver, "webdriver") == "false"
        assert "Chrome/" in (await probe(driver, "ua") or "")

        pointer = json.loads(await probe(driver, "mouse") or "null")
        assert pointer is not None
        assert FEED_RIGHT < pointer[0] < RAIL_LEFT  # in the empty margin, not on a link

        scroll = await driver.scroll_step()
        assert scroll.pagination_fired
        assert any(b.friendly_name == PAGE_QUERY for b in scroll.graphql)
        assert scroll.status is None
        assert scroll.document is None
        wheel = json.loads(await probe(driver, "wheel") or "[]")
        assert 3 <= len(wheel) <= 10
        forward = [d for *_, d in wheel if d > 0]
        assert forward
        assert all(99 <= d <= 121 for d in forward)
        assert {(x, y) for x, y, _ in wheel} == {tuple(pointer)}  # at the pointer, in the margin

        # Nothing we did counts as a click or a key press for the page.
        assert await probe(driver, "activation") == "false"
        assert driver._cdp is not None
        try:
            driver._cdp.ensure_allowed("Runtime.evaluate")
        except CdpNotAllowedError:
            pass
        else:  # pragma: no cover - the guard is broken
            raise AssertionError("the CDP guard allowed Runtime.evaluate")
        assert set(driver.cdp_sent) <= {
            "DOM.getDocument",
            "DOM.querySelector",
            "DOM.querySelectorAll",
            "DOM.getOuterHTML",
            "DOM.getBoxModel",
            "DOM.getNodeForLocation",
            "DOM.describeNode",
            "DOM.performSearch",
            "DOM.discardSearchResults",
            "Page.getLayoutMetrics",
        }

        last = await driver.dwell(0.1)
        assert last.final_url == site.url("/")
        shot = await driver.screenshot(site_tmp(driver) / "shot.jpg.gz")
        assert shot is not None
        assert shot.stat().st_mode & 0o777 == 0o600
        await driver.park()
        assert driver.current_url == "about:blank"
    finally:
        await driver.close()
    assert not driver.running


@pytest.mark.headed
async def test_headed_fingerprint(make_driver: DriverFactory, site: LocalSite) -> None:
    """What a headless Chrome gives away and a real window does not (FBC_BROWSER_HEADED=1)."""
    driver = make_driver()
    info = await driver.launch(mode=SessionMode.READ)
    try:
        assert "--headless" not in info.command_line
        await driver.navigate(site.url("/"))
        ua = await probe(driver, "ua") or ""
        assert "Chrome/" in ua
        assert "HeadlessChrome" not in ua
        inner_w, inner_h, outer_w, outer_h, screen_w, screen_h = json.loads(
            await probe(driver, "window") or "[]"
        )
        assert inner_w > 0
        assert inner_h > 0
        assert outer_h > inner_h  # the tab strip and toolbar of a real window
        # Headless Chrome reports an 800x600 screen whatever its window; a real one holds it.
        assert screen_w >= outer_w, (screen_w, outer_w)
        assert screen_h >= outer_h, (screen_h, outer_h)
        await driver.park()
    finally:
        await driver.close()


async def test_headless_is_what_the_self_test_catches(make_driver: DriverFactory) -> None:
    """Headless, the self-test fails on exactly the headless user agent and nothing else."""
    if headed():
        pytest.skip("headed run: test_selftest_passes_on_this_chrome covers the self-test")
    report = await run_selftest(
        lambda port: make_driver(port=port),
        guard_of=lambda d: d._cdp,  # type: ignore[attr-defined]
    )
    assert report.failed() == ["no_headless_ua"]
    assert report.chrome_major is not None


def site_tmp(driver: object) -> Path:
    profile: Path = driver._spec.profile  # type: ignore[attr-defined]
    return profile.parent


async def test_park_waits_for_in_flight_bodies(make_driver: DriverFactory, site: LocalSite) -> None:
    driver = make_driver()
    await driver.launch(mode=SessionMode.READ)
    try:
        # The server holds the stream's last part until the test releases it: no wall-clock
        # race between the stream and navigate().
        await driver.navigate(site.url("/?hold=1"))
        assert LOAD_QUERY not in site.log.completed  # still streaming when navigate returned
        asyncio.get_running_loop().call_later(0.5, site.release.set)
        started = time.monotonic()
        await driver.park()
        assert LOAD_QUERY in site.log.completed  # park let it land before leaving the page
        assert time.monotonic() - started >= 0.4
        assert driver.current_url == "about:blank"
    finally:
        await driver.close()


def listing(directory: Path) -> set[str]:
    return {p.name for p in directory.iterdir()} if directory.is_dir() else set()


async def session_files(sessions: Path, deadline: float = 15.0) -> set[str]:
    """Chrome's session files, once it has written some (it does so on its own schedule)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline
    while loop.time() < end:
        found = await asyncio.to_thread(listing, sessions)
        if found:
            return found
        await asyncio.sleep(0.2)
    return set()


async def test_session_restore_is_suppressed_after_a_crash(
    make_driver: DriverFactory, site: LocalSite, profile: Path
) -> None:
    first = make_driver()
    await first.launch(mode=SessionMode.READ)
    await first.navigate(site.url("/"))
    sessions = profile / "Default" / "Sessions"
    left_behind = await session_files(sessions)
    assert left_behind, "Chrome wrote no session file: the check below would be vacuous"
    assert first.pid is not None
    os.kill(first.pid, signal.SIGKILL)  # a crash: no clean exit, session files left behind
    await asyncio.sleep(1.0)
    await first.close()
    # The worst case: the owner once chose "continue where you left off", and Chrome crashed.
    prefs_path = profile / "Default" / "Preferences"
    prefs = json.loads(prefs_path.read_text())
    prefs.setdefault("session", {})["restore_on_startup"] = 1
    prefs.setdefault("profile", {})["exit_type"] = "Crashed"
    prefs_path.write_text(json.dumps(prefs))
    pages_before = len(site.log.pages)

    second = make_driver(seed=2)
    await second.launch(mode=SessionMode.READ)
    try:
        prefs = json.loads(prefs_path.read_text())
        assert prefs["session"]["restore_on_startup"] == 5
        assert not left_behind & {p.name for p in sessions.iterdir()}
        await asyncio.sleep(1.5)  # a restore would have fetched the page by now
        assert driver_url(second) == "about:blank"
        assert len(site.log.pages) == pages_before  # nothing reopened the local page
    finally:
        await second.close()


def driver_url(driver: object) -> str | None:
    return driver.current_url  # type: ignore[attr-defined, no-any-return]


async def test_cookie_survives_a_relaunch_with_identical_args(
    make_driver: DriverFactory, site: LocalSite
) -> None:
    login = make_driver()
    login_info = await login.launch(mode=SessionMode.READ)
    try:
        await login.navigate(site.url("/cookie"))
        assert {"c_user", "xs"} <= await login.cookie_names(site.url("/"))
        await login.park()
    finally:
        await login.close()

    reader = make_driver(seed=3)
    reader_info = await reader.launch(mode=SessionMode.READ)
    try:
        compare_command(login_info.command_line, reader_info.command_line)  # identical launch
        assert {"c_user", "xs"} <= await reader.cookie_names(site.url("/"))
        assert await reader.cookie_names("https://www.facebook.com/") == frozenset()
    finally:
        await reader.close()


@pytest.mark.headed
async def test_selftest_passes_on_this_chrome(make_driver: DriverFactory) -> None:
    report = await run_selftest(
        lambda port: make_driver(port=port),
        guard_of=lambda d: d._cdp,  # type: ignore[attr-defined]
    )
    assert report.passed, report.failed()
    assert report.chrome_major is not None
