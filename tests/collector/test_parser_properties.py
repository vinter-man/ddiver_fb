"""Property tests: the parser against story trees Facebook has not sent us yet.

Each generated story is assembled from optional parts (where its time, group and text live), so
the expected result can be computed from what was included; then the tree is disturbed the ways
Facebook disturbs it (typenames renamed, keys reordered with ``attached_story`` first, a shared
post nested inside) and the parser must still read the story's own fields and nothing else.
"""

from __future__ import annotations

import base64
import copy
import json
import random
from dataclasses import dataclass
from typing import Any, Final

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fbcollector.model import DeferredReason, DomFacts, GraphqlBody, Step, Surface
from fbcollector.parser import ReadParser, parse_step

T = 1_790_000_000
GROUPS = ("400000000000001", "400000000000002")
SHARED_GROUP = "400000000000009"


@dataclass
class Expected:
    post_id: str
    created_at: int | None
    group_id: str | None
    text: str
    reason: DeferredReason | None


def group_url(gid: str) -> str:
    return f"https://www.facebook.com/groups/{gid}/"


def _place_times(
    node: dict[str, Any], context: dict[str, Any], created: int, flags: list[bool]
) -> None:
    if flags[0]:
        node["creation_time"] = created
    if flags[1]:
        context["comet_sections"]["metadata"].append(
            {
                "__typename": "CometFeedStoryMinimizedTimestampStrategy",
                "story": {"creation_time": created},
            }
        )
    if flags[2]:
        node["comet_sections"]["timestamp"] = {"story": {"creation_time": created}}


def _place_groups(
    node: dict[str, Any],
    content: dict[str, Any],
    context: dict[str, Any],
    *,
    gid: str,
    pid: str,
    flags: list[bool],
) -> None:
    group_ref = {"__typename": "Group", "id": gid, "url": group_url(gid)}
    if flags[0]:
        node["to"] = dict(group_ref)
    if flags[1]:
        content["target_group"] = {"id": gid}
    if flags[2]:
        context["comet_sections"]["title"] = {"story": {"to": dict(group_ref), "post_id": pid}}
    if flags[3]:
        node["feedback"] = {"associated_group": {"id": gid}}
    if flags[4]:
        node["permalink_url"] = f"https://www.facebook.com/groups/{gid}/posts/{pid}/?__cft__=x"


def _place_message(content: dict[str, Any], own: str, words: str) -> str:
    """Put the story's own text where ``own`` says; return the text the parser should read."""
    sections = content["comet_sections"]
    if own == "root":
        content["message"] = {"__typename": "TextWithEntities", "text": words}
    elif own == "section":
        sections["message"] = {"story": {"message": {"text": words}}}
    elif own == "container":
        sections["message_container"] = {"story": {"message": {"text": words}}}
    elif own == "rich":
        sections["message"] = {"rich_message": [{"text": w} for w in words.split(" ")]}
        return "\n".join(words.split(" "))
    elif own == "bearing-empty":
        content["message"] = {"__typename": "TextWithEntities"}
        return ""
    else:
        return ""
    return words


def _place_shares(content: dict[str, Any], depth: int, words: str, parts: list[str]) -> None:
    """A chain of shared posts carrying another id, time, group and text at the same paths:
    none of it may leak into the story's own fields, only each text on its own line."""
    parent = content
    for level in range(depth):
        shared_text = f"shared {level} {words}"
        shared = {
            "__typename": "Story",
            "post_id": str(900 + level),
            "creation_time": T - 10**7 - level,
            "to": {"__typename": "Group", "id": SHARED_GROUP, "url": group_url(SHARED_GROUP)},
            "target_group": {"id": SHARED_GROUP},
            "permalink_url": f"https://www.facebook.com/groups/{SHARED_GROUP}/posts/{900 + level}/",
            "message": {"text": shared_text},
            "comet_sections": {"timestamp": {"story": {"creation_time": T - 10**7}}},
        }
        parent["attached_story"] = shared
        parent = shared
        if shared_text not in parts:
            parts.append(shared_text)


NOISE: Final = {
    "url": "https://l.facebook.com/l.php?u=https%3A%2F%2Fexample.org%2Fx&h=AT0noise",
    "id": "720000000000009",
    "tracking": "AZXnoise0tracking0token0value0AAAA",
    "web_link": {"__typename": "ExternalWebLink", "url": "https://example.org/x"},
    "media": {"__typename": "GenericAttachmentMedia", "title": {"text": "media noise words"}},
    "action_links": [{"__typename": "ArticleContextActionLink", "title": {"text": "button words"}}],
}
"""What sits beside an attachment's text and must never become match text."""


