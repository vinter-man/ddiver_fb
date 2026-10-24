"""Why stories come out without text: the report's parser diagnostics, structure only.

The alert "PARSER: 11 of 33 stories with empty text" says how often, not why, and the operator
who reads the report must never see what a post says. So this module re-reads the newest saved
steps (``raw/``) with the current parser and, for each story that still has no text, prints a
*skeleton* of the story instead of the story:

- every path under the story root whose value is a string of ``MIN_LENGTH`` or more characters,
  or a non-blank string under a ``text`` key (arrays as ``[]``, at most ``MAX_DEPTH`` keys
  deep: as deep as the parser reads), with that string's **length**;
- the ``__typename`` of every object along those paths (schema names, not content), and the
  typenames under attachments and media;
- which of the parser's own text paths were there but empty, null or absent, and the
  typenames there.

Stories alike where the parser looks (each text path's status and typenames, the attachment
typenames) are one *shape*, counted together, whatever their strings' lengths; all shapes are
drawn as one tree whose leaves say how many of the stories have them. The stories whose only
text is a card's (``stories.TextClass.ATTACHMENT_TEXT``) are drawn too, and for each shape the
subtrees under the story's and its shared story's ``attachments[]`` are drawn apart, every
string at any length and every typename (``style_infos`` among them), so a live run shows where
a link's or a listing's words really sit. Separately it counts the
``@defer`` parts that patch a story after it arrived, and how many of them are aimed at a story
that came out empty: a message section delivered that way never reaches the node the parser
reads.

It also sorts the distinct posts of those steps by the script of their authors' words (the
message and the shared texts), and again by that of their final text (card lines included):
Cyrillic only, Latin only, both, or neither (``Script``), and counts the posts whose tree holds
a key that speaks of a translation, with those keys' names: whether Facebook sent the groups'
posts as written or translated.

Nothing a post says leaves this module: no string value, no number, no URL, only key names,
typenames, lengths and counts (the posts per script among them). A key is printed only when it
is a plain name. One that looks like an id (all digits, a ``pfbid``, a long base64-ish run)
reads ``<id>``, any other odd key ``<key>``, an odd typename ``<?>``. Everything is bounded: the
stories sampled, the steps read, the paths per story, the tree's lines, each shape's
attachments drawing and the fragment rows.
"""

from __future__ import annotations

import datetime as dt
import enum
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from fbcollector import relay
from fbcollector.model import Surface
from fbcollector.parser import NO_NAME, step_frames
from fbcollector.raw import STEP_SUFFIX, RawStep, load_step
from fbcollector.relay import Frame, WalkStats, dig, looks_like_id, text
from fbcollector.stories import (
    ATTACHMENT_LINK,
    ATTACHMENT_LISTING,
    ATTACHMENT_TEXT_KEYS,
    KNOWN_CONNECTIONS,
    MAX_SHARE_DEPTH,
    TRANSLATION_MARK,
    Context,
    Reading,
    TextClass,
    find_units,
    own_text,
    read_story,
    story_root,
)

MIN_LENGTH: Final = 12
"""Shorter strings are flags, enums and small numbers; names, ids, URLs and most post texts are
longer. A non-blank string under a ``text`` key is listed at any length: every text the parser
reads ends in one, and a short post is still a post."""
TEXT_KEY: Final = "text"
MAX_DEPTH: Final = 40
"""Keys below the story root (``[]`` does not count): as deep as the parser reads text. Its
deepest is in a share of a share of a share (``stories.MAX_SHARE_DEPTH``), each 7 keys below the
last (``comet_sections.content.story.comet_sections.attached_story.story.attached_story``), then
in that story's message section (5 keys), found by the section walk 12 levels down at a
``message.text`` (14 keys): 21 + 5 + 14 = 40."""
MAX_PATHS: Final = 400
MAX_NODES: Final = 100_000
SAMPLE_STORIES: Final = 20
"""The newest distinct stories with empty text, or with only a card's text, that are drawn (at
most 26 shapes, A-Z)."""
ATTACHMENT_LINES: Final = 80
"""Lines of one shape's attachments drawing."""
ATTACHMENT_DEPTH: Final = 16
"""Keys below an ``attachments`` key the drawing goes: deeper than the parser reads a card
(``stories.MAX_ATTACHMENT_DEPTH``), so words just beyond its reach show up too."""
MAX_STEPS: Final = 100
MAX_TREE_LINES: Final = 300
MAX_FRAGMENTS: Final = 10
FRAGMENT_PATHS: Final = 3
MAX_NAMES: Final = 3
MAX_KEY_NAMES: Final = 5
"""Translation-related key names listed in the script section; the rest are counted."""

ARRAY: Final = "[]"
ID: Final = "<id>"
KEY: Final = "<key>"
ODD: Final = "<?>"
SSR: Final = "ssr"

ABSENT: Final = "absent"
NULL: Final = "null"
EMPTY: Final = "empty"
TEXT: Final = "TEXT"
_RANK: Final = {ABSENT: 0, NULL: 1, EMPTY: 2, TEXT: 3}

