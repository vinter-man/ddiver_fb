"""Find the stories in Relay frames and read each one's fields from its own root.

Two rules keep one post from turning into several posts, or into the wrong one:

- **Edge-level detection.** A story is ``edges[i].node`` of a known feed connection
  (``news_feed``, ``group_feed``, ...), the ``data.node`` of a ``@stream`` part whose path ends
  in such an edge, or the story of a ``highlight_units`` element. The walk never descends into a
  story once found: one post carries ``post_id`` at four or more nested places (verified in the
  2026-09-24 group capture), and a shared post carries a second post's id and time.
- **Scoped fields.** Every field is read at named paths relative to the story root and never
  inside ``attached_story``, except the shared post's text, which is appended on its own line.
  A share's time is the share's, not the original's (DESIGN, Parser step 3).

Attachments add match text of their own, each piece on its own line after the story's words: a
listing card's title, price and location, then a link's title, description and source (the
source only beside a title or a description), whether or not the story has a message of its
own (the owner's decision of 2026-09-30: a rule then fires on a word only the link's title
has). Both come from the story's attachments and from its shared story's. A card line that is
already in the text (equal to it or inside it as whole words, read as the brain's matcher reads
it: case-insensitively, spaces and newlines as they are, ``_new_lines``) is left out. They are
read under a short allow-list of keys by a bounded walk of each attachment, never from media,
buttons or the shared story's own sections, and a string that reads as a link, an id or a
token is never match text (``matchable``). A story without a message of its own (nor a shared
text) is classified for the health counters (``TextClass``): the empty-text alert counts only
the posts nothing explains.

The health counters also learn, per story, whether a card added lines beside the story's own
message (``Reading.card_beside_words``) and which keys of its tree speak of a translation
(``translation_keys``: key names only, never what they hold), to tell whether Facebook sent a
post as written or translated.

What is not a post is dropped with a reason: section headers, ads, comments, units without a
post id (events, reels, suggestion cards). A post-shaped node without a usable post id is dropped
too, but counted as a failure, because that is what a Facebook rename looks like from here.

Paths come from the 2026-09-24 captures and from reading other projects' parsers: Groups
Watcher (ideas only, unlicensed) for the feed's ``news_feed`` stream label and listing cards
(``attachments[].styles.attachment.title_with_entities`` and the target's price and location),
MEOMcGill/fbscrape (MIT snapshot 7fca6ca, ideas) for dispatching the timestamp by its
``*TimestampStrategy`` typename and the out-of-order anchor story, adbertram/cli-tools (MIT) for
the ``rich_message`` fallback.
"""

from __future__ import annotations

import enum
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from fbcollector.model import AuthorRef, DeferredReason, ListingRef, ParsedStory
from fbcollector.relay import (
    Frame,
    Key,
    WalkStats,
    dig,
    digit_id,
    epoch,
    facebook_url,
    looks_like_id,
    mapping,
    text,
    typed_id,
    walk,
)

STORY_CONNECTIONS: Final = frozenset({"news_feed", "group_feed", "cross_group_feed"})
"""Connections whose edges are feed stories: the Groups feed (``viewer.news_feed``), a group's
own feed and the older cross-group feed. Anything else holding stories is counted, not read."""
HIGHLIGHTS: Final = "highlight_units"
KNOWN_CONNECTIONS: Final = STORY_CONNECTIONS | {HIGHLIGHTS}

MAX_SHARE_DEPTH: Final = 3
"""How many levels of ``attached_story`` contribute text (a share of a share of a post)."""

ATTACHMENT_LINK: Final = "attachment.link"
"""``text_path`` of a story whose only text is a link's title or description (and source)."""
ATTACHMENT_LISTING: Final = "attachment.listing"
"""``text_path`` of a story whose only text is a listing card."""
MAX_ATTACHMENTS: Final = 10
"""Attachments read per list: a post has one or two (an album is one, its photos inside)."""
MAX_ATTACHMENT_DEPTH: Final = 10
"""How many keys below an attachment the node whose keys are read may sit. The deepest in the
research shapes is ``styles.attachment.comet_footer_renderer.attachment`` (4 keys; its
``title_with_entities.text`` is read there)."""
MAX_ATTACHMENT_NODES: Final = 2_000
MAX_TOKEN: Final = 20
"""A string without spaces this long (ASCII) is a token, an id or a link, not a title."""
MAX_BARE_NUMBER: Final = 5
"""A bare number up to this many digits may be a price (``1200``); a longer one is an id."""
MIN_TYPED_ID: Final = 8
"""From this length a one-word string that is base64 of ``type:number`` is an opaque id, even
one too short for ``looks_like_id`` (``UzpfSTA6MA``, ``S:_I0:0`` without its padding)."""
TRANSLATION_MARK: Final = "translat"
"""A key whose name holds this, in any case, speaks of a translation (``translation``,
``translated_message``, ``translatability_for_viewer``, ``isTranslatable``)."""
MAX_TRANSLATION_KEYS: Final = 8
"""Distinct such key names kept per story: enough to tell them apart, never a growing list."""