def _place_attachment(
    content: dict[str, Any], kind: str, where: str, words: str
) -> tuple[list[str], list[str]]:
    """An attachment of ``kind`` in the story's own attachments or its shared story's; returns
    ``(listing lines, link lines)`` it offers."""
    if kind == "none":
        return [], []
    words = words.strip()  # a line of match text is stripped
    if kind == "link":
        lines = [f"link title {words}", f"link about {words}", "EXAMPLE.ORG"]
        attachment: dict[str, Any] = {
            "styles": {
                "attachment": {
                    **copy.deepcopy(NOISE),  # tests pop keys: never share them
                    "title_with_entities": {"text": lines[0]},
                    "description": {"text": lines[1]},
                    "source": {"text": lines[2]},
                    "target": {"__typename": "ExternalUrl", "id": "730000000000001"},
                }
            }
        }
        found: tuple[list[str], list[str]] = [], lines
    else:
        lines = [f"listing {words}", "BGN 120", "Ruse"]
        attachment = {
            "styles": {
                "attachment": {
                    **copy.deepcopy(NOISE),  # tests pop keys: never share them
                    "title_with_entities": {"text": lines[0]},
                    "target": {
                        "id": "700000000000001",
                        "formatted_price": {"text": lines[1]},
                        "location_text": {"text": lines[2]},
                    },
                }
            }
        }
        found = lines, []
    holder = content.get("attached_story") if where == "shared" else None
    (holder if isinstance(holder, dict) else content)["attachments"] = [attachment]
    return found


@st.composite
def stories(draw: st.DrawFn) -> tuple[dict[str, Any], Expected]:
    pid = str(draw(st.integers(10**14, 10**16)))
    created = draw(st.integers(T - 10**6, T))
    gid = draw(st.sampled_from(GROUPS))
    own = draw(st.sampled_from(["root", "section", "container", "rich", "bearing-empty", "none"]))
    words = draw(st.text(alphabet="abcdef xyz", min_size=1, max_size=20).filter(str.strip))
    times = draw(st.lists(st.booleans(), min_size=3, max_size=3))
    groups = draw(st.lists(st.booleans(), min_size=5, max_size=5))
    share_depth = draw(st.integers(0, 3))
    attachment = draw(st.sampled_from(["none", "link", "listing"]))
    where = draw(st.sampled_from(["own", "shared"]))

    content: dict[str, Any] = {"comet_sections": {}}
    context: dict[str, Any] = {
        "comet_sections": {"metadata": [{"__typename": "CometFeedStoryAudienceStrategy"}]}
    }
    node: dict[str, Any] = {
        "__typename": "Story",
        "post_id": pid,
        "comet_sections": {"content": {"story": content}, "context_layout": {"story": context}},
    }
    _place_times(node, context, created, times)
    _place_groups(node, content, context, gid=gid, pid=pid, flags=groups)
    own_text = _place_message(content, own, words)
    parts = [own_text] if own_text else []
    _place_shares(content, share_depth, words, parts)
    listing_lines, link_lines = _place_attachment(content, attachment, where, words)
    parts += listing_lines + link_lines  # after the words, whether or not there are any

    if not any(times):
        reason: DeferredReason | None = DeferredReason.NO_CREATED_AT
    elif not any(groups):
        reason = DeferredReason.NO_GROUP
    elif own == "bearing-empty":
        reason = DeferredReason.NO_TEXT
    else:
        reason = None
    expected = Expected(
        post_id=pid,
        created_at=created if any(times) else None,
        group_id=gid if any(groups) else None,
        text="\n".join(parts),
        reason=reason,
    )
    return node, expected


