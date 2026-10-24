"""The collector's own vocabulary, shared by the driver, parser, detectors, scheduler and store.

These are plain frozen dataclasses on purpose. The parser is a pure function over bytes and its
output has not been validated yet: a story may lack its text, its time or its group, and that is
information (it is *deferred*, not lost). Wire models (``fbwire``) validate strictly and are built
from these only when an item is enqueued; keeping the two apart means a half-parsed story is
representable here without weakening the contract with the brain.

``CollectorState`` is the one exception: the brain shows it to the owner, so it is defined once,
in ``fbwire``, and re-exported here.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from fbwire.models import BudgetKind, CollectorState, GroupPrivacy, GroupState, Profile, Via

__all__ = [
    "AuthorRef",
    "BudgetKind",
    "CollectorState",
    "Coverage",
    "DeferredReason",
    "DomFacts",
    "GraphqlBody",
    "GroupMeta",
    "GroupPrivacy",
    "GroupState",
    "ListingRef",
    "PageState",
    "ParsedStory",
    "Profile",
    "ReadOutcome",
    "ReadRecord",
    "Sighting",
    "Step",
    "Surface",
    "Via",
]


class Surface(enum.StrEnum):
    """The only three kinds of page the collector ever opens (DESIGN, URL allow-list)."""

    FEED = "feed"
    GROUP = "group"
    JOINS = "joins"


class PageState(enum.StrEnum):
    """The detectors' verdict on one step. ``OK`` only when a positive signature holds."""

    OK = "ok"
    BLOCKED = "blocked"
    LOGGED_OUT = "logged_out"
    UNKNOWN = "unknown"
    FEED_NOT_CHRONO = "feed_not_chrono"
    GROUP_LOST = "group_lost"
    NOT_FOLLOWED = "not_followed"
    SORT_IGNORED = "sort_ignored"
    PARSER_BROKEN = "parser_broken"


class DeferredReason(enum.StrEnum):
    """Why a story could not become an item yet. It is retried on its next sighting."""

    NO_TEXT = "no_text"
    NO_CREATED_AT = "no_created_at"
    NO_GROUP = "no_group"


class ReadOutcome(enum.StrEnum):
    COMPLETE = "complete"  # reached its coverage target
    GAP = "gap"  # hit its step cap still seeing new stories: the uncovered interval is a gap
    ABORTED = "aborted"  # a gate closed or the machine slept mid-read
    FAILED = "failed"  # a detector said something other than OK


@dataclass(frozen=True, slots=True)
class AuthorRef:
    id: str | None = None
    name: str | None = None
    url: str | None = None
    anonymous: bool = False


@dataclass(frozen=True, slots=True)
class ListingRef:
    """A marketplace card on a post. ``id`` is a listing id, never a post id."""

    id: str | None = None
    title: str | None = None
    price: str | None = None
    location: str | None = None

    def lines(self) -> list[str]:
        """The card as the matcher sees it: title, price, location, each on its own line."""
        return [part for part in (self.title, self.price, self.location) if part]


@dataclass(frozen=True, slots=True)
class ParsedStory:
    """One story the parser found, scoped to its own root (DESIGN, Parser step 3).

    ``position`` is the story's index in the read (SSR first, then responses in arrival
    order); ``batch`` is 0 for the navigation and n for the n-th pagination response.
    Highlights and anchors are kept for the novelty check and ignored by ordering and stop
    rules.
    """

    post_id: str
    group_id: str | None
    created_at: int | None
    url: str | None
    text: str | None
    text_path: str | None
    author: AuthorRef = field(default_factory=AuthorRef)
    listing: ListingRef | None = None
    is_share: bool = False
    position: int = 0
    batch: int = 0
    is_highlight: bool = False
    is_anchor: bool = False
    cursor: str | None = None
    friendly_name: str | None = None
    group_candidates: tuple[tuple[str, str], ...] = ()
    """``(path, group id)`` for every place a group id was found (phase 0 inventory)."""
    deferred_reason: DeferredReason | None = None

    @property
    def deferred(self) -> bool:
        return self.deferred_reason is not None

    @property
    def regular(self) -> bool:
        """Counts for ordering and stop decisions."""
        return not self.is_highlight and not self.is_anchor

    @property
    def key(self) -> tuple[str, str] | None:
        return (self.group_id, self.post_id) if self.group_id else None


