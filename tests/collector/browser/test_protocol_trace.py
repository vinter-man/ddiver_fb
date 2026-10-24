"""Every CDP message of a full read cycle, recorded with ``DEBUG=pw:protocol`` (invariant 1).

The probe (``trace_probe.py``) launches the real driver on the local page, navigates, scrolls,
dwells, reads cookie names, takes a screenshot and parks. Patchright's driver process logs each
CDP message it sends to stderr; the test parses that log and checks three things:

- **our raw session** (the one ``Target.attachToTarget`` opened) sent only allow-listed methods,
  exactly the ones ``GuardedCdp`` recorded;
- **everything else** is either what Patchright sends on its own (below, observed with
  Patchright 1.63 / Chrome 153 on macOS) or what our Playwright-level calls translate to
  (``Page.navigate``, ``Input.dispatchMouseEvent``, ``Network.getResponseBody``,
  ``Storage.getCookies``);
- **nothing acts on the page**: no key or touch input, no mouse button, no script evaluation in
  the page with a user gesture, no DOM writes, no cookies set, no request or response rewritten.

What Patchright sends by itself, for the record: target discovery and auto-attach
(``Target.setAutoAttach``, ``Runtime.runIfWaitingForDebugger``, ...), ``Page.enable`` and
lifecycle events, its utility world (``Page.createIsolatedWorld``, an *empty*
``Page.addScriptToEvaluateOnNewDocument`` that names the world, ``DOM.getDocument`` /
``DOM.querySelectorAll`` on its own session), ``Network.enable``, ``Log.enable``,
``Emulation.setFocusEmulationEnabled`` (the page believes it has focus) and
``Emulation.setEmulatedMedia`` (light scheme, no reduced motion), on workers an empty
``Emulation.setUserAgentOverride`` and ``Runtime.evaluate("globalThis")``, and request
interception for every request (``Fetch.enable`` with ``*``, ``Fetch.continueRequest``
unchanged) — which **fails the browser's own ``/favicon.ico`` request** (``Fetch.failRequest``,
``Aborted``). Not ``Runtime.enable``, and no ``--enable-automation``.

The trace is taken headless by default (``conftest``), where Patchright also sets the headless
renderer's default fonts (``Page.setFontFamilies``); a headed run (``FBC_BROWSER_HEADED=1``)
must not send it.
"""

from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from fbcollector.driver.cdp import ALLOWED

from .headless import headed

REPO = Path(__file__).resolve().parents[3]

PATCHRIGHT_OWN = frozenset(
    {
        "Browser.getVersion",
        "Browser.setDownloadBehavior",
        "Browser.close",
        "Target.setAutoAttach",
        "Target.attachToBrowserTarget",
        "Target.getTargetInfo",
        "Target.detachFromTarget",
        "Target.setDiscoverTargets",
        "Runtime.runIfWaitingForDebugger",
        "Page.enable",
        "Page.getFrameTree",
        "Page.setLifecycleEventsEnabled",
        "Page.createIsolatedWorld",
        "Page.addScriptToEvaluateOnNewDocument",
        "Log.enable",
        "Network.enable",
        "Network.setCacheDisabled",
        "Fetch.enable",
        "Fetch.continueRequest",
        "Fetch.failRequest",
        "Emulation.setFocusEmulationEnabled",
        "Emulation.setEmulatedMedia",
        "Emulation.setUserAgentOverride",
        "Runtime.evaluate",
        "DOM.getDocument",
        "DOM.querySelectorAll",
    }
)
HEADLESS_ONLY = frozenset({"Page.setFontFamilies"})
"""What Patchright adds in headless mode alone (the tests' mode, never production's)."""
OUR_API = frozenset(
    {
        "Page.navigate",
        "Input.dispatchMouseEvent",
        "Network.getResponseBody",
        "Storage.getCookies",
        "Target.attachToTarget",
    }
)
NEVER = frozenset(
    {
        "Runtime.enable",
        "Runtime.callFunctionOn",
        "Runtime.addBinding",
        "Input.dispatchKeyEvent",
        "Input.insertText",
        "Input.dispatchTouchEvent",
        "Input.dispatchDragEvent",
        "Input.emulateTouchFromMouseEvent",
        "DOM.setAttributeValue",
        "DOM.setAttributesAsText",
        "DOM.setOuterHTML",
        "DOM.setNodeValue",
        "DOM.removeNode",
        "DOM.focus",
        "DOM.setFileInputFiles",
        "Page.setDocumentContent",
        "Page.reload",
        "Page.navigateToHistoryEntry",
        "Network.setCookie",
        "Network.setCookies",
        "Network.setExtraHTTPHeaders",
        "Storage.setCookies",
        "Fetch.fulfillRequest",
        "Fetch.continueWithAuth",
    }
)


def run_probe(tmp_path: Path) -> tuple[list[dict[str, Any]], dict[tuple[Any, Any], Any], list[str]]:
    env = {**os.environ, "DEBUG": "pw:protocol"}
    done = subprocess.run(  # noqa: S603 - our own probe, fixed arguments
        [sys.executable, "-m", "tests.collector.browser.trace_probe", str(tmp_path / "profile")],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    sends: list[dict[str, Any]] = []
    results: dict[tuple[Any, Any], Any] = {}
    for line in done.stderr.splitlines():
        if "SEND ►" in line:
            sends.append(json.loads(line.split("SEND ►", 1)[1]))
        elif "◀ RECV" in line:
            message = json.loads(line.split("◀ RECV", 1)[1])
            if "id" in message:
                results[(message.get("sessionId"), message["id"])] = message.get("result")
    recorded = json.loads(done.stdout.strip().splitlines()[-1])["cdp_sent"]
    return sends, results, recorded


def test_only_allow_listed_cdp_leaves_our_code(tmp_path: Path) -> None:
    sends, results, recorded = run_probe(tmp_path)
    assert sends, "no protocol trace was recorded"
    methods = collections.Counter(m["method"] for m in sends)

    attaches = [m for m in sends if m["method"] == "Target.attachToTarget"]
    assert len(attaches) == 1  # our raw session; Patchright's own sessions are auto-attached
    ours = results[(attaches[0].get("sessionId"), attaches[0]["id"])]["sessionId"]
    on_ours = [m["method"] for m in sends if m.get("sessionId") == ours]
    assert collections.Counter(on_ours) == collections.Counter(recorded)
    assert set(on_ours) <= ALLOWED
    assert "Page.captureScreenshot" in on_ours

    own = PATCHRIGHT_OWN if headed() else PATCHRIGHT_OWN | HEADLESS_ONLY
    unexpected = set(methods) - own - OUR_API - ALLOWED
    assert not unexpected, f"new CDP methods in the trace: {sorted(unexpected)}"
    if headed():
        assert not set(methods) & HEADLESS_ONLY
    assert not set(methods) & NEVER

    for message in sends:
        method, params = message["method"], message.get("params") or {}
        if method == "Input.dispatchMouseEvent":
            assert params["type"] in {"mouseMoved", "mouseWheel"}  # never a button
            assert params.get("button", "none") == "none"
        elif method == "Page.addScriptToEvaluateOnNewDocument":
            assert params.get("source") == ""  # Patchright's world name, no script
        elif method == "Runtime.evaluate":
            assert params.get("expression") == "globalThis"
            assert not params.get("userGesture")
        elif method == "Fetch.continueRequest":
            assert set(params) == {"requestId"}  # passed through unchanged
        elif method == "Page.navigate":
            assert params["url"].startswith(("http://127.0.0.1:", "about:blank"))
        elif method == "Emulation.setUserAgentOverride":
            assert params.get("userAgent") == ""
