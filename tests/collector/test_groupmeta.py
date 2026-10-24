from __future__ import annotations

from typing import Any

import pytest

from fbcollector.groupmeta import (
    PreloaderFacts,
    group_slug,
    page_group,
    preloader_facts,
    privacy_of,
)
from fbcollector.model import GroupPrivacy
from fbcollector.relay import DOCUMENT, Frame

G1, G2 = "400000000000001", "400000000000002"


def frame(data: Any) -> Frame:
    return Frame({"data": data}, DOCUMENT, None)


class TestPreloaderFacts:
    def test_reads_every_preloader(self) -> None:
        values = [
            {
                "expectedPreloaders": [
                    {"queryName": "CometNewsFeedRootQuery", "variables": {
                        "feedStyle": "MOST_RECENT_GROUPS_FEED", "orderby": ["MOST_RECENT"],
                        "feedInitialFetchSize": "3"}},
                    {"queryName": "CometNewsFeedRootQuery", "variables": {"feedStyle": "OTHER"}},
                    {"queryName": "CometGroupDiscussionRootSuccessQuery", "variables": {
                        "groupID": G1, "sortingSetting": "CHRONOLOGICAL"}},
                ]
            }
        ]  # fmt: skip
        facts = preloader_facts(values)
        assert facts.queries == ("CometNewsFeedRootQuery", "CometGroupDiscussionRootSuccessQuery")
        assert facts.feed_style == "MOST_RECENT_GROUPS_FEED"  # the first one wins
        assert facts.orderby == ("MOST_RECENT",)
        assert facts.feed_initial_fetch_size == 3
        assert facts.feed_is_chrono is True
        assert facts.group_id == G1
        assert facts.sorting_for(G1) == "CHRONOLOGICAL"
        assert facts.summary() == {
            "queries": list(facts.queries),
            "feedStyle": "MOST_RECENT_GROUPS_FEED",
            "orderby": ["MOST_RECENT"],
            "feedInitialFetchSize": 3,
            "sortingSetting": ["CHRONOLOGICAL"],
            "groupID": G1,
        }

    @pytest.mark.parametrize(
        ("style", "orderby", "chrono"),
        [
            ("MOST_RECENT_GROUPS_FEED", "MOST_RECENT", True),
            ("TOP_STORIES", ["MOST_RECENT"], False),
            ("MOST_RECENT_GROUPS_FEED", ["TOP"], False),
            (None, None, None),
            (None, ["MOST_RECENT"], False),
        ],
    )
    def test_feed_is_chrono(self, style: str | None, orderby: object, chrono: bool | None) -> None:
        facts = preloader_facts([{"variables": {"feedStyle": style, "orderby": orderby}}])
        assert facts.feed_is_chrono is chrono

    def test_sorting_scoped_to_its_group(self) -> None:
        facts = PreloaderFacts(
            sortings=((G1, "TOP_POSTS"), (None, "CHRONOLOGICAL")), group_ids=(G1, G2)
        )
        assert facts.sorting_for(G1) == "TOP_POSTS"
        assert facts.sorting_for(G2) is None  # two groups: an unscoped value is ambiguous
        single = PreloaderFacts(sortings=((None, "CHRONOLOGICAL"),), group_ids=(G2,))
        assert single.sorting_for(G2) == "CHRONOLOGICAL"
        assert PreloaderFacts().sorting_for(None) is None
        assert PreloaderFacts().group_id is None

    def test_merged_first_value_wins(self) -> None:
        a = PreloaderFacts(queries=("A",), sortings=((G1, "X"),), group_ids=(G1,))
        b = PreloaderFacts(
            queries=("A", "B"), feed_style="S", orderby=("MOST_RECENT",),
            feed_initial_fetch_size=4, sortings=((G1, "X"), (G2, "Y")), group_ids=(G2,),
        )  # fmt: skip
        merged = a.merged(b)
        assert merged.queries == ("A", "B")
        assert merged.feed_style == "S" and merged.orderby == ("MOST_RECENT",)
        assert merged.feed_initial_fetch_size == 4
        assert merged.sortings == ((G1, "X"), (G2, "Y"))
        assert merged.group_ids == (G1, G2)
        assert b.merged(a).feed_initial_fetch_size == 4


def header(gid: str, **fields: Any) -> dict[str, Any]:
    return {"profile_header_renderer": {"group": {"id": gid, **fields}}}


