from __future__ import annotations

import dataclasses

import pytest

import fbwire
from fbcollector import model
from fbcollector.model import (
    AuthorRef,
    DeferredReason,
    DomFacts,
    GraphqlBody,
    GroupMeta,
    ListingRef,
    ParsedStory,
    Sighting,
    Step,
    Surface,
)


def story(**kw: object) -> ParsedStory:
    base: dict[str, object] = {
        "post_id": "11",
        "group_id": "7",
        "created_at": 1_790_000_000,
        "url": "https://www.facebook.com/groups/7/posts/11/",
        "text": "hello",
        "text_path": "content.story.message",
    }
    base.update(kw)
    return ParsedStory(**base)  # type: ignore[arg-type]


class TestParsedStory:
    def test_regular_story(self) -> None:
        s = story()
        assert not s.deferred
        assert s.regular
        assert s.key == ("7", "11")
        assert s.author == AuthorRef()

    def test_highlight_and_anchor_are_not_regular(self) -> None:
        assert not story(is_highlight=True).regular
        assert not story(is_anchor=True).regular

    def test_deferred_story_without_a_group_has_no_key(self) -> None:
        s = story(group_id=None, deferred_reason=DeferredReason.NO_GROUP)
        assert s.deferred
        assert s.key is None

    def test_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            story().text = "x"  # type: ignore[misc]


class TestGroupMeta:
    def test_unknown_values_stay_unknown(self) -> None:
        meta = GroupMeta(id="7")
        assert meta.url == "https://www.facebook.com/groups/7/"
        assert (meta.is_member, meta.is_followed, meta.is_chronological) == (None, None, None)

    def test_only_the_exact_good_value_counts(self) -> None:
        good = GroupMeta(
            id="7",
            join_state="MEMBER",
            subscribe_status="IS_SUBSCRIBED",
            sorting_setting="CHRONOLOGICAL",
        )
        assert (good.is_member, good.is_followed, good.is_chronological) == (True, True, True)
        bad = GroupMeta(
            id="7",
            join_state="CAN_JOIN",
            subscribe_status="CAN_SUBSCRIBE",
            sorting_setting="CHRONOLOGICAL_LISTINGS",
        )
        assert (bad.is_member, bad.is_followed, bad.is_chronological) == (False, False, False)


def test_listing_lines_skip_missing_parts() -> None:
    assert ListingRef(title="2-room", location="Sofia").lines() == ["2-room", "Sofia"]
    assert ListingRef(id="5").lines() == []


def test_sighting_of_a_story_copies_its_place() -> None:
    s = story(position=4, batch=2, cursor="abc", is_anchor=True)
    sighting = Sighting.of(s, Surface.GROUP, read_id=9)
    assert sighting == Sighting(
        post_id="11",
        group_id="7",
        surface=Surface.GROUP,
        read_id=9,
        position=4,
        batch=2,
        cursor="abc",
        is_highlight=False,
        is_anchor=True,
        created_at=1_790_000_000,
    )


def test_step_defaults() -> None:
    step = Step(final_url="https://www.facebook.com/?filter=groups&sk=h_chr", status=200)
    assert step.dom == DomFacts()
    assert step.graphql == ()
    assert not step.pagination_fired
    body = GraphqlBody(friendly_name="CometNewsFeedPaginationQuery", body=b"{}")
    assert body.status is None
    assert not body.capped


def test_collector_state_is_the_wire_enum() -> None:
    assert model.CollectorState is fbwire.CollectorState
    assert {"NEEDS_LOGIN", "PAUSED", "WARMUP", "CRASH_LOOP"} <= set(
        model.CollectorState.__members__
    )
    for name in model.__all__:
        assert hasattr(model, name)
