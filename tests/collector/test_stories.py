from __future__ import annotations

import functools
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from ddiver_core.rules import CompiledRule, compile_rule, prepare_text
from fbcollector import stories
from fbcollector.model import AuthorRef, DeferredReason, ListingRef
from fbcollector.relay import Frame, WalkStats
from fbcollector.stories import (
    Context,
    DropReason,
    Reading,
    TextClass,
    Unit,
    find_units,
    own_text,
    read_story,
    story_like,
)

G1, G2 = "400000000000001", "400000000000002"
T = 1_790_000_000


def unit(node: dict[str, Any], *, connection: str = "news_feed", cursor: str | None = None) -> Unit:
    return Unit(node, connection, Frame({}, 0, "Q"), cursor)


def read(node: dict[str, Any], ctx: Context | None = None, **kw: Any) -> Reading:
    result = read_story(unit(node, **kw), ctx or Context())
    assert isinstance(result, Reading), result
    return result


def base(**extra: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "__typename": "Story",
        "post_id": "11",
        "creation_time": T,
        "to": {"__typename": "Group", "id": G1},
        "comet_sections": {"content": {"story": {"message": {"text": "hello"}}}},
    }
    node.update(extra)
    return node


def content(node: dict[str, Any]) -> dict[str, Any]:
    story: dict[str, Any] = node["comet_sections"]["content"]["story"]
    return story


@functools.lru_cache(maxsize=4096)
def _rule(raw: str) -> CompiledRule:
    return compile_rule(raw, timeout=5.0)


def fires(rule: str, text: str) -> bool:
    """Whether the brain's matcher fires ``rule`` on a post whose text is ``text``: the same
    ``prepare_text`` and ``compile_rule`` the brain runs (``ddiver_fb.source``)."""
    return _rule(rule).search(prepare_text(text).shadow, 5.0) is not None


class TestDrops:
    @pytest.mark.parametrize(
        ("node", "reason"),
        [
            (
                {"__typename": "GroupsSectionHeaderUnit", "title": {"text": "New posts"}},
                DropReason.SECTION_HEADER,
            ),
            ({"__typename": "Comment", "post_id": "11"}, DropReason.COMMENT),
            ({"__typename": "GroupsYouShouldJoinFeedUnit", "id": "x"}, DropReason.NON_STORY),
            ({"__typename": "Story", "comet_sections": {}}, DropReason.NO_POST_ID),
            ({"creation_time": T}, DropReason.NON_STORY),
            (base(post_id="pfbid0abc"), DropReason.NO_POST_ID),
            (base(post_id="0123"), DropReason.NO_POST_ID),
            (base(post_id="99999999999999999999"), DropReason.NO_POST_ID),
            (base(sponsored_data={"ad_id": "1"}), DropReason.SPONSORED),
            (base(th_dat_spo="x"), DropReason.SPONSORED),
            (base(__typename="SponsoredStory"), DropReason.SPONSORED),
        ],
    )
    def test_reasons(self, node: dict[str, Any], reason: DropReason) -> None:
        assert read_story(unit(node), Context()) is reason

    def test_null_or_empty_sponsored_data_is_not_an_ad(self) -> None:
        node = base(sponsored_data=None, th_dat_spo={})
        content(node)["sponsored_data"] = False
        assert isinstance(read_story(unit(node), Context()), Reading)

    def test_sponsored_data_inside_the_shared_post_does_not_count(self) -> None:
        node = base(attached_story={"post_id": "22", "sponsored_data": {"ad_id": "1"}})
        assert isinstance(read_story(unit(node), Context()), Reading)

    def test_highlight_without_story(self) -> None:
        node = {"__typename": "GroupHighlightUpcomingEventsUnit", "id": "1:upcoming_events"}
        assert (
            read_story(unit(node, connection="highlight_units"), Context()) is DropReason.NON_STORY
        )

    def test_highlight_unit_id_is_the_post_id_of_last_resort(self) -> None:
        inner = base()
        del inner["post_id"]
        node = {"__typename": "GroupHighlightPostUnit", "id": "77", "story": inner}
        story = read(node, connection="highlight_units").story
        assert story.post_id == "77"
        assert story.is_highlight
        assert not story.regular


class TestPostIdAndTime:
    def test_post_id_fallbacks(self) -> None:
        node = base()
        del node["post_id"]
        content(node)["post_id"] = "12"
        assert read(node).story.post_id == "12"
        del content(node)["post_id"]
        node["comet_sections"]["context_layout"] = {
            "story": {"comet_sections": {"title": {"story": {"post_id": 13}}}}
        }
        assert read(node).story.post_id == "13"

    def test_created_at_prefers_the_node_then_timestamp_strategy(self) -> None:
        node = base(creation_time=None)
        node["comet_sections"]["context_layout"] = {
            "story": {
                "comet_sections": {
                    "metadata": [
                        {"__typename": "CometStoryCommunityAttributionGroupPostByStrategy"},
                        {
                            "__typename": "CometFeedStoryAudienceStrategy",
                            "story": {"creation_time": T - 9},
                        },
                        {
                            "__typename": "CometFeedStoryBackdatedTimestampStrategy",
                            "story": {"creation_time": T - 5},
                        },
                    ]
                }
            }
        }
        node["comet_sections"]["timestamp"] = {"story": {"creation_time": T - 1}}
        assert read(node).story.created_at == T - 5  # typename dispatch before position

    def test_renamed_timestamp_strategy_still_yields_a_time(self) -> None:
        node = base(creation_time="junk")
        node["comet_sections"]["context_layout"] = {
            "story": {
                "comet_sections": {
                    "metadata": ["junk", {"__typename": "X", "story": {"creation_time": str(T)}}]
                }
            }
        }
        assert read(node).story.created_at == T

    def test_last_fallback_and_rejected_values(self) -> None:
        node = base(creation_time=T * 1000)  # milliseconds are not seconds
        node["comet_sections"]["timestamp"] = {"story": {"creation_time": T - 1}}
        assert read(node).story.created_at == T - 1
        node["comet_sections"]["timestamp"] = {"story": {"creation_time": True}}
        story = read(node).story
        assert story.created_at is None
        assert story.deferred_reason is DeferredReason.NO_CREATED_AT

    def test_share_time_is_never_the_shared_posts(self) -> None:
        node = base(creation_time=None, attached_story={"post_id": "22", "creation_time": T - 99})
        content(node)["attached_story"] = {"creation_time": T - 99}
        assert read(node).story.created_at is None


