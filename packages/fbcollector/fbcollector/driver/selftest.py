"""The local self-test: the driver against a page on 127.0.0.1, never Facebook.

DESIGN asks for it before any Facebook navigation whenever Chrome's major version is not on the
tested list, and ``fbcollector selftest`` runs it on demand. It checks what the design relies on
and a Chrome update could silently break:

- the page's own ``navigator.webdriver`` is ``false`` and its user agent has no
  ``HeadlessChrome`` (read from what the page wrote into the DOM, i.e. the main world's view);
- a streamed NDJSON response is captured whole, tagged with its friendly name;
- the DOM reads work (``lang``, ``[aria-posinset]``);
- a wheel gesture reaches the page with our deltas and fires the page's pagination request;
- our input leaves ``navigator.userActivation.hasBeenActive`` false (nothing we do counts as a
  click or a key press);
- the guarded CDP session refuses a method outside its allow-list.

``LocalSite`` is also the browser tests' page. It is a stdlib threaded HTTP server bound to
127.0.0.1 on an ephemeral port, serving one HTML page and a slow, streamed ``/api/graphql/``.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from fbcollector.driver import Driver, SessionMode
from fbcollector.driver.cdp import CdpNotAllowedError, GuardedCdp
from fbcollector.frames import iter_graphql_frames
from fbcollector.model import Step

log = logging.getLogger(__name__)

PROBE_ID: Final = "fbc-probe"
LOAD_QUERY: Final = "LocalFeedQuery"
PAGE_QUERY: Final = "LocalPaginationQuery"
STREAM_PARTS: Final = 3
HOLD_MAX: Final = 30.0
"""The longest a ``?hold=1`` stream waits for ``LocalSite.release`` (seconds)."""
FEED_RIGHT: Final = 760
RAIL_LEFT: Final = 880
"""Where the page's feed column ends and its right rail starts (CSS pixels): the margin."""

