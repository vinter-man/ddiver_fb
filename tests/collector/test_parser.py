from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

import pytest

import fbcollector.parser as parser_module
import fbcollector.stories as stories_module
from fbcollector.model import (
    DeferredReason,
    DomFacts,
    GraphqlBody,
    GroupPrivacy,
    ListingRef,
    Step,
    Surface,
)
from fbcollector.parser import (
    GraphqlError,
    PageInfo,
    ParseHealth,
    ParserVerdict,
    PostMark,
    ReadParser,
    SessionMarkers,
    StepParse,
    parse_step,
    parser_verdict,
)
from fbcollector.relay import Frame
from fbcollector.stories import Context, DropReason, Reading, TextClass, Unit
from tests.collector.parser_fixtures import FIXTURES, document_step, fixture_meta, load_steps

G1, G2, G3, G4 = "400000000000001", "400000000000002", "400000000000003", "400000000000004"


def post(n: int) -> str:
    return f"50000000000{n:05d}"


def user(n: int) -> str:
    return f"60000000000{n:04d}"


def body(
    *frames: object, name: str | None = "Q", guard: bool = True, capped: bool = False
) -> GraphqlBody:
    text = ("for (;;);" if guard else "") + "\n".join(json.dumps(f) for f in frames)
    return GraphqlBody(friendly_name=name, body=text.encode(), capped=capped)


def step(*bodies: GraphqlBody, document: bytes | None = None, posinset: int = 0) -> Step:
    return Step(
        final_url="https://www.facebook.com/",
        status=200,
        document=document,
        graphql=bodies,
        dom=DomFacts(posinset_count=posinset),
    )


def story(
    post_id: str, gid: str = G1, created: int = 1_790_000_000, text: str = "hi"
) -> dict[str, Any]:
    return {
        "__typename": "Story",
        "post_id": post_id,
        "creation_time": created,
        "to": {"__typename": "Group", "id": gid},
        "comet_sections": {"content": {"story": {"message": {"text": text}}}},
    }


def feed_frame(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"data": {"viewer": {"news_feed": {"edges": [{"node": n} for n in nodes]}}}}


class TestRealGroupCaptures:
    """Logged-out group pages of 2026-09-24, anonymised by the allow-list builder."""

    def test_group_page_facts(self) -> None:
        result = parse_step(document_step("group_chrono_loggedout.html"), surface=Surface.GROUP)
        group = result.group
        assert group is not None
        assert group.id == result.preloader.group_id
        assert group.name == "Group 1"
        assert group.slug == "slug-1"
        assert group.privacy is GroupPrivacy.PUBLIC
        assert group.join_state == "CAN_JOIN"
        assert group.is_member is False
        assert group.subscribe_status is None
        assert group.sorting_setting == "CHRONOLOGICAL"
        assert group.is_chronological is True
        assert "CometGroupDiscussionRootSuccessQuery" in result.preloader.queries
        assert result.preloader.feed_is_chrono is None

    def test_one_regular_story_and_the_pinned_highlight(self) -> None:
        result = parse_step(document_step("group_chrono_loggedout.html"), surface=Surface.GROUP)
        highlight, regular = result.stories
        gid = result.group.id if result.group else None
        assert highlight.is_highlight
        assert not highlight.regular
        assert highlight.created_at == 1_781_955_046  # three months older: a pinned post
        assert regular.regular
        assert regular.created_at == 1_790_257_445
        assert (highlight.position, regular.position) == (0, 1)
        assert {highlight.batch, regular.batch} == {0}
        assert not highlight.is_anchor and not regular.is_anchor
        for s in result.stories:
            assert s.group_id == gid
            assert s.url == f"https://www.facebook.com/groups/{gid}/posts/{s.post_id}/"
            assert not s.deferred
            assert s.text
        assert regular.text_path == "content.message"
        assert highlight.text_path == "content.sections.message"
        assert regular.author.name == "Person 3"
        assert regular.author.url == f"https://www.facebook.com/profile.php?id={regular.author.id}"
        # Every group candidate agrees, including the vanity permalink resolved by slug.
        assert {path for path, _ in regular.group_candidates} == {
            "to",
            "content.target_group",
            "title.to",
            "feedback.associated_group",
            "permalink.slug",
        }
        assert {g for _, g in regular.group_candidates} == {gid}

    def test_page_info_and_health(self) -> None:
        result = parse_step(document_step("group_chrono_loggedout.html"), surface=Surface.GROUP)
        assert result.page_info == PageInfo("group_feed", True, "cursor-4")
        health = result.health
        assert health.units == 5
        assert health.found == health.accepted == 2
        assert health.dropped == {"section_header": 1, "non_story": 2}
        assert health.page_info
        # The page carries routine field-level errors; none of them is a rate limit.
        assert result.errors
        assert all(e.partial and not e.rate_limited for e in result.errors)
        assert result.markers == SessionMarkers()

    def test_share_takes_its_own_time_and_the_shared_text(self) -> None:
        result = parse_step(document_step("group_share_loggedout.html"), surface=Surface.GROUP)
        share = result.stories[1]
        assert share.is_share
        assert share.created_at == 1_790_262_681  # the share's time, not the original's
        assert share.text_path == "attached_story"
        assert share.text and share.text.startswith("t4.0 ")
        assert not share.deferred
        assert share.author.id is None  # a pfbid, not a numeric id
        assert share.author.name == "Person 3"
        assert share.author.url is None

    def test_member_header_from_a_logged_in_capture(self) -> None:
        result = parse_step(document_step("group_header_member.html"), surface=Surface.GROUP)
        assert result.group is not None
        assert result.group.privacy is GroupPrivacy.PRIVATE
        assert result.group.is_member is True
        assert result.group.is_followed is True
        assert result.group.sorting_setting is None
        assert result.stories == ()

    def test_pending_header_picks_the_page_group_not_a_suggestion(self) -> None:
        result = parse_step(document_step("group_header_pending.html"), surface=Surface.GROUP)
        assert result.group is not None
        assert result.group.join_state == "REQUESTED"
        assert result.group.is_followed is None

    def test_group_id_given_by_the_caller_wins(self) -> None:
        result = parse_step(
            document_step("group_header_member.html"), surface=Surface.GROUP, group_id="999999999"
        )
        assert result.group is not None
        assert result.group.id == "999999999"
        assert result.group.name is None  # the page describes another group