class TestGroup:
    def test_page_group_wins_on_a_group_page(self) -> None:
        story = read(base(), Context(page_group_id=G2)).story
        assert story.group_id == G2
        assert story.url == f"https://www.facebook.com/groups/{G2}/posts/11/"
        assert story.group_candidates == (("to", G1),)

    def test_all_candidates_recorded(self) -> None:
        node = base(
            target_group={"id": G1},
            feedback={"associated_group": {"id": G1}},
            permalink_url=f"https://www.facebook.com/groups/{G1}/posts/11/?__cft__[0]=x",
        )
        content(node)["target_group"] = {"id": G1}
        node["comet_sections"]["context_layout"] = {
            "story": {
                "comet_sections": {
                    "title": {"story": {"to": {"__typename": "Group", "id": G1, "url": "x"}}}
                }
            }
        }
        story = read(node).story
        assert story.group_id == G1
        assert [p for p, _ in story.group_candidates] == [
            "to",
            "target_group",
            "content.target_group",
            "title.to",
            "feedback.associated_group",
            "permalink",
        ]

    def test_disagreeing_fields_leave_the_group_unresolved(self) -> None:
        node = base(target_group={"id": G2})
        reading = read(node)
        assert reading.story.group_id is None
        assert reading.story.deferred_reason is DeferredReason.NO_GROUP
        assert reading.group_conflict

    def test_permalink_must_agree(self) -> None:
        node = base(permalink_url=f"https://www.facebook.com/groups/{G2}/posts/11/")
        reading = read(node)
        assert reading.story.group_id is None and reading.group_conflict

    def test_permalink_alone_is_enough(self) -> None:
        node = base(to=None, wwwURL=f"https://m.facebook.com/groups/{G2}/permalink/11/")
        assert read(node).story.group_id == G2

    def test_two_permalinks_that_disagree(self) -> None:
        node = base(
            to=None,
            permalink_url=f"https://www.facebook.com/groups/{G1}/posts/11/",
            url=f"https://www.facebook.com/groups/{G2}/posts/11/",
        )
        reading = read(node)
        assert reading.story.group_id is None and reading.group_conflict

    def test_vanity_permalink_through_the_slug_map(self) -> None:
        node = base(permalink_url="https://www.facebook.com/groups/flats/posts/11/")
        assert ("permalink.slug", G1) in read(
            node, Context(slugs={"flats": G1})
        ).story.group_candidates
        assert read(node, Context(slugs={"flats": G2})).story.group_id is None
        assert read(node).story.group_id == G1  # an unknown slug cannot be checked

    def test_urls_of_another_post_or_not_a_group_post_are_ignored(self) -> None:
        node = base(
            permalink_url=f"https://www.facebook.com/groups/{G2}/posts/99/",
            url="https://example.com/groups/1/posts/11/",
            wwwURL=f"https://www.facebook.com/groups/{G2}/",
        )
        assert read(node).story.group_id == G1

    def test_to_can_be_a_user(self) -> None:
        node = base(to={"__typename": "User", "id": "5", "url": "https://www.facebook.com/someone"})
        assert read(node).story.group_id is None
        node = base(
            to={"__typename": "Group2", "id": G1, "url": f"https://www.facebook.com/groups/{G1}/"}
        )
        assert read(node).story.group_id == G1


class TestText:
    def test_paths_in_order(self) -> None:
        sections: dict[str, Any] = {
            "message": {"story": {"message": {"text": "from section"}}},
            "message_container": {"story": {"message": {"text": "from container"}}},
        }
        node = base()
        content(node)["comet_sections"] = sections
        assert own_text(node)[:2] == ("hello", "content.message")
        content(node)["message"] = None
        assert own_text(node)[:2] == ("from section", "content.sections.message")
        sections["message"] = {"story": {}}
        assert own_text(node)[:2] == ("from container", "content.sections.message_container")
        sections["message_container"] = None
        sections["message"] = {
            "deep": [{"x": {"message": {"text": "deep"}}}],
            "attached_story": {"message": {"text": "no"}},
        }
        assert own_text(node)[:2] == ("deep", "content.sections.message")
        sections["message"] = {"rich_message": [{"text": "a"}, {"text": ""}, "x", {"text": "b"}]}
        assert own_text(node) == ("a\n\nb", "content.sections.rich_message", True)
        sections["message"] = {"story": {"rich_message": [{"text": " "}]}}
        assert own_text(node) == (None, None, True)

    def test_root_message_and_photo_only(self) -> None:
        assert own_text({"message": {"text": "root"}}) == ("root", "message", True)
        assert own_text({"comet_sections": {"content": {"story": {"message": None}}}}) == (
            None,
            None,
            False,
        )

    def test_text_bearing_section_without_text_defers(self) -> None:
        node = base()
        content(node)["message"] = {"__typename": "TextWithEntities"}
        story = read(node).story
        assert story.deferred_reason is DeferredReason.NO_TEXT
        assert story.text == "" and story.text_path is None

    def test_photo_only_post_is_accepted_with_empty_text(self) -> None:
        node = base()
        content(node)["message"] = None
        story = read(node).story
        assert not story.deferred
        assert story.text == ""

    def test_share_text_on_a_new_line_only_if_different(self) -> None:
        node = base()
        content(node)["attached_story"] = {"message": {"text": "shared"}}
        assert read(node).story.text == "hello\nshared"
        content(node)["attached_story"] = {"message": {"text": "hello"}}
        story = read(node).story
        assert story.text == "hello" and story.is_share

    def test_share_text_from_other_attached_shapes(self) -> None:
        node = base(
            attached_story={"comet_sections": {"message": {"story": {"message": {"text": "top"}}}}}
        )
        content(node)["message"] = None
        story = read(node).story
        assert story.text == "top" and story.text_path == "attached_story"
        node = base()
        content(node)["comet_sections"] = {
            "attached_story": {"story": {"attached_story": {"message": {"text": "renderer"}}}}
        }
        assert read(node).story.text == "hello\nrenderer"

    def test_share_chain_is_bounded(self) -> None:
        chain: dict[str, Any] = {"message": {"text": "level 5"}}
        for level in range(4, 0, -1):
            chain = {"message": {"text": f"level {level}"}, "attached_story": chain}
        node = base()
        content(node)["attached_story"] = chain
        assert read(node).story.text == "hello\nlevel 1\nlevel 2\nlevel 3"

    def test_listing_lines(self) -> None:
        node = base()
        content(node)["attachments"] = [
            {
                "styles": {
                    "attachment": {
                        "title_with_entities": {"text": " Sofa "},
                        "target": {"id": "5", "formatted_price": {"text": "BGN 1"}},
                    }
                }
            },
        ]
        story = read(node).story
        assert story.listing == ListingRef(id="5", title="Sofa", price="BGN 1")
        assert story.text == "hello\nSofa\nBGN 1"

    def test_listing_shapes(self) -> None:
        node = base(
            attachments=[
                "junk",
                {
                    "style_type_renderer": {
                        "attachment": {"marketplace_listing_title": "T", "location_text": "Varna"}
                    }
                },
            ]
        )
        assert read(node).story.listing == ListingRef(title="T", location="Varna")
        node = base(
            attachments=[
                {
                    "target": {
                        "id": 12345,
                        "formatted_price": "BGN 2",
                        "location_text": {"text": "Ruse"},
                    }
                }
            ]
        )
        assert read(node).story.listing == ListingRef(id="12345", price="BGN 2", location="Ruse")
        deep = base(extra={"somewhere": {"marketplace_listing_title": "Deep", "id": "x" * 65}})
        assert read(deep).story.listing == ListingRef(title="Deep")
        empty = base(attachments=[{"target": {"formatted_price": {"text": " "}}}])
        assert read(empty).story.listing is None

    def test_listing_only_story(self) -> None:
        node = base(attachments=[{"target": {"marketplace_listing_title": "Bike"}}])
        content(node)["message"] = None
        story = read(node).story
        assert story.text == "Bike" and story.text_path == "attachment.listing"

    def test_listing_of_the_shared_post_is_appended(self) -> None:
        """A share of a listing post: the shared card's lines are match text, and the wire's
        listing, when the story has no card of its own."""
        node = base(
            attached_story={"attachments": [{"target": {"marketplace_listing_title": "Theirs"}}]}
        )
        story = read(node).story
        assert story.listing == ListingRef(title="Theirs")
        assert story.text == "hello\nTheirs"
        own = base(attachments=[{"target": {"marketplace_listing_title": "Ours"}}])
        own["attached_story"] = node["attached_story"]
        story = read(own).story
        assert story.listing == ListingRef(title="Ours")
        assert story.text == "hello\nOurs\nTheirs"