@dataclass(frozen=True, slots=True)
class GroupMeta:
    """What a group page (or the joined-groups list) says about one group.

    The Relay values are kept raw (``MEMBER``, ``IS_SUBSCRIBED``, ``CHRONOLOGICAL``): an enum
    would have to guess at values Facebook has not shown us yet, and the detectors only ever ask
    "is it exactly the good one".
    """

    id: str
    name: str | None = None
    slug: str | None = None
    privacy: GroupPrivacy = GroupPrivacy.UNKNOWN
    join_state: str | None = None
    subscribe_status: str | None = None
    sorting_setting: str | None = None

    @property
    def url(self) -> str:
        return f"https://www.facebook.com/groups/{self.id}/"

    @property
    def is_member(self) -> bool | None:
        return None if self.join_state is None else self.join_state == "MEMBER"

    @property
    def is_followed(self) -> bool | None:
        return None if self.subscribe_status is None else self.subscribe_status == "IS_SUBSCRIBED"

    @property
    def is_chronological(self) -> bool | None:
        return None if self.sorting_setting is None else self.sorting_setting == "CHRONOLOGICAL"


@dataclass(frozen=True, slots=True)
class GraphqlBody:
    """One ``/api/graphql/`` response the page itself received during a step."""

    friendly_name: str | None
    body: bytes
    status: int | None = None
    label: str | None = None
    capped: bool = False
    """The body was cut at the driver's size cap; its tail frames are missing."""


@dataclass(frozen=True, slots=True)
class DomFacts:
    """What the isolated world could see on the rendered page. Never read from the main world."""

    html_lang: str | None = None
    posinset_count: int = 0
    dialog_texts: tuple[str, ...] = ()
    dialog_count: int | None = None
    """``[role=dialog]`` nodes on the page; the driver reads the texts of the first few
    (``MAX_DIALOGS``). ``None``: not counted (steps saved before it was)."""
    joined_header_count: int | None = None
    has_password_field: bool = False
    see_more: int = 0
    """"See more" buttons on the page: the DOM truncated a post's text (phase-0 measure)."""


@dataclass(frozen=True, slots=True)
class Step:
    """The result of one driver step (DESIGN, Driver). The driver never loops."""

    final_url: str
    status: int | None
    document: bytes | None = None  # SSR HTML (navigate only)
    graphql: tuple[GraphqlBody, ...] = ()
    dom: DomFacts = field(default_factory=DomFacts)
    lost_bodies: int = 0
    pagination_fired: bool = False  # scroll_step only
    latency_ms: int | None = None


@dataclass(frozen=True, slots=True)
class Sighting:
    """One appearance of a story in one read, recorded whether or not it was new."""

    post_id: str
    group_id: str | None
    surface: Surface
    read_id: int | None = None
    position: int = 0
    batch: int = 0
    cursor: str | None = None
    is_highlight: bool = False
    is_anchor: bool = False
    created_at: int | None = None

    @classmethod
    def of(cls, story: ParsedStory, surface: Surface, read_id: int | None) -> Sighting:
        return cls(
            post_id=story.post_id,
            group_id=story.group_id,
            surface=surface,
            read_id=read_id,
            position=story.position,
            batch=story.batch,
            cursor=story.cursor,
            is_highlight=story.is_highlight,
            is_anchor=story.is_anchor,
            created_at=story.created_at,
        )


@dataclass(frozen=True, slots=True)
class Coverage:
    """The span of creation times a read reached, and whether it reached its target.

    ``oldest`` is the 2nd-oldest regular story of the last batch (highlights and the anchor
    excluded), which is what the stop rules compare against the target.
    """

    newest: int | None = None
    oldest: int | None = None
    target: float | None = None
    target_reached: bool = False


@dataclass(frozen=True, slots=True)
class ReadRecord:
    """One read (a navigation plus its scroll steps) as the ``reads`` table keeps it."""

    surface: Surface
    started_at: float
    id: int | None = None
    group_id: str | None = None
    ended_at: float | None = None
    coverage: Coverage = field(default_factory=Coverage)
    steps: int = 0
    outcome: ReadOutcome | None = None
    page_state: PageState | None = None
    stats: dict[str, Any] = field(default_factory=dict)
    """Health counters: friendly names, text paths, page_info, preloader variables, latencies."""