class TestFeedSynthetic:
    def read(self) -> tuple[ReadParser, list[StepParse]]:
        parser = ReadParser(Surface.FEED)
        results = [parser.parse(s) for s in load_steps("feed_hchr_synthetic.json")]
        return parser, results

    def test_fixture_is_labelled_synthetic(self) -> None:
        assert fixture_meta("feed_hchr_synthetic.json")["synthetic"] is True

    def test_positions_batches_anchors_and_dedupe(self) -> None:
        parser, results = self.read()
        got = [(s.post_id, s.position, s.batch, s.is_anchor) for s in parser.stories]
        assert got == [
            (post(1), 0, 0, False),
            (post(2), 1, 0, False),
            (post(3), 2, 1, True),
            (post(4), 3, 1, False),
            (post(5), 4, 1, False),
            (post(7), 5, 1, False),
            (post(8), 6, 1, False),
            (post(10), 7, 1, False),
            (post(11), 8, 2, True),
            (post(12), 9, 2, False),
        ]
        # post 2 came back in the second batch; the SSR sighting (first position) wins.
        assert [s.post_id for s in results[2].stories] == [post(11), post(12)]
        assert results[2].health.dropped == {"duplicate": 1}
        assert results[3].stories == ()

    def test_fields_of_a_plain_story(self) -> None:
        parser, _ = self.read()
        first = parser.stories[0]
        assert first.group_id == G1
        assert first.created_at == 1_790_300_000 - 120
        assert first.url == f"https://www.facebook.com/groups/{G1}/posts/{post(1)}/"
        assert first.text == "Synthetic text: 2-room flat in Lozenets, 650 EUR."
        assert first.author.id == user(1)
        assert first.author.name == "Synthetic Author One"
        assert first.cursor == "cursor-feed-1"
        assert first.friendly_name is None  # it came from the SSR

    def test_share_listing_anonymous_and_nested_share(self) -> None:
        parser, _ = self.read()
        by_id = {s.post_id: s for s in parser.stories}
        share = by_id[post(2)]
        assert share.group_id == G2
        assert share.is_share
        assert share.text == "Synthetic shared text: studio near the sea."
        listing = by_id[post(4)]
        assert listing.listing == ListingRef(
            id="700000000000001",
            title="Synthetic listing: grey sofa",
            price="BGN 120",
            location="Plovdiv, Bulgaria",
        )
        assert listing.text == (
            "Synthetic text: selling a sofa.\nSynthetic listing: grey sofa\nBGN 120\n"
            "Plovdiv, Bulgaria"
        )
        anonymous = by_id[post(5)].author
        assert anonymous.anonymous
        assert (anonymous.id, anonymous.url) == (None, None)
        assert anonymous.name == "Anonymous participant 4821"
        nested = by_id[post(12)]
        assert nested.text == (
            "Synthetic middle text: shared once.\nSynthetic nested text: the original."
        )
        assert nested.friendly_name == "CometNewsFeedPaginationQuery"

    def test_deferred_stories_say_why(self) -> None:
        parser, _ = self.read()
        by_id = {s.post_id: s for s in parser.stories}
        assert by_id[post(7)].deferred_reason is DeferredReason.NO_CREATED_AT
        conflict = by_id[post(8)]
        assert conflict.deferred_reason is DeferredReason.NO_GROUP
        assert conflict.group_id is None and conflict.url is None
        assert ("content.target_group", G2) in conflict.group_candidates
        assert ("to", G1) in conflict.group_candidates
        no_text = by_id[post(10)]
        assert no_text.deferred_reason is DeferredReason.NO_TEXT
        assert no_text.text == ""

    def test_read_health_and_page_info(self) -> None:
        parser, results = self.read()
        assert results[0].page_info == PageInfo("news_feed", True, "cursor-feed-2")
        assert results[1].page_info == PageInfo("news_feed", True, "cursor-feed-10")
        assert parser.page_info == PageInfo("news_feed", False, "cursor-feed-13")
        health = parser.health
        assert health is not None
        assert (health.units, health.found, health.accepted, health.deferred) == (13, 10, 7, 3)
        assert health.dropped == {"sponsored": 1, "non_story": 1, "duplicate": 1}
        assert health.missing == {"no_created_at": 1, "no_group": 1, "no_text": 1}
        assert health.empty_text == 1
        assert health.group_conflicts == 1
        assert health.text_paths == {"content.message": 7, "attached_story": 2, "(none)": 1}
        assert health.friendly_names == {"CometNewsFeedPaginationQuery": 2}
        # Stories per pagination response: two bodies carried stories (6 and 2), the SSR's
        # stories and the duplicate do not count.
        assert (health.story_bodies, health.batch_stories) == (2, 8)
        assert health.steps == 4
        assert health.dom_posts == 11
        stats = health.as_stats()
        assert json.loads(json.dumps(stats)) == stats

    def test_feed_preloader_is_chronological(self) -> None:
        parser, _ = self.read()
        assert parser.preloader.feed_style == "MOST_RECENT_GROUPS_FEED"
        assert parser.preloader.orderby == ("MOST_RECENT",)
        assert parser.preloader.feed_initial_fetch_size == 3
        assert parser.preloader.feed_is_chrono is True
        assert parser.preloader.summary()["feedStyle"] == "MOST_RECENT_GROUPS_FEED"
        assert parser.markers.viewer_present >= 1