SkPath = tuple[str, ...]
"""A path below the story root: masked keys, and ``[]`` for an array."""
Shape = tuple[tuple[tuple[str, str, tuple[str, ...]], ...], frozenset[str]]
"""``((text path, status, typenames there) per parser text path, attachment typenames)``."""

_NAME: Final = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]{0,127}")
_TEXT_LABEL: Final = re.compile(r"[A-Za-z_.()]{1,64}")


def safe_name(name: object, odd: str = KEY) -> str:
    """``name`` if it is a plain name, ``<id>`` if it looks like an id, else ``odd``."""
    if not isinstance(name, str):
        return odd
    if looks_like_id(name):
        return ID
    return name if _NAME.fullmatch(name) else odd


def text_label(label: object) -> str:
    """A text path as the parser names it (``content.sections.message``, ``(none)``)."""
    return label if isinstance(label, str) and _TEXT_LABEL.fullmatch(label) else "<other>"


# -- the parser's own text paths --------------------------------------------------------------


def _message_text(value: Any) -> bool:
    return text(dig(value, "text")) is not None


def _section_text(value: Any) -> bool:
    return text(dig(value, "story", "message", "text")) is not None


def _rich_text(value: Any) -> bool:
    return isinstance(value, list) and any(
        isinstance(block, Mapping) and text(block.get("text")) for block in value
    )


def _shared_text(value: Any) -> bool:
    return isinstance(value, Mapping) and own_text(value)[0] is not None


_C: Final = ("comet_sections", "content", "story")
_S: Final = ("comet_sections",)
_Known = tuple[str, tuple[tuple[str, ...], ...], Callable[[Any], bool]]
KNOWN_TEXT_PATHS: Final[tuple[_Known, ...]] = (
    ("content.message", ((*_C, "message"),), _message_text),
    ("content.sections.message", ((*_C, *_S, "message"),), _section_text),
    ("content.sections.message_container", ((*_C, *_S, "message_container"),), _section_text),
    (
        "content.sections.rich_message",
        ((*_C, *_S, "message", "rich_message"), (*_C, *_S, "message", "story", "rich_message")),
        _rich_text,
    ),
    ("message", (("message",),), _message_text),
    ("sections.message", ((*_S, "message"),), _section_text),
    ("sections.message_container", ((*_S, "message_container"),), _section_text),
    (
        "sections.rich_message",
        ((*_S, "message", "rich_message"), (*_S, "message", "story", "rich_message")),
        _rich_text,
    ),
    (
        "attached_story",
        (
            (*_C, "attached_story"),
            ("attached_story",),
            (*_C, *_S, "attached_story", "story", "attached_story"),
        ),
        _shared_text,
    ),
)
"""Where ``stories.own_text`` (and the share) looks, under the ``text_path`` names it reports:
``(label, the container at each place it may sit, whether a container has text)``."""
CLASS_NOTES: Final = {
    TextClass.ATTACHMENT_TEXT.value: " (the text of a link or listing card)",
    TextClass.SHARE_WITHOUT_COMMENT.value: " (shares a story, or an unavailable one)",
    TextClass.MEDIA_ONLY.value: " (a photo, a video)",
    TextClass.UNEXPLAINED.value: " (no message, shared story or known card)",
}
"""What each ``stories.TextClass`` means, after its name in the report's tables."""
TEXT_PATH_ORDER: Final = (
    *(label for label, _c, _t in KNOWN_TEXT_PATHS),
    ATTACHMENT_LISTING,
    ATTACHMENT_LINK,
)


def _containers(root: Mapping[str, Any], where: Iterable[tuple[str, ...]]) -> Iterator[Any]:
    """The value at each place of ``where`` whose key is there (``None`` included)."""
    for path in where:
        parent = dig(root, *path[:-1])
        if isinstance(parent, Mapping) and path[-1] in parent:
            yield parent[path[-1]]


def _status(
    root: Mapping[str, Any], containers: Iterable[tuple[str, ...]], has: Callable[[Any], bool]
) -> str:
    best = ABSENT
    for value in _containers(root, containers):
        status = NULL if value is None else TEXT if has(value) else EMPTY
        best = max(best, status, key=_RANK.__getitem__)
    return best


def known_paths(root: Mapping[str, Any]) -> dict[str, str]:
    """``label -> absent | null | empty | TEXT`` for each of the parser's text paths."""
    return {label: _status(root, where, has) for label, where, has in KNOWN_TEXT_PATHS}


def known_kinds(root: Mapping[str, Any]) -> dict[str, frozenset[str]]:
    """``label -> the typenames at that text path`` (a message section's renderer), masked like
    any typename."""
    return {
        label: frozenset(
            safe_name(value["__typename"], ODD)
            for value in _containers(root, where)
            if isinstance(value, Mapping) and value.get("__typename") is not None
        )
        for label, where, _has in KNOWN_TEXT_PATHS
    }