def rename_typenames(value: Any, suffix: str) -> Any:
    if isinstance(value, dict):
        return {
            # The old name is gone entirely: no suffix like "...TimestampStrategy" survives.
            k: (
                f"Renamed{suffix}{len(v)}"
                if k == "__typename" and isinstance(v, str)
                else rename_typenames(v, suffix)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [rename_typenames(v, suffix) for v in value]
    return value


def reorder(value: Any, rng: random.Random, attached_first: bool) -> Any:
    if isinstance(value, dict):
        keys = list(value)
        rng.shuffle(keys)
        if attached_first and "attached_story" in keys:
            keys.remove("attached_story")
            keys.insert(0, "attached_story")
        return {k: reorder(value[k], rng, attached_first) for k in keys}
    if isinstance(value, list):
        return [reorder(v, rng, attached_first) for v in value]
    return value


def feed_body(
    nodes: list[dict[str, Any]], name: str = "CometNewsFeedPaginationQuery"
) -> GraphqlBody:
    frames = [
        {
            "data": {
                "viewer": {
                    "news_feed": {
                        "edges": [{"node": n, "cursor": f"c{i}"} for i, n in enumerate(nodes)]
                    }
                }
            }
        }
    ]
    return GraphqlBody(
        friendly_name=name, body=("for (;;);" + "\n".join(json.dumps(f) for f in frames)).encode()
    )


def one_story(node: dict[str, Any]) -> Any:
    result = parse_step(
        Step(final_url="u", status=200, graphql=(feed_body([node]),)), surface=Surface.FEED
    )
    assert result.quarantined == ()
    (story,) = result.stories
    return story


def check(story: Any, expected: Expected) -> None:
    assert story.post_id == expected.post_id
    assert story.created_at == expected.created_at
    assert story.group_id == expected.group_id
    assert story.text == expected.text
    assert story.deferred_reason is expected.reason
    if expected.group_id is not None:
        assert (
            story.url
            == f"https://www.facebook.com/groups/{expected.group_id}/posts/{expected.post_id}/"
        )
    assert all(gid != SHARED_GROUP for _path, gid in story.group_candidates)


@given(stories())
def test_scoped_fields(case: tuple[dict[str, Any], Expected]) -> None:
    node, expected = case
    check(one_story(node), expected)


@given(stories(), st.text(alphabet="ABCxyz_", max_size=6))
def test_typename_renames(case: tuple[dict[str, Any], Expected], suffix: str) -> None:
    node, expected = case
    story = one_story(rename_typenames(node, suffix))
    # A renamed group reference still counts through its /groups/ URL, a renamed timestamp
    # strategy through any strategy that carries a time: renames degrade, never mislead.
    check(story, expected)


@given(stories(), st.integers(0, 2**32), st.booleans())
def test_key_order_does_not_matter(
    case: tuple[dict[str, Any], Expected], seed: int, first: bool
) -> None:
    node, expected = case
    check(one_story(reorder(node, random.Random(seed), attached_first=first)), expected)


@given(stories(), st.data())
def test_missing_arbitrary_keys_never_raise(
    case: tuple[dict[str, Any], Expected], data: st.DataObject
) -> None:
    node, _expected = case
    flat: list[tuple[dict[str, Any], str]] = []
    stack: list[Any] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            flat.extend((current, k) for k in current)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    for holder, key in data.draw(
        st.lists(st.sampled_from(flat), max_size=6, unique_by=lambda p: (id(p[0]), p[1]))
    ):
        holder.pop(key, None)
    result = parse_step(
        Step(final_url="u", status=200, graphql=(feed_body([node]),)), surface=Surface.FEED
    )
    assert result.quarantined == ()
    assert (
        result.health.found
        == result.health.accepted
        + result.health.deferred
        + result.health.dropped.get("no_post_id", 0)
    )
    for story in result.stories:
        assert story.post_id == node.get("post_id") or story.post_id.isdigit()
        assert story.deferred or (story.created_at and story.group_id)


@st.composite
def reads(draw: st.DrawFn) -> list[list[tuple[str, bool]]]:
    """Batches of (post id, highlight?) drawn from a small pool so posts repeat across batches."""
    pool = [str(10**15 + n) for n in range(8)]
    return draw(
        st.lists(
            st.lists(st.tuples(st.sampled_from(pool), st.booleans()), min_size=0, max_size=6),
            min_size=1,
            max_size=5,
        )
    )


def edge_node(post_id: str, highlight: bool) -> tuple[str, dict[str, Any]]:
    story = {
        "__typename": "Story",
        "post_id": post_id,
        "creation_time": T,
        "to": {"__typename": "Group", "id": GROUPS[0]},
        "comet_sections": {"content": {"story": {"message": {"text": post_id}}}},
    }
    if highlight:
        return "highlight_units", {
            "__typename": "GroupHighlightPostUnit",
            "id": post_id,
            "story": story,
        }
    return "news_feed", story


@given(reads(), st.booleans())
def test_anchors_highlights_positions_and_dedupe(
    batches: list[list[tuple[str, bool]]], ssr: bool
) -> None:
    parser = ReadParser(Surface.FEED)
    expected_order: list[str] = []
    for index, batch in enumerate(batches):
        frames = []
        expected_anchor: list[str] = []
        first_regular_done = False
        for post_id, highlight in batch:
            connection, node = edge_node(post_id, highlight)
            frames.append({"data": {"viewer": {connection: {"edges": [{"node": node}]}}}})
            if not highlight and not first_regular_done:
                first_regular_done = True
                if post_id not in expected_order and not (ssr and index == 0):
                    expected_anchor = [post_id]
            if post_id not in expected_order:
                expected_order.append(post_id)
        text = "for (;;);" + "\n".join(json.dumps(f) for f in frames)
        if ssr and index == 0:
            html = "".join(
                f'<script type="application/json">{json.dumps({"__bbox": {"result": f}})}</script>'
                for f in frames
            )
            step = Step(
                final_url="u",
                status=200,
                document=html.encode(),
                dom=DomFacts(posinset_count=len(batch)),
            )
        else:
            step = Step(
                final_url="u",
                status=200,
                graphql=(GraphqlBody(friendly_name="Q", body=text.encode()),),
            )
        result = parser.parse(step)
        assert [s.post_id for s in result.stories if s.is_anchor] == expected_anchor
        for story in result.stories:
            assert not (story.is_anchor and story.is_highlight)
            if story.batch == 0:
                assert not story.is_anchor
        highlights = {pid for pid, h in batch if h}
        for story in result.stories:
            if story.post_id in highlights and story.is_highlight:
                assert not story.regular
    assert [s.post_id for s in parser.stories] == expected_order
    assert [s.position for s in parser.stories] == list(range(len(expected_order)))
    batches_seen = [s.batch for s in parser.stories]
    assert batches_seen == sorted(batches_seen)
    health = parser.health
    assert health is not None
    assert health.found == health.accepted == len(expected_order)
    assert health.units == sum(len(b) for b in batches)
    assert health.dropped.get("duplicate", 0) == health.units - len(expected_order)


# Where each surface's connection sits, and the labels its @stream / @defer parts carry.
CONNECTIONS = {
    Surface.FEED: (
        ("viewer", "news_feed"),
        "CometNewsFeed_viewerConnection$stream$CometNewsFeed_viewer_news_feed",
        "CometNewsFeed_viewer$defer$CometNewsFeed_viewer_news_feed$page_info",
    ),
    Surface.GROUP: (
        ("node", "group_feed"),
        "GroupsCometFeedRegularStories_group_group_feed$stream$GroupsCometFeedRegularStories_group_group_feed",
        "GroupsCometFeedRegularStories_group$defer$GroupsCometFeedRegularStories_group_group_feed$page_info",
    ),
}  # fmt: skip


def nest(path: tuple[str, ...], leaf: Any) -> Any:
    for key in reversed(path):
        leaf = {key: leaf}
    return leaf


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(stories(), min_size=1, max_size=5, unique_by=lambda case: case[1].post_id),
    st.sampled_from([Surface.FEED, Surface.GROUP]),
    st.data(),
)
def test_streamed_delivery_reads_like_one_connection(
    cases: list[tuple[dict[str, Any], Expected]], surface: Surface, data: st.DataObject
) -> None:
    """The same stories as one connection, or as NDJSON: an initial frame with the first
    edges, one ``@stream`` part per later edge (some without their ``path``, only the label),
    and a ``@defer`` part with ``page_info`` at the connection's path."""
    path, stream_label, defer_label = CONNECTIONS[surface]
    edges = [{"node": node, "cursor": f"c{i}"} for i, (node, _e) in enumerate(cases)]
    first = data.draw(st.integers(0, len(edges)), label="edges in the initial frame")
    has_next = data.draw(st.booleans(), label="has_next_page")
    guard = data.draw(st.booleans(), label="guard prefix")
    separator = data.draw(st.sampled_from(["\n", "\r\n", "", " \n"]), label="separator")

    frames: list[dict[str, Any]] = [{"data": nest(path, {"edges": edges[:first]})}]
    for index in range(first, len(edges)):
        part: dict[str, Any] = {"label": stream_label, "data": edges[index]}
        if data.draw(st.booleans(), label=f"part {index} keeps its path"):
            part["path"] = [*path, "edges", index]
        frames.append(part)
    frames.append(
        {
            "label": defer_label,
            "path": list(path),
            "data": {"page_info": {"has_next_page": has_next, "end_cursor": "end"}},
        }
    )
    streamed = ("for (;;);" if guard else "") + separator.join(json.dumps(f) for f in frames)
    whole = json.dumps({"data": nest(path, {"edges": edges})})

    def parse(body: str) -> Any:
        step = Step(
            final_url="u",
            status=200,
            graphql=(GraphqlBody(friendly_name="PaginationQuery", body=body.encode()),),
        )
        return parse_step(step, surface=surface)

    got, want = parse(streamed), parse(whole)
    assert got.stories == want.stories
    assert got.health.text_paths == want.health.text_paths
    assert got.health.found == want.health.found == len(cases)
    for story, (_node, expected) in zip(got.stories, cases, strict=True):
        check(story, expected)
    assert [s.post_id for s in got.stories if s.is_anchor] == [cases[0][1].post_id]
    assert [s.cursor for s in got.stories] == [f"c{i}" for i in range(len(cases))]
    assert got.page_info is not None
    assert (got.page_info.connection, got.page_info.has_next_page) == (path[-1], has_next)
    assert got.page_info.end_cursor == "end"
    assert want.page_info is None


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=10),
    lambda children: st.lists(children, max_size=4)
    | st.dictionaries(
        st.sampled_from(["post_id", "node", "edges", "news_feed", "group_feed", "highlight_units",
                         "story", "comet_sections", "content", "message", "text", "attached_story",
                         "creation_time", "to", "id", "url", "actors", "attachments", "target",
                         "metadata", "__typename", "page_info", "viewer", "errors", "data", "path",
                         "label", "sponsored_data", "profile_header_renderer", "group"]),
        children,
        max_size=5,
    ),
    max_leaves=40,
)  # fmt: skip


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(json_values, max_size=4), st.sampled_from(list(Surface)))
def test_arbitrary_frames_never_raise(frames: list[Any], surface: Surface) -> None:
    text = "for (;;);" + "\n".join(json.dumps(f) for f in frames)
    html = "".join(f'<script type="application/json">{json.dumps(f)}</script>' for f in frames)
    step = Step(
        final_url="u",
        status=200,
        document=html.encode(),
        graphql=(GraphqlBody(friendly_name=None, body=text.encode()),),
    )
    result = parse_step(
        step, surface=surface, group_id=GROUPS[0] if surface is Surface.GROUP else None
    )
    health = result.health
    failures = health.dropped.get("no_post_id", 0) + health.dropped.get("quarantined", 0)
    assert health.found == health.accepted + health.deferred + failures
    assert len(result.stories) == health.accepted + health.deferred
    assert result.quarantined == ()
    json.dumps(health.as_stats())