def link(**attachment: Any) -> dict[str, Any]:
    """A link share's attachment in the shape the research notes give (``styles.attachment``)."""
    fields: dict[str, Any] = {
        "url": "https://l.facebook.com/l.php?u=https%3A%2F%2Fimot.bg%2Fx&h=AT0abc",
        "title_with_entities": {"text": "Двустаен, Лозенец"},
        "description": {"text": "Обзаведен, до метрото."},
        "source": {"text": "IMOT.BG"},
    }
    fields.update(attachment)
    return {
        "__typename": "StoryAttachment",
        "target": {"__typename": "ExternalUrl", "id": "710000000000001"},
        "styles": {"__typename": "StoryAttachmentShareStyleRenderer", "attachment": fields},
    }


def no_message(**extra: Any) -> dict[str, Any]:
    node = base(**extra)
    content(node)["message"] = None
    return node


class TestAttachmentText:
    def test_a_link_share_without_words(self) -> None:
        story = read(no_message(attachments=[link()])).story
        assert story.text == "Двустаен, Лозенец\nОбзаведен, до метрото.\nIMOT.BG"
        assert story.text_path == "attachment.link"
        assert story.listing is None

    def test_a_sites_name_rides_only_with_a_title_or_a_description(self) -> None:
        """A renamed title and description path leaves a link card only its source: no text,
        and the story stays unexplained for the alert, not explained by ``IMOT.BG``."""
        alone = read(no_message(attachments=[link(title_with_entities=None, description=None)]))
        assert alone.story.text == "" and alone.story.text_path is None
        assert alone.text_class is TextClass.UNEXPLAINED
        described = read(no_message(attachments=[link(title_with_entities=None)])).story
        assert described.text == "Обзаведен, до метрото.\nIMOT.BG"
        assert described.text_path == "attachment.link"
        titled = read(no_message(attachments=[link(description=None)])).story
        assert titled.text == "Двустаен, Лозенец\nIMOT.BG"

    def test_a_link_is_appended_to_a_story_with_words(self) -> None:
        """The owner's decision of 2026-09-30: a rule fires on a word only the link's title
        has. The text path and the class stay the message's."""
        reading = read(base(attachments=[link()]))
        assert reading.story.text == "hello\nДвустаен, Лозенец\nОбзаведен, до метрото.\nIMOT.BG"
        assert reading.story.text_path == "content.message"
        assert reading.text_class is None
        assert reading.card_beside_words

    def test_a_link_beside_a_shared_text(self) -> None:
        node = no_message(attachments=[link()], attached_story={"message": {"text": "shared"}})
        reading = read(node)
        assert (reading.story.text or "").splitlines() == [
            "shared",
            "Двустаен, Лозенец",
            "Обзаведен, до метрото.",
            "IMOT.BG",
        ]
        assert reading.story.text_path == "attached_story"
        assert reading.text_class is None
        assert not reading.card_beside_words  # not a message of its own

    def test_a_card_line_already_in_the_text_is_left_out(self) -> None:
        """Equal to the text or inside it, whatever its case, its Unicode form or an invisible
        character in it; as whole words only, and with the text's own spaces."""
        words = "Продавам ДВУСТАЕН, лозенец — обзаведен, до метрото. Виж IMOT.BG"
        node = base(attachments=[link()])
        content(node)["message"] = {"text": words}
        reading = read(node)
        assert reading.story.text == words
        assert not reading.card_beside_words  # nothing was added
        for title, kept in (
            ("двустаен, ЛОЗЕНЕЦ", False),  # case
            ("двустаен,   лозенец", True),  # spacing: a rule with these spaces misses the text
            ("Двуста\u200bен, Лозенец", False),  # a zero-width space
            ("Двустаи\u0306ен", True),  # NFD of a word the text does not have
            ("Лозен", True),  # inside a word only: a rule on "лозен" would miss the text
            ("Продавам ДВУСТАЕН", False),  # the text's first words
            ("\u200b", False),  # nothing left once folded
        ):
            node = base(attachments=[link(title_with_entities={"text": title})])
            content(node)["message"] = {"text": words}
            lines = (read(node).story.text or "").splitlines()
            assert (title.strip() in lines) is kept, title
        nfd = base(attachments=[link(title_with_entities={"text": "Двустаи\u0306ен"})])
        content(nfd)["message"] = {"text": "Двустайен и тих"}  # its NFC form
        assert (read(nfd).story.text or "").splitlines()[1:] == [
            "Обзаведен, до метрото.",
            "IMOT.BG",
        ]

    @pytest.mark.parametrize(
        ("message", "shared", "title", "rule"),
        [
            # A newline, in the message or between the message and a shared text, is not the
            # card's space: the rule fires on the card line and not on the words.
            (
                "Продавам\nдвустаен апартамент",
                None,
                "Продавам двустаен апартамент",
                "продавам двустаен",
            ),
            ("Продавам", "двустаен апартамент", "Продавам двустаен", "продавам двустаен"),
            ("Двустаен  в Лозенец", None, "Двустаен в Лозенец", "двустаен в лозенец"),
            # ß is not ss to the matcher (simple case folding), whatever casefold() says.
            ("STRASSE 5", None, "Straße 5", "straße"),
            # A combining mark is part of a word to the matcher: "замо" is not a word here.
            ("замо\u0301к", None, "замо", "=замо"),
            # A dotted İ matches i and not I, so I and i are never taken for one letter.
            ("Виж IMOT.BG", None, "imot.bg", "İmot"),
        ],
    )
    def test_a_line_the_matcher_would_not_find_in_the_text_is_kept(
        self, message: str, shared: str | None, title: str, rule: str
    ) -> None:
        """Held means held to the brain's matcher: a rule that fires on the card line and not
        on the words keeps the line."""
        node = base(attachments=[link(title_with_entities={"text": title})])
        content(node)["message"] = {"text": message}
        if shared is not None:
            content(node)["attached_story"] = {"message": {"text": shared}}
        head = "\n".join(t for t in (message, shared) if t)
        assert fires(rule, title) and not fires(rule, head)  # why the line must stay
        reading = read(node)
        assert (reading.story.text or "").splitlines()[: head.count("\n") + 2] == [
            *head.splitlines(),
            title,
        ]
        assert fires(rule, reading.story.text or "")
        assert reading.card_beside_words

    @pytest.mark.parametrize(
        ("message", "title"),
        [
            ("ПРОДАВАМ ДВУСТАЕН в Лозенец", "Продавам двустаен"),  # Cyrillic case folds
            ("Улица STRAẞE 5", "straße 5"),  # capital sharp s is ß to the matcher
            ("Obiava na IMOT.BG", "obiava NA"),
            ("замо\u0301к и още", "замо\u0301к"),
            ("Двустаен в Лозенец", "Двустаен\u200b в Лозенец"),
        ],
    )
    def test_a_line_the_matcher_finds_in_the_text_is_left_out(
        self, message: str, title: str
    ) -> None:
        node = base(attachments=[link(title_with_entities={"text": title})])
        content(node)["message"] = {"text": message}
        assert title not in (read(node).story.text or "").splitlines()

    def test_card_lines_are_left_out_when_another_card_line_holds_them(self) -> None:
        card = link(
            title_with_entities={"text": "Двустаен"},
            description={"text": "Двустаен в Лозенец, IMOT.BG"},
        )
        lines = (read(base(attachments=[card])).story.text or "").splitlines()
        assert lines == ["hello", "Двустаен", "Двустаен в Лозенец, IMOT.BG"]

    def test_an_unavailable_card_adds_nothing_to_words(self) -> None:
        unavailable = {
            "styles": {
                "__typename": "StoryAttachmentUnavailableStyleRenderer",
                "attachment": {"title_with_entities": {"text": "This content isn't available"}},
            }
        }
        reading = read(base(attachments=[unavailable]))
        assert reading.story.text == "hello"
        assert not reading.card_beside_words

    def test_the_link_of_the_shared_story_counts(self) -> None:
        node = no_message(attached_story={"__typename": "Story", "attachments": [link()]})
        story = read(node).story
        assert (story.text or "").splitlines()[0] == "Двустаен, Лозенец"
        assert story.text_path == "attachment.link" and story.is_share

    def test_the_allow_list_is_pinned(self) -> None:
        """Widening the keys a card's words are read from is a deliberate change to this test,
        never a link, an id, a tracking token, a picture's caption or a person's name."""
        keys = stories.ATTACHMENT_TEXT_KEYS
        assert keys == frozenset(
            {
                "marketplace_listing_title",
                "title_with_entities",
                "title",
                "formatted_price",
                "listing_price",
                "location_text",
                "description",
                "source",
            }
        )
        assert keys.isdisjoint(
            {
                "url",
                "id",
                "tracking",
                "web_link",
                "external_url",
                "fbclid",
                "uri",
                "deduplication_key",
                "accessibility_caption",
                "name",
            }
        )

    def test_only_allow_listed_keys_are_read(self) -> None:
        """Links, ids, tokens, the picture, buttons and the shared story inside an attachment
        are never match text, whatever words they hold."""
        noise = {
            "id": "Думи в полето id",
            "url": "Думи в полето url",
            "tracking": "Думи в полето tracking",
            "name": "Думи в полето name",
            "web_link": {"__typename": "ExternalWebLink", "title": {"text": "Думи в web_link"}},
            "media": {"__typename": "GenericAttachmentMedia", "title": {"text": "Думи в media"}},
            "action_links": [{"title": {"text": "Думи в бутон"}}],
            "call_to_action": {"title": {"text": "Думи в призив"}},
            "comet_footer_cta_renderer": {"title": {"text": "Думи в cta"}},
            "actors": [{"name": "Думи в actors", "title": {"text": "Думи в actors"}}],
            "attached_story": {"title": {"text": "Думи в споделеното"}},
            "all_subattachments": {"nodes": [{"title": {"text": "Думи в снимките"}}]},
        }
        only = link(title_with_entities=None, description=None, source=None, **noise)
        story = read(no_message(attachments=[only])).story
        assert story.text == ""
        found = link(**noise)
        assert "Думи" not in (read(no_message(attachments=[found])).story.text or "")
        beside = (read(base(attachments=[found])).story.text or "").splitlines()
        assert beside == ["hello", "Двустаен, Лозенец", "Обзаведен, до метрото.", "IMOT.BG"]

    def test_the_walk_is_bounded(self) -> None:
        deep: dict[str, Any] = {"title": {"text": "Дълбоко"}}
        for i in range(stories.MAX_ATTACHMENT_DEPTH + 1):  # the title's holder one key too deep
            deep = {f"k{i}": deep}
        assert read(no_message(attachments=[deep])).story.text == ""
        shallow = deep[f"k{stories.MAX_ATTACHMENT_DEPTH}"]
        assert read(no_message(attachments=[shallow])).story.text == "Дълбоко"
        many = [{"title": {"text": f"Карта {i}"}} for i in range(stories.MAX_ATTACHMENTS + 1)]
        assert (read(no_message(attachments=many)).story.text or "").count("Карта") == 10
        crowd: list[dict[str, Any]] = [{} for _ in range(stories.MAX_ATTACHMENT_NODES)]
        wide = {"crowd": crowd, "later": {"title": {"text": "Късно"}}}
        assert read(no_message(attachments=[wide])).story.text == ""
        assert read(no_message(attachments=[{"later": wide["later"]}])).story.text == "Късно"

    def test_an_unavailable_share_offers_nothing(self) -> None:
        unavailable = {
            "__typename": "StoryAttachment",
            "styles": {
                "__typename": "StoryAttachmentUnavailableStyleRenderer",
                "attachment": {
                    "title_with_entities": {"text": "This content isn't available right now"},
                    "description": {"text": "When this happens, it's usually because..."},
                },
            },
        }
        reading = read(no_message(attachments=[unavailable]))
        assert reading.story.text == "" and reading.story.text_path is None

    def test_repeated_lines_appear_once(self) -> None:
        footer = {
            "__typename": "CometStoryAttachmentFooterGenericShareStrategy",
            "attachment": {"title_with_entities": {"text": "Двустаен, Лозенец"}},
        }
        node = no_message(attachments=[link(comet_footer_renderer=footer), link()])
        content(node)["attachments"] = [link()]
        story = read(node).story
        assert (story.text or "").splitlines() == [
            "Двустаен, Лозенец",
            "Обзаведен, до метрото.",
            "IMOT.BG",
        ]
        listing = base(attachments=[{"target": {"marketplace_listing_title": "hello"}}])
        assert read(listing).story.text == "hello"  # the title is the post's own words

    def test_a_listing_known_by_its_style_info(self) -> None:
        """A shared Marketplace listing: known by ``CommerceProductItemStyleInfo`` (no listing
        key needed), its title in the card, its price as a formatted amount."""
        card = {
            "__typename": "StoryAttachment",
            "styles": {
                "__typename": "StoryAttachmentFeedStandardStyleRenderer",
                "attachment": {
                    "title_with_entities": {"text": "Ъглов диван"},
                    "description": {"text": "Почти нов, без забележки."},
                    "style_infos": [
                        {"__typename": "CommerceProductItemStyleInfo"},
                        {"__typename": "FeedStandardAttachmentStyleInfo"},
                    ],
                    "target": {
                        "__typename": "MarketplaceListing",
                        "id": "700000000000011",
                        "marketplace_listing_seller": {"name": "Продавач"},
                    },
                    "subtitle": {"listing_price": {"formatted_amount": "250 лв."}},
                },
            },
        }
        story = read(no_message(attachments=[card])).story
        assert story.listing == ListingRef(
            id="700000000000011", title="Ъглов диван", price="250 лв."
        )
        assert story.text == "Ъглов диван\n250 лв.\nПочти нов, без забележки."
        assert story.text_path == "attachment.listing"
        with_words = read(base(attachments=[card])).story
        assert with_words.text == "hello\nЪглов диван\n250 лв.\nПочти нов, без забележки."
        assert with_words.text_path == "content.message"

    def test_listing_ids_prefer_the_listing_target(self) -> None:
        card = {
            "styles": {
                "attachment": {
                    "id": "holder-id",
                    "marketplace_listing_title": "T",
                    "target": {"__typename": "GroupCommerceProductItem", "id": 12345},
                }
            }
        }
        assert read(base(attachments=[card])).story.listing == ListingRef(id="12345", title="T")
        card["styles"]["attachment"]["target"] = {"id": "67890", "formatted_price": "BGN 5"}
        assert read(base(attachments=[card])).story.listing == ListingRef(
            id="67890", title="T", price="BGN 5"
        )

    def test_media_is_known_by_its_own_renderer_not_a_preview(self) -> None:
        photo = {
            "__typename": "StoryAttachment",
            "target": {"__typename": "Photo", "id": "1"},
            "styles": {"__typename": "StoryAttachmentPhotoStyleRenderer"},
        }
        found = stories.attachment_text(no_message(attachments=[photo]), {})
        assert found.media and not found.link_lines
        preview = link(title_with_entities=None, description=None, source=None)
        preview["styles"]["attachment"]["media"] = {"__typename": "Photo"}
        assert not stories.attachment_text(no_message(attachments=[preview]), {}).media