# -- skeletons ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Skeleton:
    """A story without its content: long strings (and texts) by path, and the typenames along
    the way."""

    lengths: Mapping[SkPath, tuple[int, int]]
    """Each path's shortest and longest string (a path through ``[]`` may hold several)."""
    typenames: Mapping[SkPath, frozenset[str]]
    """The typenames of the objects on the way to a long string, by their path (``()``: the
    root)."""
    attachments: frozenset[str] = frozenset()
    """Typenames under a key naming an attachment or media."""
    known: Mapping[str, str] = field(default_factory=dict)
    sections: Mapping[str, frozenset[str]] = field(default_factory=dict)
    """The typenames at each of the parser's text paths (``known_kinds``)."""
    deeper: int = 0
    """Strings that would be listed, more than ``MAX_DEPTH`` keys down (not listed)."""
    overflow: int = 0
    """Strings that would be listed, at paths beyond the first ``MAX_PATHS`` (not listed)."""
    typed: frozenset[SkPath] = frozenset()
    """Objects drawn for their typename alone: with no string below them (a ``style_infos``
    entry). Only the attachments drawing has them."""

    @property
    def shape(self) -> Shape:
        """What the stories of one shape have in common: how each of the parser's text paths
        was there, the typenames there, and the attachment typenames. Not which strings are
        long: a name one character longer is the same shape."""
        return (
            tuple(
                (label, self.known.get(label, ABSENT), tuple(sorted(self.sections.get(label, ()))))
                for label, _where, _has in KNOWN_TEXT_PATHS
            ),
            self.attachments,
        )


def _attachment_key(key: object) -> bool:
    lowered = key.lower() if isinstance(key, str) else ""
    return "attachment" in lowered or "media" in lowered


@dataclass
class _Strings:
    lengths: dict[SkPath, tuple[int, int]] = field(default_factory=dict)
    deeper: int = 0
    overflow: int = 0

    def add(self, path: SkPath, depth: int, value: str) -> None:
        size = len(value)
        if size < MIN_LENGTH and not (path[-1:] == (TEXT_KEY,) and text(value)):
            return
        if depth > MAX_DEPTH:
            self.deeper += 1
        elif path in self.lengths:
            low, high = self.lengths[path]
            self.lengths[path] = (min(low, size), max(high, size))
        elif len(self.lengths) >= MAX_PATHS:
            self.overflow += 1
        else:
            self.lengths[path] = (size, size)


def skeleton(root: Any) -> Skeleton:
    """The skeleton of ``root`` (a story, or a fragment's data). Never raises."""
    strings = _Strings()
    typenames: dict[SkPath, set[str]] = defaultdict(set)
    attachments: set[str] = set()
    visited = 0
    stack: list[tuple[SkPath, int, bool, Any]] = [((), 0, False, root)]
    while stack and visited < MAX_NODES:
        path, depth, attached, value = stack.pop()
        if isinstance(value, str):
            strings.add(path, depth, value)
            continue
        visited += 1
        children: list[tuple[SkPath, int, bool, Any]]
        if isinstance(value, Mapping):
            kind = value.get("__typename")
            if kind is not None and depth <= MAX_DEPTH:
                name = safe_name(kind, ODD)
                typenames[path].add(name)
                if attached:
                    attachments.add(name)
            children = [
                ((*path, safe_name(key)), depth + 1, attached or _attachment_key(key), child)
                for key, child in value.items()
                if key != "__typename"
            ]
        elif isinstance(value, list):
            children = [((*path, ARRAY), depth, attached, child) for child in value]
        else:
            continue
        stack.extend(reversed(children))
    lengths = strings.lengths
    on_the_way = {path[:end] for path in lengths for end in range(len(path))}
    story = isinstance(root, Mapping)
    return Skeleton(
        lengths=lengths,
        typenames={p: frozenset(n) for p, n in typenames.items() if p in on_the_way},
        attachments=frozenset(attachments),
        known=known_paths(root) if story else {},
        sections=known_kinds(root) if story else {},
        deeper=strings.deeper,
        overflow=strings.overflow,
    )


_ATTACHMENT_PLACES: Final[tuple[tuple[str, ...], ...]] = ((*_C, "attachments"), ("attachments",))
"""Where ``stories.attachment_text`` finds a story's cards."""
_SHARED_PLACES: Final[tuple[tuple[str, ...], ...]] = next(
    where for label, where, _has in KNOWN_TEXT_PATHS if label == "attached_story"
)
_FIRST_KEYS: Final = frozenset({"style_infos", *ATTACHMENT_TEXT_KEYS})
"""A path through one of these is drawn first: the parser's allow-listed keys, the style infos."""


def _attachment_lists(
    story: Mapping[str, Any], prefix: tuple[str, ...] = (), level: int = 0
) -> Iterator[tuple[tuple[str, ...], list[Any]]]:
    """``(path, attachments)`` for the story's lists and its shared stories' (as deep as the
    parser reads shared stories)."""
    for place in _ATTACHMENT_PLACES:
        found = dig(story, *place)
        if isinstance(found, list):
            yield (*prefix, *place), found
    if level < MAX_SHARE_DEPTH:
        for place in _SHARED_PLACES:
            shared = dig(story, *place)
            if isinstance(shared, Mapping):
                yield from _attachment_lists(shared, (*prefix, *place), level + 1)