def test_huge_body_within_the_driver_cap() -> None:
    nodes = [edge_node(str(10**15 + n), False)[1] for n in range(3000)]
    body = feed_body(nodes)
    assert len(body.body) < 4 * 1024 * 1024
    result = parse_step(Step(final_url="u", status=200, graphql=(body,)), surface=Surface.FEED)
    assert len(result.stories) == 3000
    assert result.stories[0].is_anchor and not any(s.is_anchor for s in result.stories[1:])
    assert result.health.skipped_nodes == 0


def test_deep_nesting_inside_a_story_is_skipped_not_fatal() -> None:
    node = edge_node("1000000000000001", False)[1]
    deep: dict[str, Any] = {}
    cursor = deep
    for _ in range(400):
        cursor["x"] = {}
        cursor = cursor["x"]
    cursor["sponsored_data"] = {"ad_id": "1"}  # too deep to be seen: not an ad
    node["comet_sections"]["deep"] = deep
    frames = {"data": {"viewer": {"news_feed": {"edges": [{"node": node}]}}, "junk": deep}}
    body = GraphqlBody(friendly_name="Q", body=json.dumps(frames).encode())
    result = parse_step(Step(final_url="u", status=200, graphql=(body,)), surface=Surface.FEED)
    assert [s.post_id for s in result.stories] == ["1000000000000001"]
    assert result.health.skipped_nodes >= 1


