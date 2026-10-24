"""A Facebook notification and digest: Facebook's links, the fb wording, and never a t.me link."""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest
from telethon.tl import types as tl

from ddiver_core.bot import menus
from ddiver_core.formatter import DigestEntry, Payload, Rendered, render_digest, render_notification
from ddiver_core.matcher import Match
from ddiver_core.rules import RuleKind, Span, prepare_text
from ddiver_core.storage import Rule
from ddiver_fb.source import build_payload
from ddiver_fb.store import FbGroup
from fbwire import Author, GroupPrivacy, GroupState
from tests.brain.helpers import EPOCH0, G1, G2, T0, item

NOW = T0 + dt.timedelta(minutes=5)
RULE = Rule(
    id=7,
    user_id=1,
    chat_id=G1,
    kind=RuleKind.INCLUDE,
    pattern="varna",
    disabled_reason=None,
    hits=0,
)


def group(gid: int = G1, title: str = "Оренда Варна", **kw) -> FbGroup:
    return FbGroup(
        chat_id=gid,
        title=title,
        url=f"https://www.facebook.com/groups/{gid}/",
        state=GroupState.MEMBER,
        state_changed_at=EPOCH0,
        **kw,
    )


def payload(text: str = "Flat in Varna, 400 EUR", **kw) -> Payload:
    post = item(text=text, **kw)
    prepared = prepare_text(post.text)  # plain text: the shadow is the text itself
    start = prepared.shadow.lower().find("varna")
    match = Match(user_id=1, rule=RULE, spans=[Span(start, start + 5)])
    return build_payload(post, group(), match, prepared)


def urls(rendered: Rendered) -> list[str]:
    return [e.url for e in rendered.entities if isinstance(e, tl.MessageEntityTextUrl)]


def assert_facebook_only(rendered: Rendered) -> None:
    assert "t.me" not in rendered.text
    assert "tg://" not in rendered.text
    for url in urls(rendered):
        assert url.startswith("https://www.facebook.com/"), url


class TestNotification:
    @pytest.mark.parametrize("lang", ["en", "ru", "uk"])
    def test_every_link_is_facebook(self, lang: str):
        rendered = render_notification(payload(), lang=lang, tz="Europe/Kyiv", now=NOW)
        assert_facebook_only(rendered)
        links = urls(rendered)
        assert f"https://www.facebook.com/groups/{G1}/" in links  # the group line
        assert "https://www.facebook.com/profile.php?id=100000000000001" in links  # the author
        assert f"https://www.facebook.com/groups/{G1}/posts/9876543210987654/" in links  # the post

    def test_english_wording(self):
        text = render_notification(payload(), lang="en", tz="UTC", now=NOW).text
        assert "👥 Group Оренда Варна" in text
        assert "👤 Author Olena K." in text
        assert "🔗 Post https://www.facebook.com/groups/" in text
        assert "🔒 Private group — the link opens for members only" in text
        assert "(group rule)" in text
        assert "Chat" not in text and "Sender" not in text

    def test_ukrainian_wording(self):
        text = render_notification(payload(), lang="uk", tz="Europe/Kyiv", now=NOW).text
        assert "👥 Група Оренда Варна" in text and "🔗 Допис" in text
        assert "🔒 Закрита група" in text

    def test_russian_wording(self):
        text = render_notification(payload(), lang="ru", tz="Europe/Kyiv", now=NOW).text
        assert "👥 Группа" in text and "👤 Автор" in text and "🔗 Пост" in text

    def test_a_late_post_says_so(self):
        late = payload(late=True, created_at=EPOCH0 - 30 * 3600)
        text = render_notification(late, lang="en", tz="UTC", now=NOW).text
        assert "⏳ Posted 1 d ago, appeared in the group only now" in text

    def test_a_public_group_has_no_private_line(self):
        public = payload(group_privacy=GroupPrivacy.PUBLIC)
        text = render_notification(public, lang="en", tz="UTC", now=NOW).text
        assert "🔒" not in text

    def test_an_anonymous_participant(self):
        anonymous = payload(author=Author(anonymous=True))
        rendered = render_notification(anonymous, lang="en", tz="UTC", now=NOW)
        assert "👤 Author Anonymous participant" in rendered.text
        assert_facebook_only(rendered)
        assert not any("profile.php" in u for u in urls(rendered))

    def test_a_shared_post_is_marked(self):
        shared = payload(is_share=True)
        assert "↪️ shared post" in render_notification(shared, lang="en", tz="UTC", now=NOW).text

    def test_the_match_is_bold(self):
        rendered = render_notification(payload(), lang="en", tz="UTC", now=NOW)
        bold = [e for e in rendered.entities if isinstance(e, tl.MessageEntityBold)]
        assert bold, "the matched keyword is highlighted"

    def test_a_link_that_is_not_http_is_dropped(self):
        broken = Payload.from_json({**payload().to_json(), "sender_url": "javascript:alert(1)"})
        rendered = render_notification(broken, lang="en", tz="UTC", now=NOW)
        assert all("javascript" not in u for u in urls(rendered))

    def test_it_survives_the_outbox_round_trip(self):
        original = payload(late=True)
        restored = Payload.from_json(original.to_json())
        assert restored == original

    def test_the_buttons_are_ignore_author_and_remove_rule(self):
        rows = menus.notification_buttons(G1, 1, 100000000000001, RULE.id)
        assert rows == [
            [
                ("notif.btn.ignore", f"ig:{G1}:100000000000001"),
                ("notif.btn.drop_rule", "rdrop:7"),
            ]
        ]
        # both fit Telegram's 64-byte callback limit with 19-digit Facebook ids
        assert all(len(data.encode()) <= 64 for row in rows for _, data in row)


