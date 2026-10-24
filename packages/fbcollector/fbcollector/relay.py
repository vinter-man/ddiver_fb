"""Relay payload plumbing for the parser: frames, bounded walks and strict getters.

Facebook's Relay payloads are deep (one story is 600+ leaves) and change shape without notice,
so everything the parser reads from them goes through three small tools:

- **Frames.** The unit of a Relay response is a frame, ``{"data", "label", "path", ...}``. A
  GraphQL body is a sequence of them (the query result, then ``@stream`` / ``@defer``
  increments); a server-rendered page embeds the same frames as ``__bbox.result`` inside its
  ``application/json`` script blocks (``RelayPrefetchedStreamCache``). Both become ``Frame``, so
  the rest of the parser does not care where a story arrived.
- **Walks.** Iterative and bounded in depth and size: a broken or hostile body can make the
  parser skip data, never overflow the stack or spin for minutes.
- **Getters.** ``dig`` never raises; ``digit_id`` and ``epoch`` accept only what the wire model
  would accept, so a value the brain would reject is treated as missing here, where the story
  can still be deferred instead of failing at enqueue time.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from fbcollector.frames import FrameStats, iter_document_json, iter_graphql_frames
from fbcollector.model import GraphqlBody
from fbwire.models import EPOCH_MAX, EPOCH_MIN, INT64_MAX

MAX_DEPTH: Final = 256
"""Deeper than any real payload (a story is ~40 levels); anything below is skipped, counted."""
MAX_NODES: Final = 3_000_000
"""Containers visited by one walk. A 4 MiB body (the driver's cap) holds well under this."""

DOCUMENT: Final = -1
"""``Frame.body`` of frames that came from the navigation's HTML."""

Key = str | int
Path = tuple[Key, ...]

# [0-9], not \d: \d accepts other scripts' digits, which int() parses into the same number.
_DIGITS: Final = re.compile(r"[1-9][0-9]{0,18}")
_POST_PATHS: Final = frozenset({"posts", "permalink"})
_FACEBOOK_HOSTS: Final = frozenset(
    {"facebook.com", "www.facebook.com", "web.facebook.com", "m.facebook.com"}
)
_ID_CHARS: Final = re.compile(r"[A-Za-z0-9+/=_-]+")
_NOT_A_NAME: Final = re.compile(r"[+/=-]")
_DIGIT_RUN: Final = re.compile(r"[0-9]{4,}")
_HUMP: Final = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
_RANDOM_FROM: Final = 12
_WORD: Final = 3
_DIGIT: Final = re.compile(rb"[0-9]")


@dataclass
class WalkStats:
    """How much of the input a walk had to skip. Surfaced in the parser's health counters."""

    too_deep: int = 0
    truncated: int = 0


@dataclass(frozen=True, slots=True)
class Frame:
    """One Relay frame and where it came from.

    ``body`` is the index of the GraphQL body in the step, or ``DOCUMENT`` for the SSR.
    """

    raw: Mapping[str, Any]
    body: int
    friendly_name: str | None

    @property
    def data(self) -> Any:
        return self.raw.get("data")

    @property
    def label(self) -> str | None:
        label = self.raw.get("label")
        return label if isinstance(label, str) else None

    @property
    def path(self) -> Path | None:
        path = self.raw.get("path")
        if not isinstance(path, list) or not path:
            return None
        if not all(isinstance(p, str | int) and not isinstance(p, bool) for p in path):
            return None
        return tuple(path)

    @property
    def from_document(self) -> bool:
        return self.body == DOCUMENT


def document_values(document: bytes, stats: FrameStats) -> list[Any]:
    """Every ``application/json`` script value of the SSR, in document order."""
    values: list[Any] = []
    try:
        values.extend(iter_document_json(document, stats))
    except RecursionError:
        # The json module recurses; a pathologically nested block ends the scan, never the read.
        stats.undecodable += 1
    return values


def document_frames(values: Sequence[Any], stats: WalkStats) -> list[Frame]:
    """The ``__bbox.result`` frames embedded in the SSR values, in document order."""
    frames: list[Frame] = []

    def is_frame(_key: Key, value: Any) -> bool:
        return isinstance(value, Mapping) and _bbox_result(value) is not None

    for value in values:
        if isinstance(value, Mapping) and (result := _bbox_result(value)) is not None:
            frames.append(Frame(result, DOCUMENT, None))
            continue
        for _path, node in walk(value, stop=is_frame, stats=stats, include_stopped=True):
            if (result := _bbox_result(node)) is not None:
                frames.append(Frame(result, DOCUMENT, None))
    return frames


def body_frames(body: GraphqlBody, index: int, stats: FrameStats) -> list[Frame]:
    """The frames of one GraphQL body, in arrival order."""
    frames: list[Frame] = []
    try:
        for value in iter_graphql_frames(body.body, stats):
            items = value if isinstance(value, list) else [value]
            frames.extend(
                Frame(v, index, body.friendly_name) for v in items if isinstance(v, Mapping)
            )
    except RecursionError:
        stats.undecodable += 1
    return frames


def _bbox_result(value: Mapping[str, Any]) -> Mapping[str, Any] | None:
    bbox = value.get("__bbox")
    if isinstance(bbox, Mapping):
        result = bbox.get("result")
        if isinstance(result, Mapping):
            return result
    return None


def walk(
    root: Any,
    *,
    stop: Callable[[Key, Any], bool] | None = None,
    stats: WalkStats | None = None,
    include_stopped: bool = False,
    max_depth: int = MAX_DEPTH,
) -> Iterator[tuple[Path, Mapping[str, Any]]]:
    """Yield ``(path, dict)`` for every dict under ``root``, pre-order, in document order.

    ``stop(key, value)`` true means the walk does not descend into ``value``; with
    ``include_stopped`` the stopped dict itself is still yielded. Iterative on purpose: Relay
    payloads nest deeply and a recursive walk is one malformed body away from RecursionError.
    """
    stats = stats if stats is not None else WalkStats()
    stack: list[tuple[Path, Any, bool]] = [((), root, False)]
    visited = 0
    while stack:
        path, value, stopped = stack.pop()
        if isinstance(value, Mapping):
            yield path, value
            if stopped:
                continue
            children: Iterator[tuple[Key, Any]] = iter(value.items())
        elif isinstance(value, list):
            children = iter(enumerate(value))  # a stopped list is never pushed
        else:
            continue
        visited += 1
        if visited > MAX_NODES:
            stats.truncated += 1
            return
        if len(path) >= max_depth:
            stats.too_deep += 1
            continue
        pending: list[tuple[Path, Any, bool]] = []
        for key, child in children:
            if not isinstance(child, Mapping | list):
                continue
            halt = stop is not None and stop(key, child)
            if halt and not (include_stopped and isinstance(child, Mapping)):
                continue
            pending.append(((*path, key), child, halt))
        stack.extend(reversed(pending))


def dig(value: Any, *keys: Key) -> Any:
    """``value[k1][k2]...`` or ``None``; never raises."""
    for key in keys:
        if isinstance(key, str):
            if not isinstance(value, Mapping):
                return None
            value = value.get(key)
        else:
            if not isinstance(value, list) or not -len(value) <= key < len(value):
                return None
            value = value[key]
    return value


def mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def text(value: Any) -> str | None:
    """A non-blank string, or ``None``."""
    return value if isinstance(value, str) and value.strip() else None


def digit_id(value: Any) -> str | None:
    """A Facebook numeric id as the wire model wants it (``DigitId``), or ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or not _DIGITS.fullmatch(value) or int(value) > INT64_MAX:
        return None
    return value


def epoch(value: Any) -> int | None:
    """Epoch seconds inside the wire model's range, or ``None`` (zero, milliseconds, junk)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    if not isinstance(value, int) or not EPOCH_MIN <= value <= EPOCH_MAX:
        return None
    return value


def looks_like_id(name: str) -> bool:
    """A key (or a one-word string) that is an id, not a name: all digits, a ``pfbid``, a run
    of four digits, base64 punctuation, or 12+ characters without ``_`` that are base64 of a
    typed id, hold three digits or read as humps of three letters or fewer on average
    (``UzpfSTEwMjow``, not ``CometFeedStoryDefaultContentStrategy``). The report masks such
    keys (``diagnostics.safe_name``); the parser never takes such a string as match text."""
    if not name.isascii() or not _ID_CHARS.fullmatch(name):
        return False
    if name.isdigit() or name.startswith("pfbid"):
        return True
    if _DIGIT_RUN.search(name) or _NOT_A_NAME.search(name):
        return True
    core = name.strip("_")
    if len(core) < _RANDOM_FROM or "_" in core:
        return False
    if typed_id(core):
        return True
    humps = _HUMP.findall(core)
    return sum(c.isdigit() for c in core) >= 3 or len(core) <= _WORD * len(humps)


def typed_id(core: str) -> bool:
    """Base64 of printable text with a ``:`` and a digit in it. Facebook's opaque ids encode
    ``type:number``: ``UzpfSTI3NjoxMzkw`` is ``S:_I276:1390``, whose humps alone do not give it
    away."""
    try:
        raw = base64.b64decode(core + "=" * (-len(core) % 4), validate=True)
    except (binascii.Error, ValueError):
        return False
    return raw.isascii() and raw.decode().isprintable() and b":" in raw and bool(_DIGIT.search(raw))


def integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.lstrip("-").isdigit():
        return int(value)
    return None


@dataclass(frozen=True, slots=True)
class FacebookUrl:
    """A facebook.com URL split into what the parser uses. The query is dropped on purpose:
    it carries tracking parameters (``__cft__``, ``__tn__``, ``comment_id``...)."""

    path: tuple[str, ...]
    profile_id: str | None = None

    @property
    def canonical(self) -> str:
        if self.path == ("profile.php",) and self.profile_id:
            return f"https://www.facebook.com/profile.php?id={self.profile_id}"
        return "https://www.facebook.com/" + "".join(f"{p}/" for p in self.path)

    @property
    def group_ref(self) -> str | None:
        """The ``<g>`` of ``/groups/<g>/...``: a numeric id or a vanity slug."""
        return self.path[1] if len(self.path) >= 2 and self.path[0] == "groups" else None

    @property
    def post_ref(self) -> str | None:
        """The ``<p>`` of ``/groups/<g>/(posts|permalink)/<p>/`` (digits, or a ``pfbid``)."""
        if self.group_ref is not None and len(self.path) >= 4 and self.path[2] in _POST_PATHS:
            return self.path[3]
        return None


def facebook_url(value: Any) -> FacebookUrl | None:
    """Parse a facebook.com URL (any of its web hosts); anything else is ``None``."""
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"} or host not in _FACEBOOK_HOSTS:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    segments = tuple(s for s in parts.path.split("/") if s)
    if any(s in {".", ".."} for s in segments):
        return None
    profile_id = None
    if segments == ("profile.php",):
        ids = parse_qs(parts.query).get("id") or []
        profile_id = digit_id(ids[0]) if ids else None
        if profile_id is None:
            return None
    return FacebookUrl(segments, profile_id)