# -- attachments: nothing but words becomes match text -------------------------------------------

ATTACHMENT_KEYS = [
    # the allow-list: where the parser looks for words
    "title_with_entities", "title", "description", "source", "marketplace_listing_title",
    "formatted_price", "listing_price", "formatted_amount", "location_text", "text",
    # where a card keeps them
    "styles", "style_type_renderer", "attachment", "target", "style_infos",
    "comet_footer_renderer", "__typename",
    # beside them
    "url", "id", "tracking", "web_link", "external_url", "fbclid", "media", "uri",
    "action_links", "deduplication_key",
]  # fmt: skip
TYPENAMES = [
    "StoryAttachment", "ExternalUrl", "ExternalWebLink", "GroupCommerceProductItem",
    "MarketplaceListing", "CommerceProductItemStyleInfo", "StoryAttachmentShareStyleRenderer",
]  # fmt: skip
ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def _typed_id(a: int, b: int, pad: bool) -> str:
    encoded = base64.b64encode(f"S:_I{a}:{b}".encode()).decode()
    return encoded if pad else encoded.rstrip("=")


not_words = st.one_of(
    st.builds(
        lambda scheme, host, path: f"{scheme}://{host}/{path}",
        st.sampled_from(["https", "http", "fb", "intent"]),
        st.sampled_from(["www.facebook.com", "l.facebook.com", "imot.bg", "example.org"]),
        st.text(alphabet=ALNUM + "/?=&%.-_", max_size=40),
    ),
    st.builds(lambda host: f"www.{host}", st.text(alphabet=ALNUM + ".", min_size=1)),
    st.builds(
        lambda a, b: f"{a}/{b}",
        st.text(alphabet=ALNUM + ".", max_size=12),
        st.text(alphabet=ALNUM, max_size=12),
    ),
    st.integers(min_value=10**5, max_value=2**63 - 1).map(str),  # ids
    st.text(alphabet=ALNUM, min_size=30, max_size=50).map(lambda s: "pfbid0" + s),
    st.builds(  # an opaque typed id (``S:_I<n>:<n>``), with or without its padding
        _typed_id,
        st.integers(min_value=0, max_value=10**17),
        st.integers(min_value=0, max_value=10**17),
        st.booleans(),
    ),
    st.text(alphabet=ALNUM + "-_", min_size=20, max_size=80),  # tracking tokens
    st.text(alphabet=_B64 + "=", min_size=20, max_size=80),
)
"""Strings that are a link, an id or a token, never words: no space in them."""