_CONTEXT_STORY: Final = ("comet_sections", "context_layout", "story")
_TITLE_STORY: Final = (*_CONTEXT_STORY, "comet_sections", "title", "story")
_METADATA: Final = (*_CONTEXT_STORY, "comet_sections", "metadata")
_CONTENT_STORY: Final = ("comet_sections", "content", "story")
_SPONSORED_KEYS: Final = ("sponsored_data", "th_dat_spo")
"""``th_dat_spo`` is the obfuscated name the Groups feed query asks for
(``shouldChangeSponsoredDataFieldName``)."""
_LISTING_KEYS: Final = frozenset(
    {"formatted_price", "marketplace_listing_title", "location_text", "listing_price"}
)
_ANONYMOUS_NAME: Final = re.compile(r"anonymous (participant|member)\b", re.IGNORECASE)
_PERMALINK_PATHS: Final = frozenset({"permalink", "permalink.slug"})


class DropReason(enum.StrEnum):
    """Why a unit found at edge level did not become a story."""

    SECTION_HEADER = "section_header"
    SPONSORED = "sponsored"
    COMMENT = "comment"
    NON_STORY = "non_story"  # events, reels, suggestion cards: no post id and not post-shaped
    DUPLICATE = "duplicate"  # the same post earlier in the read (first position wins)
    NO_POST_ID = "no_post_id"  # post-shaped but no usable post id: a parser failure
    QUARANTINED = "quarantined"  # reading it raised; kept raw, a parser failure

    @property
    def by_design(self) -> bool:
        """Dropped on purpose (not a post), as opposed to a post the parser failed to read."""
        return self not in {DropReason.NO_POST_ID, DropReason.QUARANTINED}


@dataclass(frozen=True, slots=True)
class Unit:
    """One edge-level node of a known connection, before it is read."""

    node: Mapping[str, Any]
    connection: str
    frame: Frame
    cursor: str | None = None
    edge: tuple[Key, ...] | None = None
    """Where the edge sits in its response (``(..., "news_feed", "edges", 3)``): a ``@defer``
    part that patches this story later names the same path, then ``node``."""

    @property
    def highlight(self) -> bool:
        return self.connection == HIGHLIGHTS


def story_root(unit: Unit) -> Mapping[str, Any] | None:
    """The story a unit carries: the node itself, or a highlight unit's ``story``."""
    return mapping(unit.node.get("story")) if unit.highlight else unit.node


@dataclass(frozen=True, slots=True)
class Context:
    """What the read knows beyond the story itself."""

    page_group_id: str | None = None
    """The group page being read: every story on it belongs to this group."""
    slugs: Mapping[str, str] = field(default_factory=dict)
    """Vanity slug -> numeric group id, to cross-check permalinks like ``/groups/<slug>/``."""


class TextClass(enum.StrEnum):
    """Why a story without a message of its own (and without a shared text) has the text it
    has. The empty-text alert counts only ``UNEXPLAINED`` posts."""

    ATTACHMENT_TEXT = "attachment_text"  # its text comes from a card: not empty any more
    SHARE_WITHOUT_COMMENT = "share_without_comment"  # shares a story, or an unavailable one
    MEDIA_ONLY = "media_only"  # a photo, a video, an album: no words to read
    UNEXPLAINED = "unexplained"  # no message, no shared story, no card we know


@dataclass(frozen=True, slots=True)
class Reading:
    """A story as read, plus what the health counters want to know about the reading."""

    story: ParsedStory
    group_conflict: bool = False
    text_bearing: bool = False
    text_class: TextClass | None = None
    """``None`` when the story has a message of its own or a shared story's text."""
    card_beside_words: bool = False
    """The story has a message of its own and its text also got a card's lines (a link's or
    a listing's)."""
    words: str = ""
    """The words the story's authors wrote: its message and the shared texts, without the
    card lines (``story.text`` up to them). Empty for a story whose only text is a card's."""
    translation_keys: frozenset[str] = frozenset()
    """The names of the keys in the story's tree, its shared story's too, that speak of a
    translation (``translation_keys``), whatever they hold; empty when there is none."""


def typename(value: Any) -> str | None:
    name = dig(value, "__typename")
    return name if isinstance(name, str) else None


def story_like(node: Any) -> bool:
    return isinstance(node, Mapping) and (
        "post_id" in node or "comet_sections" in node or typename(node) == "Story"
    )


def _is_connection(value: Any) -> bool:
    return isinstance(value, Mapping) and isinstance(value.get("edges"), list)