def attachment_skeleton(story: Any) -> Skeleton | None:
    """The subtrees under the story's and its shared stories' ``attachments[]``: every string
    by path (its length, at any length), every typename, and the typed objects with no string
    below them. ``None`` when there is none. Never raises."""
    if not isinstance(story, Mapping):
        return None
    lengths: dict[SkPath, tuple[int, int]] = {}
    typenames: dict[SkPath, set[str]] = defaultdict(set)
    deeper = overflow = visited = 0
    stack: list[tuple[SkPath, int, Any]] = [
        (tuple(safe_name(k) for k in place), 0, value)
        for place, value in reversed(list(_attachment_lists(story)))
    ]
    while stack and visited < MAX_NODES:
        path, depth, value = stack.pop()
        if isinstance(value, str):
            if depth > ATTACHMENT_DEPTH:
                deeper += 1
            elif path in lengths:
                low, high = lengths[path]
                lengths[path] = (min(low, len(value)), max(high, len(value)))
            elif len(lengths) >= MAX_PATHS:
                overflow += 1
            else:
                lengths[path] = (len(value), len(value))
            continue
        visited += 1
        if isinstance(value, Mapping):
            kind = value.get("__typename")
            if kind is not None and depth <= ATTACHMENT_DEPTH:
                typenames[path].add(safe_name(kind, ODD))
            children = [
                ((*path, safe_name(key)), depth + 1, child)
                for key, child in value.items()
                if key != "__typename"
            ]
        elif isinstance(value, list):
            children = [((*path, ARRAY), depth, child) for child in value]
        else:
            continue
        stack.extend(reversed(children))
    if not lengths and not typenames:
        return None
    on_the_way = {path[:end] for path in lengths for end in range(len(path))}
    return Skeleton(
        lengths=lengths,
        typenames={p: frozenset(n) for p, n in typenames.items()},
        deeper=deeper,
        overflow=overflow,
        typed=frozenset(p for p in typenames if p not in on_the_way and p not in lengths),
    )


def _first(path: SkPath) -> int:
    return 0 if _FIRST_KEYS.intersection(path) else 1


# -- scripts ------------------------------------------------------------------------------------


class Script(enum.StrEnum):
    """The script of a text, by its letters (digits, signs and emoji do not count)."""

    CYRILLIC = "cyrillic"
    LATIN = "latin"
    MIXED = "mixed"
    OTHER = "other"
    """Neither a Cyrillic nor a Latin letter: another script, only digits or signs, no text."""


SCRIPT_LABELS: Final = {
    Script.CYRILLIC: "Cyrillic only",
    Script.LATIN: "Latin only",
    Script.MIXED: "Mixed (Cyrillic and Latin letters)",
    Script.OTHER: "Other or none (no Cyrillic or Latin letter, or no text)",
}


def script_of(value: str | None) -> Script:
    """Which of the two scripts the letters of ``value`` belong to: a letter is Cyrillic or
    Latin by its Unicode name (``CYRILLIC SMALL LETTER A``, ``LATIN CAPITAL LETTER E WITH
    ACUTE``); letters of other scripts and anything that is not a letter do not count. A
    styled letter counts as the plain one it stands for (``_letter_names``)."""
    cyrillic = latin = False
    for char in value or "":
        for words in _letter_names(char):
            cyrillic = cyrillic or "CYRILLIC" in words
            latin = latin or "LATIN" in words
        if cyrillic and latin:
            return Script.MIXED
    if cyrillic:
        return Script.CYRILLIC
    return Script.LATIN if latin else Script.OTHER


def _letter_names(char: str) -> list[list[str]]:
    """The words of the Unicode names of the letters ``char`` is. A letter is itself or, when
    it is a styled one, the letters of its compatibility form (NFKC: ``MATHEMATICAL BOLD
    CAPITAL P`` is ``P``); a symbol named as a letter is its own name (``CIRCLED LATIN CAPITAL
    LETTER A``). Other symbols are none, even where NFKC spells them in letters (``№``)."""
    if char.isalpha():
        return [
            unicodedata.name(letter, "").split()
            for letter in unicodedata.normalize("NFKC", char)
            if letter.isalpha()
        ]
    words = unicodedata.name(char, "").split()
    return [words] if unicodedata.category(char) == "So" and "LETTER" in words else []


# -- the saved steps ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sample:
    """One story with empty text, as far as the report may show it."""

    skeleton: Skeleton
    surface: str
    query: str
    """The GraphQL query that delivered it (``x-fb-friendly-name``), or ``ssr``."""
    highlight: bool = False
    unread_section: bool = False
    """It has a message section the parser could not read (deferred as ``no_text``)."""
    text_class: str | None = None
    """Why it has no text (``stories.TextClass``), when it has no message of its own."""
    attachments: Skeleton | None = None
    """Its attachments' subtrees (``attachment_skeleton``)."""


@dataclass
class FragmentStats:
    frames: int = 0
    at_empty: int = 0
    """Aimed at a story that came out with empty text in the same step."""
    lengths: dict[SkPath, tuple[int, int]] = field(default_factory=dict)