def cards(leaves: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
    def objects(children: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
        return st.builds(
            lambda d, kind: {**d, "__typename": kind} if kind else d,
            st.dictionaries(st.sampled_from(ATTACHMENT_KEYS), children, max_size=5),
            st.one_of(st.none(), st.sampled_from(TYPENAMES)),
        )

    return st.recursive(
        leaves, lambda c: st.one_of(st.lists(c, max_size=3), objects(c)), max_leaves=30
    )


def _strings(tree: Any) -> set[str]:
    out: set[str] = set()
    stack = [tree]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            stack.extend(v for k, v in value.items() if k != "__typename")
        elif isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, str):
            out.add(value)
    return out


WORDS: Final = "Думите на автора"


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(
    own=st.lists(cards(not_words), min_size=1, max_size=3),
    shared=st.lists(cards(not_words), max_size=2),
    with_words=st.booleans(),
)
def test_no_link_id_or_token_ever_becomes_match_text(
    own: list[Any], shared: list[Any], with_words: bool
) -> None:
    """Links, ids and tracking tokens planted anywhere in a story's attachments (and its
    shared story's), under the allow-listed keys too, never reach its text or its listing,
    beside the author's words (a card's lines are appended to them) or without them."""
    node = edge_node("1000000000000001", False)[1]
    content = node["comet_sections"]["content"]["story"]
    content["message"] = {"text": WORDS} if with_words else None
    content["attachments"] = own
    content["attached_story"] = {"__typename": "Story", "attachments": shared}
    story = one_story(node)
    planted = _strings(own) | _strings(shared)
    assert story.text == (WORDS if with_words else "")
    assert story.listing is None or story.listing.lines() == []
    assert not [s for s in planted if s.strip() and s.strip() in (story.text or "")]