def find_units(frame: Frame, *, unknown: Counter[str], stats: WalkStats) -> list[Unit]:
    """Every edge-level unit of a known connection in ``frame``, in document order.

    Edges of other connections that look like stories are counted in ``unknown`` by connection
    key (a phase-0 inventory of where else posts show up) and not read.
    """
    data = frame.data
    if isinstance(data, Mapping):
        edge_of = _stream_connection(frame)
        if edge_of is not None:
            node = mapping(data.get("node"))
            if edge_of in KNOWN_CONNECTIONS:
                cursor = text(data.get("cursor"))
                return [Unit(node, edge_of, frame, cursor, frame.path)] if node else []
            if story_like(node):
                unknown[edge_of] += 1
            return []

    units: list[Unit] = []

    def stop(key: Key, value: Any) -> bool:
        return isinstance(key, str) and _is_connection(value)

    base = frame.path or ()
    for node_path, node in walk(data, stop=stop, stats=stats, include_stopped=True):
        key = node_path[-1] if node_path else None
        if not isinstance(key, str) or not _is_connection(node):
            continue
        if key in KNOWN_CONNECTIONS:
            for index, edge in enumerate(node["edges"]):
                inner = mapping(dig(edge, "node"))
                if inner is not None:
                    where = (*base, *node_path, "edges", index)
                    units.append(Unit(inner, key, frame, text(edge.get("cursor")), where))
        else:
            count = sum(1 for e in node["edges"] if story_like(dig(e, "node")))
            if count:
                unknown[key] += count
    return units


def _stream_connection(frame: Frame) -> str | None:
    """The connection a ``@stream`` part is one edge of, or ``None`` if it is not one."""
    path = frame.path
    if path is not None:
        if len(path) >= 3 and path[-2] == "edges" and isinstance(path[-1], int):
            conn = path[-3]
            return conn if isinstance(conn, str) else None
        return None
    label = frame.label or ""
    data = frame.data
    if "$stream$" in label and isinstance(data, Mapping) and isinstance(data.get("node"), Mapping):
        # A stream part that lost its path: its label still names the connection
        # (``CometNewsFeed_viewerConnection$stream$CometNewsFeed_viewer_news_feed``).
        return next((c for c in sorted(STORY_CONNECTIONS) if label.endswith(c)), None)
    return None


def read_story(unit: Unit, ctx: Context) -> Reading | DropReason:
    """Read one unit's fields from its own root, or say why it is not a post.

    Position, batch and the anchor flag are the caller's: they depend on the read, not on
    the story.
    """
    node = unit.node
    kind = typename(node) or ""
    if "SectionHeader" in kind:
        return DropReason.SECTION_HEADER
    if kind == "Comment":
        return DropReason.COMMENT
    root = story_root(unit)
    if root is None:
        return DropReason.NON_STORY  # a highlight without a story: events, "all caught up"
    # A GroupHighlightPostUnit's id is its post id.
    hint = digit_id(node.get("id")) if unit.highlight else None
    if _sponsored(root):
        return DropReason.SPONSORED
    content = mapping(dig(root, *_CONTENT_STORY)) or {}
    post_id = (
        digit_id(root.get("post_id"))
        or digit_id(content.get("post_id"))
        or digit_id(dig(root, *_TITLE_STORY, "post_id"))
        or hint
    )
    if post_id is None:
        return DropReason.NO_POST_ID if story_like(root) else DropReason.NON_STORY

    created_at = _created_at(root)
    group_id, candidates, conflict = _group(root, content, post_id, ctx)
    own, own_path, bearing = own_text(root)
    shared = _shared_texts(root, content, own)
    found = attachment_text(root, content)
    shares = bool(_attached_level(root, content))
    composed = _compose(own, own_path, shared, found, shares=shares)

    if created_at is None:
        reason: DeferredReason | None = DeferredReason.NO_CREATED_AT
    elif group_id is None:
        reason = DeferredReason.NO_GROUP
    elif bearing and own is None:
        reason = DeferredReason.NO_TEXT
    else:
        reason = None
    story = ParsedStory(
        post_id=post_id,
        group_id=group_id,
        created_at=created_at,
        url=f"https://www.facebook.com/groups/{group_id}/posts/{post_id}/" if group_id else None,
        text="\n".join(composed.parts),
        text_path=composed.text_path,
        author=_author(root, content),
        listing=found.listing,
        is_share=shares,
        is_highlight=unit.highlight,
        cursor=unit.cursor,
        friendly_name=unit.frame.friendly_name,
        group_candidates=candidates,
        deferred_reason=reason,
    )
    return Reading(
        story,
        group_conflict=conflict,
        text_bearing=bearing,
        text_class=composed.text_class,
        card_beside_words=own is not None and composed.card_lines > 0,
        words="\n".join(composed.parts[: len(composed.parts) - composed.card_lines]),
        translation_keys=translation_keys(root),
    )


def translation_keys(root: Mapping[str, Any]) -> frozenset[str]:
    """The names of the keys under ``root`` (its shared story included) that hold
    ``TRANSLATION_MARK``, at most ``MAX_TRANSLATION_KEYS`` of them. Only the names: whether
    Facebook sent a translation-related field at all, never what it says."""
    found: set[str] = set()
    for _path, node in walk(root):
        for key in node:
            if isinstance(key, str) and TRANSLATION_MARK in key.lower():
                found.add(key)
                if len(found) >= MAX_TRANSLATION_KEYS:
                    return frozenset(found)
    return frozenset(found)