class TestDigest:
    def entries(self) -> list[DigestEntry]:
        first = payload()
        other = Payload.from_json(
            {
                **payload("Room in Varna").to_json(),
                "chat_id": G2,
                "chat_title": "Robota Sofia",
                "chat_url": f"https://www.facebook.com/groups/{G2}/",
                "msg_id": 2,
                "url": f"https://www.facebook.com/groups/{G2}/posts/2/",
                "chat_private": False,
            }
        )
        return [DigestEntry(first, (1, G1, first.msg_id)), DigestEntry(other, (1, G2, 2))]

    @pytest.mark.parametrize("lang", ["en", "ru", "uk"])
    def test_a_digest_links_to_facebook_only(self, lang: str):
        rendered = render_digest(self.entries(), lang=lang, tz="UTC", now=NOW)
        assert_facebook_only(rendered)
        links = urls(rendered)
        assert f"https://www.facebook.com/groups/{G1}/" in links
        assert f"https://www.facebook.com/groups/{G2}/posts/2/" in links

    def test_a_private_group_heading_carries_the_lock(self):
        text = render_digest(self.entries(), lang="en", tz="UTC", now=NOW).text
        assert "Оренда Варна 🔒 — 1" in text
        assert "Robota Sofia — 1" in text

    def test_many_groups_are_counted_as_groups(self):
        entries = []
        for n in range(15):
            p = Payload.from_json({**payload().to_json(), "chat_id": 10_000 + n, "msg_id": n})
            entries.append(DigestEntry(p, (1, 10_000 + n, n)))
        text = render_digest(entries, lang="en", tz="UTC", now=NOW).text
        assert "more groups" in text


FROM_RULE = dataclasses.replace(RULE, id=8, pattern="from:100000000000001")


def from_payload(text: str = "") -> Payload:
    """A ``from:`` hit (ddiver-core v0.1.0): no span to highlight, often no text at all."""
    post = item(text=text)
    match = Match(user_id=1, rule=FROM_RULE, spans=[])
    return build_payload(post, group(), match, prepare_text(post.text))


class TestFromMatch:
    @pytest.mark.parametrize("lang", ["en", "ru", "uk"])
    def test_a_photo_without_a_caption_names_the_rule_and_the_author(self, lang: str):
        rendered = render_notification(from_payload(), lang=lang, tz="UTC", now=NOW)
        assert "from:100000000000001" in rendered.text
        assert "Olena K. · 100000000000001" in rendered.text  # the id a from: rule names
        assert_facebook_only(rendered)
        assert f"https://www.facebook.com/groups/{G1}/posts/9876543210987654/" in urls(rendered)

    def test_a_captioned_post_is_shown_whole_without_a_highlight(self):
        caption = "Flat in Varna, 400 EUR"

        def bold(rendered: Rendered) -> int:
            return sum(isinstance(e, tl.MessageEntityBold) for e in rendered.entities)

        by_author = render_notification(from_payload(caption), lang="en", tz="UTC", now=NOW)
        by_keyword = render_notification(payload(caption), lang="en", tz="UTC", now=NOW)
        assert caption in by_author.text
        assert bold(by_author) == bold(by_keyword) - 1  # only the keyword's "Varna" is bold

    def test_a_digest_of_from_hits_links_to_facebook_only(self):
        first, second = from_payload(), from_payload("Room in Varna")
        second = Payload.from_json({**second.to_json(), "msg_id": 2})
        entries = [DigestEntry(first, (1, G1, first.msg_id)), DigestEntry(second, (1, G1, 2))]
        rendered = render_digest(entries, lang="en", tz="UTC", now=NOW)
        assert_facebook_only(rendered)
        assert "Оренда Варна 🔒 — 2" in rendered.text