@dataclass
class RawScan:
    kept: int = 0
    """Saved steps in the report's window."""
    steps: int = 0
    unreadable: int = 0
    newest: float | None = None
    oldest: float | None = None
    stories: int = 0
    """Distinct posts read from those steps."""
    empty: int = 0
    classes: Counter[str] = field(default_factory=Counter)
    """Distinct posts without a message of their own (nor a shared text), by class."""
    raised: int = 0
    samples: list[Sample] = field(default_factory=list)
    fragments: dict[tuple[SkPath, str], FragmentStats] = field(default_factory=dict)
    scripts: Counter[str] = field(default_factory=Counter)
    """Distinct posts by the script of their authors' words (``Reading.words``, ``Script``)."""
    final_scripts: Counter[str] = field(default_factory=Counter)
    """Distinct posts by the script of their final text, card lines included."""
    translated: int = 0
    """Distinct posts whose tree holds a key that speaks of a translation."""
    translation_keys: Counter[str] = field(default_factory=Counter)
    """Those keys' names (masked like any key), by the distinct posts that hold them."""


def _in_window(path: Path, since: float | None) -> bool:
    stamp = path.name.split("-", 1)[0]
    return since is None or not stamp.isdigit() or int(stamp) + 1 >= since


def scan_raw(
    directory: Path,
    *,
    since: float | None = None,
    max_steps: int = MAX_STEPS,
    sample: int = SAMPLE_STORIES,
) -> RawScan:
    """Read the newest saved steps until ``sample`` distinct stories with empty text, or with
    only a card's text, are found (or ``max_steps`` steps are read). Unreadable files are
    counted, never fatal."""
    scan = RawScan()
    if not directory.is_dir():
        return scan
    files = sorted(
        (p for p in directory.glob(f"*{STEP_SUFFIX}") if _in_window(p, since)), reverse=True
    )
    scan.kept = len(files)
    seen: set[str] = set()
    for path in files[:max_steps]:
        if len(scan.samples) >= sample:
            break
        try:
            saved = load_step(path)
        except Exception:  # noqa: BLE001 - one damaged file never ends the report
            scan.unreadable += 1
            continue
        scan.steps += 1
        scan.newest = saved.at if scan.newest is None else max(scan.newest, saved.at)
        scan.oldest = saved.at if scan.oldest is None else min(scan.oldest, saved.at)
        _scan_step(saved, scan, seen, sample)
    return scan


def _query(frame: Frame) -> str:
    if frame.from_document:
        return SSR
    return safe_name(frame.friendly_name, ODD) if frame.friendly_name else NO_NAME


def _scan_step(saved: RawStep, scan: RawScan, seen: set[str], sample: int) -> None:
    if saved.surface is Surface.JOINS:
        return
    _values, frames = step_frames(saved.step)
    ctx = Context(page_group_id=saved.group_id if saved.surface is Surface.GROUP else None)
    at_empty: set[tuple[int, relay.Path]] = set()
    unknown: Counter[str] = Counter()
    stats = WalkStats()
    for frame in frames:
        for unit in find_units(frame, unknown=unknown, stats=stats):
            try:
                reading = read_story(unit, ctx)
            except Exception:  # noqa: BLE001 - the live read quarantines it; here it is counted
                scan.raised += 1
                continue
            if not isinstance(reading, Reading):
                continue
            story = reading.story
            empty = not story.text
            if empty and unit.edge is not None:
                at_empty.add((frame.body, unit.edge))
            if story.post_id in seen:
                continue
            seen.add(story.post_id)
            _count_post(scan, reading)
            if not empty and reading.text_class is not TextClass.ATTACHMENT_TEXT:
                continue
            scan.empty += empty
            root = story_root(unit)
            if len(scan.samples) < sample and root is not None:
                scan.samples.append(
                    Sample(
                        skeleton(root),
                        surface=saved.surface.value,
                        query=_query(frame),
                        highlight=unit.highlight,
                        unread_section=reading.text_bearing,
                        text_class=reading.text_class.value if reading.text_class else None,
                        attachments=attachment_skeleton(root),
                    )
                )
    for frame in frames:
        target = _fragment_target(frame)
        if target is None:
            continue
        edge, below = target
        label = safe_name(frame.label, ODD) if frame.label else NO_NAME
        group = scan.fragments.setdefault((below, label), FragmentStats())
        group.frames += 1
        group.at_empty += (frame.body, edge) in at_empty
        _merge(group.lengths, skeleton(frame.data).lengths)


def _count_post(scan: RawScan, reading: Reading) -> None:
    """A distinct post's counts: its script, its translation keys, its text class."""
    scan.stories += 1
    scan.scripts[script_of(reading.words).value] += 1
    scan.final_scripts[script_of(reading.story.text).value] += 1
    if reading.translation_keys:
        scan.translated += 1
        scan.translation_keys.update({safe_name(k) for k in reading.translation_keys})
    if reading.text_class is not None:
        scan.classes[reading.text_class.value] += 1


