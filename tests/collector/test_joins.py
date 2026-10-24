from __future__ import annotations

from typing import Any

import pytest

from fbcollector.joins import JoinedGroups, group_shaped, joined_groups
from fbcollector.model import GroupMeta, GroupPrivacy
from fbcollector.relay import Frame

G1, G2, G3 = "400000000000001", "400000000000002", "400000000000003"


def group(gid: str, **extra: Any) -> dict[str, Any]:
    return {
        "__typename": "Group",
        "id": gid,
        "name": f"G{gid[-1]}",
        "url": f"https://www.facebook.com/groups/{gid}/",
        **extra,
    }


class TestGroupShaped:
    def test_minimal(self) -> None:
        assert group_shaped(group(G1)) == GroupMeta(id=G1, name="G1")

    def test_slug_privacy_and_state(self) -> None:
        node = {
            "id": G2,
            "name": " Flats ",
            "profile_url": "https://www.facebook.com/groups/flats/",
            "visibility": "CLOSED",
            "viewer_join_state": "MEMBER",
        }
        assert group_shaped(node) == GroupMeta(
            id=G2, name="Flats", slug="flats", privacy=GroupPrivacy.PRIVATE, join_state="MEMBER"
        )

    @pytest.mark.parametrize(
        "node",
        [
            group(G1, __typename="User"),
            group(G1, id="pfbid0x"),
            group(G1, name=" "),
            group(G1, url=f"https://www.facebook.com/groups/{G1}/posts/5/"),
            group(G1, url=f"https://www.facebook.com/groups/{G2}/"),
            group(G1, url="https://www.facebook.com/flats/"),
            group(G1, url="https://example.com/groups/1/"),
            {"id": G1, "name": "x"},
        ],
    )
    def test_not_group_shaped(self, node: dict[str, Any]) -> None:
        assert group_shaped(node) is None

    def test_a_later_url_key_can_qualify(self) -> None:
        node = group(
            G1, url="https://www.facebook.com/", uri=f"https://www.facebook.com/groups/{G1}/"
        )
        assert group_shaped(node) is not None


class TestJoinedGroups:
    def test_connections_exclusions_and_dedupe(self) -> None:
        root = Frame(
            {
                "data": {
                    "viewer": {
                        "joined": {"edges": [{"node": group(G1)}, {"node": group(G2)}]},
                        "suggested": {"nodes": [group(G3, viewer_join_state="CAN_JOIN")]},
                        "news_feed": {"edges": [{"node": {"to": group(G3)}}]},
                    }
                }
            },
            -1,
            None,
        )
        stream = Frame(
            {"path": ["viewer", "joined", "edges", 2], "data": {"node": group(G1)}}, 0, "Q"
        )
        bare = Frame({"data": group(G3)}, 1, "Q")
        result = joined_groups([root, stream, bare])
        assert [g.id for g in result.groups] == [G1, G2, G3]
        assert result.connections == {"joined": 3, "(root)": 1}
        assert result.excluded == 1

    def test_merge(self) -> None:
        a = JoinedGroups((GroupMeta(G1, name="a"), GroupMeta(G2, name="b")), {"k": 2}, 1)
        b = JoinedGroups(
            (GroupMeta(G2, name="changed"), GroupMeta(G3, name="c")), {"k": 1, "j": 1}, 2
        )
        merged = a.merged(b)
        assert [(g.id, g.name) for g in merged.groups] == [(G1, "a"), (G2, "b"), (G3, "c")]
        assert merged.connections == {"k": 3, "j": 1}
        assert merged.excluded == 3
