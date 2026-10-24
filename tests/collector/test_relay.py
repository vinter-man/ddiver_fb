from __future__ import annotations

from typing import Any

import pytest

from fbcollector.frames import FrameStats
from fbcollector.model import GraphqlBody
from fbcollector.relay import (
    DOCUMENT,
    MAX_NODES,
    Frame,
    WalkStats,
    body_frames,
    dig,
    digit_id,
    document_frames,
    document_values,
    epoch,
    facebook_url,
    integer,
    mapping,
    text,
    walk,
)


class TestFrame:
    def test_accessors(self) -> None:
        frame = Frame({"data": {"a": 1}, "label": "L", "path": ["viewer", 0]}, 2, "Q")
        assert frame.data == {"a": 1}
        assert frame.label == "L"
        assert frame.path == ("viewer", 0)
        assert not frame.from_document

    @pytest.mark.parametrize("path", [None, [], "viewer", ["a", None], ["a", True], [1.5]])
    def test_unusable_paths(self, path: object) -> None:
        frame = Frame({"path": path, "label": 3}, DOCUMENT, None)
        assert frame.path is None
        assert frame.label is None
        assert frame.from_document


class TestDocumentFrames:
    def test_bbox_results_anywhere_in_the_ssr(self) -> None:
        values = [
            {"__bbox": {"result": {"data": 1}}},  # the block itself is a frame
            {
                "require": [
                    [
                        "X",
                        "next",
                        [],
                        ["adp", {"__bbox": {"result": {"data": 2}, "complete": True}}],
                    ]
                ]
            },
            {
                "__bbox": {
                    "require": [
                        {"__bbox": {"result": {"data": 3, "inner": {"__bbox": {"result": {}}}}}}
                    ]
                }
            },
            {"__bbox": {"result": "not a frame"}},
        ]
        frames = document_frames(values, WalkStats())
        assert [f.data for f in frames] == [1, 2, 3]
        assert all(f.body == DOCUMENT and f.friendly_name is None for f in frames)

    def test_document_values_guard_recursion(self) -> None:
        stats = FrameStats()
        deep = b'<script type="application/json">' + b"[" * 50_000 + b"</script>"
        ok = b'<script type="application/json">{"a":1}</script>'
        assert document_values(ok + deep, stats) == [{"a": 1}]
        assert stats.undecodable == 1


class TestBodyFrames:
    def test_frames_and_lists_of_frames(self) -> None:
        body = GraphqlBody(friendly_name="Q", body=b'for (;;);{"data":1}\n[{"data":2},3]\n"x"')
        frames = body_frames(body, 4, FrameStats())
        assert [(f.data, f.body, f.friendly_name) for f in frames] == [(1, 4, "Q"), (2, 4, "Q")]

    def test_recursion_is_counted(self) -> None:
        stats = FrameStats()
        body = GraphqlBody(friendly_name=None, body=b'{"data":1}' + b"[" * 50_000)
        assert [f.data for f in body_frames(body, 0, stats)] == [1]
        assert stats.undecodable == 1


class TestWalk:
    def test_preorder_document_order_and_stop(self) -> None:
        tree = {"a": {"b": [{"c": 1}, {"d": {"e": 2}}]}, "f": {"g": 3}}
        paths = [p for p, _ in walk(tree)]
        assert paths == [(), ("a",), ("a", "b", 0), ("a", "b", 1), ("a", "b", 1, "d"), ("f",)]
        stopped = [p for p, _ in walk(tree, stop=lambda k, _v: k == "d")]
        assert ("a", "b", 1, "d") not in stopped
        included = [p for p, _ in walk(tree, stop=lambda k, _v: k == "d", include_stopped=True)]
        assert ("a", "b", 1, "d") in included
        assert all(p[:5] != ("a", "b", 1, "d", "e") for p in included)
        assert list(walk([1, "x"])) == []

    def test_stopped_lists_are_not_entered(self) -> None:
        tree = {"skip": [{"x": 1}], "keep": [{"y": 1}]}
        found = [p for p, _ in walk(tree, stop=lambda k, _v: k == "skip", include_stopped=True)]
        assert found == [(), ("keep", 0)]

    def test_depth_bound(self) -> None:
        tree: dict[str, Any] = {}
        node = tree
        for _ in range(50):
            node["n"] = {}
            node = node["n"]
        stats = WalkStats()
        assert len(list(walk(tree, max_depth=10, stats=stats))) == 11
        assert stats.too_deep == 1

    def test_size_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fbcollector import relay

        monkeypatch.setattr(relay, "MAX_NODES", 3)
        stats = WalkStats()
        assert len(list(relay.walk({"a": [{}, {}, {}, {}]}, stats=stats))) < 5
        assert stats.truncated == 1
        assert MAX_NODES > 1_000_000


