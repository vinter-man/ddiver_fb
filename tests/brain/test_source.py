"""FacebookSource: one post in, durable claims out, and never a claim twice."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from ddiver_core.matcher import Matcher
from ddiver_core.rules import RuleKind
from ddiver_core.storage import ALL_CHATS, OutboxItem, Storage
from ddiver_core_testing import FakeClock
from ddiver_fb.groups import GroupBook
from ddiver_fb.source import SOURCE_FACEBOOK, FacebookSource, PoisonItem, author_name
from ddiver_fb.store import FbStore
from fbwire import Author, GroupPrivacy, Item, ItemOutcome
from tests.brain.helpers import EPOCH0, G1, G2, MOD, OWNER, item, seed_groups


class Delivered:
    def __init__(self) -> None:
        self.rows: list[OutboxItem] = []

    async def __call__(self, row: OutboxItem) -> None:
        self.rows.append(row)


@pytest.fixture
async def matcher(storage: Storage) -> Matcher:
    return Matcher(storage, timeout=0.2)


@pytest.fixture
def delivered() -> Delivered:
    return Delivered()


@pytest.fixture
async def source(
    storage: Storage, fb: FbStore, matcher: Matcher, delivered: Delivered, clock: FakeClock
) -> FacebookSource:
    return FacebookSource(storage, fb, matcher, deliver=delivered, clock=clock)


async def watch(
    storage: Storage, matcher: Matcher, user: int, gid: int, *patterns: str, kind=RuleKind.INCLUDE
) -> None:
    if user != OWNER:
        await storage.set_chat_shared(gid, True)
    assert await storage.subscribe(user, gid)
    for pattern in patterns:
        await storage.add_rule(user, gid, kind, pattern)
    await matcher.reload()


@pytest.fixture
async def watched(book: GroupBook, storage: Storage, matcher: Matcher, clock: FakeClock) -> None:
    await seed_groups(book, G1, G2)
    clock.tick(-3600)  # subscriptions an hour before the posts
    await watch(storage, matcher, OWNER, G1, "varna")
    await watch(storage, matcher, MOD, G1, "=flat")
    clock.tick(3600)


class TestOutcomes:
    async def test_an_unwatched_group_is_not_monitored(self, source: FacebookSource, watched):
        assert await source.process(item(G2)) is ItemOutcome.NOT_MONITORED

    async def test_an_unknown_group_is_not_monitored(self, source: FacebookSource, watched):
        assert await source.process(item(999)) is ItemOutcome.NOT_MONITORED

    async def test_a_monitored_chat_without_a_group_row_is_not_monitored(
        self, source: FacebookSource, storage: Storage, matcher: Matcher
    ):
        from ddiver_core.links import ChatRef

        await storage.upsert_chat(ChatRef(G1, "hand-made", None, "group"))
        await storage.subscribe(OWNER, G1)
        await matcher.reload()
        assert await source.process(item(G1)) is ItemOutcome.NOT_MONITORED

    async def test_a_stale_matcher_cache_is_not_an_answer(
        self,
        *,
        source: FacebookSource,
        book: GroupBook,
        storage: Storage,
        matcher: Matcher,
        delivered: Delivered,
        clock: FakeClock,
    ):
        """C1-03: ``not_monitored`` makes the collector delete the post, so a matcher whose
        refresh after a group change failed must not be the one to say it."""
        await seed_groups(book, G2)
        clock.tick(-3600)
        await storage.subscribe(OWNER, G2)
        await storage.add_rule(OWNER, G2, RuleKind.INCLUDE, "varna")
        clock.tick(3600)
        assert not matcher.is_monitored(G2)  # never reloaded: the cache is stale
        assert await source.process(item(G2)) is ItemOutcome.STORED
        assert matcher.is_monitored(G2)
        assert [r.chat_id for r in delivered.rows] == [G2]

    async def test_a_match_is_claimed_for_each_subscriber_and_handed_to_delivery(
        self, source: FacebookSource, storage: Storage, delivered: Delivered, watched
    ):
        assert await source.process(item()) is ItemOutcome.STORED
        assert sorted(r.user_id for r in delivered.rows) == [OWNER, MOD]
        for row in delivered.rows:
            assert (row.chat_id, row.msg_id) == (G1, 9_876_543_210_987_654)
            assert row.payload["source"] == SOURCE_FACEBOOK
        assert source.metrics.claimed == 2 and source.metrics.matched == 1
        assert source.last_item_at[G1] == EPOCH0 - 120

    async def test_the_same_post_again_is_a_duplicate_and_claims_nothing(
        self, source: FacebookSource, delivered: Delivered, watched
    ):
        await source.process(item())
        assert await source.process(item()) is ItemOutcome.DUPLICATE
        assert len(delivered.rows) == 2
        assert source.metrics.results[ItemOutcome.DUPLICATE] == 1

    async def test_a_post_nobody_matches_is_still_stored(
        self, source: FacebookSource, fb: FbStore, delivered: Delivered, watched
    ):
        assert await source.process(item(text="Selling a bicycle")) is ItemOutcome.STORED
        assert delivered.rows == []
        assert await fb.has_item(G1, 9_876_543_210_987_654)

    async def test_every_stored_post_is_kept_as_it_came_once(
        self, source: FacebookSource, fb: FbStore, watched
    ):
        await source.process(item(text="Selling a bicycle"))
        await source.process(item(text="Selling a bicycle"))
        assert await source.process(item(G2, text="unwatched")) is ItemOutcome.NOT_MONITORED
        cur = await fb.db.execute("SELECT group_id, post_id, received_at, item FROM fb_texts")
        [row] = list(await cur.fetchall())
        assert (row["group_id"], row["post_id"], row["received_at"]) == (
            G1,
            int(item().post_id),
            EPOCH0,
        )
        assert Item.model_validate_json(row["item"]) == item(text="Selling a bicycle")

    async def test_a_post_that_breaks_a_step_is_not_kept(
        self, source: FacebookSource, fb: FbStore, watched, monkeypatch: pytest.MonkeyPatch
    ):
        def broken(text: str) -> Any:
            raise ValueError("unpreparable")

        monkeypatch.setattr("ddiver_fb.source.prepare_text", broken)
        with pytest.raises(PoisonItem):
            await source.process(item())
        cur = await fb.db.execute("SELECT 1 FROM fb_texts")
        assert list(await cur.fetchall()) == []

    async def test_an_empty_text_is_stored_without_matching(
        self, source: FacebookSource, delivered: Delivered, watched
    ):
        assert await source.process(item(text="")) is ItemOutcome.STORED
        assert delivered.rows == []


class TestWhoGetsIt:
    async def test_no_backfill_for_a_user_who_subscribed_after_the_post(
        self,
        source: FacebookSource,
        storage: Storage,
        matcher: Matcher,
        book: GroupBook,
        delivered: Delivered,
    ):
        await seed_groups(book, G1)
        await watch(storage, matcher, OWNER, G1, "varna")  # subscribed at T0
        assert await source.process(item(created_at=EPOCH0 - 1)) is ItemOutcome.STORED
        assert delivered.rows == []
        assert source.metrics.skipped_before_subscription == 1
        assert await source.process(item(pid=2, created_at=EPOCH0)) is ItemOutcome.STORED
        assert [r.msg_id for r in delivered.rows] == [2]  # posted the same second: delivered

    async def test_an_ignored_author_is_skipped_for_that_user_only(
        self, source: FacebookSource, storage: Storage, matcher: Matcher, delivered, watched
    ):
        await storage.add_ignored(MOD, G1, 100000000000001)
        await matcher.invalidate(G1)
        await source.process(item())
        assert [r.user_id for r in delivered.rows] == [OWNER]

    async def test_an_exclusion_wins(
        self, source: FacebookSource, storage: Storage, matcher: Matcher, delivered, watched
    ):
        await storage.add_rule(OWNER, G1, RuleKind.EXCLUDE, "agency")
        await matcher.invalidate(G1)
        await source.process(item(text="Flat in Varna, agency fee"))
        assert [r.user_id for r in delivered.rows] == [MOD]

    async def test_an_anonymous_author_cannot_be_ignored_or_linked(
        self, source: FacebookSource, delivered: Delivered, watched
    ):
        await source.process(item(author=Author(anonymous=True, name="ignored")))
        payload = delivered.rows[0].payload
        assert payload["sender_id"] is None and payload["sender_name"] == ""
        assert payload["sender_is_channel"] is True and "sender_url" not in payload


AUTHOR_ID = 100000000000001  # the default ``item()`` author, "Olena K."


@pytest.fixture
async def follows(book: GroupBook, storage: Storage, matcher: Matcher, clock: FakeClock):
    """G1 watched by the owner through ``from:`` rules only; returns a helper that adds more."""
    await seed_groups(book, G1, G2)
    clock.tick(-3600)
    await watch(storage, matcher, OWNER, G1)
    clock.tick(3600)

    async def add(*patterns: str, kind: RuleKind = RuleKind.INCLUDE, chat: int = G1) -> None:
        for pattern in patterns:
            await storage.add_rule(OWNER, chat, kind, pattern)
        await matcher.reload()

    return add


class TestFromRules:
    """``from:`` (ddiver-core v0.1.0) on Facebook: the author's id — the number every
    notification shows next to the name — or part of the author's name. Facebook shows no
    username, and an anonymous participant is nobody, so neither can be matched."""

    async def test_the_authors_id_matches_whatever_the_post_says(
        self, source: FacebookSource, delivered: Delivered, follows
    ):
        await follows(f"from:{AUTHOR_ID}")
        assert await source.process(item(text="Selling a bicycle")) is ItemOutcome.STORED
        (row,) = delivered.rows
        assert (row.user_id, row.chat_id) == (OWNER, G1)
        assert row.payload["rule_pattern"] == f"from:{AUTHOR_ID}"
        assert row.payload["spans"] == [] and row.payload["sender_id"] == AUTHOR_ID

    async def test_a_photo_without_a_caption_matches_too(
        self, source: FacebookSource, delivered: Delivered, follows
    ):
        await follows(f"from:{AUTHOR_ID}")
        assert await source.process(item(text="")) is ItemOutcome.STORED
        assert [r.user_id for r in delivered.rows] == [OWNER]

    async def test_part_of_the_name_matches(
        self, source: FacebookSource, delivered: Delivered, follows
    ):
        await follows("from:olena")
        await source.process(item(author=Author(id="100000000000002", name="OLENA 🌻 K.")))
        assert [r.payload["rule_pattern"] for r in delivered.rows] == ["from:olena"]

    async def test_somebody_else_does_not_match(
        self, source: FacebookSource, delivered: Delivered, follows
    ):
        await follows(f"from:{AUTHOR_ID + 1}", "from:petrenko")
        assert await source.process(item(text="Olena K. and 100000000000002")) is (
            ItemOutcome.STORED
        )
        assert delivered.rows == []

    async def test_an_anonymous_participant_matches_no_from_rule(
        self, source: FacebookSource, delivered: Delivered, follows
    ):
        await follows("from:anonymous participant", "from:ignored", f"from:{AUTHOR_ID}")
        for pid, name in enumerate(("Anonymous participant", "ignored"), start=1):
            post = item(pid=pid, text="", author=Author(anonymous=True, name=name))
            assert author_name(post) == ""
            assert await source.process(post) is ItemOutcome.STORED
        assert delivered.rows == []

    async def test_a_username_rule_matches_nothing_on_facebook(
        self, source: FacebookSource, delivered: Delivered, follows
    ):
        """Facebook shows no username; not even a profile link or a name spelled like one
        stands in for it, so ``from:@olena`` can never fire on a post."""
        await follows("from:@olena")
        olena = Author(id="100000000000003", name="olena", url="https://www.facebook.com/olena")
        assert await source.process(item(author=olena)) is ItemOutcome.STORED
        assert await source.process(item(pid=2, text="@olena")) is ItemOutcome.STORED
        assert delivered.rows == []

    async def test_a_from_exclusion_drops_the_authors_keyword_hits(
        self, source: FacebookSource, delivered: Delivered, follows
    ):
        await follows("varna")
        await follows(f"from:{AUTHOR_ID}", kind=RuleKind.EXCLUDE)
        await source.process(item())  # "Flat for rent in Varna" by the excluded author
        other = Author(id="100000000000002", name="Petro P.")
        await source.process(item(pid=2, author=other))
        assert [r.msg_id for r in delivered.rows] == [2]

    async def test_the_ignore_list_beats_a_from_rule(
        self, source: FacebookSource, storage: Storage, matcher: Matcher, delivered, follows
    ):
        await follows(f"from:{AUTHOR_ID}")
        await storage.add_ignored(OWNER, G1, AUTHOR_ID)
        await matcher.invalidate(G1)
        await source.process(item())
        assert delivered.rows == []

    async def test_a_keyword_hit_is_named_before_the_author(
        self, source: FacebookSource, delivered: Delivered, follows
    ):
        """A keyword says why and highlights the words; the core prefers it."""
        await follows(f"from:{AUTHOR_ID}", "varna")
        await source.process(item())
        (row,) = delivered.rows
        assert row.payload["rule_pattern"] == "varna" and row.payload["spans"]

    async def test_a_rule_for_all_groups_follows_the_author_everywhere_watched(
        self, source: FacebookSource, storage: Storage, matcher: Matcher, delivered, follows
    ):
        await follows(f"from:{AUTHOR_ID}", chat=ALL_CHATS)
        await source.process(item())
        await source.process(item(G2, pid=2))  # G2 is not watched: never matched
        assert [(r.chat_id, r.payload["rule_pattern"]) for r in delivered.rows] == [
            (G1, f"from:{AUTHOR_ID}")
        ]


class TestPayload:
    async def test_links_and_flags_come_from_facebook(
        self, source: FacebookSource, delivered: Delivered, watched
    ):
        post = item(text="Big flat in Varna", late=True, is_share=True)
        await source.process(post)
        payload = next(r.payload for r in delivered.rows if r.user_id == OWNER)
        assert payload["url"] == post.url
        assert payload["chat_url"] == post.group_url
        assert payload["sender_url"] == post.author.url
        assert payload["chat_username"] is None and payload["chat_kind"] == "group"
        assert payload["chat_private"] is True and payload["late"] is True
        assert payload["is_forwarded"] is True
        assert payload["sender_id"] == 100000000000001
        assert payload["rule_pattern"] == "varna"
        start, end = payload["spans"][0]
        assert post.text[start:end].lower() == "varna"

    async def test_unknown_privacy_falls_back_to_the_synced_group(
        self, source: FacebookSource, delivered: Delivered, book: GroupBook, storage, matcher
    ):
        await seed_groups(book, G1, privacy=GroupPrivacy.PUBLIC)
        await watch(storage, matcher, OWNER, G1, "varna")
        await source.process(item(created_at=EPOCH0, group_privacy=GroupPrivacy.UNKNOWN))
        assert "chat_private" not in delivered.rows[0].payload


class Flaky:
    """Makes the n-th claim raise the way a locked database does."""

    def __init__(self, storage: Storage, fail_at: int) -> None:
        self.storage = storage
        self.fail_at = fail_at
        self.calls = 0
        self.original = storage.claim_delivery

    async def __call__(self, *args: Any, **kw: Any) -> OutboxItem | None:
        self.calls += 1
        if self.calls == self.fail_at:
            raise sqlite3.OperationalError("database is locked")
        return await self.original(*args, **kw)


class TestBusyDatabase:
    async def test_a_busy_claim_leaves_the_post_unrecorded_and_a_resend_finishes_it(
        self,
        *,
        source: FacebookSource,
        storage: Storage,
        fb: FbStore,
        delivered: Delivered,
        watched,
        monkeypatch: pytest.MonkeyPatch,
    ):
        flaky = Flaky(storage, fail_at=2)
        monkeypatch.setattr(storage, "claim_delivery", flaky)
        with pytest.raises(sqlite3.OperationalError):
            await source.process(item())
        assert not await fb.has_item(G1, 9_876_543_210_987_654)
        assert len(delivered.rows) == 1  # the first claim is durable and already handed over
        assert await source.process(item()) is ItemOutcome.STORED  # the collector's resend
        assert sorted(r.user_id for r in delivered.rows) == [OWNER, MOD]  # each exactly once
        assert source.metrics.claim_duplicates == 1
        assert await fb.has_item(G1, 9_876_543_210_987_654)

    async def test_a_busy_record_is_retried_the_same_way(
        self,
        source: FacebookSource,
        fb: FbStore,
        delivered: Delivered,
        watched,
        monkeypatch: pytest.MonkeyPatch,
    ):
        original = fb.record_item
        calls = {"n": 0}

        async def busy_once(*args: Any, **kw: Any) -> bool:
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return await original(*args, **kw)

        monkeypatch.setattr(fb, "record_item", busy_once)
        with pytest.raises(sqlite3.OperationalError):
            await source.process(item())
        assert await source.process(item()) is ItemOutcome.STORED
        assert len(delivered.rows) == 2

    async def test_a_busy_text_is_retried_the_same_way(
        self,
        source: FacebookSource,
        fb: FbStore,
        delivered: Delivered,
        watched,
        monkeypatch: pytest.MonkeyPatch,
    ):
        original = fb.record_text
        calls = {"n": 0}

        async def busy_once(*args: Any, **kw: Any) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            await original(*args, **kw)

        monkeypatch.setattr(fb, "record_text", busy_once)
        with pytest.raises(sqlite3.OperationalError):
            await source.process(item())
        assert not await fb.has_item(G1, int(item().post_id))
        assert await source.process(item()) is ItemOutcome.STORED
        assert len(delivered.rows) == 2  # each claim once
        cur = await fb.db.execute("SELECT 1 FROM fb_texts")
        assert len(list(await cur.fetchall())) == 1
