from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st

from fbcollector.frames import FrameStats, iter_document_json, iter_graphql_frames


def test_guard_prefix_and_ndjson_stream() -> None:
    body = (
        b'for (;;);{"data":{"node":{"post_id":"1"}}}\n'
        b'{"label":"Q$stream$x","data":{"post_id":"2"}}\r\n'
        b'{"label":"Q$defer$y","data":{"page_info":{"has_next_page":true}}}\n'
    )
    stats = FrameStats()
    frames = list(iter_graphql_frames(body, stats))
    ids = [f["data"].get("post_id") or f["data"].get("node", {}).get("post_id") for f in frames[:2]]
    assert ids == ["1", "2"]
    assert len(frames) == 3
    assert stats.frames == 3
    assert stats.undecodable == 0


def test_body_without_guard_and_with_leading_whitespace() -> None:
    assert list(iter_graphql_frames('  \n{"a":1}{"b":2}')) == [{"a": 1}, {"b": 2}]


def test_truncated_stream_keeps_what_decoded_and_counts_the_rest() -> None:
    stats = FrameStats()
    frames = list(iter_graphql_frames(b'for (;;);{"a":1}\n{"b":', stats))
    assert frames == [{"a": 1}]
    assert stats.undecodable == 1
    assert stats.errors


def test_empty_and_guard_only_bodies() -> None:
    assert list(iter_graphql_frames(b"")) == []
    assert list(iter_graphql_frames(b"for (;;);")) == []


def test_repeated_guard_between_documents() -> None:
    assert list(iter_graphql_frames('for (;;);{"a":1}\nfor (;;);{"b":2}')) == [{"a": 1}, {"b": 2}]


def test_invalid_utf8_does_not_raise() -> None:
    assert list(iter_graphql_frames(b'{"t":"\xff"}')) == [{"t": "�"}]


def test_document_json_blocks_any_attribute_order() -> None:
    html = (
        '<html><head><script type="application/json" data-sjs>{"require":[["A"]]}</script>'
        '<script data-content-len="12" type=\'application/json\'>{"x": 1}</script>'
        '<script type="text/javascript">var no = {"y": 2};</script>'
        "<SCRIPT TYPE=application/json>\n  []\n</SCRIPT>"
        '<script type="application/json"></script>'
        '<script type="application/json">{broken</script></head></html>'
    )
    stats = FrameStats()
    values = list(iter_document_json(html, stats))
    assert values == [{"require": [["A"]]}, {"x": 1}, []]
    assert stats.frames == 3
    assert stats.undecodable == 1


def test_document_json_escaped_script_close_stays_inside() -> None:
    payload = {"text": "</script> is escaped by Facebook"}
    encoded = json.dumps(payload).replace("<", "\\u003C")
    html = f'<script type="application/json">{encoded}</script>'
    assert list(iter_document_json(html.encode())) == [payload]


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=20),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=20,
)


@given(st.lists(json_values, max_size=5), st.booleans(), st.sampled_from(["\n", "", "\r\n", " "]))
def test_roundtrip_any_sequence(values: list[object], guard: bool, sep: str) -> None:
    body = ("for (;;);" if guard else "") + sep.join(json.dumps(v) for v in values)
    if sep == "" and any(not isinstance(v, (dict, list)) for v in values):
        # Adjacent scalars without a separator are ambiguous ("1" "2" -> "12"); Facebook never
        # emits scalars at the top level, so only containers are glued without separators.
        return
    assert list(iter_graphql_frames(body)) == values