def _fragment_target(frame: Frame) -> tuple[relay.Path, SkPath] | None:
    """``(the edge's path, the path below the story)`` for a part aimed inside a story."""
    path = frame.path
    if path is None:
        return None
    for k in range(1, len(path) - 2):
        if (
            path[k] == "edges"
            and path[k - 1] in KNOWN_CONNECTIONS
            and isinstance(path[k + 1], int)
            and path[k + 2] == "node"
        ):
            below = tuple(ARRAY if isinstance(p, int) else safe_name(p) for p in path[k + 3 :])
            return path[: k + 2], below
    return None


def _merge(into: dict[SkPath, tuple[int, int]], more: Mapping[SkPath, tuple[int, int]]) -> None:
    for path, (low, high) in more.items():
        if path in into:
            old_low, old_high = into[path]
            into[path] = (min(old_low, low), max(old_high, high))
        elif len(into) < MAX_PATHS:
            into[path] = (low, high)


# -- rendering ----------------------------------------------------------------------------------


def _segments(path: SkPath) -> list[tuple[str, int]]:
    """``(label, end)`` per displayed step: a key with the ``[]`` that follow it."""
    out: list[tuple[str, int]] = []
    for index, part in enumerate(path):
        if part == ARRAY and out:
            out[-1] = (out[-1][0] + ARRAY, index + 1)
        else:
            out.append((part, index + 1))
    return out


def dotted(path: SkPath) -> str:
    return ".".join(label for label, _end in _segments(path)) or "(the story node)"


def _names(counts: Counter[str] | None, limit: int = MAX_NAMES) -> str:
    if not counts:
        return ""
    ranked = sorted(counts, key=lambda n: (-counts[n], n))
    more = f"|+{len(ranked) - limit}" if len(ranked) > limit else ""
    return " <" + "|".join(ranked[:limit]) + more + ">"


def _span(low: int, high: int) -> str:
    return str(low) if low == high else f"{low}-{high}"


def _counts(counter: Counter[str], limit: int = 5) -> str:
    ranked = sorted(counter, key=lambda n: (-counter[n], n))
    shown = ", ".join(f"{n} {counter[n]}" for n in ranked[:limit])
    return (shown + (f", +{len(ranked) - limit} more" if len(ranked) > limit else "")) or "-"


@dataclass
class _Leaf:
    low: int | None
    """``None``: an object drawn for its typename alone (``Skeleton.typed``)."""
    high: int
    stories: int = 0
    shapes: set[str] = field(default_factory=set)


def _shapes(samples: Sequence[Sample]) -> list[tuple[str, list[Sample]]]:
    """Samples grouped by shape: the most frequent first, then the newest; lettered A-Z."""
    groups: dict[Shape, list[Sample]] = {}
    for item in samples:
        groups.setdefault(item.skeleton.shape, []).append(item)
    ordered = sorted(groups.values(), key=lambda members: -len(members))  # stable: newest first
    return [(chr(ord("A") + i), members) for i, members in enumerate(ordered[:26])]


def _tree(shapes: Sequence[tuple[str, list[Sample]]], total: int) -> tuple[list[str], int]:
    """One tree for all shapes, and how many paths did not fit in ``MAX_TREE_LINES`` lines:
    the paths most stories share are drawn first."""
    members = [(letter, item.skeleton) for letter, items in shapes for item in items]
    return _draw(members, total, budget=MAX_TREE_LINES)


def _draw(
    members: Sequence[tuple[str, Skeleton]],
    total: int,
    *,
    budget: int,
    first: Callable[[SkPath], int] = lambda _path: 0,
) -> tuple[list[str], int]:
    """One tree for ``(shape letter, skeleton)`` pairs in at most ``budget`` lines, and how
    many paths did not fit: the ``first`` ones first, then those most skeletons share."""
    leaves: dict[SkPath, _Leaf] = {}
    names: dict[SkPath, Counter[str]] = defaultdict(Counter)
    for letter, drawn in members:
        for path, (low, high) in drawn.lengths.items():
            leaf = leaves.setdefault(path, _Leaf(low, high))
            leaf.low = low if leaf.low is None else min(leaf.low, low)
            leaf.high = max(leaf.high, high)
            leaf.stories += 1
            leaf.shapes.add(letter)
        for path in drawn.typed:
            leaf = leaves.setdefault(path, _Leaf(None, 0))
            leaf.stories += 1
            leaf.shapes.add(letter)
        for prefix, kinds in drawn.typenames.items():
            names[prefix].update(kinds)
    order = {path: i for i, path in enumerate(leaves)}
    nodes: set[SkPath] = set()
    chosen: set[SkPath] = set()
    for path in sorted(leaves, key=lambda p: (first(p), -leaves[p].stories, order[p])):
        new = {path[:end] for _label, end in _segments(path)} - nodes
        if len(nodes) + len(new) + 1 > budget:
            continue
        nodes |= new
        chosen.add(path)
    children: dict[SkPath, dict[SkPath, str]] = defaultdict(dict)
    for path in leaves:
        if path not in chosen:
            continue
        parent: SkPath = ()
        for label, end in _segments(path):
            children[parent].setdefault(path[:end], label)
            parent = path[:end]
    everyone = {letter for letter, _m in members}
    lines = [f"(story){_names(names.get(()))}"]
    stack = [(node, label, 1) for node, label in reversed(children[()].items())]
    while stack:
        node, label, level = stack.pop()
        line = "  " * level + label + _names(names.get(node))
        shown = leaves.get(node) if node in chosen else None
        if shown is not None:
            span = "" if shown.low is None else f": {_span(shown.low, shown.high)}"
            line += f"{span} · {shown.stories}/{total}"
            if shown.shapes != everyone:
                line += " · " + ",".join(sorted(shown.shapes))
        lines.append(line)
        stack.extend((c, lab, level + 1) for c, lab in reversed(children[node].items()))
    return lines, len(leaves) - len(chosen)