@dataclass(frozen=True, slots=True)
class _Composed:
    parts: list[str]
    text_path: str | None
    text_class: TextClass | None
    card_lines: int
    """How many of ``parts`` are a card's lines."""


def _compose(
    own: str | None,
    own_path: str | None,
    shared: list[str],
    found: AttachmentText,
    *,
    shares: bool,
) -> _Composed:
    """The story's text: its own message, the shared texts, then the listing lines, then the
    link lines, each card line only when the text does not hold it yet (``_new_lines``). The
    text path names where the first words came from, a card only when there are no others,
    and the class is ``None`` whenever the story has a message or a shared text."""
    parts = [t for t in (own, *shared) if t]
    listing_lines = _new_lines(parts, found.listing_lines)
    link_lines = _new_lines(parts + listing_lines, found.link_lines)
    if own_path is not None:
        text_path: str | None = own_path
    elif shared:
        text_path = "attached_story"
    elif listing_lines:
        text_path = ATTACHMENT_LISTING
    else:
        text_path = ATTACHMENT_LINK if link_lines else None
    if own or shared:
        text_class: TextClass | None = None
    elif listing_lines or link_lines:
        text_class = TextClass.ATTACHMENT_TEXT
    elif found.unavailable or shares:
        text_class = TextClass.SHARE_WITHOUT_COMMENT
    else:
        text_class = TextClass.MEDIA_ONLY if found.media else TextClass.UNEXPLAINED
    card = listing_lines + link_lines
    return _Composed(parts + card, text_path, text_class, len(card))


def _sponsored(root: Mapping[str, Any]) -> bool:
    if "Sponsored" in (typename(root) or ""):
        return True
    for _path, node in walk(root, stop=lambda key, _v: key == "attached_story", max_depth=16):
        for key in _SPONSORED_KEYS:
            value = node.get(key)
            if value is not None and value is not False and value != {}:
                return True
    return False


def _created_at(root: Mapping[str, Any]) -> int | None:
    """The story's own creation time: the node, then its timestamp strategies."""
    found = epoch(root.get("creation_time"))
    if found is not None:
        return found
    metadata = [m for m in (dig(root, *_METADATA) or []) if isinstance(m, Mapping)]
    # Dispatch on the typename suffix (Minimized / Longer / Backdated ...TimestampStrategy),
    # then on any strategy that carries a time at all, so a rename degrades, not breaks.
    ordered = [m for m in metadata if (typename(m) or "").endswith("TimestampStrategy")]
    ordered += [m for m in metadata if m not in ordered]
    for strategy in ordered:
        found = epoch(dig(strategy, "story", "creation_time"))
        if found is not None:
            return found
    return epoch(dig(root, "comet_sections", "timestamp", "story", "creation_time"))


def _post_urls(root: Mapping[str, Any], content: Mapping[str, Any]) -> list[Any]:
    urls: list[Any] = [root.get("permalink_url")]
    urls += [
        dig(m, "story", "url")
        for m in (dig(root, *_METADATA) or [])
        if (typename(m) or "").endswith("TimestampStrategy")
    ]
    urls += [
        dig(root, "comet_sections", "timestamp", "story", "url"),
        content.get("wwwURL"),
        root.get("wwwURL"),
        root.get("url"),
    ]
    return urls


def _group(
    root: Mapping[str, Any], content: Mapping[str, Any], post_id: str, ctx: Context
) -> tuple[str | None, tuple[tuple[str, str], ...], bool]:
    """``(group id, every candidate as (path, id), conflict)``.

    Every candidate is recorded (phase 0 picks the authoritative path). A group page names its
    own group; elsewhere the story's group fields must agree with each other and with its
    permalink, or the group is unresolved and the story waits (deferred) instead of being
    filed under the wrong group.
    """
    candidates: list[tuple[str, str]] = []
    for name, value in (
        ("to", root.get("to")),
        ("target_group", root.get("target_group")),
        ("content.target_group", content.get("target_group")),
        ("title.to", dig(root, *_TITLE_STORY, "to")),
        ("feedback.associated_group", dig(root, "feedback", "associated_group")),
    ):
        gid = _group_ref(value)
        if gid is not None:
            candidates.append((name, gid))
    for url in _post_urls(root, content):
        parsed = facebook_url(url)
        if parsed is None or parsed.group_ref is None or parsed.post_ref is None:
            continue  # not a group post's URL (a group, a profile, another site)
        linked_post = digit_id(parsed.post_ref)
        if linked_post is not None and linked_post != post_id:
            continue  # another post's URL (a share's original)
        if digit_id(parsed.group_ref):
            candidates.append(("permalink", parsed.group_ref))
        elif parsed.group_ref in ctx.slugs:
            candidates.append(("permalink.slug", ctx.slugs[parsed.group_ref]))
    unique = tuple(dict.fromkeys(candidates))
    named = {gid for path, gid in unique if path not in _PERMALINK_PATHS}
    linked = {gid for path, gid in unique if path in _PERMALINK_PATHS}
    if ctx.page_group_id is not None:
        chosen: str | None = ctx.page_group_id
    elif len(named) == 1:
        chosen = next(iter(named))
    elif not named and len(linked) == 1:
        chosen = next(iter(linked))
    else:
        return None, unique, len(named) > 1 or len(linked) > 1
    if linked - {chosen}:
        return None, unique, True
    return chosen, unique, False


