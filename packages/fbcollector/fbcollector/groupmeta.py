"""What a group page says about its group, and what the page's preloaders asked for.

Both feed the detectors, and both are reported raw (``MEMBER``, ``IS_SUBSCRIBED``,
``CHRONOLOGICAL``): the detectors only ever ask "is it exactly the good value".

- ``GroupMeta`` comes from ``profile_header_renderer.group`` (membership, privacy and follow
  state live there; the idea is from adbertram/cli-tools, MIT), the root query's ``data.group``
  (name, vanity slug) and the discussion preloader's ``sortingSetting``. A group page carries
  other groups too (suggested groups under ``related_groups_v2``, each with its own
  ``viewer_join_state``), so only the page's own group is read, selected by id.
- ``PreloaderFacts`` are the variables of the queries the page preloaded. ``feedStyle`` and
  ``orderby`` say whether the Groups feed is the chronological one (``FEED_NOT_CHRONO``);
  ``sortingSetting`` whether a group honoured ``CHRONOLOGICAL`` (``SORT_IGNORED``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from fbcollector.model import GroupMeta, GroupPrivacy
from fbcollector.relay import Frame, WalkStats, dig, digit_id, facebook_url, integer, mapping, text
from fbcollector.relay import walk as walk_values
from fbcollector.stories import KNOWN_CONNECTIONS

CHRONO_FEED_STYLE: Final = "MOST_RECENT_GROUPS_FEED"
CHRONO_ORDER: Final = "MOST_RECENT"
_SLUG: Final = re.compile(r"[A-Za-z0-9._-]{1,128}")
_VISIBILITY: Final = {
    "OPEN": GroupPrivacy.PUBLIC,
    "CLOSED": GroupPrivacy.PRIVATE,
    "SECRET": GroupPrivacy.PRIVATE,
}


@dataclass(frozen=True, slots=True)
class PreloaderFacts:
    """Selected variables of the page's preloaded queries (never tokens or ids of people)."""

    queries: tuple[str, ...] = ()
    feed_style: str | None = None
    orderby: tuple[str, ...] = ()
    feed_initial_fetch_size: int | None = None
    sortings: tuple[tuple[str | None, str], ...] = ()
    """``(groupID, sortingSetting)`` as the preloaders state them."""
    group_ids: tuple[str, ...] = ()

    @property
    def group_id(self) -> str | None:
        """The group the page preloaded (vanity URLs resolve to their numeric id here)."""
        return self.group_ids[0] if self.group_ids else None

    @property
    def feed_is_chrono(self) -> bool | None:
        """``None`` when no feed preloader was seen at all (not a feed page, or a new shape)."""
        if self.feed_style is None and not self.orderby:
            return None
        return self.feed_style == CHRONO_FEED_STYLE and CHRONO_ORDER in self.orderby

    def sorting_for(self, group_id: str | None) -> str | None:
        for gid, sorting in self.sortings:
            if gid is not None and gid == group_id:
                return sorting
        # A sortingSetting stated without a groupID can only be the page's group's, and only
        # when the page preloaded a single group.
        unscoped = [s for gid, s in self.sortings if gid is None]
        if unscoped and len(set(self.group_ids)) <= 1:
            return unscoped[0]
        return None

    def merged(self, other: PreloaderFacts) -> PreloaderFacts:
        """This read's facts plus a later step's; the first value seen wins."""
        return PreloaderFacts(
            queries=tuple(dict.fromkeys((*self.queries, *other.queries))),
            feed_style=self.feed_style or other.feed_style,
            orderby=self.orderby or other.orderby,
            feed_initial_fetch_size=(
                self.feed_initial_fetch_size
                if self.feed_initial_fetch_size is not None
                else other.feed_initial_fetch_size
            ),
            sortings=tuple(dict.fromkeys((*self.sortings, *other.sortings))),
            group_ids=tuple(dict.fromkeys((*self.group_ids, *other.group_ids))),
        )

    def summary(self) -> dict[str, Any]:
        """JSON-able, for the ``reads`` table (DESIGN: preloader variables per read)."""
        return {
            "queries": list(self.queries),
            "feedStyle": self.feed_style,
            "orderby": list(self.orderby),
            "feedInitialFetchSize": self.feed_initial_fetch_size,
            "sortingSetting": [s for _g, s in self.sortings],
            "groupID": self.group_id,
        }