PAGE: Final = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>fbcollector self-test</title>
<style>
 body { margin: 0; font: 14px sans-serif; }
 #layout { display: flex; }
 #layout > div { flex-shrink: 0; }
 #nav { width: 200px; }
 [role=feed] { width: 500px; margin-left: 60px; }
 [role=feed] > div { height: 380px; border-bottom: 1px solid #ccc; }
 [role=complementary] { width: 200px; margin-left: 120px; }
</style></head>
<body>
<div id="%(probe)s" data-webdriver="" data-ua="" data-activation="false" data-wheel="[]"
     data-mouse="" data-pages="0" data-window="[]"></div>
<div id="layout" role="main">
  <div id="nav"><a href="#one">A link</a><br><a href="#two">Another link</a></div>
  <div role="feed">%(posts)s</div>
  <div role="complementary"><a href="#rail">Rail link</a><img alt="avatar" width="40"
       height="40"></div>
</div>
<script>
(function () {
  var probe = document.getElementById("%(probe)s");
  var wheel = [];
  probe.dataset.webdriver = String(navigator.webdriver);
  probe.dataset.ua = navigator.userAgent;
  probe.dataset.window = JSON.stringify([innerWidth, innerHeight, outerWidth, outerHeight,
    screen.width, screen.height]);
  function activation() {
    var ua = navigator.userActivation;
    probe.dataset.activation = String(!!(ua && ua.hasBeenActive));
  }
  setInterval(activation, 50);
  window.addEventListener("wheel", function (e) {
    wheel.push([Math.round(e.clientX), Math.round(e.clientY), e.deltaY]);
    probe.dataset.wheel = JSON.stringify(wheel);
  }, {passive: true});
  window.addEventListener("mousemove", function (e) {
    probe.dataset.mouse = JSON.stringify([Math.round(e.clientX), Math.round(e.clientY)]);
  });
  function query(name, extra) {
    return fetch("/api/graphql/" + (extra || ""), {method: "POST",
      headers: {"Content-Type": "application/x-www-form-urlencoded"},
      body: "fb_api_req_friendly_name=" + name + "&variables=%%7B%%7D"})
      .then(function (r) { return r.text(); });
  }
  var params = new URLSearchParams(location.search);
  var slow = params.get("slow");
  query("%(load)s", slow ? "?slow=" + slow : (params.get("hold") ? "?hold=1" : ""));
  var fetchedAt = 0, pages = 0;
  window.addEventListener("scroll", function () {
    if (window.scrollY - fetchedAt > 250) {
      fetchedAt = window.scrollY;
      pages += 1;
      probe.dataset.pages = String(pages);
      query("%(page)s");
    }
  }, {passive: true});
})();
</script></body></html>
"""


def page_html(posts: int = 16) -> bytes:
    items = "".join(f'<div aria-posinset="{i + 1}">Post {i + 1}</div>' for i in range(posts))
    values = {"probe": PROBE_ID, "posts": items, "load": LOAD_QUERY, "page": PAGE_QUERY}
    return (PAGE % values).encode()


@dataclass
class SiteLog:
    """What the server saw, for the tests."""

    pages: list[str] = field(default_factory=list)
    graphql: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    cookies: list[str] = field(default_factory=list)


class _Handler(http.server.BaseHTTPRequestHandler):
    site: LocalSite

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ARG002 - quiet
        return

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        self.site.log.pages.append(parts.path)
        if parts.path == "/cookie":
            self.site.log.cookies.append(self.headers.get("Cookie") or "")
            self._send(
                b"<!doctype html><html lang='en'><body>cookie set</body></html>",
                cookies=(
                    "c_user=100000000000001; Max-Age=86400; Path=/; HttpOnly",
                    "xs=local-test; Max-Age=86400; Path=/; HttpOnly",
                ),
            )
            return
        self._send(page_html())

    def do_POST(self) -> None:
        parts = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode(errors="replace"))
        name = (form.get("fb_api_req_friendly_name") or ["?"])[0]
        self.site.log.graphql.append(name)
        query = parse_qs(parts.query)
        slow = float((query.get("slow") or ["0"])[0])
        pause = max(0.05, slow / STREAM_PARTS)
        hold = bool(query.get("hold"))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        with contextlib.suppress(OSError):
            for index in range(STREAM_PARTS):
                if hold and index == STREAM_PARTS - 1:
                    self.site.release.wait(timeout=HOLD_MAX)  # the test lets it finish
                prefix = b"for (;;);" if index == 0 else b""
                frame = {"data": {"part": index, "query": name}, "label": f"{name}$stream"}
                self.wfile.write(prefix + json.dumps(frame).encode() + b"\n")
                self.wfile.flush()
                time.sleep(pause)
            self.site.log.completed.append(name)

    def _send(self, body: bytes, cookies: tuple[str, ...] = ()) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)


class LocalSite:
    """The self-test page on ``http://127.0.0.1:<ephemeral port>/``."""

    def __init__(self) -> None:
        handler = type("Handler", (_Handler,), {"site": self})
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.log = SiteLog()
        self.release = threading.Event()
        """Lets a ``?hold=1`` stream send its last part (tests of in-flight bodies)."""

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def url(self, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def __enter__(self) -> LocalSite:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release.set()
        self._server.shutdown()
        self._server.server_close()


@dataclass(frozen=True, slots=True)
class SelfTestReport:
    checks: tuple[tuple[str, bool], ...]
    chrome_major: int | None = None

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(ok for _, ok in self.checks)

    def failed(self) -> list[str]:
        return [name for name, ok in self.checks if not ok]


def whole_stream(body: bytes) -> bool:
    return len(list(iter_graphql_frames(body))) == STREAM_PARTS


async def probe(driver: Driver, name: str) -> str | None:
    """One ``data-*`` attribute the page's own script wrote into the probe element."""
    return await driver.read_attribute(f"#{PROBE_ID}", f"data-{name}")


async def run_selftest(
    make_driver: Callable[[int], Driver],
    *,
    guard_of: Callable[[Driver], GuardedCdp | None] | None = None,
) -> SelfTestReport:
    """Launch a driver restricted to the local site (``make_driver(port)``), check it, close it."""
    attribute = probe
    checks: list[tuple[str, bool]] = []
    with LocalSite() as site:
        driver = make_driver(site.port)
        info = await driver.launch(mode=SessionMode.READ)
        try:
            step = await driver.navigate(site.url("/"))
            checks += _page_checks(step)
            checks.append(("webdriver_false", await attribute(driver, "webdriver") == "false"))
            ua = await attribute(driver, "ua") or ""
            checks.append(("no_headless_ua", bool(ua) and "HeadlessChrome" not in ua))
            scroll = await driver.scroll_step()
            checks.append(("pagination_fired", scroll.pagination_fired))
            wheel = json.loads(await attribute(driver, "wheel") or "[]")
            checks.append(("wheel_delivered", bool(wheel) and all(d > 0 for *_, d in wheel[:3])))
            checks.append(("no_user_activation", await attribute(driver, "activation") == "false"))
            checks.append(("cdp_allow_list", _refuses(guard_of(driver) if guard_of else None)))
            await driver.park()
        finally:
            await driver.close()
    major = info.chrome_version.major if info.chrome_version else None
    return SelfTestReport(tuple(checks), major)


def _page_checks(step: Step) -> list[tuple[str, bool]]:
    loads = [b for b in step.graphql if b.friendly_name == LOAD_QUERY]
    return [
        ("document_captured", bool(step.document) and step.status == 200),
        ("stream_captured_whole", bool(loads) and whole_stream(loads[0].body)),
        ("dom_lang", step.dom.html_lang == "en"),
        ("dom_stories", step.dom.posinset_count > 0),
    ]


def _refuses(guard: GuardedCdp | None) -> bool:
    if guard is None:
        return False
    try:
        guard.ensure_allowed("Runtime.evaluate")
    except CdpNotAllowedError:
        return True
    return False