def _group_ref(value: Any) -> str | None:
    """The id of a group reference; ``to`` can also be a user (a timeline post)."""
    group = mapping(value)
    if group is None:
        return None
    kind = typename(group)
    parsed = facebook_url(group.get("url"))
    is_group_url = parsed is not None and parsed.group_ref is not None
    if kind not in {None, "Group"} and not is_group_url:
        return None
    return digit_id(group.get("id"))


def own_text(story: Mapping[str, Any]) -> tuple[str | None, str | None, bool]:
    """``(text, path, text-bearing)`` of a story's own message, never inside attached_story.

    Text-bearing means a message section exists (non-null); a photo-only post has none and
    legitimately has no text, while a present section we cannot read means a renderer we do
    not know, and the story is deferred rather than sent without its words.
    """
    bearing = False
    content = mapping(dig(story, *_CONTENT_STORY))
    for prefix, base in (("content.", content), ("", story)):
        if base is None:
            continue
        message = base.get("message")
        bearing = bearing or message is not None
        found = text(dig(message, "text"))
        if found:
            return found, f"{prefix}message", True
        sections = mapping(base.get("comet_sections"))
        if sections is None:
            continue
        for name in ("message", "message_container"):
            section = sections.get(name)
            if section is None:
                continue
            bearing = True
            found = text(dig(section, "story", "message", "text")) or _deep_message_text(section)
            if found:
                return found, f"{prefix}sections.{name}", True
        blocks = dig(sections, "message", "rich_message") or dig(
            sections, "message", "story", "rich_message"
        )
        found = _rich_text(blocks)
        if found:
            return found, f"{prefix}sections.rich_message", True
    return None, None, bearing


def _deep_message_text(section: Any) -> str | None:
    for _path, node in walk(section, stop=lambda key, _v: key == "attached_story", max_depth=12):
        found = text(dig(node, "message", "text"))
        if found:
            return found
    return None


def _rich_text(blocks: Any) -> str | None:
    if not isinstance(blocks, list):
        return None
    lines = [b["text"] for b in blocks if isinstance(b, Mapping) and isinstance(b.get("text"), str)]
    return text("\n".join(lines))


def _attached_level(root: Mapping[str, Any], content: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The directly shared story, under each name it is known by (most complete first)."""
    return [
        m
        for m in (
            content.get("attached_story"),
            root.get("attached_story"),
            dig(content, "comet_sections", "attached_story", "story", "attached_story"),
        )
        if isinstance(m, Mapping)
    ]


def _shared_texts(
    root: Mapping[str, Any], content: Mapping[str, Any], own: str | None
) -> list[str]:
    """Texts of the shared story (and what it shares, up to ``MAX_SHARE_DEPTH``), each once."""
    seen = {own} if own else set()
    texts: list[str] = []
    level = _attached_level(root, content)
    for _ in range(MAX_SHARE_DEPTH):
        if not level:
            break
        found = next((t for t, _p, _b in (own_text(s) for s in level) if t), None)
        if found and found not in seen:
            seen.add(found)
            texts.append(found)
        level = [
            nested
            for shared in level
            for nested in _attached_level(shared, mapping(dig(shared, *_CONTENT_STORY)) or {})
        ]
    return texts


# -- attachments ---------------------------------------------------------------------------------

_LISTING_MARKS: Final = ("CommerceProductItem", "MarketplaceListing")
"""A typename holding one of these marks a listing card: the target (``GroupCommerceProductItem``,
``MarketplaceListing``), its style info (``CommerceProductItemStyleInfo``) or renderer."""
_LISTING_TARGETS: Final = frozenset(
    {"GroupCommerceProductItem", "MarketplaceListing", "CommerceProductItem"}
)
_MEDIA: Final = re.compile(r"Photo|Video|Album|Reel|Sticker|Gif|Animated")
"""Typenames of a photo, video or album card (its renderer or target): no words to read."""
_UNAVAILABLE: Final = "Unavailable"
"""``StoryAttachmentUnavailableStyleRenderer``: a share of a post that is gone or private. Its
title and description are Facebook's own apology, never match text."""
_UNREAD_KEYS: Final = frozenset(
    {
        "media", "web_link", "actors", "feedback", "comet_sections", "all_subattachments",
        "attached_story",
    }
)  # fmt: skip
"""Subtrees of an attachment whose strings are never match text: the picture (a photo's
generated caption), the link itself, people, reactions, a story's own sections and an album's
photos. Buttons too (a key with a word ``cta`` or starting with ``action``)."""


def _plain_or_text(value: Any) -> Any:
    return dig(value, "text") if isinstance(value, Mapping) else value


def _text_only(value: Any) -> Any:
    return dig(value, "text")


def _formatted_amount(value: Any) -> Any:
    return dig(value, "formatted_amount")


_FIELDS: Final = (
    ("title", "marketplace_listing_title", _plain_or_text),
    ("title", "title_with_entities", _text_only),
    ("title", "title", _text_only),
    ("price", "formatted_price", _plain_or_text),
    ("price", "listing_price", _formatted_amount),
    ("location", "location_text", _plain_or_text),
    ("description", "description", _text_only),
    ("source", "source", _text_only),
)
"""The allow-list: ``(field, key, how the key holds its string)``, best first per field. Every
other key of an attachment (``url``, ``id``, ``web_link``, ``tracking``, ...) is never read."""
ATTACHMENT_TEXT_KEYS: Final = frozenset(key for _name, key, _get in _FIELDS)
LINK_FIELDS: Final = ("title", "description", "source")
_SCHEME: Final = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:|www\.)")
_PATH: Final = re.compile(r"^/|//|\.[^\W\d_]{2,}/")
"""A ``/`` that makes a path: at the start, doubled, or after a domain (``imot.bg/``,
``имот.бг/``)."""
_ASCII_TOKEN: Final = re.compile(r"[\x21-\x7e]+")
_JOINED_WORDS: Final = re.compile(r"[A-Za-z0-9]+(?:[-+][A-Za-z0-9]+)+")
"""Words joined by ``-`` or ``+`` (``Mercedes-Benz``, ``Wi-Fi``, ``2+1``), which
``relay.looks_like_id`` takes for base64 punctuation."""
_LONG_NUMBER: Final = re.compile(rf"[0-9]{{{MAX_BARE_NUMBER + 1},}}")


