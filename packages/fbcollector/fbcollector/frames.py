"""Turn the raw bytes a Facebook page received into JSON values.

Two shapes reach us:

- GraphQL responses (``/api/graphql/``): optionally prefixed with ``for (;;);`` (an anti-JSON-
  hijacking guard) and often several JSON documents back to back — the first is the query result,
  the rest are ``@stream`` / ``@defer`` increments separated by newlines.
- The navigation's HTML: server-rendered Relay data sits in ``<script type="application/json">``
  blocks (``data-sjs``), usually wrapping ``RelayPrefetchedStreamCache`` entries.

Nothing here knows what a post is; the walker does. Bad input never raises: a frame that does
not decode ends the scan of that body and is counted, because a half-read response is normal when
a page navigates away mid-stream.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

GUARD_PREFIX = "for (;;);"

# Attribute order and quoting vary; the type attribute is what matters. Non-greedy body up to the
# first closing tag: JSON cannot contain a literal "</script" because Facebook escapes "<" as <.
_JSON_SCRIPT = re.compile(
    r"<script\b[^>]*\btype\s*=\s*(?:\"application/json\"|'application/json'|application/json)[^>]*>"
    r"(.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)

_decoder = json.JSONDecoder()


@dataclass
class FrameStats:
    """What a scan saw; the parser health counters are built from these."""

    frames: int = 0
    undecodable: int = 0
    errors: list[str] = field(default_factory=list)

    def note_error(self, where: str, exc: ValueError) -> None:
        self.undecodable += 1
        if len(self.errors) < 5:
            self.errors.append(f"{where}: {exc}")


def iter_graphql_frames(body: bytes | str, stats: FrameStats | None = None) -> Iterator[Any]:
    """Yield every JSON document in a GraphQL response body, in order."""
    stats = stats if stats is not None else FrameStats()
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    pos = _skip_ws(text, 0)
    if text.startswith(GUARD_PREFIX, pos):
        pos = _skip_ws(text, pos + len(GUARD_PREFIX))
    while pos < len(text):
        try:
            value, end = _decoder.raw_decode(text, pos)
        except ValueError as exc:
            stats.note_error("graphql", exc)
            return
        stats.frames += 1
        yield value
        pos = _skip_ws(text, end)
        # A second guard prefix can appear when responses are concatenated by a proxy.
        if text.startswith(GUARD_PREFIX, pos):
            pos = _skip_ws(text, pos + len(GUARD_PREFIX))


def iter_document_json(html: bytes | str, stats: FrameStats | None = None) -> Iterator[Any]:
    """Yield the JSON value of every ``application/json`` script block in an HTML document."""
    stats = stats if stats is not None else FrameStats()
    text = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    for match in _JSON_SCRIPT.finditer(text):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            value = json.loads(raw)
        except ValueError as exc:
            stats.note_error("document", exc)
            continue
        stats.frames += 1
        yield value


def _skip_ws(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    return pos