def preloader_facts(values: Sequence[Any], stats: WalkStats | None = None) -> PreloaderFacts:
    """Read the preloader variables out of the SSR's JSON values."""
    queries: list[str] = []
    feed_style: str | None = None
    orderby: tuple[str, ...] = ()
    fetch_size: int | None = None
    sortings: list[tuple[str | None, str]] = []
    group_ids: list[str] = []
    for value in values:
        for _path, node in walk_values(value, stats=stats):
            name = text(node.get("queryName"))
            if name and name not in queries:
                queries.append(name)
            feed_style = feed_style or text(node.get("feedStyle"))
            orderby = orderby or _strings(node.get("orderby"))
            if fetch_size is None:
                fetch_size = integer(node.get("feedInitialFetchSize"))
            gid = digit_id(node.get("groupID"))
            if gid is not None and gid not in group_ids:
                group_ids.append(gid)
            sorting = text(node.get("sortingSetting"))
            if sorting is not None and (gid, sorting) not in sortings:
                sortings.append((gid, sorting))
    return PreloaderFacts(
        queries=tuple(queries),
        feed_style=feed_style,
        orderby=orderby,
        feed_initial_fetch_size=fetch_size,
        sortings=tuple(sortings),
        group_ids=tuple(group_ids),
    )


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) and value:
        return (value,)
    if isinstance(value, list):
        return tuple(v for v in value if isinstance(v, str) and v)
    return ()


def page_group(
    frames: Sequence[Frame],
    *,
    group_id: str | None,
    preloader: PreloaderFacts,
    stats: WalkStats | None = None,
) -> GroupMeta | None:
    """The page's own group, or ``None`` when the page names no group at all."""
    headers: list[Mapping[str, Any]] = []
    roots: list[Mapping[str, Any]] = []
    follows: list[tuple[str | None, str]] = []
    for frame in frames:
        root = mapping(dig(frame.data, "group"))
        if root is not None and digit_id(root.get("id")):
            roots.append(root)
        for _path, node in walk_values(
            frame.data, stop=lambda key, _v: key in KNOWN_CONNECTIONS, stats=stats
        ):
            header = mapping(dig(node, "profile_header_renderer", "group"))
            if header is not None and digit_id(header.get("id")):
                headers.append(header)
            follow = mapping(node.get("if_viewer_can_change_follow_setting"))
            status = text(dig(follow, "subscribe_status"))
            if follow is not None and status is not None:
                owner = digit_id(follow.get("id")) or digit_id(node.get("id"))
                follows.append((owner, status))

    target = group_id or preloader.group_id
    if target is None:
        first = headers[0] if headers else roots[0] if roots else None
        target = digit_id(first.get("id")) if first is not None else None
    if target is None:
        return None
    header = next((h for h in headers if digit_id(h.get("id")) == target), {})
    root = next((r for r in roots if digit_id(r.get("id")) == target), {})
    subscribe = text(
        dig(header, "if_viewer_can_change_follow_setting", "subscribe_status")
    ) or next((status for owner, status in follows if owner == target), None)
    return GroupMeta(
        id=target,
        name=_name(header) or _name(root),
        slug=group_slug(root) or group_slug(header),
        privacy=privacy_of(header, root),
        join_state=text(header.get("viewer_join_state"))
        or text(dig(header, "join_action", "group", "viewer_join_state"))
        or text(root.get("viewer_join_state")),
        subscribe_status=subscribe,
        sorting_setting=preloader.sorting_for(target),
    )


def _name(group: Mapping[str, Any]) -> str | None:
    name = text(group.get("name"))
    return name.strip() if name else None


def group_slug(group: Mapping[str, Any]) -> str | None:
    """A vanity slug (``Jerseyask``), never a numeric id."""
    for candidate in (group.get("vanity"), group.get("group_address")):
        if isinstance(candidate, str) and _SLUG.fullmatch(candidate) and not candidate.isdigit():
            return candidate
    parsed = facebook_url(group.get("url"))
    ref = parsed.group_ref if parsed else None
    if ref is not None and _SLUG.fullmatch(ref) and not ref.isdigit():
        return ref
    return None


def privacy_of(*groups: Mapping[str, Any]) -> GroupPrivacy:
    """Public or private from ``privacy_info`` (title, icon, description) or ``visibility``."""
    for group in groups:
        info = group.get("privacy_info")
        title = (text(dig(info, "title", "text")) or "").lower()
        if "public" in title:
            return GroupPrivacy.PUBLIC
        if "private" in title:
            return GroupPrivacy.PRIVATE
        icon = text(dig(info, "icon_name")) or ""
        if icon.startswith("globe"):
            return GroupPrivacy.PUBLIC
        if icon in {"privacy", "lock"}:
            return GroupPrivacy.PRIVATE
        described = (text(dig(info, "description", "text")) or "").lower()
        if described.startswith("anyone"):
            return GroupPrivacy.PUBLIC
        if described.startswith("only members"):
            return GroupPrivacy.PRIVATE
        visibility = group.get("visibility")
        if isinstance(visibility, str) and visibility in _VISIBILITY:
            return _VISIBILITY[visibility]
    return GroupPrivacy.UNKNOWN
