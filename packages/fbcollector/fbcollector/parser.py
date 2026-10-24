"""Turn what one driver step captured into stories, page facts and health counters.

Pure functions over bytes (DESIGN, Parser): no browser types, no I/O, no clock, so the same code
runs on a live step, on a fixture and on ``fbcollector replay`` over raw payloads.

A read is one navigation plus its scroll steps. ``ReadParser`` carries what must continue
across the steps of one read: positions (SSR first, then responses in arrival order), batch
numbers (0 for the navigation's HTML, then one per GraphQL body that held stories) and the
post ids already seen, because the same post often arrives twice and the first position wins.

The first story of every GraphQL batch is marked as the batch's *anchor*: Facebook injects one
out-of-order story at the head of each pagination batch (observed by MEOMcGill/fbscrape), so the
stop rules and coverage skip it, and the novelty check still sees it.

Nothing a story contains can end a read. Each story is read inside its own ``try``; one that
raises is quarantined with its raw bytes, logged by post id only (invariant 8), and counted, so
a Facebook change shows up as a ``PARSER_BROKEN`` verdict instead of a crash loop.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field, replace
from typing import Any, Final, Protocol

from fbcollector.frames import FrameStats
from fbcollector.groupmeta import PreloaderFacts, page_group, preloader_facts
from fbcollector.joins import JoinedGroups, joined_groups
from fbcollector.model import DomFacts, GraphqlBody, GroupMeta, ParsedStory, Surface
from fbcollector.relay import (
    Frame,
    WalkStats,
    body_frames,
    dig,
    document_frames,
    document_values,
    integer,
    mapping,
    text,
    walk,
)
from fbcollector.stories import (
    ATTACHMENT_LINK,
    ATTACHMENT_LISTING,
    STORY_CONNECTIONS,
    Context,
    DropReason,
    Reading,
    TextClass,
    Unit,
    find_units,
    read_story,
)

log = logging.getLogger(__name__)

RATE_LIMIT_CODES: Final = frozenset({1675004})
"""Facebook's account-scoped throttle (``severity: CRITICAL``); a human in a browser gets it too
(fbscrape's notes). The detectors treat it as BLOCKED."""
MAX_QUARANTINE_BYTES: Final = 256 * 1024
MAX_ERROR_SUMMARY: Final = 200
MAX_DEFINE_SCAN: Final = 2_000_000
NO_NAME: Final = "(none)"
NOT_OWN_TEXT: Final = frozenset({None, "attached_story", ATTACHMENT_LISTING, ATTACHMENT_LINK})
"""Text paths that are not the story's own message: with a message section there, the story
lacks its author's words (``missing.no_text``)."""

_USER_ID: Final = re.compile(rb'"USER_ID"\s*:\s*"([0-9]{1,20})"')


class StepLike(Protocol):
    """What the parser needs from a driver step (``model.Step`` is one)."""

    @property
    def document(self) -> bytes | None: ...

    @property
    def graphql(self) -> Sequence[GraphqlBody]: ...

    @property
    def dom(self) -> DomFacts: ...

    @property
    def lost_bodies(self) -> int: ...


@dataclass(frozen=True, slots=True)
class PageInfo:
    """The main connection's pagination state, from the ``@defer`` part whose path ends in the
    connection key (``news_feed``, ``group_feed``, the joined-groups connection)."""

    connection: str
    has_next_page: bool | None
    end_cursor: str | None


@dataclass(frozen=True, slots=True)
class SessionMarkers:
    """What the page says about the session, for the detectors: ``USER_ID``, whether queries that
    select ``viewer`` got one, and the UI locale. (The ``c_user`` cookie is the driver's.)"""

    user_id: str | None = None
    viewer_present: int = 0
    viewer_null: tuple[str, ...] = ()
    """Friendly names of the frames whose ``data.viewer`` was ``null``."""
    locale: str | None = None
    """``IntlCurrentLocale`` of the SSR (``en_US``); the detectors want an English UI."""

    @property
    def logged_in(self) -> bool | None:
        """``False`` for ``USER_ID "0"``, ``None`` when the page did not say."""
        return None if self.user_id is None else self.user_id != "0"

    def merged(self, other: SessionMarkers) -> SessionMarkers:
        return SessionMarkers(
            user_id=self.user_id if self.user_id is not None else other.user_id,
            viewer_present=self.viewer_present + other.viewer_present,
            viewer_null=self.viewer_null + other.viewer_null,
            locale=self.locale or other.locale,
        )


@dataclass(frozen=True, slots=True)
class GraphqlError:
    """An error Facebook put inside a response body (a 200 can still say "rate limited")."""

    code: int | None
    summary: str | None
    severity: str | None = None
    friendly_name: str | None = None
    partial: bool = False
    """A field-level error (it names a ``path``): the rest of the response is fine. Logged-out
    and member pages alike carry a few ``field_exception`` ones as a matter of course."""

    @property
    def rate_limited(self) -> bool:
        return self.code in RATE_LIMIT_CODES or "rate limit" in (self.summary or "").lower()


@dataclass(frozen=True, slots=True)
class Quarantined:
    """A story whose reading raised. Its raw JSON goes to ``raw/`` for replay after a fix."""

    post_id: str | None
    error: str
    raw: bytes
    friendly_name: str | None = None


@dataclass(frozen=True, slots=True)
class PostMark:
    """How one read saw one post: what the verdict counts once per post over its window."""

    accepted: bool
    no_time: bool = False
    text_class: TextClass | None = None

    def as_json(self) -> list[Any]:
        return [int(self.accepted), int(self.no_time), self.text_class]

    @classmethod
    def from_json(cls, raw: Any) -> PostMark | None:
        """The mark ``as_json`` wrote, or ``None`` for anything else."""
        if not isinstance(raw, list) or len(raw) != 3:
            return None
        accepted, no_time, kind = raw
        known = kind is None or kind in TextClass.__members__.values()
        if accepted not in (0, 1) or no_time not in (0, 1) or not known:
            return None
        return cls(bool(accepted), bool(no_time), None if kind is None else TextClass(kind))


@dataclass(frozen=True, slots=True)
class ParseHealth:
    """Per-step (and, merged, per-read) counters behind ``PARSER_BROKEN`` and the phase-0 report.

    ``units`` are the edge-level nodes found; ``found`` those that should have been posts (units
    minus headers, ads, comments, non-post cards and duplicates); ``accepted`` and ``deferred``
    split the posts that were read; the rest of ``found`` failed (``no_post_id``,
    ``quarantined``).
    """

    units: int = 0
    found: int = 0
    accepted: int = 0
    deferred: int = 0
    dropped: Mapping[str, int] = field(default_factory=dict)
    missing: Mapping[str, int] = field(default_factory=dict)
    """``no_created_at`` / ``no_group`` / ``no_text``, each counted on its own."""
    empty_text: int = 0
    group_conflicts: int = 0
    text_paths: Mapping[str, int] = field(default_factory=dict)
    friendly_names: Mapping[str, int] = field(default_factory=dict)
    unknown_connections: Mapping[str, int] = field(default_factory=dict)
    frames: int = 0
    undecodable: int = 0
    capped_bodies: int = 0
    lost_bodies: int = 0
    skipped_nodes: int = 0
    page_info: bool = False
    dom_posts: int = 0
    steps: int = 1
    story_bodies: int = 0
    """GraphQL bodies that carried at least one story (pagination responses)."""
    batch_stories: int = 0
    """Regular stories in those bodies (not the SSR's, not highlights)."""
    text_classes: Mapping[str, int] = field(default_factory=dict)
    """Stories without a message of their own (nor a shared text), by ``TextClass``."""
    card_beside_words: int = 0
    """Stories with a message of their own whose text also got a card's lines (a link's or a
    listing's): what the owner's decision of 2026-09-30 adds (the text path stays the
    message's)."""
    translation_fields: int = 0
    """Stories whose tree holds a key that speaks of a translation (``stories.
    translation_keys``), whatever it holds."""
    posts: Mapping[str, PostMark] = field(default_factory=dict)
    """Post id -> how this read saw it, for the verdict's distinct counts. Not in
    ``as_stats``: the ids stay in the reader's parser history."""

    @property
    def failures(self) -> int:
        """Stories that should have been posts and could not be read (no post id, raised)."""
        return self.dropped.get(DropReason.NO_POST_ID.value, 0) + self.dropped.get(
            DropReason.QUARANTINED.value, 0
        )

    def merged(self, other: ParseHealth) -> ParseHealth:
        """This read's counters plus a later step's (``dom_posts`` keeps the maximum)."""

        def add(a: Mapping[str, int], b: Mapping[str, int]) -> dict[str, int]:
            return dict(Counter(a) + Counter(b))

        return ParseHealth(
            units=self.units + other.units,
            found=self.found + other.found,
            accepted=self.accepted + other.accepted,
            deferred=self.deferred + other.deferred,
            dropped=add(self.dropped, other.dropped),
            missing=add(self.missing, other.missing),
            empty_text=self.empty_text + other.empty_text,
            group_conflicts=self.group_conflicts + other.group_conflicts,
            text_paths=add(self.text_paths, other.text_paths),
            friendly_names=add(self.friendly_names, other.friendly_names),
            unknown_connections=add(self.unknown_connections, other.unknown_connections),
            frames=self.frames + other.frames,
            undecodable=self.undecodable + other.undecodable,
            capped_bodies=self.capped_bodies + other.capped_bodies,
            lost_bodies=self.lost_bodies + other.lost_bodies,
            skipped_nodes=self.skipped_nodes + other.skipped_nodes,
            page_info=self.page_info or other.page_info,
            dom_posts=max(self.dom_posts, other.dom_posts),
            steps=self.steps + other.steps,
            story_bodies=self.story_bodies + other.story_bodies,
            batch_stories=self.batch_stories + other.batch_stories,
            text_classes=add(self.text_classes, other.text_classes),
            card_beside_words=self.card_beside_words + other.card_beside_words,
            translation_fields=self.translation_fields + other.translation_fields,
            posts={**self.posts, **other.posts},
        )

    def as_stats(self) -> dict[str, Any]:
        """JSON-able, for ``ReadRecord.stats``."""
        return {
            "units": self.units,
            "found": self.found,
            "accepted": self.accepted,
            "deferred": self.deferred,
            "dropped": dict(self.dropped),
            "missing": dict(self.missing),
            "empty_text": self.empty_text,
            "group_conflicts": self.group_conflicts,
            "text_paths": dict(self.text_paths),
            "friendly_names": dict(self.friendly_names),
            "unknown_connections": dict(self.unknown_connections),
            "frames": self.frames,
            "undecodable": self.undecodable,
            "capped_bodies": self.capped_bodies,
            "lost_bodies": self.lost_bodies,
            "skipped_nodes": self.skipped_nodes,
            "page_info": self.page_info,
            "dom_posts": self.dom_posts,
            "steps": self.steps,
            "story_bodies": self.story_bodies,
            "batch_stories": self.batch_stories,
            "text_classes": dict(self.text_classes),
            "card_beside_words": self.card_beside_words,
            "translation_fields": self.translation_fields,
        }


@dataclass(frozen=True, slots=True)
class StepParse:
    """Everything one step yielded."""

    stories: tuple[ParsedStory, ...] = ()
    health: ParseHealth = field(default_factory=ParseHealth)
    group: GroupMeta | None = None
    """Group surface: the page's own group."""
    joined: JoinedGroups | None = None
    """Joins surface: the Group-shaped dicts found."""
    preloader: PreloaderFacts = field(default_factory=PreloaderFacts)
    page_info: PageInfo | None = None
    markers: SessionMarkers = field(default_factory=SessionMarkers)
    errors: tuple[GraphqlError, ...] = ()
    quarantined: tuple[Quarantined, ...] = ()
    next_position: int = 0
    next_batch: int = 1


def parse_step(
    step: StepLike,
    *,
    surface: Surface,
    group_id: str | None = None,
    slugs: Mapping[str, str] | None = None,
    start_position: int = 0,
    next_batch: int = 1,
    seen: AbstractSet[str] = frozenset(),
    connections: AbstractSet[str] = frozenset(),
) -> StepParse:
    """Parse one step. ``seen`` are post ids found earlier in the same read (dedupe).

    ``group_id`` is the group page being read (``None`` for a vanity URL: the preloader's
    ``groupID`` is used). ``slugs`` maps known vanity slugs to ids for permalink checks.
    ``connections`` are the joined-groups connection keys learned from earlier steps of the
    read, so the last page's ``page_info`` is recognised even when it arrives without groups.
    """
    frame_stats = FrameStats()
    walk_stats = WalkStats()
    values, frames = step_frames(step, frame_stats, walk_stats)
    friendly = Counter(body.friendly_name or NO_NAME for body in step.graphql)
    capped = sum(body.capped for body in step.graphql)

    preloader = preloader_facts(values, walk_stats)
    group: GroupMeta | None = None
    joined: JoinedGroups | None = None
    ctx_group: str | None = None
    slug_map = dict(slugs or {})
    if surface is Surface.GROUP:
        group = page_group(frames, group_id=group_id, preloader=preloader, stats=walk_stats)
        ctx_group = group.id if group is not None else group_id
        if group is not None and group.slug:
            slug_map.setdefault(group.slug, group.id)
    elif surface is Surface.JOINS:
        joined = joined_groups(frames, walk_stats)

    collected = _collect(
        frames,
        Context(page_group_id=ctx_group, slugs=slug_map),
        start_position=start_position,
        next_batch=next_batch,
        seen=seen,
        stats=walk_stats,
    )
    if joined is not None:
        learned = frozenset(connections) | frozenset(joined.connections)
        page_info = _page_info(frames, lambda key: key in learned or _groups_key(key), walk_stats)
    else:
        page_info = _page_info(frames, STORY_CONNECTIONS.__contains__, walk_stats)
    health = replace(
        collected.health,
        friendly_names=dict(friendly),
        frames=frame_stats.frames,
        undecodable=frame_stats.undecodable,
        capped_bodies=capped,
        lost_bodies=step.lost_bodies,
        skipped_nodes=walk_stats.too_deep + walk_stats.truncated,
        page_info=page_info is not None,
        dom_posts=step.dom.posinset_count,
    )
    return StepParse(
        stories=collected.stories,
        health=health,
        group=group,
        joined=joined,
        preloader=preloader,
        page_info=page_info,
        markers=_markers(step.document, values, frames, walk_stats),
        errors=_errors(frames),
        quarantined=collected.quarantined,
        next_position=collected.next_position,
        next_batch=collected.next_batch,
    )


def step_frames(
    step: StepLike, frame_stats: FrameStats | None = None, walk_stats: WalkStats | None = None
) -> tuple[list[Any], list[Frame]]:
    """The SSR's JSON values, and every frame of the step: the SSR's, then each body's in
    arrival order (the report's diagnostics read a saved step the same way)."""
    frame_stats = frame_stats if frame_stats is not None else FrameStats()
    walk_stats = walk_stats if walk_stats is not None else WalkStats()
    values = document_values(step.document, frame_stats) if step.document else []
    frames = document_frames(values, walk_stats)
    for index, body in enumerate(step.graphql):
        frames.extend(body_frames(body, index, frame_stats))
    return values, frames


@dataclass(frozen=True, slots=True)
class _Collected:
    stories: tuple[ParsedStory, ...]
    quarantined: tuple[Quarantined, ...]
    health: ParseHealth
    next_position: int
    next_batch: int


def _collect(
    frames: Sequence[Frame],
    ctx: Context,
    *,
    start_position: int,
    next_batch: int,
    seen: AbstractSet[str],
    stats: WalkStats,
) -> _Collected:
    stories: list[ParsedStory] = []
    quarantined: list[Quarantined] = []
    dropped: Counter[str] = Counter()
    tally = _Tally()
    unknown: Counter[str] = Counter()
    batches: dict[int, int] = {}
    anchored: set[int] = set()
    known = set(seen)
    position = start_position
    units = batch_stories = 0
    story_bodies: set[int] = set()
    for frame in frames:
        for unit in find_units(frame, unknown=unknown, stats=stats):
            units += 1
            try:
                reading = read_story(unit, ctx)
            except Exception as exc:  # noqa: BLE001 - one poison story must never end a read
                dropped[DropReason.QUARANTINED.value] += 1
                quarantined.append(_quarantine(unit, exc))
                continue
            if isinstance(reading, DropReason):
                dropped[reading.value] += 1
                continue
            story = reading.story
            if frame.from_document:
                batch = 0
            else:
                batch = batches.setdefault(frame.body, next_batch + len(batches))
            anchor = batch > 0 and batch not in anchored and not story.is_highlight
            if anchor:
                anchored.add(batch)
            if story.post_id in known:
                dropped[DropReason.DUPLICATE.value] += 1
                continue
            known.add(story.post_id)
            stories.append(replace(story, position=position, batch=batch, is_anchor=anchor))
            position += 1
            if batch > 0:
                story_bodies.add(batch)
                batch_stories += not story.is_highlight
            tally.add(reading)
    failures = dropped[DropReason.NO_POST_ID.value] + dropped[DropReason.QUARANTINED.value]
    health = ParseHealth(
        units=units,
        found=tally.accepted + tally.deferred + failures,
        accepted=tally.accepted,
        deferred=tally.deferred,
        dropped=dict(dropped),
        missing=dict(tally.missing),
        empty_text=tally.empty,
        group_conflicts=tally.conflicts,
        text_paths=dict(tally.text_paths),
        unknown_connections=dict(unknown),
        story_bodies=len(story_bodies),
        batch_stories=batch_stories,
        text_classes=dict(tally.classes),
        card_beside_words=tally.card_beside_words,
        translation_fields=tally.translation_fields,
        posts=tally.posts,
    )
    return _Collected(
        stories=tuple(stories),
        quarantined=tuple(quarantined),
        health=health,
        next_position=position,
        next_batch=next_batch + len(batches),
    )


@dataclass
class _Tally:
    """The per-story counters of one step's stories that were read (duplicates aside)."""

    accepted: int = 0
    deferred: int = 0
    empty: int = 0
    conflicts: int = 0
    card_beside_words: int = 0
    translation_fields: int = 0
    missing: Counter[str] = field(default_factory=Counter)
    text_paths: Counter[str] = field(default_factory=Counter)
    classes: Counter[str] = field(default_factory=Counter)
    posts: dict[str, PostMark] = field(default_factory=dict)

    def add(self, reading: Reading) -> None:
        story = reading.story
        if story.created_at is None:
            self.missing["no_created_at"] += 1
        if story.group_id is None:
            self.missing["no_group"] += 1
        if reading.text_bearing and story.text_path in NOT_OWN_TEXT:
            self.missing["no_text"] += 1
        self.text_paths[story.text_path or NO_NAME] += 1
        if reading.text_class is not None:
            self.classes[reading.text_class.value] += 1
        self.posts[story.post_id] = PostMark(
            accepted=not story.deferred,
            no_time=story.created_at is None,
            text_class=reading.text_class,
        )
        self.conflicts += reading.group_conflict
        self.card_beside_words += reading.card_beside_words
        self.translation_fields += bool(reading.translation_keys)
        self.empty += not story.text
        if story.deferred:
            self.deferred += 1
        else:
            self.accepted += 1


def _quarantine(unit: Unit, exc: Exception) -> Quarantined:
    post_id = unit.node.get("post_id")
    shown = (
        post_id if isinstance(post_id, str) and post_id.isascii() and post_id.isdigit() else None
    )
    log.warning(
        "parser: quarantined a story (post %s, %s): %s",
        shown,
        unit.frame.friendly_name,
        type(exc).__name__,
    )
    try:
        raw = json.dumps(unit.node, ensure_ascii=False, default=repr).encode()
    except (TypeError, ValueError, RecursionError):
        raw = repr(unit.node).encode(errors="replace")
    return Quarantined(
        post_id=shown,
        error=type(exc).__name__,
        raw=raw[:MAX_QUARANTINE_BYTES],
        friendly_name=unit.frame.friendly_name,
    )


def _groups_key(key: str) -> bool:
    # Until phase 0 names the joined-groups connection, any connection called "...group..."
    # counts. A wrong guess cannot complete a sync on its own: completeness also needs the
    # count to equal the page's "All groups you've joined (N)" header.
    return "group" in key.lower() and key not in STORY_CONNECTIONS


def _page_info(
    frames: Sequence[Frame], is_key: Callable[[str], bool], stats: WalkStats
) -> PageInfo | None:
    """The last page_info of the main connection the step received (arrival order)."""
    found: PageInfo | None = None
    for frame in frames:
        path = frame.path
        data = mapping(frame.data)
        if data is not None and path and isinstance(path[-1], str) and is_key(path[-1]):
            info = _info(path[-1], data.get("page_info"))
            if info is not None:
                found = info
                continue
        for node_path, node in walk(frame.data, stop=lambda key, _v: key == "edges", stats=stats):
            key = node_path[-1] if node_path else None
            if isinstance(key, str) and is_key(key):
                info = _info(key, node.get("page_info"))
                found = info or found
    return found


def _info(connection: str, value: Any) -> PageInfo | None:
    info = mapping(value)
    if info is None:
        return None
    has_next = info.get("has_next_page")
    return PageInfo(
        connection=connection,
        has_next_page=has_next if isinstance(has_next, bool) else None,
        end_cursor=text(info.get("end_cursor")),
    )


def _markers(
    document: bytes | None, values: Sequence[Any], frames: Sequence[Frame], stats: WalkStats
) -> SessionMarkers:
    defines = _defines(values, {"CurrentUserInitialData", "IntlCurrentLocale"})
    user_id = _user_id(dig(defines.get("CurrentUserInitialData"), "USER_ID"))
    if user_id is None:
        user_id = _any_user_id(values, stats)
    if user_id is None and document:
        match = _USER_ID.search(document)
        user_id = match.group(1).decode() if match else None
    present = 0
    null: list[str] = []
    for frame in frames:
        data = mapping(frame.data)
        if data is None or "viewer" not in data:
            continue
        if data["viewer"] is None:
            null.append(frame.friendly_name or ("ssr" if frame.from_document else NO_NAME))
        else:
            present += 1
    return SessionMarkers(
        user_id=user_id,
        viewer_present=present,
        viewer_null=tuple(null),
        locale=text(dig(defines.get("IntlCurrentLocale"), "code")),
    )


def _defines(values: Sequence[Any], names: AbstractSet[str]) -> dict[str, Mapping[str, Any]]:
    """ServerJS defines, ``["Name", [deps], {config}, id]``, found anywhere in the SSR."""
    found: dict[str, Mapping[str, Any]] = {}
    stack: list[Any] = list(values)
    budget = MAX_DEFINE_SCAN
    while stack and budget > 0:
        budget -= 1
        value = stack.pop()
        if isinstance(value, Mapping):
            stack.extend(v for v in value.values() if isinstance(v, Mapping | list))
            continue
        if not isinstance(value, list):
            continue
        head = value[0] if value else None
        if isinstance(head, str) and head in names and len(value) >= 3:
            config = value[2]
            if isinstance(config, Mapping):
                found.setdefault(head, config)
                continue
        stack.extend(v for v in value if isinstance(v, Mapping | list))
    return found


def _user_id(raw: Any) -> str | None:
    if isinstance(raw, int) and not isinstance(raw, bool):
        raw = str(raw)
    return raw if isinstance(raw, str) and raw.isascii() and raw.isdigit() else None


def _any_user_id(values: Sequence[Any], stats: WalkStats) -> str | None:
    """A ``USER_ID`` outside the define (another config object carries it too)."""
    for value in values:
        for _path, node in walk(value, stats=stats):
            found = _user_id(node.get("USER_ID"))
            if found is not None:
                return found
    return None


def _errors(frames: Sequence[Frame]) -> tuple[GraphqlError, ...]:
    errors: list[GraphqlError] = []
    for frame in frames:
        raw = frame.raw
        items = raw.get("errors")
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, Mapping):
                continue
            errors.append(
                GraphqlError(
                    code=integer(item.get("code"))
                    or integer(item.get("api_error_code"))
                    or integer(dig(item, "extensions", "code")),
                    summary=_summary(item.get("summary") or item.get("message")),
                    severity=text(item.get("severity")),
                    friendly_name=frame.friendly_name,
                    partial=isinstance(item.get("path"), list),
                )
            )
        code = integer(raw.get("error"))
        if code is not None:
            errors.append(
                GraphqlError(
                    code=code,
                    summary=_summary(raw.get("errorSummary") or raw.get("errorDescription")),
                    friendly_name=frame.friendly_name,
                )
            )
    return tuple(errors)