def _attachment_rows(shapes: Sequence[tuple[str, list[Sample]]]) -> list[str]:
    """For each shape, its attachments drawn apart (``ATTACHMENT_LINES`` lines at most)."""
    out = [
        "",
        "#### The attachments of each shape",
        "",
        (
            f"Every string under the story's and its shared story's `attachments[]`, at any"
            f" length and at most {ATTACHMENT_DEPTH} keys below the list, and every typename"
            " there, as `key <typename>: length (shortest-longest) · stories with it`; an object"
            " without a string below it shows its typename alone. The parser's keys and"
            " `style_infos` are drawn first."
        ),
    ]
    bare: list[str] = []
    for letter, members in shapes:
        drawn = [(letter, m.attachments) for m in members if m.attachments is not None]
        if not drawn:
            bare.append(letter)
            continue
        lines, left_out = _draw(drawn, len(members), budget=ATTACHMENT_LINES, first=_first)
        hidden = sum(item.deeper + item.overflow for _l, item in drawn)
        out += ["", f"Shape {letter}:", "", "```text", *lines, "```"]
        if left_out:
            out.append(
                f"{left_out} more path(s) not drawn: a shape's drawing is capped at"
                f" {ATTACHMENT_LINES} lines."
            )
        if hidden:
            out.append(f"{hidden} string(s) deeper or beyond the path cap, not listed.")
    if bare:
        out += ["", f"No attachments in shape(s) {', '.join(bare)}."]
    return out


def _when(epoch: float | None, zone: dt.tzinfo) -> str:
    return (
        "-" if epoch is None else dt.datetime.fromtimestamp(epoch, zone).strftime("%Y-%m-%d %H:%M")
    )