ATTACHMENTS = "feed_attachments_synthetic.json"


class TestAttachmentsSynthetic:
    """The phase-0 shapes whose text is in an attachment: a link share (C), a shared
    Marketplace listing and a share of a group's listing post (D); synthetic until a capture
    confirms the paths. The share of nothing (A), of an unavailable post (B) and a photo stay
    empty."""

    def read(self) -> dict[str, Any]:
        parser = ReadParser(Surface.FEED)
        for s in load_steps(ATTACHMENTS):
            parser.parse(s)
        self.health = parser.health
        return {s.post_id: s for s in parser.stories}

    def test_fixture_is_labelled_synthetic(self) -> None:
        meta = fixture_meta(ATTACHMENTS)
        assert meta["synthetic"] is True
        assert "synthetic until a capture confirms the paths" in meta["source"]

    def test_a_link_share_reads_the_links_title_description_and_source(self) -> None:
        story = self.read()[post(301)]
        assert story.text.splitlines() == [
            "Synthetic link title: two-room flat in Lozenets, 650 EUR",
            "Synthetic link description: furnished, near the metro, free from November.",
            "SYNTHETIC-HOMES.BG",
        ]
        assert story.text_path == "attachment.link"
        assert story.listing is None and not story.deferred

    def test_a_link_under_the_members_own_words_is_appended(self) -> None:
        story = self.read()[post(302)]
        assert story.text.splitlines() == [
            "Synthetic text: have a look at this one.",
            "Synthetic link title: two-room flat in Lozenets, 650 EUR",
            "Synthetic link description: furnished, near the metro, free from November.",
            "SYNTHETIC-HOMES.BG",
        ]
        assert story.text_path == "content.message"

    def test_a_shared_marketplace_listing(self) -> None:
        story = self.read()[post(303)]
        assert story.listing == ListingRef(
            id="700000000000011",
            title="Synthetic listing: corner sofa, grey",
            price="250 лв.",
            location="Plovdiv, Bulgaria",
        )
        assert story.text.splitlines() == [
            "Synthetic listing: corner sofa, grey",
            "250 лв.",
            "Plovdiv, Bulgaria",
        ]
        assert story.text_path == "attachment.listing"

    def test_a_share_of_a_groups_listing_post(self) -> None:
        story = self.read()[post(304)]
        assert story.listing == ListingRef(
            id="700000000000012",
            title="Synthetic listing: bicycle, 26 inch",
            price="BGN 90",
            location="Varna",
        )
        assert story.text == "Synthetic listing: bicycle, 26 inch\nBGN 90\nVarna"
        assert story.text_path == "attachment.listing" and story.is_share

    def test_what_offers_no_text_stays_empty(self) -> None:
        stories = self.read()
        for n in (305, 306, 307):  # a share of nothing, of an unavailable post, a photo
            assert stories[post(n)].text == ""
            assert stories[post(n)].text_path is None
            assert not stories[post(n)].deferred

    def test_nothing_beside_the_text_becomes_text(self) -> None:
        """Links, tokens, captions, buttons, the seller and Facebook's notice of an
        unavailable post are in the fixture and in no story's text."""
        text = "\n".join(s.text or "" for s in self.read().values())
        for noise in (
            "http",
            "example.invalid",
            "IwZXh0bg",
            "AZXsynthetic",
            "c3ludGhldGlj",
            "Synthetic caption",
            "Synthetic button",
            "Synthetic Seller Name",
            "Synthetic notice",
            "Synthetic Member",
            "710000000000001",
        ):
            assert noise not in text
            assert noise in (FIXTURES / ATTACHMENTS).read_text(encoding="utf-8")

    def test_text_paths_in_the_health_counters(self) -> None:
        self.read()
        assert self.health is not None
        assert self.health.text_paths == {
            "attachment.link": 1,
            "content.message": 1,
            "attachment.listing": 2,
            "(none)": 3,
        }
        assert self.health.empty_text == 3
        assert self.health.card_beside_words == 1  # the link under the member's words
        assert self.health.missing == {}
        assert self.health.text_classes == {
            "attachment_text": 3,
            "share_without_comment": 2,
            "media_only": 1,
        }
        assert self.health.posts[post(301)] == PostMark(
            accepted=True, text_class=TextClass.ATTACHMENT_TEXT
        )
        assert self.health.posts[post(302)] == PostMark(accepted=True)