class TestPageGroup:
    def test_the_page_group_among_suggestions(self) -> None:
        data = {
            "group": {
                "id": G1,
                "name": "Root name",
                "vanity": "flats",
                **header(
                    G1,
                    name=" Flats ",
                    viewer_join_state="MEMBER",
                    privacy_info={"title": {"text": "Private group"}},
                    if_viewer_can_change_follow_setting={
                        "id": G1,
                        "subscribe_status": "IS_SUBSCRIBED",
                    },
                    related_groups_v2={"nodes": [header(G2, viewer_join_state="CAN_REQUEST")]},
                ),
            }
        }
        meta = page_group([frame(data)], group_id=None, preloader=PreloaderFacts(group_ids=(G1,)))
        assert meta is not None
        assert (meta.id, meta.name, meta.slug) == (G1, "Flats", "flats")
        assert meta.privacy is GroupPrivacy.PRIVATE
        assert meta.is_member and meta.is_followed

    def test_first_header_when_nothing_names_the_group(self) -> None:
        meta = page_group(
            [frame(header(G2, name="Two"))], group_id=None, preloader=PreloaderFacts()
        )
        assert meta is not None and meta.id == G2 and meta.name == "Two"
        root_only = page_group(
            [frame({"group": {"id": G1}})], group_id=None, preloader=PreloaderFacts()
        )
        assert root_only is not None and root_only.id == G1
        assert page_group([frame({"x": 1})], group_id=None, preloader=PreloaderFacts()) is None

    def test_follow_state_and_join_state_fallbacks(self) -> None:
        data = {
            "group": {"id": G1, "viewer_join_state": "CAN_JOIN"},
            "elsewhere": {
                "id": G1,
                "if_viewer_can_change_follow_setting": {"subscribe_status": "UNSUBSCRIBED"},
            },
            "other": {
                "if_viewer_can_change_follow_setting": {
                    "id": G2,
                    "subscribe_status": "IS_SUBSCRIBED",
                }
            },
            "module": {
                "if_viewer_can_change_follow_setting": {
                    "__dr": "GroupsCometFollowActionMenuItem.react"
                }
            },
        }
        meta = page_group([frame(data)], group_id=G1, preloader=PreloaderFacts())
        assert meta is not None
        assert meta.join_state == "CAN_JOIN"
        assert meta.subscribe_status == "UNSUBSCRIBED"
        assert meta.is_followed is False
        joined = page_group(
            [frame(header(G1, join_action={"group": {"viewer_join_state": "REQUESTED"}}))],
            group_id=G1,
            preloader=PreloaderFacts(),
        )
        assert joined is not None and joined.join_state == "REQUESTED"

    def test_sorting_comes_from_the_preloader(self) -> None:
        facts = PreloaderFacts(sortings=((G1, "RECENT_ACTIVITY"),), group_ids=(G1,))
        meta = page_group([], group_id=G1, preloader=facts)
        assert meta is not None
        assert meta.sorting_setting == "RECENT_ACTIVITY"
        assert meta.is_chronological is False

    def test_headers_inside_feeds_are_not_read(self) -> None:
        data = {"group_feed": {"edges": [{"node": header(G1, name="In a story")}]}}
        meta = page_group([frame(data)], group_id=G1, preloader=PreloaderFacts())
        assert meta is not None and meta.name is None


class TestSlugAndPrivacy:
    @pytest.mark.parametrize(
        ("group", "slug"),
        [
            ({"vanity": "flats.sofia"}, "flats.sofia"),
            ({"vanity": "", "group_address": "flats"}, "flats"),
            ({"vanity": "123"}, None),
            ({"vanity": "bad slug"}, None),
            ({"url": "https://www.facebook.com/groups/flats/"}, "flats"),
            ({"url": "https://www.facebook.com/groups/123/"}, None),
            ({"url": "https://www.facebook.com/flats/"}, None),
            ({}, None),
        ],
    )
    def test_group_slug(self, group: dict[str, Any], slug: str | None) -> None:
        assert group_slug(group) == slug

    @pytest.mark.parametrize(
        ("info", "privacy"),
        [
            ({"privacy_info": {"title": {"text": "Public group"}}}, GroupPrivacy.PUBLIC),
            ({"privacy_info": {"title": {"text": "Private group"}}}, GroupPrivacy.PRIVATE),
            ({"privacy_info": {"icon_name": "globe-americas"}}, GroupPrivacy.PUBLIC),
            ({"privacy_info": {"icon_name": "privacy"}}, GroupPrivacy.PRIVATE),
            (
                {"privacy_info": {"description": {"text": "Anyone can see who's in the group."}}},
                GroupPrivacy.PUBLIC,
            ),
            (
                {"privacy_info": {"description": {"text": "Only members can see who's in it."}}},
                GroupPrivacy.PRIVATE,
            ),
            ({"visibility": "OPEN"}, GroupPrivacy.PUBLIC),
            ({"visibility": "SECRET"}, GroupPrivacy.PRIVATE),
            ({"visibility": "NEW"}, GroupPrivacy.UNKNOWN),
            ({}, GroupPrivacy.UNKNOWN),
        ],
    )
    def test_privacy(self, info: dict[str, Any], privacy: GroupPrivacy) -> None:
        assert privacy_of(info) is privacy

    def test_privacy_first_group_that_says_wins(self) -> None:
        assert privacy_of({}, {"visibility": "CLOSED"}) is GroupPrivacy.PRIVATE