PHOTO = {
    "__typename": "StoryAttachment",
    "target": {"__typename": "Photo", "id": "1"},
    "styles": {"__typename": "StoryAttachmentPhotoStyleRenderer"},
}
UNAVAILABLE = {
    "__typename": "StoryAttachment",
    "styles": {
        "__typename": "StoryAttachmentUnavailableStyleRenderer",
        "attachment": {"title_with_entities": {"text": "This content isn't available"}},
    },
}


class TestTextClass:
    """Why a story without a message of its own has the text it has (the alert's classes)."""

    @pytest.mark.parametrize(
        ("node", "kind"),
        [
            (base(), None),  # words of its own
            (no_message(attached_story={"message": {"text": "shared"}}), None),  # shared words
            (base(attachments=[PHOTO]), None),  # a photo with words
            (base(attachments=[link()]), None),  # a link with words: never attachment_text
            (
                base(attachments=[{"target": {"marketplace_listing_title": "Bike"}}]),
                None,  # a listing with words
            ),
            (no_message(attachments=[link()]), TextClass.ATTACHMENT_TEXT),
            (
                no_message(attachments=[{"target": {"marketplace_listing_title": "Bike"}}]),
                TextClass.ATTACHMENT_TEXT,
            ),
            (no_message(attached_story={"__typename": "Story"}), TextClass.SHARE_WITHOUT_COMMENT),
            (no_message(attachments=[UNAVAILABLE]), TextClass.SHARE_WITHOUT_COMMENT),
            (no_message(attachments=[PHOTO]), TextClass.MEDIA_ONLY),
            (no_message(attachments=[PHOTO, UNAVAILABLE]), TextClass.SHARE_WITHOUT_COMMENT),
            (no_message(), TextClass.UNEXPLAINED),
            (
                no_message(attachments=[{"styles": {"__typename": "SomethingNew"}}]),
                TextClass.UNEXPLAINED,
            ),
            (
                no_message(
                    attachments=[link(title_with_entities=None, description=None, source=None)]
                ),
                TextClass.UNEXPLAINED,  # a link card whose words were not found: a parser gap
            ),
            (
                no_message(attachments=[link(title_with_entities=None, description=None)]),
                TextClass.UNEXPLAINED,  # the site's name alone is not the post's words
            ),
            (
                no_message(attachments=[link(title_with_entities=None)]),
                TextClass.ATTACHMENT_TEXT,  # a description is
            ),
        ],
    )
    def test_classes(self, node: dict[str, Any], kind: TextClass | None) -> None:
        assert read(node).text_class is kind

    def test_an_unreadable_message_section_is_unexplained(self) -> None:
        node = base()
        content(node)["message"] = {"__typename": "TextWithEntities"}
        reading = read(node)
        assert reading.story.deferred_reason is DeferredReason.NO_TEXT
        assert reading.text_class is TextClass.UNEXPLAINED