def matchable(value: Any) -> str | None:
    """``value`` as a line of match text, or ``None``: blank, a link, an id or a token.

    A string with words in it is text. A string without spaces is one word, a price or a
    site's name (``IMOT.BG``), unless it holds a scheme or ``www.``, holds a ``/`` and is all
    ASCII or a path (``_PATH``; ``€500/month`` is a price), is a bare number longer than a
    price, reads as an id (``relay.looks_like_id``, or base64 of a typed id) or is an ASCII run
    of ``MAX_TOKEN`` or more characters (a tracking token). Words joined by ``-`` or ``+`` are
    not an id for their punctuation alone, unless a part is a number longer than a price.
    """
    found = _line(value)
    if found is None or any(c.isspace() for c in found):
        return found
    if _SCHEME.search(found) or ("/" in found and (found.isascii() or _PATH.search(found))):
        return None
    if found.isdigit():
        return found if len(found) <= MAX_BARE_NUMBER else None
    if _ASCII_TOKEN.fullmatch(found) and (
        len(found) >= MAX_TOKEN
        or (looks_like_id(found) and not _joined_words(found))
        or (len(found) >= MIN_TYPED_ID and typed_id(found))
    ):
        return None
    return found


def _joined_words(found: str) -> bool:
    return bool(_JOINED_WORDS.fullmatch(found)) and not _LONG_NUMBER.search(found)


def _unread(key: str) -> bool:
    words = key.lower().split("_")
    return key in _UNREAD_KEYS or "cta" in words or any(w.startswith("action") for w in words)


def _listing_mark(kind: str | None) -> bool:
    return kind is not None and any(mark in kind for mark in _LISTING_MARKS)


@dataclass
class _Card:
    """What one attachment offers: its best string per field, and what kind of card it is."""

    best: dict[str, tuple[int, str]] = field(default_factory=dict)
    """``field -> (rank in the allow-list, string)``: the best key, first found."""
    ids: list[tuple[int, int, str]] = field(default_factory=list)
    """Listing id candidates: ``(0 a listing target, 1 under "target", 2 other, order, id)``."""
    listing: bool = False
    unavailable: bool = False
    media: bool = False

    def visit(self, key: Key | None, node: Mapping[str, Any], readable: bool) -> None:
        kind = typename(node)
        if kind is not None:
            self.unavailable = self.unavailable or _UNAVAILABLE in kind
            self.listing = self.listing or _listing_mark(kind)
        if not readable:
            return
        for rank, (name, holder_key, get) in enumerate(_FIELDS):
            if holder_key not in node:
                continue
            found = matchable(get(node[holder_key]))
            if found is not None and (name not in self.best or rank < self.best[name][0]):
                self.best[name] = (rank, found)
        keyed = _is_listing(node)
        if keyed or kind in _LISTING_TARGETS:
            self.listing = True
            raw_id = node.get("id")
            found_id = (
                raw_id if isinstance(raw_id, str) and 0 < len(raw_id) <= 64 else digit_id(raw_id)
            )
            if found_id is not None:
                rank = 0 if kind in _LISTING_TARGETS else 1 if key == "target" else 2
                self.ids.append((rank, len(self.ids), found_id))

    def text(self, name: str) -> str | None:
        found = self.best.get(name)
        return found[1] if found else None

    def listing_ref(self) -> ListingRef | None:
        if not self.listing:
            return None
        ref = ListingRef(
            id=min(self.ids)[2] if self.ids else None,
            title=self.text("title"),
            price=self.text("price"),
            location=self.text("location"),
        )
        return ref if ref.title or ref.price or ref.location else None

    def link_lines(self) -> list[str]:
        """The card's title, description and source; none without a title or a description.
        A site's name alone (``IMOT.BG``) is not the post's words: a link card whose title and
        description the parser missed stays ``UNEXPLAINED``, a gap that should alert."""
        if self.text("title") is None and self.text("description") is None:
            return []
        return [t for t in (self.text(name) for name in LINK_FIELDS) if t]