class TestGroupPaginationSynthetic:
    def test_highlights_anchor_and_dedupe_across_scrolls(self) -> None:
        parser = ReadParser(Surface.GROUP, group_id=G4)
        for s in load_steps("group_pagination_synthetic.json"):
            parser.parse(s)
        got = [(s.post_id, s.batch, s.is_highlight, s.is_anchor) for s in parser.stories]
        assert got == [
            (post(198), 1, True, False),  # a highlight is never the anchor
            (post(101), 2, False, True),
            (post(102), 2, False, False),
            (post(103), 2, False, False),
            (post(199), 3, False, True),  # the injected out-of-order story
            (post(104), 3, False, False),
            (post(105), 3, False, False),
            (post(106), 3, False, False),
        ]
        assert all(s.group_id == G4 for s in parser.stories)
        assert parser.page_info == PageInfo("group_feed", False, "cursor-group-8")
        assert parser.health is not None
        assert parser.health.dropped == {"section_header": 1, "duplicate": 1}
        assert parser.group is not None and parser.group.id == G4


class TestJoinsSynthetic:
    def test_joined_groups_over_three_steps(self) -> None:
        parser = ReadParser(Surface.JOINS)
        results = [parser.parse(s) for s in load_steps("groups_joins_synthetic.json")]
        joined = parser.joined
        assert joined is not None
        assert [g.id for g in joined.groups] == [G1, G2, G3, G4, "400000000000005"]
        assert joined.groups[1].slug == "synthetic-rentals-varna"
        assert joined.excluded == 1  # the suggested group the account has not joined
        assert joined.connections == {"joined_groups": 6}
        assert results[0].page_info == PageInfo("joined_groups", True, "cursor-joins-3")
        # The last page arrives without a single group; the learned connection still counts.
        assert results[2].page_info == PageInfo("joined_groups", False, None)
        assert parser.page_info == PageInfo("joined_groups", False, None)
        assert parser.stories == []


class TestErrorsAndMarkers:
    def test_rate_limit_logout_and_viewer_null(self) -> None:
        (only,) = load_steps("graphql_errors_synthetic.json")
        result = parse_step(only, surface=Surface.FEED)
        rate, logged_out, field = result.errors
        assert rate == GraphqlError(
            code=1675004,
            summary="Rate limit exceeded",
            severity="CRITICAL",
            friendly_name="CometNewsFeedPaginationQuery",
        )
        assert rate.rate_limited
        assert logged_out.code == 1357001 and not logged_out.rate_limited
        assert logged_out.summary == "Not logged in"
        assert field.partial and field.code is None
        assert result.markers.viewer_null == ("CometNewsFeedPaginationQuery",)
        assert result.markers.viewer_present == 1

    def test_rate_limit_by_message_only(self) -> None:
        frame = {"errors": [{"message": "Rate limit exceeded for this account"}]}
        result = parse_step(step(body(frame)), surface=Surface.FEED)
        assert result.errors[0].rate_limited
        assert result.errors[0].code is None

    def test_error_codes_in_other_places(self) -> None:
        frames = [
            {"errors": [{"api_error_code": "368", "summary": "x" * 500}]},
            {"errors": [{"extensions": {"code": 1675004}}, "not a dict"]},
            {"error": "not a code"},
        ]
        result = parse_step(step(body(*frames)), surface=Surface.FEED)
        assert [e.code for e in result.errors] == [368, 1675004]
        assert len(result.errors[0].summary or "") == parser_module.MAX_ERROR_SUMMARY

    def test_user_id_and_locale_from_the_defines(self) -> None:
        define = ["CurrentUserInitialData", [], {"ACCOUNT_ID": "12345", "USER_ID": "12345"}, 270]
        locale = ["IntlCurrentLocale", [], {"code": "en_US"}, 1]
        other = {"USER_ID": "777", "x": ["CurrentUserInitialData", [], "not a config"]}
        html = (
            '<script type="application/json">'
            + json.dumps({"x": other})
            + '</script><script type="application/json">'
            + json.dumps({"define": [define, locale]})
            + "</script>"
        )
        result = parse_step(step(document=html.encode()), surface=Surface.FEED)
        assert result.markers.user_id == "12345"
        assert result.markers.logged_in is True
        assert result.markers.locale == "en_US"

    def test_logged_out_user_id(self) -> None:
        define = ["CurrentUserInitialData", [], {"ACCOUNT_ID": "0", "USER_ID": 0}, 270]
        html = f'<script type="application/json">{json.dumps({"define": [define]})}</script>'
        result = parse_step(step(document=html.encode()), surface=Surface.FEED)
        assert result.markers.user_id == "0"
        assert result.markers.logged_in is False

    def test_user_id_fallbacks(self) -> None:
        html = f'<script type="application/json">{json.dumps({"USER_ID": "42"})}</script>'
        assert (
            parse_step(step(document=html.encode()), surface=Surface.FEED).markers.user_id == "42"
        )
        raw = b'<script>requireLazy(["x"], {"USER_ID":"4242"})</script>'
        assert parse_step(step(document=raw), surface=Surface.FEED).markers.user_id == "4242"
        assert (
            parse_step(step(document=b"<html></html>"), surface=Surface.FEED).markers.logged_in
            is None
        )

    def test_markers_merge(self) -> None:
        a = SessionMarkers(user_id=None, viewer_present=1, viewer_null=("A",))
        b = SessionMarkers(user_id="5", viewer_present=2, viewer_null=("B",), locale="en_US")
        assert a.merged(b) == SessionMarkers("5", 3, ("A", "B"), "en_US")
        assert b.merged(SessionMarkers("0")).user_id == "5"


