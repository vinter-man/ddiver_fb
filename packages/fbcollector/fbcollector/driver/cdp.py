"""The raw CDP session, behind a runtime allow-list, and the DOM reads made through it.

Invariant 1 allows reading the page, never acting on it. Patchright already speaks CDP for
navigation, input and response bodies; the one session *we* open is wrapped in ``GuardedCdp``,
which raises on any method outside ``ALLOWED`` before it is sent. A driver test records the
protocol traffic of a local page and checks that nothing else left this session.

**Why DOM facts are read through the DOM domain and not with an isolated-world evaluate.**
DESIGN planned ``page.evaluate(..., isolated_context=True)``. Patchright runs every evaluate
with ``userGesture: true``, and a check on this Mac (Chrome 153, Patchright 1.63) showed what
that does: the page's ``navigator.userActivation.hasBeenActive`` turned ``true`` with no click
or key press — a page-visible trace that no human produces, and a transient activation the page
could use to open windows or play sound. The DOM domain reads the same facts (``lang``,
``[aria-posinset]`` count, dialog texts, the joined-groups header, the password field) with no
script at all; mouse moves and wheel events do not activate the page. So the driver never
evaluates JavaScript; ``isolated_context=True`` remains required by the invariant test should
an evaluate ever be added.
"""

from __future__ import annotations

import base64
import html
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from fbcollector.model import DomFacts

log = logging.getLogger(__name__)