def _card(attachment: Mapping[str, Any]) -> _Card:
    """One attachment read by a bounded walk (``MAX_ATTACHMENT_DEPTH`` keys down,
    ``MAX_ATTACHMENT_NODES`` containers). Strings are read only outside ``_unread`` subtrees,
    typenames everywhere, to tell a listing or an unavailable share. A photo or a video says so
    in its own renderer or target, not in a link's preview picture."""
    kinds = (
        typename(attachment.get("styles")),
        typename(attachment.get("style_type_renderer")),
        typename(attachment.get("target")),
    )
    card = _Card(media=any(_MEDIA.search(kind) for kind in kinds if kind))
    stack: list[tuple[Key | None, Any, int, bool]] = [(None, attachment, 0, True)]
    visited = 0
    while stack and visited < MAX_ATTACHMENT_NODES:
        key, value, depth, readable = stack.pop()
        visited += 1
        children: list[tuple[Key | None, Any, int, bool]]
        if isinstance(value, Mapping):
            card.visit(key, value, readable)
            if depth >= MAX_ATTACHMENT_DEPTH:
                continue
            children = [
                (k, child, depth + 1, readable and not _unread(k))
                for k, child in value.items()
                if isinstance(child, Mapping | list) and k != "attached_story"
            ]
        else:  # a list: only containers are ever pushed
            children = [
                (key, child, depth, readable)
                for child in value
                if isinstance(child, Mapping | list)
            ]
        stack.extend(reversed(children))
    return card


@dataclass(frozen=True, slots=True)
class AttachmentText:
    """The match text a story's attachments offer: its own and its shared story's."""

    listing: ListingRef | None = None
    """The first listing card: the story's own, else any in the story, else the shared
    story's (the wire item's ``listing``)."""
    listing_lines: tuple[str, ...] = ()
    """Every listing card's title, price and location, in that order."""
    link_lines: tuple[str, ...] = ()
    """Every card's title, description and source."""
    unavailable: bool = False
    """A card shows a shared post that is gone or private."""
    media: bool = False
    """A card is a photo, a video or an album."""