class TestParseStep:
    def test_anchor_is_first_story_of_each_body_even_across_steps(self) -> None:
        parser = ReadParser(Surface.FEED)
        first = parser.parse(step(body(feed_frame(story("11"), story("12")))))
        second = parser.parse(
            step(body(feed_frame(story("13"))), body(feed_frame(story("14"), story("15"))))
        )
        assert [(s.post_id, s.batch, s.is_anchor) for s in first.stories] == [
            ("11", 1, True),
            ("12", 1, False),
        ]
        assert [(s.post_id, s.batch, s.is_anchor) for s in second.stories] == [
            ("13", 2, True),
            ("14", 3, True),
            ("15", 3, False),
        ]
        assert [s.position for s in parser.stories] == [0, 1, 2, 3, 4]
        assert second.next_batch == 4 and second.next_position == 5

    def test_duplicate_anchor_leaves_the_batch_without_an_anchor(self) -> None:
        parser = ReadParser(Surface.FEED)
        parser.parse(step(body(feed_frame(story("11")))))
        result = parser.parse(step(body(feed_frame(story("11"), story("12")))))
        assert [(s.post_id, s.is_anchor) for s in result.stories] == [("12", False)]

    def test_body_without_stories_takes_no_batch_number(self) -> None:
        result = parse_step(
            step(body({"data": {"viewer": {"x": 1}}}), body(feed_frame(story("11")))),
            surface=Surface.FEED,
        )
        assert result.stories[0].batch == 1
        assert result.next_batch == 2

    def test_friendly_names_capped_bodies_and_lost_bodies(self) -> None:
        s = Step(
            final_url="u",
            status=200,
            graphql=(body(feed_frame(), name=None), body(feed_frame(), capped=True)),
            lost_bodies=2,
        )
        health = parse_step(s, surface=Surface.FEED).health
        assert health.friendly_names == {"(none)": 1, "Q": 1}
        assert health.capped_bodies == 1
        assert health.lost_bodies == 2
        assert health.frames == 2

    def test_undecodable_body_is_counted_not_raised(self) -> None:
        broken = GraphqlBody(friendly_name="Q", body=b'for (;;);{"data":{"viewer":')
        deep = GraphqlBody(friendly_name="Q", body=b"[" * 100_000)
        health = parse_step(step(broken, deep), surface=Surface.FEED).health
        assert health.undecodable == 2

    def test_deeply_nested_document_block(self) -> None:
        html = b'<script type="application/json">' + b"[" * 100_000 + b"</script>"
        assert parse_step(step(document=html), surface=Surface.FEED).health.undecodable == 1

    def test_frames_given_as_a_list(self) -> None:
        payload = [feed_frame(story("11")), "junk", feed_frame(story("12"))]
        result = parse_step(step(body(payload)), surface=Surface.FEED)
        assert [s.post_id for s in result.stories] == ["11", "12"]

    def test_unknown_connections_are_counted_not_read(self) -> None:
        frame = {
            "data": {
                "viewer": {
                    "related_posts": {"edges": [{"node": story("11")}, {"node": story("12")}]},
                    "members": {"edges": [{"node": {"id": "1", "name": "x"}}]},
                }
            }
        }
        stream = {"path": ["viewer", "suggested_feed", "edges", 0], "data": {"node": story("13")}}
        empty_stream = {
            "path": ["viewer", "suggested_feed", "edges", 1],
            "data": {"node": {"x": 1}},
        }
        result = parse_step(step(body(frame, stream, empty_stream)), surface=Surface.FEED)
        assert result.stories == ()
        assert result.health.unknown_connections == {"related_posts": 2, "suggested_feed": 1}

    def test_group_surface_resolves_group_from_preloader_for_later_steps(self) -> None:
        preloader = {"variables": {"groupID": "900000000000001", "sortingSetting": "CHRONOLOGICAL"}}
        html = f'<script type="application/json">{json.dumps(preloader)}</script>'.encode()
        parser = ReadParser(Surface.GROUP)
        nav = parser.parse(step(document=html))
        assert nav.group is not None and nav.group.id == "900000000000001"
        assert parser.group_id == "900000000000001"
        group_frame = {"data": {"node": {"group_feed": {"edges": [{"node": story("11", gid=G2)}]}}}}
        scroll = parser.parse(step(body(group_frame)))
        # On a group page every story is the page's group's, whatever else it names.
        assert scroll.stories[0].group_id == "900000000000001"
        assert parser.group is nav.group

    def test_group_slug_learned_on_navigation_resolves_later_permalinks(self) -> None:
        root = {"data": {"group": {"id": "900000000000001", "vanity": "flats-sofia"}}}
        html = (
            '<script type="application/json">'
            + json.dumps({"__bbox": {"result": root}})
            + "</script>"
        ).encode()
        parser = ReadParser(Surface.GROUP)
        parser.parse(step(document=html))
        node = story("11", gid="900000000000001")
        node["permalink_url"] = "https://www.facebook.com/groups/flats-sofia/posts/11/"
        result = parser.parse(
            step(body({"data": {"node": {"group_feed": {"edges": [{"node": node}]}}}}))
        )
        assert ("permalink.slug", "900000000000001") in result.stories[0].group_candidates

    def test_page_info_inline_and_defer_parts_without_it(self) -> None:
        frames = [
            {
                "data": {
                    "viewer": {"news_feed": {"edges": [], "page_info": {"has_next_page": "yes"}}}
                }
            },
            {"label": "x$defer$y", "path": ["viewer", "news_feed"], "data": {"other": 1}},
            {"path": ["viewer", "unrelated"], "data": {"page_info": {"has_next_page": False}}},
        ]
        result = parse_step(step(body(*frames)), surface=Surface.FEED)
        assert result.page_info == PageInfo("news_feed", None, None)

    def test_no_document_no_bodies(self) -> None:
        result = parse_step(step(), surface=Surface.JOINS)
        assert result.stories == ()
        assert result.joined is not None and result.joined.groups == ()
        assert result.page_info is None
        assert result.health == ParseHealth(steps=1)