def _summary(value: Any) -> str | None:
    found = text(value)
    return found[:MAX_ERROR_SUMMARY] if found else None


class ReadParser:
    """Parses the steps of one read in order, continuing positions, batches and dedupe."""

    def __init__(
        self,
        surface: Surface,
        *,
        group_id: str | None = None,
        slugs: Mapping[str, str] | None = None,
    ) -> None:
        self.surface = surface
        self.group_id = group_id
        self._slugs = dict(slugs or {})
        self._position = 0
        self._next_batch = 1
        self._seen: set[str] = set()
        self.stories: list[ParsedStory] = []
        self.quarantined: list[Quarantined] = []
        self.errors: list[GraphqlError] = []
        self.health: ParseHealth | None = None
        self.page_info: PageInfo | None = None
        self.group: GroupMeta | None = None
        self.joined: JoinedGroups | None = None
        self.preloader = PreloaderFacts()
        self.markers = SessionMarkers()

    def parse(self, step: StepLike) -> StepParse:
        result = parse_step(
            step,
            surface=self.surface,
            group_id=self.group_id,
            slugs=self._slugs,
            start_position=self._position,
            next_batch=self._next_batch,
            seen=self._seen,
            connections=set(self.joined.connections) if self.joined is not None else frozenset(),
        )
        self._position = result.next_position
        self._next_batch = result.next_batch
        self._seen.update(s.post_id for s in result.stories)
        self.stories.extend(result.stories)
        self.quarantined.extend(result.quarantined)
        self.errors.extend(result.errors)
        self.health = result.health if self.health is None else self.health.merged(result.health)
        self.page_info = result.page_info or self.page_info
        self.preloader = self.preloader.merged(result.preloader)
        self.markers = self.markers.merged(result.markers)
        if result.group is not None:
            # A later step (GraphQL only) rarely names the group; never let it erase the page's.
            self.group = result.group if self.group is None else self.group
            self.group_id = self.group_id or result.group.id
            if result.group.slug:
                self._slugs.setdefault(result.group.slug, result.group.id)
        if result.joined is not None:
            self.joined = (
                result.joined if self.joined is None else self.joined.merged(result.joined)
            )
        return result