def _shape_rows(shapes: Sequence[tuple[str, list[Sample]]]) -> list[str]:
    out = [
        (
            "| Shape | Stories | Unread message section | Surface | Query | Root |"
            " Attachments and media | Strings listed | Class |"
        ),
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for letter, members in shapes:
        surfaces = Counter(m.surface for m in members)
        surfaces.update("highlight" for m in members if m.highlight)
        roots = Counter(n for m in members for n in m.skeleton.typenames.get((), ()))
        attachments = Counter(n for m in members for n in m.skeleton.attachments)
        listed = [len(m.skeleton.lengths) for m in members]
        deeper = sum(m.skeleton.deeper + m.skeleton.overflow for m in members)
        more = f" (+{deeper} not listed)" if deeper else ""
        classes = Counter(text_label(m.text_class) for m in members if m.text_class)
        out.append(
            f"| {letter} | {len(members)} | {sum(m.unread_section for m in members)}"
            f" | {_counts(surfaces)} | {_counts(Counter(m.query for m in members), 3)}"
            f" | {_counts(roots, 3)} | {_counts(attachments)}"
            f" | {_span(min(listed), max(listed))}{more} | {_counts(classes)} |"
        )
    return out


def _known_rows(shapes: Sequence[tuple[str, list[Sample]]]) -> list[str]:
    letters = [letter for letter, _m in shapes]
    out = [
        "| Parser text path | " + " | ".join(letters) + " |",
        "|---|" + "---|" * len(letters),
    ]
    for label, _where, _has in KNOWN_TEXT_PATHS:
        cells = []
        for _letter, members in shapes:
            first = members[0].skeleton  # a shape's stories agree here: it is the shape
            status = first.known.get(label, ABSENT)
            cells.append(status + _names(Counter(first.sections.get(label, ()))))
        out.append(f"| `{label}` | " + " | ".join(cells) + " |")
    return out


def _fragment_rows(fragments: Mapping[tuple[SkPath, str], FragmentStats]) -> list[str]:
    if not fragments:
        return ["No `@defer` part in these steps is aimed inside a story."]
    out = [
        "| Below the story node | Label | Parts | At a story with empty text | Longest strings |",
        "|---|---|---|---|---|",
    ]
    ranked = sorted(fragments.items(), key=lambda kv: (-kv[1].at_empty, -kv[1].frames))
    for (below, label), stats in ranked[:MAX_FRAGMENTS]:
        longest = sorted(stats.lengths.items(), key=lambda kv: -kv[1][1])[:FRAGMENT_PATHS]
        shown = ", ".join(f"`{dotted(p)}` {_span(*span)}" for p, span in longest) or "-"
        out.append(
            f"| `{dotted(below)}` | `{label}` | {stats.frames} | {stats.at_empty} | {shown} |"
        )
    if len(ranked) > MAX_FRAGMENTS:
        out.append(f"| … {len(ranked) - MAX_FRAGMENTS} more kinds | | | | |")
    return out


def _share(part: int, whole: int) -> str:
    return "-" if not whole else f"{100 * part / whole:.1f} %"


def render_scripts(scan: RawScan | None) -> list[str]:
    """The report's lines about the script of the posts' texts: counts and shares only."""
    out = ["### The script of the post texts in the saved payloads", ""]
    if scan is None:
        return [*out, "The saved payloads were not read."]
    if not scan.stories:
        return [*out, "No post in the saved steps read."]
    total = scan.stories
    out += [
        (
            f"The {total} distinct posts of the saved steps read above, by the letters of their"
            " authors' words (the message and the shared texts), and of their final text (the"
            " card lines too: a link's title, description and source, a listing's lines)."
            " Digits, signs and emoji do not count; a styled letter counts as the plain one."
            " Facebook's translation of a Cyrillic post would make its authors' words Latin"
            " only, whatever its card says; a Cyrillic post with a Latin brand is mixed, and a"
            " post with a card alone has no words of its authors."
        ),
        "",
        "| Script | Authors' words | Share | Final text | Share |",
        "|---|---|---|---|---|",
    ]
    for kind in Script:
        words, final = scan.scripts.get(kind.value, 0), scan.final_scripts.get(kind.value, 0)
        out.append(
            f"| {SCRIPT_LABELS[kind]} | {words} | {_share(words, total)}"
            f" | {final} | {_share(final, total)} |"
        )
    ranked = sorted(scan.translation_keys.items(), key=lambda kv: (-kv[1], kv[0]))
    keys = ", ".join(f"`{name}` {count}" for name, count in ranked[:MAX_KEY_NAMES])
    if len(ranked) > MAX_KEY_NAMES:
        keys += f", +{len(ranked) - MAX_KEY_NAMES} more"
    out += [
        f"| **all** | {total} | 100.0 % | {total} | 100.0 % |",
        "",
        (
            f"- With a key that speaks of a translation (`{TRANSLATION_MARK}` in its name,"
            f" whatever it holds) anywhere in the story: {scan.translated} of {total}"
            f" ({_share(scan.translated, total)})" + (f"; keys: {keys}." if keys else ".")
        ),
    ]
    return out


def render_scan(scan: RawScan | None, *, zone: dt.tzinfo) -> list[str]:
    """The report's lines about the saved steps (structure only)."""
    out = ["### Stories with empty text in the saved payloads", ""]
    if scan is None:
        return [*out, "The saved payloads were not read."]
    if not scan.kept:
        return [
            *out,
            "No saved payloads in this period (kept 48 h, or 21 days in observe mode).",
        ]
    samples = scan.samples
    out += [
        (
            f"- Saved steps read: {scan.steps} of {scan.kept}, newest first"
            f" ({_when(scan.oldest, zone)} → {_when(scan.newest, zone)}); unreadable:"
            f" {scan.unreadable}."
        ),
        (
            f"- Distinct posts in them: {scan.stories}, with empty text: {scan.empty}"
            f"; stories whose reading raised: {scan.raised}."
        ),
        (
            "- Without a message of their own (nor a shared text), distinct posts: "
            + ", ".join(f"`{kind.value}` {scan.classes.get(kind.value, 0)}" for kind in TextClass)
            + " (`attachment_text`: the text of a link or listing card; `unexplained`: what"
            " the empty-text alert counts)."
        ),
    ]
    if samples:
        shapes = _shapes(samples)
        drawn = sum(len(members) for _letter, members in shapes)
        out += [
            (
                f"- Drawn below: the {drawn} newest with empty text or with only a card's text"
                f" (`attachment_text`), in {len(shapes)} shape(s) (a shape: the same status and"
                " typenames at each parser text path below, and the same attachment typenames;"
                " string lengths do not count)."
            ),
            "",
            *_shape_rows(shapes),
            "",
            (
                "Where the parser looks for text (`absent`: no such key; `null`; `empty`: there,"
                " without text; `TEXT`: there, with text; `<…>`: the typenames there):"
            ),
            "",
            *_known_rows(shapes),
            "",
            (
                f"Every string of {MIN_LENGTH}+ characters and every non-blank `{TEXT_KEY}`, at"
                f" most {MAX_DEPTH} keys deep, as"
                " `key <typename>: length (shortest-longest) · stories with it · shapes`"
                " (shapes only when not all of them):"
            ),
            "",
            "```text",
        ]
        lines, left_out = _tree(shapes, drawn)
        out += [*lines, "```"]
        if left_out:
            out.append(
                f"{left_out} rarer path(s) not drawn: the tree is capped at {MAX_TREE_LINES} lines."
            )
        out += _attachment_rows(shapes)
    else:
        out.append("- No story with empty text in these steps.")
    out += ["", "#### `@defer` parts aimed inside a story", "", *_fragment_rows(scan.fragments)]
    return out