class TestQuarantine:
    def test_a_story_that_raises_is_quarantined_and_the_rest_survive(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        real = stories_module.read_story

        def flaky(unit: Unit, ctx: Context) -> Reading | DropReason:
            if unit.node.get("post_id") == "12":
                raise KeyError("boom")
            return real(unit, ctx)

        monkeypatch.setattr(parser_module, "read_story", flaky)
        secret = "private words that must not reach the log"
        frame = feed_frame(story("11"), story("12", text=secret), story("13"))
        with caplog.at_level(logging.WARNING):
            result = parse_step(step(body(frame)), surface=Surface.FEED)
        assert [s.post_id for s in result.stories] == ["11", "13"]
        (q,) = result.quarantined
        assert q.post_id == "12" and q.error == "KeyError" and q.friendly_name == "Q"
        assert json.loads(q.raw)["post_id"] == "12"
        assert result.health.dropped == {"quarantined": 1}
        assert result.health.found == 3 and result.health.accepted == 2
        assert secret not in caplog.text
        assert "quarantined" in caplog.text

    def test_quarantine_caps_raw_and_hides_odd_post_ids(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(unit: Unit, ctx: Context) -> Reading | DropReason:
            raise ValueError

        monkeypatch.setattr(parser_module, "read_story", boom)
        node = story("11", text="x" * (parser_module.MAX_QUARANTINE_BYTES + 10))
        node["post_id"] = "not-digits"
        parser = ReadParser(Surface.FEED)
        parser.parse(step(body(feed_frame(node))))
        (q,) = parser.quarantined
        assert q.post_id is None
        assert len(q.raw) == parser_module.MAX_QUARANTINE_BYTES

    def test_unserialisable_node_falls_back_to_repr(self) -> None:
        unit = Unit({"post_id": "1", "bad": {1, 2}}, "news_feed", Frame({}, 0, "Q"))
        cyclic: dict[str, Any] = {"post_id": "2"}
        cyclic["self"] = cyclic
        assert parser_module._quarantine(unit, ValueError()).raw.startswith(b"{")
        loop = Unit(cyclic, "news_feed", Frame({}, 0, None))
        assert b"post_id" in parser_module._quarantine(loop, ValueError()).raw


def read_of(
    *posts: tuple[int, PostMark], failures: int = 0, dom: int = 0, found: int | None = None
) -> ParseHealth:
    """One read's health: the posts it read (by number) and the stories it failed to read."""
    marks = {post(n): mark for n, mark in posts}
    return ParseHealth(
        found=len(marks) + failures if found is None else found,
        accepted=sum(m.accepted for m in marks.values()),
        dom_posts=dom,
        dropped={"no_post_id": failures} if failures else {},
        posts=marks,
    )


OK = PostMark(accepted=True)
DEFERRED = PostMark(accepted=False)
NO_TIME = PostMark(accepted=False, no_time=True)
UNEXPLAINED = PostMark(accepted=True, text_class=TextClass.UNEXPLAINED)
SHARE = PostMark(accepted=True, text_class=TextClass.SHARE_WITHOUT_COMMENT)
CARD = PostMark(accepted=True, text_class=TextClass.ATTACHMENT_TEXT)
MEDIA = PostMark(accepted=True, text_class=TextClass.MEDIA_ONLY)


def same_posts(marks: dict[int, PostMark], reads: int = 3) -> list[ParseHealth]:
    """``reads`` reads of the same posts: a feed read again before anything new arrived."""
    return [read_of(*marks.items()) for _ in range(reads)]


class TestParserVerdict:
    def test_needs_three_reads(self) -> None:
        bad = dict.fromkeys(range(10), DEFERRED)
        assert parser_verdict(same_posts(bad, reads=2)).broken is False

    def test_accept_ratio_over_the_window_counts_each_post_once(self) -> None:
        """The latest read of a post decides; the stories without a post id count as often
        as the window read its posts again."""
        first = read_of(*((n, OK) for n in range(10)))
        again = read_of(*((n, DEFERRED if n < 6 else OK) for n in range(10)))
        verdict = parser_verdict([first, first, again, again])
        assert verdict.broken
        assert verdict.reasons == ("accepted 4 of 10 distinct stories over 3 reads",)
        assert not parser_verdict([again, again, first]).broken  # read again, all accepted
        failing = read_of((1, OK), failures=2)
        verdict = parser_verdict([failing] * 3)
        assert verdict.reasons == ("accepted 1 of 3 distinct stories over 3 reads",)
        pages = [read_of((n, OK), failures=2) for n in range(3)]  # three different pages
        verdict = parser_verdict(pages)
        assert verdict.reasons == ("accepted 3 of 9 distinct stories over 3 reads",)
        assert parser_verdict([read_of(failures=1)] * 3).reasons == (
            "accepted 0 of 3 distinct stories over 3 reads",  # no post read: each counts
        )

    def test_failures_read_again_count_like_the_posts(self) -> None:
        """A feed read three times over the same 10 posts and 4 stories without a post id:
        10 of 14 in every read, 10 of 14 over the window, not 10 of 22 (broken)."""
        posts = dict.fromkeys(range(10), OK)
        verdict = parser_verdict([read_of(*posts.items(), failures=4) for _ in range(3)])
        assert verdict == ParserVerdict()
        lopsided = [read_of(*posts.items(), failures=f) for f in (0, 0, 1)]
        assert parser_verdict(lopsided, min_accepted=0.95).reasons == (
            "accepted 10 of 11 distinct stories over 3 reads",  # 1/3 of a failure rounds up
        )
        broken = [read_of(*posts.items(), failures=11) for _ in range(3)]
        assert parser_verdict(broken).reasons == (
            "accepted 10 of 21 distinct stories over 3 reads",
        )
        marks = {**posts, 0: NO_TIME, 1: NO_TIME, 2: NO_TIME}
        verdict = parser_verdict([read_of(*marks.items(), failures=2) for _ in range(3)])
        assert verdict.warnings == {  # 3 of 16 with every read's failures: no alert
            "no_created_at": "3 of 12 distinct stories without a creation time"
        }

    def test_a_post_read_again_counts_once(self) -> None:
        """The phase-0 alert said "11 of 33 stories with empty text": three reads of the same
        11 posts. Counted once each, 3 of 11 is still over 20 %, 2 of 11 is not."""
        marks = dict.fromkeys(range(11), OK)
        marks.update({0: UNEXPLAINED, 1: UNEXPLAINED, 2: UNEXPLAINED})
        verdict = parser_verdict(same_posts(marks))
        assert verdict.warnings == {
            "empty_text": "3 of 11 distinct posts with empty text nothing explains"
        }
        marks[2] = OK
        assert parser_verdict(same_posts(marks)).warnings == {}

    def test_only_unexplained_empty_text_warns(self) -> None:
        """Explained classes do not count, up to half the posts; past that the backstop warns
        instead, unless the empty-text alert already does (it lists the classes)."""
        marks = dict.fromkeys(range(20), OK)
        marks.update(dict.fromkeys(range(4), SHARE))
        marks.update({4: CARD, 5: CARD, 6: MEDIA})
        assert parser_verdict(same_posts(marks)).warnings == {}
        marks.update(dict.fromkeys(range(7, 12), UNEXPLAINED))
        assert parser_verdict(same_posts(marks)).warnings == {
            "empty_text": (
                "5 of 20 distinct posts with empty text nothing explains"
                " (explained: attachment_text 2, media_only 1, share_without_comment 4)"
            )
        }

    def test_more_than_half_without_their_authors_words_warns(self) -> None:
        """A renamed message section makes a photo post ``media_only`` and a link post
        ``attachment_text``; only plain-text posts come out unexplained. The backstop looks at
        the distinct posts of the last 10 reads, once there are 10: the review's window (9
        photos, 2 plain posts and a link, 2 of 12 unexplained) read ten times warns."""
        marks = {**dict.fromkeys(range(9), MEDIA), 9: UNEXPLAINED, 10: UNEXPLAINED, 11: CARD}
        assert parser_verdict(same_posts(marks, reads=9)).warnings == {}  # 9 reads: too few
        assert parser_verdict(same_posts(marks, reads=10)).warnings == {
            "no_own_text": (
                "12 of 12 distinct posts without their author's words"
                " (attachment_text 1, media_only 9, unexplained 2)"
            )
        }
        half = {**dict.fromkeys(range(10), OK), **dict.fromkeys(range(5), MEDIA)}
        assert parser_verdict(same_posts(half, reads=10)).warnings == {}  # 5 of 10: not more
        half[5] = SHARE
        assert parser_verdict(same_posts(half, reads=10)).warnings == {
            "no_own_text": (
                "6 of 10 distinct posts without their author's words"
                " (media_only 5, share_without_comment 1)"
            )
        }
        nine = dict.fromkeys(range(9), MEDIA)
        assert parser_verdict(same_posts(nine, reads=10)).warnings == {}  # too few posts

    def test_a_few_wordless_reads_among_many_do_not_warn(self) -> None:
        """The live false alarm was 6 of 10 over three reads. The ten reads' posts decide:
        here 6 of 10 over the last nine reads, 6 of 40 over ten."""
        many = read_of(*((n, OK) for n in range(100, 130)))
        few = {**dict.fromkeys(range(10), OK), **dict.fromkeys(range(6), MEDIA)}
        reads = [many, *same_posts(few, reads=9)]
        assert parser_verdict(reads).warnings == {}
        assert (
            parser_verdict(reads, wordless_window=9).warnings["no_own_text"].startswith("6 of 10")
        )

    def test_a_renamed_message_section_alerts_whatever_the_posts_carry(self) -> None:
        """Stories whose ``comet_sections.message`` Facebook renamed, parsed: the photos,
        links and shares keep their classes, and the backstop still warns."""

        def renamed(n: int, **extra: Any) -> dict[str, Any]:
            node = story(post(n))
            section = {"message_v2": {"story": {"message": {"text": "Думите на автора"}}}}
            node["comet_sections"]["content"]["story"] = {"comet_sections": section, **extra}
            return node

        photo = {
            "__typename": "StoryAttachment",
            "target": {"__typename": "Photo", "id": "1"},
            "styles": {"__typename": "StoryAttachmentPhotoStyleRenderer"},
        }
        link = {
            "styles": {
                "__typename": "StoryAttachmentShareStyleRenderer",
                "attachment": {
                    "title_with_entities": {"text": "Двустаен, Лозенец"},
                    "source": {"text": "IMOT.BG"},
                },
            }
        }
        nodes = [
            *(renamed(n, attachments=[photo]) for n in range(6)),
            *(renamed(n, attachments=[link]) for n in range(6, 9)),
            *(renamed(n, attached_story={"__typename": "Story"}) for n in range(9, 11)),
            renamed(11),
        ]
        health = parse_step(step(body(feed_frame(*nodes))), surface=Surface.FEED).health
        assert health.text_classes == {
            "media_only": 6,
            "attachment_text": 3,
            "share_without_comment": 2,
            "unexplained": 1,
        }
        assert parser_verdict([health] * 10).warnings == {
            "no_own_text": (
                "12 of 12 distinct posts without their author's words"
                " (attachment_text 3, media_only 6, share_without_comment 2, unexplained 1)"
            )
        }
        for node in nodes:
            node["comet_sections"]["content"]["story"]["message"] = {"text": "Думите"}
        health = parse_step(step(body(feed_frame(*nodes))), surface=Surface.FEED).health
        assert health.text_classes == {}
        assert parser_verdict([health] * 10).warnings == {}

    def test_the_empty_text_rate_needs_ten_distinct_posts(self) -> None:
        nine = dict.fromkeys(range(9), UNEXPLAINED)
        assert parser_verdict(same_posts(nine)).warnings == {}  # 27 reads, 9 posts
        spread = [read_of((n, UNEXPLAINED), (n + 10, OK), (n + 20, OK)) for n in range(3)]
        assert parser_verdict(spread).warnings == {}  # 3 of 9 distinct posts
        spread.append(read_of((30, OK), (31, UNEXPLAINED), (32, UNEXPLAINED), (33, OK)))
        verdict = parser_verdict(spread)  # the last three reads: 3 + 3 + 4 posts
        assert verdict.warnings["empty_text"].startswith("4 of 10 distinct posts")

    def test_zero_stories_while_the_dom_shows_posts(self) -> None:
        verdict = parser_verdict([read_of(dom=5)] * 3)
        assert verdict.broken and "no stories" in verdict.reasons[0]
        assert not parser_verdict([read_of(dom=5), read_of(), read_of(dom=5)]).broken
        assert not parser_verdict([read_of()] * 3).broken

    def test_missing_times_count_each_post_once(self) -> None:
        marks = dict.fromkeys(range(10), OK)
        marks.update({0: NO_TIME, 1: NO_TIME, 2: NO_TIME})
        verdict = parser_verdict(same_posts(marks))
        assert verdict.warnings == {
            "no_created_at": "3 of 10 distinct stories without a creation time"
        }
        few = {0: NO_TIME, 1: OK, 2: OK, 3: OK}
        assert parser_verdict(same_posts(few)).warnings == {}  # 4 stories: too few for a rate
        marks[2] = OK
        assert parser_verdict(same_posts(marks)).warnings == {}

    def test_health_merge(self) -> None:
        a = ParseHealth(
            units=2,
            found=1,
            dropped={"x": 1},
            dom_posts=5,
            page_info=False,
            text_classes={"unexplained": 1},
            posts={post(1): DEFERRED},
        )
        b = ParseHealth(
            units=3,
            found=2,
            dropped={"x": 2, "y": 1},
            dom_posts=3,
            page_info=True,
            text_classes={"unexplained": 1, "media_only": 1},
            posts={post(1): OK, post(2): OK},
        )
        a = replace(a, card_beside_words=1)
        b = replace(b, card_beside_words=2)
        merged = a.merged(b)
        assert merged.card_beside_words == 3
        assert merged.as_stats()["card_beside_words"] == 3
        assert (merged.units, merged.found, merged.dom_posts, merged.steps) == (5, 3, 5, 2)
        assert merged.dropped == {"x": 3, "y": 1}
        assert merged.page_info
        assert merged.text_classes == {"unexplained": 2, "media_only": 1}
        assert merged.posts == {post(1): OK, post(2): OK}
        assert "posts" not in merged.as_stats()  # post ids stay out of the reads table
        assert merged.as_stats()["text_classes"] == {"unexplained": 2, "media_only": 1}

    def test_translation_fields_count_stories(self) -> None:
        nodes = [
            {**story(post(1)), "translatability_for_viewer": {"source_dialect": "bg_BG"}},
            {**story(post(2)), "translation": None, "translated_message": None},
            story(post(3)),
        ]
        health = parse_step(step(body(feed_frame(*nodes))), surface=Surface.FEED).health
        assert health.translation_fields == 2  # stories, not keys
        merged = health.merged(health)
        assert merged.translation_fields == 4
        assert merged.as_stats()["translation_fields"] == 4

    def test_failures(self) -> None:
        health = ParseHealth(dropped={"no_post_id": 2, "quarantined": 1, "duplicate": 4})
        assert health.failures == 3


@pytest.mark.parametrize(
    "mark",
    [OK, DEFERRED, NO_TIME, UNEXPLAINED, SHARE, CARD, PostMark(False, True, TextClass.MEDIA_ONLY)],
)
def test_post_marks_round_trip(mark: PostMark) -> None:
    assert PostMark.from_json(json.loads(json.dumps(mark.as_json()))) == mark


@pytest.mark.parametrize(
    "raw", [None, [], [1, 0], [2, 0, None], [1, 0, "other"], [1, "0", None], {"a": 1}, [1, 0, 7]]
)
def test_post_marks_refuse_anything_else(raw: Any) -> None:
    assert PostMark.from_json(raw) is None


def test_drop_reasons_by_design() -> None:
    assert DropReason.DUPLICATE.by_design
    assert DropReason.SECTION_HEADER.by_design
    assert not DropReason.NO_POST_ID.by_design
    assert not DropReason.QUARANTINED.by_design