# -- property: a card's lines never repeat what the text says ----------------------------------

CASES: list[Callable[[str], str]] = [str, str.upper, str.lower, str.swapcase]
WORD = st.text(alphabet="айкоXYZst1_", min_size=1, max_size=7)
"""Words with a letter NFD splits (``й``), letters that change case, a digit and ``_``: the
dedupe and the matcher agree on every one, so the matcher can be the oracle of the exact
text."""
TRICKY = st.text(alphabet="аоЙйSsßẞſIiİıΣσςK\u212a\u0301\u203f²Ⓐ1_", min_size=1, max_size=5)
"""Characters where a casual folding and the matcher part ways: ß and ẞ, the long ſ, the
dotted and the dotless i, the Greek sigmas, the Kelvin sign, a combining mark, a connector
(``‿``), a superscript digit and a circled letter."""


TURKIC_I = frozenset("İı")
"""The dotted capital and the dotless small i. A rule holding one can miss a match that is
there once the text has a few characters before it: a bug of the ``regex`` module the matcher
compiles with (2026.9.10: ``i  1ı`` finds ``I  1ı`` and not ``1111I  1ı``), whatever the
collector does, so the property leaves those rules out."""


def _holds(text: str, line: str) -> bool:
    """``line`` is in ``text`` to the brain's matcher: a word rule made of the whole line, as
    the matcher reads it, fires on the text (or the line has nothing left once read)."""
    needle = prepare_text(line).shadow.strip()
    return not needle or fires("=" + needle, text)