@dataclass(frozen=True, slots=True)
class ParserVerdict:
    broken: bool = False
    reasons: tuple[str, ...] = ()
    warnings: Mapping[str, str] = field(default_factory=dict)
    """Alert-worthy but not a pause: too many stories without a time (``no_created_at``),
    with empty text nothing explains (``empty_text``) or, failing that, without their author's
    words (``no_own_text``), by a key that stays the same while the counts change (the alert's
    dedup key)."""


def parser_verdict(
    history: Sequence[ParseHealth],
    *,
    window: int = 3,
    min_accepted: float = 0.5,
    max_missing: float = 0.2,
    max_wordless: float = 0.5,
    min_found_for_rates: int = 5,
    min_posts_for_text: int = 10,
    wordless_window: int = 10,
) -> ParserVerdict:
    """``PARSER_BROKEN`` over the last ``window`` reads (DESIGN, Parser step 6), each post once.

    A post read again within the window counts once, as its latest read saw it (``posts``);
    the stories without a usable post id count on the same basis (``_distinct_failures``).
    Broken when accepted/found < 50 % over the window, or when every read of the window found
    no story while the DOM showed posts. Warnings when more than 20 % of the distinct stories
    have no creation time (from ``min_found_for_rates`` stories), or when more than 20 % of the
    distinct posts have empty text that nothing explains (``TextClass.UNEXPLAINED``, from
    ``min_posts_for_text`` posts): a share without words, a photo, a card's text are not the
    parser failing. Failing that, when more than half of the distinct posts of the last
    ``wordless_window`` reads (once the history holds that many, from ``min_posts_for_text``
    posts) have no words of their own nor a shared story's, whatever their class: a renamed
    message section leaves a photo post ``media_only`` and a link post ``attachment_text``, and
    only plain-text posts would count as unexplained (the live baseline was 5 of 82). The
    wider window is for chance: three reads once held 6 photos and shares among 10 posts. A
    rename stays, so the backstop may take a few reads longer to see it.
    """
    recent = list(history)[-window:]
    if len(recent) < window:
        return ParserVerdict()
    marks: dict[str, PostMark] = {}
    for health in recent:
        marks.update(health.posts)
    found = len(marks) + _distinct_failures(recent, len(marks))
    accepted = sum(mark.accepted for mark in marks.values())
    reasons: list[str] = []
    if found and accepted / found < min_accepted:
        reasons.append(f"accepted {accepted} of {found} distinct stories over {window} reads")
    if all(h.found == 0 and h.dom_posts > 0 for h in recent):
        reasons.append(f"no stories while the page showed posts, {window} reads in a row")
    warnings: dict[str, str] = {}
    no_time = sum(mark.no_time for mark in marks.values())
    if found >= min_found_for_rates and no_time / found > max_missing:
        warnings["no_created_at"] = f"{no_time} of {found} distinct stories without a creation time"
    classes = Counter(mark.text_class for mark in marks.values() if mark.text_class)
    unexplained = classes.pop(TextClass.UNEXPLAINED, 0)
    posts = len(marks)
    if posts >= min_posts_for_text and unexplained / posts > max_missing:
        besides = ", ".join(f"{kind.value} {n}" for kind, n in sorted(classes.items()))
        warnings["empty_text"] = (
            f"{unexplained} of {posts} distinct posts with empty text nothing explains"
            + (f" (explained: {besides})" if besides else "")
        )
    else:
        # The backstop, one alert for one cause: the empty-text alert lists the classes.
        reads = list(history)[-wordless_window:]
        wide: dict[str, PostMark] = {}
        for health in reads:
            wide.update(health.posts)
        kinds = Counter(mark.text_class for mark in wide.values() if mark.text_class)
        wordless = sum(kinds.values())
        full = len(reads) == wordless_window and len(wide) >= min_posts_for_text
        if full and wordless / len(wide) > max_wordless:
            listed = ", ".join(f"{kind.value} {n}" for kind, n in sorted(kinds.items()))
            warnings["no_own_text"] = (
                f"{wordless} of {len(wide)} distinct posts without their author's words ({listed})"
            )
    return ParserVerdict(broken=bool(reasons), reasons=tuple(reasons), warnings=warnings)


def _distinct_failures(recent: Sequence[ParseHealth], distinct: int) -> int:
    """The window's stories without a usable post id, on the same basis as its posts.

    They have no id to count once by, so each read's are scaled by how often the window read
    its posts again: the feed read three times over the same 10 posts and the same 4 failures
    gives 4, not 12, while three reads of different pages keep every one. Rounded up, so a
    failure is never scaled away; when the window read no post at all, every failure counts.
    """
    failed = sum(h.failures for h in recent)
    sightings = sum(len(h.posts) for h in recent)
    if not sightings:
        return failed
    return -(-failed * distinct // sightings)