DESIGN_METHODS: Final = frozenset(
    {"Network.enable", "Network.getResponseBody", "Page.captureScreenshot"}
)
DOM_READS: Final = frozenset(
    {
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
)
"""Read-only DOM methods: no script runs, nothing on the page changes."""
ALLOWED: Final = DESIGN_METHODS | DOM_READS

MAX_DIALOGS: Final = 5
MAX_DIALOG_TEXT: Final = 4000
MAX_HEADINGS: Final = 30
SEE_MORE_XPATH: Final = "//div[@role='button'][normalize-space(.)='See more']"
HEADINGS: Final = "h1, h2, [role=heading][aria-level='1'], [role=heading][aria-level='2']"
_JOINED: Final = re.compile(r"groups you(?:'|’)ve joined\s*\(\s*(\d[\d,.\s]*)\)", re.IGNORECASE)
_TAG: Final = re.compile(r"<[^>]+>")
_NON_TEXT: Final = re.compile(
    r"<(script|style|template)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_SPACE: Final = re.compile(r"\s+")
NON_INTERACTIVE: Final = frozenset({"DIV", "MAIN", "SECTION", "BODY", "HTML", "SPAN"})
INTERACTIVE_ROLES: Final = frozenset(
    {"link", "button", "menuitem", "tab", "checkbox", "textbox", "img", "article"}
)


class CdpNotAllowedError(Exception):
    """A CDP method outside the allow-list was about to be sent (nothing was sent)."""


class CdpSession(Protocol):
    """The part of Patchright's ``CDPSession`` we use."""

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...

    async def detach(self) -> None: ...


class GuardedCdp:
    """A CDP session that refuses every method outside ``allowed`` before sending it."""

    def __init__(self, session: CdpSession, allowed: frozenset[str] = ALLOWED) -> None:
        self._session = session
        self._allowed = allowed
        self.sent: list[str] = []

    def ensure_allowed(self, method: str) -> None:
        if method not in self._allowed:
            msg = f"CDP method {method!r} is not on the allow-list"
            raise CdpNotAllowedError(msg)

    async def send(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self.ensure_allowed(method)
        self.sent.append(method)
        return await self._session.send(method, dict(params) if params is not None else None)

    async def detach(self) -> None:
        await self._session.detach()


# -- DOM reads --------------------------------------------------------------------------------


def html_text(markup: str) -> str:
    """Visible text of an HTML fragment: tags dropped, entities decoded, whitespace collapsed."""
    return _SPACE.sub(" ", html.unescape(_TAG.sub(" ", _NON_TEXT.sub(" ", markup)))).strip()


def joined_count(texts: Sequence[str]) -> int | None:
    """N from "All groups you've joined (N)" in any of ``texts``."""
    for text in texts:
        match = _JOINED.search(text)
        if match:
            digits = re.sub(r"\D", "", match.group(1))
            if digits:
                return int(digits)
    return None


def _attributes(node: Mapping[str, Any]) -> dict[str, str]:
    raw = node.get("attributes")
    if not isinstance(raw, list):
        return {}
    pairs = zip(raw[::2], raw[1::2], strict=False)
    return {str(k): str(v) for k, v in pairs}


def html_lang(document: Mapping[str, Any]) -> str | None:
    """``<html lang>`` from a ``DOM.getDocument(depth=1)`` answer."""
    for child in document.get("root", {}).get("children", ()) or ():
        if isinstance(child, Mapping) and child.get("nodeName") == "HTML":
            return _attributes(child).get("lang") or None
    return None


async def read_dom(cdp: GuardedCdp) -> DomFacts:
    """The isolated facts the detectors and the parser health need, without running script."""
    document = await cdp.send("DOM.getDocument", {"depth": 1})
    root = int(document["root"]["nodeId"])

    async def select_all(selector: str) -> list[int]:
        found = await cdp.send("DOM.querySelectorAll", {"nodeId": root, "selector": selector})
        return [int(n) for n in found.get("nodeIds", ())]

    async def texts(nodes: Sequence[int], limit: int) -> list[str]:
        out = []
        for node in nodes[:limit]:
            markup = await cdp.send("DOM.getOuterHTML", {"nodeId": node})
            out.append(html_text(str(markup.get("outerHTML", "")))[:MAX_DIALOG_TEXT])
        return out

    posinset = len(await select_all("[aria-posinset]"))
    dialog_nodes = await select_all("[role=dialog]")
    dialogs = await texts(dialog_nodes, MAX_DIALOGS)
    password = bool(await select_all("input[name=pass]"))
    headings = await texts(await select_all(HEADINGS), MAX_HEADINGS)
    search = await cdp.send("DOM.performSearch", {"query": SEE_MORE_XPATH})
    see_more = int(search.get("resultCount", 0))
    await cdp.send("DOM.discardSearchResults", {"searchId": search["searchId"]})
    return DomFacts(
        html_lang=html_lang(document),
        posinset_count=posinset,
        dialog_texts=tuple(dialogs),
        dialog_count=len(dialog_nodes),
        joined_header_count=joined_count(headings),
        has_password_field=password,
        see_more=see_more,
    )


async def count_nodes(cdp: GuardedCdp, selector: str) -> int:
    document = await cdp.send("DOM.getDocument", {"depth": 0})
    found = await cdp.send(
        "DOM.querySelectorAll", {"nodeId": int(document["root"]["nodeId"]), "selector": selector}
    )
    return len(found.get("nodeIds", ()))


async def read_attribute(cdp: GuardedCdp, selector: str, name: str) -> str | None:
    """One attribute of the first element matching ``selector`` (the self-test page's facts)."""
    document = await cdp.send("DOM.getDocument", {"depth": 0})
    found = await cdp.send(
        "DOM.querySelector", {"nodeId": int(document["root"]["nodeId"]), "selector": selector}
    )
    node = int(found.get("nodeId", 0))
    if not node:
        return None
    described = await cdp.send("DOM.describeNode", {"nodeId": node})
    return _attributes(described.get("node", {})).get(name)


# -- layout, for the mouse ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Box:
    left: float
    top: float
    right: float
    bottom: float


@dataclass(frozen=True, slots=True)
class Layout:
    width: float
    height: float
    feed: Box | None
    rail: Box | None


def _box(model: Mapping[str, Any]) -> Box | None:
    quad = model.get("model", {}).get("border")
    if not isinstance(quad, list) or len(quad) != 8:
        return None
    xs, ys = quad[0::2], quad[1::2]
    return Box(min(xs), min(ys), max(xs), max(ys))


async def read_layout(cdp: GuardedCdp, feed: str, rail: str) -> Layout:
    metrics = await cdp.send("Page.getLayoutMetrics")
    viewport = metrics.get("cssVisualViewport") or metrics.get("cssLayoutViewport") or {}
    document = await cdp.send("DOM.getDocument", {"depth": 0})
    root = int(document["root"]["nodeId"])

    async def box(selector: str) -> Box | None:
        found = await cdp.send("DOM.querySelector", {"nodeId": root, "selector": selector})
        node = int(found.get("nodeId", 0))
        if not node:
            return None
        return _box(await cdp.send("DOM.getBoxModel", {"nodeId": node}))

    return Layout(
        width=float(viewport.get("clientWidth", 0)),
        height=float(viewport.get("clientHeight", 0)),
        feed=await box(feed),
        rail=await box(rail),
    )


def is_quiet_node(node: Mapping[str, Any]) -> bool:
    """A layout container, not a link, button, image or anything that reacts to hover."""
    attributes = _attributes(node)
    if str(node.get("nodeName", "")).upper() not in NON_INTERACTIVE:
        return False
    if attributes.get("role", "").lower() in INTERACTIVE_ROLES:
        return False
    return not any(k in attributes for k in ("href", "onclick", "tabindex", "aria-label"))


async def point_is_quiet(cdp: GuardedCdp, x: float, y: float) -> bool:
    try:
        hit = await cdp.send("DOM.getNodeForLocation", {"x": int(x), "y": int(y)})
        described = await cdp.send("DOM.describeNode", {"backendNodeId": hit["backendNodeId"]})
    except Exception as exc:  # noqa: BLE001 - a failed hit test only skips the mouse move
        log.debug("hit test failed: %s", type(exc).__name__)
        return False
    return is_quiet_node(described.get("node", {}))


async def screenshot_jpeg(cdp: GuardedCdp, quality: int = 60) -> bytes:
    shot = await cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": quality})
    return base64.b64decode(shot["data"])