@st.composite
def card_lines(draw: st.DrawFn, words: list[str], new: st.SearchStrategy[str] = WORD) -> str:
    """A card line: new words, a run of the text's words, or a piece of one of its words,
    in another case, spacing or Unicode form, or with an invisible character in it."""
    kind = draw(st.sampled_from(["new", "run", "piece"]))
    if kind == "new":
        run = draw(st.lists(new, min_size=1, max_size=4))
    else:
        start = draw(st.integers(0, len(words) - 1))
        run = words[start : draw(st.integers(start + 1, len(words)))]
        if kind == "piece":
            word = run[0]
            cut = draw(st.integers(0, len(word) - 1))
            run = [word[cut : draw(st.integers(cut + 1, len(word)))]]
    case = draw(st.sampled_from(CASES))
    gap = draw(st.sampled_from([" ", "  ", "\u00a0", " \u200b"]))
    line = gap.join(case(w) for w in run)
    return unicodedata.normalize("NFD", line) if draw(st.booleans()) else line


@st.composite
def worded_cards(draw: st.DrawFn) -> tuple[dict[str, Any], str, list[str]]:
    """A story with a message (and maybe a shared text), a listing card and a link card whose
    lines may repeat the words; returns the story, the text before the cards and the card
    lines the parser may take, in its order."""
    words = draw(st.lists(WORD, min_size=1, max_size=8))
    message = "".join(w + draw(st.sampled_from([" ", "\n", ", "])) for w in words).strip()
    shared = draw(st.one_of(st.none(), card_lines(words)))
    maybe = st.one_of(st.none(), card_lines(words))
    listing, title, description = draw(maybe), draw(maybe), draw(maybe)
    source = draw(card_lines(words))
    node = base()
    content(node)["message"] = {"text": message}
    if shared is not None:
        content(node)["attached_story"] = {"message": {"text": shared}}
    card = link(
        title_with_entities=None if title is None else {"text": title},
        description=None if description is None else {"text": description},
        source={"text": source},
    )
    attachments: list[dict[str, Any]] = [card]
    if listing is not None:
        attachments.insert(0, {"target": {"marketplace_listing_title": listing}})
    content(node)["attachments"] = attachments
    head = [message]
    if shared is not None and shared.strip() and shared != message:
        head.append(shared)
    got = [stories.matchable(v) for v in (listing, title, description, source)]
    listed, titled, described, sourced = got
    candidates = [listed] if listed else []
    candidates += [listed] if listed else []  # the listing card's title as its link line
    if titled or described:
        candidates += [line for line in (titled, described, sourced) if line]
    return node, "\n".join(head), [c for c in candidates if c]


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(worded_cards())
def test_card_lines_never_repeat_the_text(case: tuple[dict[str, Any], str, list[str]]) -> None:
    """Every card line comes after the post's words, is left out when the text before it
    holds it to the brain's matcher (a word rule of the whole line fires there: any case or
    Unicode form, the text's own spaces, whole words) and is there otherwise; the story stays
    a story with words, whatever its cards."""
    node, head, candidates = case
    reading = read(node)
    added: list[str] = []
    for line in candidates:
        if not _holds("\n".join([head, *added]), line):
            added.append(line)
    assert reading.story.text == "\n".join([head, *added])
    for index, line in enumerate(added):
        assert not _holds("\n".join([head, *added[:index]]), line)
    assert reading.text_class is None
    assert reading.story.text_path == "content.message"
    assert reading.card_beside_words is bool(added)
    assert reading.words == head


