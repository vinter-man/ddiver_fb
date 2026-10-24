"""The fb overlay's catalogs stay in sync, and only ever say what the code can fill in."""

from __future__ import annotations

import re
import string

import pytest

from ddiver_core import i18n
from ddiver_core.i18n import en as core_en
from ddiver_core.i18n import ru as core_ru
from ddiver_core.i18n import t
from ddiver_core.i18n import uk as core_uk
from ddiver_core.rules import SenderField, compile_rule
from ddiver_fb import strings
from fbwire import BudgetKind, CollectorState, Profile

LANGS = ("en", "ru", "uk")


def fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}


class TestCatalogSync:
    def test_every_language_has_every_key(self):
        keys = {lang: set(catalog) for lang, catalog in strings.OVERLAYS.items()}
        assert keys["en"] == keys["ru"] == keys["uk"]

    @pytest.mark.parametrize("lang", ["ru", "uk"])
    def test_every_translation_uses_the_same_placeholders(self, lang: str):
        for key, text in strings.EN.items():
            assert fields(strings.OVERLAYS[lang][key]) == fields(text), key

    def test_nothing_is_missing_after_install(self):
        assert i18n.missing_keys() == {"ru": [], "uk": []}

    def test_an_overridden_core_string_uses_only_what_the_core_passes(self):
        for key, text in strings.EN.items():
            base = core_en.STRINGS.get(key)
            if base is not None:
                assert fields(text) <= fields(base), key

    def test_install_is_idempotent(self):
        strings.install()
        strings.install()
        assert t("uk", "notif.chat") == "👥 Група"

    def test_without_the_overlay_the_core_speaks_of_chats(self):
        i18n.clear_overlays()
        assert t("en", "notif.chat") == "📡 Chat"
        strings.install()
        assert t("en", "notif.chat") == "👥 Group"


class TestCoverage:
    """Every key the code builds from a value exists, in every language."""

    @pytest.mark.parametrize("lang", LANGS)
    def test_states_profiles_and_budget_kinds(self, lang: str):
        catalog = strings.OVERLAYS[lang]
        for state in CollectorState:
            assert f"fb.state.{state.value}" in catalog
        for profile in Profile:
            assert f"fb.profile.{profile.value}" in catalog
        for kind in BudgetKind:
            assert f"fb.budget.{kind.value}" in catalog


class TestWording:
    @pytest.mark.parametrize("lang", LANGS)
    def test_the_help_keeps_the_core_rule_syntax(self, lang: str):
        text = t(lang, "bot.help")
        assert "Facebook" in text
        assert "<code>re:" in text and "/menu · /test · /status · /cancel" in text

    @pytest.mark.parametrize("lang", LANGS)
    def test_the_export_caption_never_mentions_a_telegram_session(self, lang: str):
        caption = t(lang, "bot.export.caption", chats=1, rules=2, ignored=3)
        assert "Telegram" not in caption and "Facebook" in caption

    def test_notifications_speak_of_groups_posts_and_authors(self):
        for key, text in strings.EN.items():
            if key.startswith("notif."):
                words = set(re.findall(r"[a-z]+", text.lower()))
                assert not {"chat", "message", "sender"} & words, key
                assert "t.me" not in text


CORE = {"en": core_en, "ru": core_ru, "uk": core_uk}


class TestFromRules:
    """ddiver-core v0.1.0's ``from:`` paragraph watches Telegram's ``@vasya``; on Facebook a
    rule names the author's id or part of the name, and the texts say usernames find nothing."""

    @pytest.mark.parametrize("lang", LANGS)
    @pytest.mark.parametrize("key", ["bot.help", "bot.rules.prompt"])
    def test_every_example_is_a_rule_that_works_on_an_author(self, lang: str, key: str):
        text = t(lang, key)
        examples = re.findall(r"<code>(from:[^<]*)</code>", text)
        assert examples and "@vasya" not in text and "Telegram" not in text
        working = [e for e in examples if not e.startswith("from:@")]
        assert working
        for example in working:
            target = compile_rule(example, timeout=0.2).target
            assert target is not None and target.field in {SenderField.ID, SenderField.NAME}

    @pytest.mark.parametrize("lang", LANGS)
    def test_the_prompt_says_a_username_finds_nothing(self, lang: str):
        text = t(lang, "bot.rules.prompt")
        assert "<code>from:@…</code>" in text and "Facebook" in text

    @pytest.mark.parametrize("lang", LANGS)
    @pytest.mark.parametrize("key", ["bot.help", "bot.rules.prompt"])
    def test_only_the_from_paragraph_differs_from_the_core(self, lang: str, key: str):
        core = CORE[lang].STRINGS[key].split("\n\n")
        mine = t(lang, key).split("\n\n")
        changed = [(a, b) for a, b in zip(core, mine, strict=True) if a != b]
        if key == "bot.help":  # its first paragraph is the brain's own introduction
            assert changed[0][0] == core[0]
            changed = changed[1:]
        ((before, after),) = changed
        assert before.startswith("<code>from:@vasya") and after.startswith("<code>from:1")

    def test_the_paragraph_must_be_found_exactly_once(self):
        with pytest.raises(ValueError, match="found 0"):
            strings._from("intro\n\n<code>taxi</code>", "x")
        with pytest.raises(ValueError, match="found 2"):
            strings._from("<code>from:a</code> one\n\n<code>from:b</code> two", "x")

    @pytest.mark.parametrize("lang", LANGS)
    def test_errors_about_authors_speak_of_facebook(self, lang: str):
        for key in ("rule.err.no_phone", "rule.err.bad_username", "rule.err.bad_sender_id"):
            text = t(lang, key)
            assert "Telegram" not in text and "Facebook" in text, key
        assert "Telegram" not in t(lang, "rule.err.name_too_short", limit=3)
        assert "Telegram" not in t(lang, "bot.test.sender_rules", n=2)
