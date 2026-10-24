"""The groups the listener account has joined, read from ``/groups/joins/`` (group sync, D8).

The queries behind that page are a phase-0 inventory item, so nothing here depends on their
names or shapes: every Group-shaped dict (a numeric ``id``, a ``name`` and a ``/groups/`` URL) in
the SSR and in every GraphQL body is a candidate, and the key of the connection it sat in is
counted so phase 0 can see which connection is the joined list. That also means a page with
suggested groups would over-report, which is why a sync only counts as complete when the list
matches the "All groups you've joined (N)" header (the scheduler's rule, not this module's), and
why a dict that says ``viewer_join_state`` is anything but ``MEMBER`` is left out here.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from fbcollector.groupmeta import group_slug, privacy_of
from fbcollector.model import GroupMeta
from fbcollector.relay import Frame, Key, WalkStats, digit_id, facebook_url, text, walk
from fbcollector.stories import KNOWN_CONNECTIONS

_STRUCTURAL: Final = frozenset({"node", "nodes", "edges"})


@dataclass(frozen=True, slots=True)
class JoinedGroups:
    groups: tuple[GroupMeta, ...] = ()
    connections: Mapping[str, int] = field(default_factory=dict)
    """Connection key -> Group-shaped dicts found under it (which query is the list)."""
    excluded: int = 0
    """Group-shaped dicts whose ``viewer_join_state`` said the account is not a member."""

    def merged(self, other: JoinedGroups) -> JoinedGroups:
        """This sync's groups plus a later scroll step's, first sighting of a group wins."""
        by_id = {g.id: g for g in reversed((*self.groups, *other.groups))}
        order = dict.fromkeys(g.id for g in (*self.groups, *other.groups))
        return JoinedGroups(
            groups=tuple(by_id[gid] for gid in order),
            connections=dict(Counter(self.connections) + Counter(other.connections)),
            excluded=self.excluded + other.excluded,
        )


def group_shaped(node: Mapping[str, Any]) -> GroupMeta | None:
    """A ``GroupMeta`` for a dict that has a group's id, name and group URL, else ``None``."""
    kind = node.get("__typename")
    if kind is not None and kind != "Group":
        return None
    group_id = digit_id(node.get("id"))
    name = text(node.get("name"))
    if group_id is None or name is None:
        return None
    for key in ("url", "profile_url", "uri"):
        parsed = facebook_url(node.get(key))
        if parsed is None or len(parsed.path) != 2 or parsed.path[0] != "groups":
            continue  # not a group's own URL (a post, a profile, another site)
        ref = digit_id(parsed.path[1])
        if ref is not None and ref != group_id:
            continue  # the URL names another group
        return GroupMeta(
            id=group_id,
            name=name.strip(),
            slug=group_slug(node) or group_slug({"url": node.get(key)}),
            privacy=privacy_of(node),
            join_state=text(node.get("viewer_join_state")),
        )
    return None


def joined_groups(frames: Sequence[Frame], stats: WalkStats | None = None) -> JoinedGroups:
    found: dict[str, GroupMeta] = {}
    connections: Counter[str] = Counter()
    excluded = 0
    for frame in frames:
        for path, node in walk(frame.data, stop=_skip_feeds, stats=stats):
            meta = group_shaped(node)
            if meta is None:
                continue
            if meta.join_state is not None and meta.join_state != "MEMBER":
                excluded += 1
                continue
            connections[_connection((*(frame.path or ()), *path))] += 1
            found.setdefault(meta.id, meta)
    return JoinedGroups(tuple(found.values()), dict(connections), excluded)


def _skip_feeds(key: Key, _value: Any) -> bool:
    # A story's ``to`` is Group-shaped too; stories are never part of the joined list.
    return key in KNOWN_CONNECTIONS


def _connection(path: Sequence[Key]) -> str:
    for key in reversed(path):
        if isinstance(key, str) and key not in _STRUCTURAL:
            return key
    return "(root)"