@st.composite
def cards_beside_parts(draw: st.DrawFn) -> tuple[dict[str, Any], list[str]]:
    """A story whose words are split between its message and maybe a shared text, joined by
    a space, two spaces, a newline, a comma or a no-break space, with a listing card and a
    link card whose lines are new words, runs of those words (across the two parts too) or
    pieces of one, in any case; returns the story and its cards' four values."""
    words = draw(st.lists(TRICKY, min_size=1, max_size=5))
    cut = draw(st.integers(1, len(words)))
    gaps = st.sampled_from([" ", "  ", "\n", ", ", "\u00a0"])
    message = "".join(w + draw(gaps) for w in words[:cut]).strip()
    shared = "".join(w + draw(gaps) for w in words[cut:]).strip()
    values = [draw(card_lines(words, TRICKY)) for _ in range(4)]
    listing, title, description, source = values
    node = base()
    content(node)["message"] = {"text": message}
    if shared:
        content(node)["attached_story"] = {"message": {"text": shared}}
    content(node)["attachments"] = [
        {"target": {"marketplace_listing_title": listing}},
        link(
            title_with_entities={"text": title},
            description={"text": description},
            source={"text": source},
        ),
    ]
    return node, values


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(cards_beside_parts(), st.sampled_from(CASES), st.data())
def test_a_rule_that_fires_on_a_card_line_fires_on_the_text(
    case: tuple[dict[str, Any], list[str]], rule_case: Callable[[str], str], data: st.DataObject
) -> None:
    """What the dedupe exists for, checked with the brain's own matcher: a substring or word
    rule made of the whole of a card line or of a run of its words, in any case, that fires on
    the line fires on the post's text, whether the line was kept or left out."""
    node, values = case
    text = read(node).story.text or ""
    for value in values:
        line = stories.matchable(value)
        if line is None:
            continue
        seen = prepare_text(line).shadow
        tokens = [m.span() for m in re.finditer(r"\S+", seen)]
        first = data.draw(st.integers(0, len(tokens) - 1))
        last = data.draw(st.integers(first, len(tokens) - 1))
        runs = {seen.strip(), seen[tokens[first][0] : tokens[last][1]]}
        for rule in {c + rule_case(run) for run in runs for c in ("", "=")} | {
            c + run for run in runs for c in ("", "=")
        }:
            if TURKIC_I & set(unicodedata.normalize("NFC", rule)):
                continue  # regex 2026.9.10 misses such a rule's match further into a text
            if fires(rule, line):
                assert fires(rule, text), (rule, line)


@settings(max_examples=300)
@given(st.text(alphabet=st.characters(codec="utf-8")))
@example("\u1100\u1161 и\u0306\u200b")  # jamo stay apart, a mark joins its letter, no ZWSP
@example("\u0301a\u200b\u0301")  # a mark first, a mark after a format character
def test_shadow_is_the_matchers(value: str) -> None:
    """The dedupe reads a text exactly as the brain's ``prepare_text`` does."""
    assert stories.shadow(value) == prepare_text(value).shadow


def _all_characters() -> str:
    return "".join(chr(c) for c in range(sys.maxunicode + 1) if not 0xD800 <= c <= 0xDFFF)


def test_wordy_holds_every_word_character_of_the_matcher() -> None:
    """Every character the matcher's ``\\w`` takes for part of a word, ``_wordy`` does too:
    combining marks, connectors, letters drawn as symbols and letters of a newer Unicode than
    this Python's. Wider only keeps a line. The zero-width joiners never reach it: ``shadow``
    drops format characters."""
    everything = _all_characters()
    word = _rule(r"re:\w")
    found = [everything[span.start] for span in word.search_all(everything, 60.0, limit=10**7)]
    assert len(found) > 100_000
    assert [c for c in found if unicodedata.category(c) != "Cf" and not stories._wordy(c)] == []
    for char in "\u0301\u203f_Ⓐ":
        assert stories._wordy(char)
    for char in " \n\u00a0,.-✝":
        assert not stories._wordy(char)


def test_the_folding_is_sound() -> None:
    """Two characters the dedupe folds together are one to every rule: a rule's character
    that matches one of them under the matcher's ``IGNORECASE`` matches the other. ``ß`` is
    not ``ss``, ``I`` is not ``i`` (a rule's ``İ`` matches ``i`` only)."""
    everything = _all_characters()
    groups: dict[str, set[str]] = defaultdict(set)
    for char in everything:
        target = stories._fold_char(char)
        assert len(target) == 1
        if target != char:
            groups[target] |= {char, target}
    pool = "".join(sorted(set().union(*groups.values())))
    rules = [c for c in everything if c.lower() != c or c.upper() != c or c.casefold() != c]
    assert len(rules) > 2_000
    for char in rules:
        matched = {pool[s.start] for s in _rule(char).search_all(pool, 5.0, limit=len(pool))}
        for other in matched:
            assert groups.get(stories._fold_char(other), set()) <= matched, (char, other)
    assert stories.folded("STRAẞE Straße I") == "straße straße I"


@pytest.mark.parametrize(
    "value",
    [
        "Двустаен",
        "IMOT.BG",
        "BGN 120",
        "250 лв.",
        "1200",
        "€1200",
        "Sofa",
        "see https://imot.bg for more",  # words around a link are still words
        "  Varna ",
        "Mercedes-Benz",  # words joined by "-" or "+" are not base64 punctuation
        "Coca-Cola",
        "Wi-Fi",
        "T-Shirt",
        "2+1",  # a flat's layout
        "ABC-12345",
        "3-стаен",
        "€500/month",  # a "/" in a price, not a path
        "500лв/месец",
    ],
)
def test_matchable_keeps_words_prices_and_site_names(value: str) -> None:
    assert stories.matchable(value) == value.strip()


def test_one_word_titles_reach_the_text_and_the_listing() -> None:
    card = {"target": {"marketplace_listing_title": "Mercedes-Benz", "formatted_price": "2+1"}}
    story = read(no_message(attachments=[card])).story
    assert story.listing == ListingRef(title="Mercedes-Benz", price="2+1")
    assert story.text == "Mercedes-Benz\n2+1"


