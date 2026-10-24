"""Tabs the page opens (``window.open``) in a real Chrome, headless, on 127.0.0.1.

A read session keeps one tab: the driver closes the page's popup and logs a warning. A login
session is the owner's: the popup (2FA, "confirm it's you") and a tab they open by hand stay
open, the login finished in the popup is in the profile's cookies, and the next read launch
finds it there on one tab.

Chrome blocks ``window.open`` without a user gesture (Patchright does not pass
``--disable-popup-blocking``), so the page opens its popup from a button's click handler and the
test clicks the button, standing in for the owner. The test also types the address in the
login session: in that mode the driver itself sends a tab nowhere.
"""

from __future__ import annotations

import asyncio
import http.server
import logging
import threading
from collections.abc import Callable, Iterator
from typing import Any, Final

import pytest

from fbcollector.driver._patchright import PatchrightDriver
from fbcollector.driver.launch import LaunchError, SessionMode

from .conftest import DriverFactory

DRIVER_LOG: Final = "fbcollector.driver._patchright"
CLOSED_STRAY: Final = "closing a tab the page opened by itself"
LEFT_OPEN: Final = "a tab opened in the login session; it stays open"
SESSION: Final = frozenset({"c_user", "xs"})

OPENER: Final = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>opener</title></head><body>
<button id="open" style="position:absolute;left:40px;top:40px;width:200px;height:60px">
Continue</button>
<script>
document.getElementById("open").addEventListener("click", function () {
  window.open("/popup", "_blank");
});
</script></body></html>
"""
POPUP: Final = b"<!doctype html><html lang='en'><body>confirmed</body></html>"


class TabSite:
    """``/``: a button whose click opens ``/popup``; ``/popup`` finishes a login (cookies)."""

    def __init__(self) -> None:
        site = self
        self.paths: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - quiet
                return

            def do_GET(self) -> None:
                site.paths.append(self.path)
                popup = self.path == "/popup"
                body = POPUP if popup else OPENER
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                if popup:
                    for name in sorted(SESSION):
                        self.send_header("Set-Cookie", f"{name}=1; Max-Age=86400; Path=/; HttpOnly")
                self.end_headers()
                self.wfile.write(body)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def url(self, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def __enter__(self) -> TabSite:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def tab_site() -> Iterator[TabSite]:
    with TabSite() as running:
        yield running


def running(driver: PatchrightDriver) -> bool:
    """Read afresh each time (mypy would narrow ``driver.running`` across the awaits)."""
    return driver.running


async def until(condition: Callable[[], bool], deadline: float = 10.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline
    while not condition():
        if loop.time() > end:
            return False
        await asyncio.sleep(0.05)
    return True


async def test_a_read_session_closes_the_tab_the_page_opens(
    make_driver: DriverFactory, tab_site: TabSite, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=DRIVER_LOG)
    driver = make_driver(port=tab_site.port)
    await driver.launch(mode=SessionMode.READ)
    try:
        await driver.navigate(tab_site.url("/"))
        page = driver._page
        assert page is not None
        context = page.context
        async with context.expect_page() as opened:
            await page.click("#open")  # whatever it was that made the page open one
        popup = await opened.value
        assert await until(popup.is_closed), "the read session left the page's popup open"
        assert context.pages == [page]
        assert CLOSED_STRAY in caplog.messages
        assert LEFT_OPEN not in caplog.messages
        step = await driver.dwell(0.1)  # the read goes on in its one tab
        assert step.final_url == tab_site.url("/")
    finally:
        await driver.close()


async def test_a_login_session_leaves_the_owner_every_tab_and_the_next_read_one(
    make_driver: DriverFactory, tab_site: TabSite, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=DRIVER_LOG)
    login = make_driver(port=tab_site.port)
    await login.launch(mode=SessionMode.LOGIN)
    try:
        start = login._page
        assert start is not None
        context = start.context
        await start.goto(tab_site.url("/"))  # the owner types the address
        async with context.expect_page() as opened:
            await start.click("#open")  # and clicks: the page opens its popup
        popup = await opened.value
        await popup.wait_for_load_state()  # the login finishes there: the cookies are set
        own = await context.new_page()  # a tab of the owner's own (Cmd+T)
        await asyncio.sleep(1.0)  # a read session would have closed both by now
        assert not popup.is_closed()
        assert not own.is_closed()
        assert len(context.pages) == 3
        await start.close()  # the owner closes the tab Chrome started on
        assert running(login)
        assert await login.cookie_names(tab_site.url("/")) >= SESSION  # the profile's, not a tab's
        assert "/popup" in tab_site.paths
        assert caplog.messages.count(LEFT_OPEN) == 2
        assert CLOSED_STRAY not in caplog.messages
        assert login.cdp_sent == ()  # nothing read, no CDP session at all
        with pytest.raises(LaunchError, match="login session"):
            await login.navigate(tab_site.url("/"))
    finally:
        await login.close()
    assert not running(login)

    reader = make_driver(port=tab_site.port, seed=2)
    requests_before = len(tab_site.paths)
    await reader.launch(mode=SessionMode.READ)
    try:
        assert await reader.cookie_names(tab_site.url("/")) >= SESSION  # the cookie gate's read
        await asyncio.sleep(1.0)  # a restored tab would have loaded by now
        assert reader.current_url == "about:blank"
        assert len(tab_site.paths) == requests_before  # none of the login's tabs came back
        assert reader._page is not None
        assert reader._page.context.pages == [reader._page]
    finally:
        await reader.close()