def _attachments(story: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """A story's attachments where the parser finds them: its content section's, its own."""
    content = mapping(dig(story, *_CONTENT_STORY)) or {}
    found: list[Mapping[str, Any]] = []
    for base in (content, story):
        listed = base.get("attachments")
        if isinstance(listed, list):
            found += [a for a in listed[:MAX_ATTACHMENTS] if isinstance(a, Mapping)]
    return found


def attachment_text(root: Mapping[str, Any], content: Mapping[str, Any]) -> AttachmentText:
    """What the story's attachments, and its shared story's (``MAX_SHARE_DEPTH`` levels), offer
    as match text. An unavailable card offers nothing."""
    own = [_card(a) for a in _attachments(root)]
    shared: list[_Card] = []
    level = _attached_level(root, content)
    for _ in range(MAX_SHARE_DEPTH):
        if not level:
            break
        shared += [_card(a) for story in level for a in _attachments(story)]
        level = [
            nested
            for story in level
            for nested in _attached_level(story, mapping(dig(story, *_CONTENT_STORY)) or {})
        ]
    listings, links = _read_cards(own)
    listings = listings or _listing_anywhere(root)
    shared_listings, shared_links = _read_cards(shared)
    listings += shared_listings
    return AttachmentText(
        listing=listings[0] if listings else None,
        listing_lines=tuple(line for ref in listings for line in ref.lines()),
        link_lines=(*links, *shared_links),
        unavailable=any(card.unavailable for card in own + shared),
        media=any(card.media for card in own + shared),
    )


def _read_cards(cards: Iterable[_Card]) -> tuple[list[ListingRef], list[str]]:
    """The listing cards and the link lines of ``cards``; an unavailable card has neither."""
    listings: list[ListingRef] = []
    links: list[str] = []
    for card in cards:
        if card.unavailable:
            continue
        ref = card.listing_ref()
        if ref is not None:
            listings.append(ref)
        links += card.link_lines()
    return listings, links


def _listing_anywhere(root: Mapping[str, Any]) -> list[ListingRef]:
    """A listing card elsewhere in the story, outside its attachments' usual places (never in
    the shared story): the first node holding a listing key."""
    for _path, node in walk(root, stop=lambda key, _v: key == "attached_story", max_depth=16):
        if _is_listing(node):
            card = _Card()
            card.visit(None, node, readable=True)
            ref = card.listing_ref()
            return [ref] if ref is not None else []
    return []


def shadow(value: str) -> str:
    """``value`` as the brain's matcher reads it (ddiver_core's ``prepare_text``): without
    invisible format characters (``Cf``: a zero-width space, a BOM), each base character
    NFC-normalized with the combining marks that follow it. Whitespace and newlines stay as
    they are: the matcher never collapses them."""
    if unicodedata.is_normalized("NFC", value) and not any(
        unicodedata.category(ch) == "Cf" for ch in value
    ):
        return value
    units: list[str] = []
    for ch in value:
        category = unicodedata.category(ch)
        if category == "Cf":
            continue
        if category[0] == "M" and units:
            units[-1] += ch
        else:
            units.append(ch)
    return "".join(unicodedata.normalize("NFC", unit) for unit in units)


def _fold_char(char: str) -> str:
    """``char`` in lower case where that is one character, else itself. Two characters this
    makes equal are one to the matcher too: a rule's character that matches one of them under
    ``IGNORECASE`` matches the other. So ``ß`` never becomes ``ss``, and ``I`` stays ``I``,
    since a rule's dotted ``İ`` matches ``i`` and not ``I`` (``test_the_folding_is_sound``)."""
    lower = char.lower()
    return lower if len(lower) == 1 and char != "I" else char


def folded(value: str) -> str:
    """``value`` as the card lines' dedupe compares it: its ``shadow``, each character
    folded (``_fold_char``). One character stays one, so positions keep their meaning."""
    return "".join(map(_fold_char, shadow(value)))


def _wordy(char: str) -> bool:
    """Whether the matcher's ``\\w`` may take ``char`` for part of a word: a letter, a mark, a
    digit or number, ``_`` and the other connectors, a symbol named as a letter (``Ⓐ``), and a
    character this Python does not know yet (the brain's ``regex`` knows a newer Unicode).
    Wider than the matcher's ``\\w`` on purpose: wider only keeps a line
    (``test_wordy_holds_every_word_character_of_the_matcher``)."""
    category = unicodedata.category(char)
    return (
        category[0] in "LMN"
        or category in {"Pc", "Cn"}
        or (category == "So" and "LETTER" in unicodedata.name(char, "").split())
    )


def contained(line: str, text: str) -> bool:
    """Whether ``line`` is already in ``text``: equal to it or inside it, compared ``folded``
    with every space and newline as it is, and as whole words (not ``car`` in ``carpet``), so
    a substring or word rule that fires on the line fires on the text. A line with nothing
    left once folded counts as contained."""
    needle = folded(line).strip()
    if not needle:
        return True
    seen = shadow(text)
    hay = "".join(map(_fold_char, seen))
    at = hay.find(needle)
    while at >= 0:
        end = at + len(needle)
        if (at == 0 or not _wordy(seen[at - 1])) and (end == len(seen) or not _wordy(seen[end])):
            return True
        at = hay.find(needle, at + 1)
    return False


def _new_lines(parts: Iterable[str], lines: Iterable[str]) -> list[str]:
    """``lines`` that are not already in ``parts`` (nor in the lines kept before them)."""
    kept = list(parts)
    out: list[str] = []
    for line in lines:
        if not contained(line, "\n".join(kept)):
            kept.append(line)
            out.append(line)
    return out


def _is_listing(node: Mapping[str, Any]) -> bool:
    return not _LISTING_KEYS.isdisjoint(node.keys())


def _line(value: Any) -> str | None:
    found = text(value)
    return found.strip() if found else None


def _author(root: Mapping[str, Any], content: Mapping[str, Any]) -> AuthorRef:
    # The same actor appears in several sections, not all of which select its name (a
    # highlight's content section has only the id); the first one with a name wins.
    actors = [
        found
        for found in (
            dig(root, "actors", 0),
            dig(content, "actors", 0),
            dig(root, *_CONTEXT_STORY, "actors", 0),
            dig(root, *_TITLE_STORY, "actors", 0),
            dig(root, "feedback", "owning_profile"),
        )
        if isinstance(found, Mapping)
    ]
    actor = next((a for a in actors if text(a.get("name"))), actors[0] if actors else None)
    if actor is None:
        return AuthorRef()
    name = _line(actor.get("name"))
    if _anonymous(actor, name):
        # An anonymous participant has a per-group pseudonym; its id and link would identify
        # nobody, and linking them would be wrong, so only the name travels.
        return AuthorRef(name=name or "Anonymous participant", anonymous=True)
    author_id = digit_id(actor.get("id"))
    link = facebook_url(actor.get("url")) or facebook_url(actor.get("profile_url"))
    if link is not None:
        url: str | None = link.canonical
    elif author_id is not None:
        url = f"https://www.facebook.com/profile.php?id={author_id}"
    else:
        url = None
    return AuthorRef(id=author_id, name=name, url=url)


def _anonymous(actor: Mapping[str, Any], name: str | None) -> bool:
    if "anon" in (typename(actor) or "").lower():
        return True
    if actor.get("is_anonymous") is True or actor.get("anonymous") is True:
        return True
    return bool(name and _ANONYMOUS_NAME.match(name))