class TestGetters:
    def test_dig(self) -> None:
        value = {"a": [{"b": 1}]}
        assert dig(value, "a", 0, "b") == 1
        assert dig(value, "a", -1, "b") == 1
        assert dig(value, "a", 5) is None
        assert dig(value, "a", "b") is None
        assert dig(value, 0) is None
        assert dig(None, "a") is None

    def test_mapping_and_text(self) -> None:
        assert mapping({"a": 1}) == {"a": 1}
        assert mapping([1]) is None
        assert text("x") == "x"
        assert text("  ") is None
        assert text(1) is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("123", "123"),
            (123, "123"),
            ("9223372036854775807", "9223372036854775807"),
            ("9223372036854775808", None),
            ("0", None),
            ("0123", None),
            ("١٢٣", None),  # Arabic-Indic digits are not ids
            (" 12", None),
            (True, None),
            (-5, None),
            (None, None),
            ("pfbid02abc", None),
        ],
    )
    def test_digit_id(self, value: object, expected: str | None) -> None:
        assert digit_id(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1_790_000_000, 1_790_000_000),
            ("1790000000", 1_790_000_000),
            (0, None),
            (1_790_000_000_000, None),
            (True, None),
            ("1.5e9", None),
            (1.79e9, None),
        ],
    )
    def test_epoch(self, value: object, expected: int | None) -> None:
        assert epoch(value) == expected

    def test_integer(self) -> None:
        assert integer("-12") == -12
        assert integer(7) == 7
        assert integer(False) is None
        assert integer("1e3") is None
        assert integer(None) is None


class TestFacebookUrl:
    def test_canonical_drops_tracking(self) -> None:
        url = facebook_url("http://m.facebook.com/groups/123/posts/456/?__cft__[0]=x#y")
        assert url is not None
        assert url.canonical == "https://www.facebook.com/groups/123/posts/456/"
        assert (url.group_ref, url.post_ref) == ("123", "456")

    def test_group_refs(self) -> None:
        bare = facebook_url("https://www.facebook.com/groups/flats/")
        assert bare is not None and (bare.group_ref, bare.post_ref) == ("flats", None)
        permalink = facebook_url("https://web.facebook.com/groups/flats/permalink/pfbid0x/")
        assert permalink is not None and permalink.post_ref == "pfbid0x"
        other = facebook_url("https://www.facebook.com/marketplace/item/1/")
        assert other is not None and other.group_ref is None and other.post_ref is None

    def test_profile_php_keeps_its_id(self) -> None:
        url = facebook_url("https://www.facebook.com/profile.php?id=42&sk=about")
        assert url is not None
        assert url.canonical == "https://www.facebook.com/profile.php?id=42"
        assert facebook_url("https://www.facebook.com/profile.php?id=abc") is None
        assert facebook_url("https://www.facebook.com/profile.php") is None

    @pytest.mark.parametrize(
        "value",
        [
            None,
            42,
            "ftp://www.facebook.com/x",
            "https://facebook.com.evil.example/groups/1/",
            "https://user:pw@www.facebook.com/groups/1/",
            "https://www.facebook.com/groups/../x/",
            "https://[::1/",
            "https://www.facebook.com/" + "a" * 3000,
        ],
    )
    def test_rejects(self, value: object) -> None:
        assert facebook_url(value) is None