@pytest.mark.parametrize(
    "value",
    [
        "https://imot.bg/obiava-1",
        "http://x",
        "mailto:someone@example.org",
        "www.imot.bg",
        "imot.bg/obiava",
        "/groups/1/posts/2/",
        "groups/1",
        "a.b/c",  # an ASCII "/" is a path's, even where the id rule does not see one
        "имот.бг/обява",  # a domain's "/"
        "/обява",
        "€//x",
        "id-100064455566778",  # joined, but a part is an id
        "Ref-123456",
        "a=b",
        "123456",
        "100064455566778",
        "pfbid02Hk5Zr8cvQ4kLp1mS7dNqX",
        "UzpfSTI3NjoxMzkw",
        "UzpfSTEwMjow",
        "UzpfSTA6MA",  # S:_I0:0 without its padding: short, still an opaque id
        "AZXsynthetic0tracking0token",
        "IwAR3xYzAbCdEfGhIjKl",
        " ",
        "",
        None,
        7,
    ],
)
def test_matchable_refuses_links_ids_and_tokens(value: Any) -> None:
    assert stories.matchable(value) is None


class TestTranslationKeys:
    """Whether Facebook sent a field about a translation: the key names, never their values."""

    def test_none(self) -> None:
        assert read(base()).translation_keys == frozenset()

    def test_anywhere_in_the_tree_in_any_case(self) -> None:
        node = base(translatability_for_viewer={"source_dialect": "bg_BG"})
        content(node)["comet_sections"] = {
            "message": {"story": {"translated_message": None}},
        }
        content(node)["attachments"] = [{"styles": {"isTranslatable": False}}]
        content(node)["attached_story"] = {"message": {"text": "x"}, "TRANSLATION": {}}
        assert read(node).translation_keys == frozenset(
            {"translatability_for_viewer", "translated_message", "isTranslatable", "TRANSLATION"}
        )

    def test_only_names_that_speak_of_it(self) -> None:
        node = base(transl={"text": "b"}, trans_lation="c", language="bg", translit=None)
        assert read(node).translation_keys == frozenset()  # "translat" is the mark
        assert read(base(translate={"text": "a"})).translation_keys == {"translate"}

    def test_the_names_kept_are_bounded(self) -> None:
        many = {f"translation_{i}": i for i in range(stories.MAX_TRANSLATION_KEYS + 3)}
        found = stories.translation_keys(base(**many))
        assert len(found) == stories.MAX_TRANSLATION_KEYS
        assert found <= set(many)


class TestAuthor:
    def test_first_actor_with_a_name(self) -> None:
        node = base(actors=[{"id": "61590793646233"}])
        node["comet_sections"]["context_layout"] = {
            "story": {"actors": [{"id": "1", "name": " Ann "}]}
        }
        assert read(node).story.author == AuthorRef(
            id="1", name="Ann", url="https://www.facebook.com/profile.php?id=1"
        )

    def test_url_is_canonical_facebook_only(self) -> None:
        node = base(
            actors=[{"id": "7", "name": "Bo", "url": "https://m.facebook.com/bo.smith?__tn__=x"}]
        )
        assert read(node).story.author.url == "https://www.facebook.com/bo.smith/"
        node = base(actors=[{"id": "7", "name": "Bo", "url": "https://evil.example/bo"}])
        assert read(node).story.author.url == "https://www.facebook.com/profile.php?id=7"
        node = base(actors=[{"id": "pfbid02x", "name": "Bo", "url": None}])
        assert read(node).story.author == AuthorRef(name="Bo")

    def test_owning_profile_fallback_and_none(self) -> None:
        node = base(feedback={"owning_profile": {"id": "8", "name": "Page"}})
        assert read(node).story.author.name == "Page"
        assert read(base()).story.author == AuthorRef()

    @pytest.mark.parametrize(
        "actor",
        [
            {"__typename": "GroupAnonAuthorProfile", "id": "9", "name": "Anonymous participant 12"},
            {"id": "9", "name": "Anonymous member", "url": "https://www.facebook.com/x"},
            {"id": "9", "is_anonymous": True},
        ],
    )
    def test_anonymous(self, actor: dict[str, Any]) -> None:
        author = read(base(actors=[actor])).story.author
        assert author.anonymous
        assert author.id is None and author.url is None
        assert author.name


class TestFindUnits:
    def test_stream_parts_by_path_and_by_label(self) -> None:
        unknown: Counter[str] = Counter()
        by_path = Frame(
            {"path": ["viewer", "news_feed", "edges", 3], "data": {"node": base(), "cursor": "c"}},
            0,
            "Q",
        )
        (found,) = find_units(by_path, unknown=unknown, stats=WalkStats())
        assert found.connection == "news_feed" and found.cursor == "c"
        by_label = Frame(
            {
                "label": "CometNewsFeed_viewerConnection$stream$CometNewsFeed_viewer_news_feed",
                "data": {"node": base()},
            },
            0,
            "Q",
        )
        assert len(find_units(by_label, unknown=unknown, stats=WalkStats())) == 1
        no_node = Frame(
            {"path": ["viewer", "news_feed", "edges", 3], "data": {"cursor": "c"}}, 0, "Q"
        )
        assert find_units(no_node, unknown=unknown, stats=WalkStats()) == []
        odd_label = Frame({"label": "X$stream$Y", "data": {"node": base()}}, 0, "Q")
        assert find_units(odd_label, unknown=unknown, stats=WalkStats()) == []
        bad_path = Frame({"path": [True, "edges", 1], "data": {"node": base()}}, 0, "Q")
        assert find_units(bad_path, unknown=unknown, stats=WalkStats()) == []
        numeric_conn = Frame({"path": [0, "edges", 1], "data": {"node": base()}}, 0, "Q")
        assert find_units(numeric_conn, unknown=unknown, stats=WalkStats()) == []
        assert not unknown

    def test_never_descends_into_a_found_story(self) -> None:
        inner = base(post_id="22")
        outer = base(nested={"news_feed": {"edges": [{"node": inner}]}})
        frame = Frame(
            {"data": {"news_feed": {"edges": [{"node": outer}, "junk", {"node": None}]}}}, 0, "Q"
        )
        units = find_units(frame, unknown=Counter(), stats=WalkStats())
        assert [u.node["post_id"] for u in units] == ["11"]

    def test_story_like(self) -> None:
        assert story_like({"post_id": "1"})
        assert story_like({"__typename": "Story"})
        assert not story_like({"id": "1"})
        assert not story_like(None)
