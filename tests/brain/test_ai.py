"""The AI rule: the judge's call and answer, the durable queue, the worker, and /ai."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import httpx
import pytest

from ddiver_core.bot.router import Incoming, Router
from ddiver_core.matcher import Matcher
from ddiver_core.storage import Storage
from ddiver_core_testing import FakeBotClient, FakeClock
from ddiver_fb.ai import (
    BACKOFF,
    FAILURES_TO_ALERT,
    GIVE_UP_AFTER,
    AiStore,
    AiWorker,
    post_text,
)
from ddiver_fb.control import ControlDesk
from ddiver_fb.groups import GroupBook
from ddiver_fb.judge import SCHEMA, SYSTEM, JudgeError, OpenRouterJudge, Verdict, parse
from ddiver_fb.monitor import CollectorMonitor
from ddiver_fb.source import FacebookSource, SourceMetrics
from ddiver_fb.store import FbStore
from ddiver_fb.ui import StatusSources, build_fb_router
from fbwire import ItemOutcome, Listing
from tests.brain.helpers import EPOCH0, G1, G2, MOD, OWNER, T0, epoch, item, seed_groups

WISH = "ищу людей которые ищут попутку из пловдива в софию"


def answer(content: str, *, cost: float | None = 0.0002) -> dict[str, Any]:
    return {
        "model": "deepseek/deepseek-v4.1-flash",
        "choices": [{"message": {"content": content}}],
        "usage": {"cost": cost},
    }


class Server:
    """OpenRouter as a list of canned answers; records what it was asked."""

    def __init__(self, *replies: httpx.Response | Exception) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def judge(self) -> OpenRouterJudge:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self))
        return OpenRouterJudge("sk-or-v1-test", client=client)


def ok(percent: int = 92, reason: str = "пассажир ищет место до Софии") -> httpx.Response:
    return httpx.Response(200, json=answer(json.dumps({"percent": percent, "reason": reason})))


class TestJudge:
    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            ('{"percent": 85, "reason": "да"}', (85, "да")),
            ('```json\n{"percent": 3, "reason": " нет "}\n```', (3, "нет")),
            ('{"percent": 140, "reason": "x"}', (100, "x")),
            ('{"percent": 70.6}', (71, "")),
        ],
    )
    def test_an_answer_is_read(self, content: str, expected: tuple[int, str]):
        assert parse(content) == expected

    @pytest.mark.parametrize(
        "content",
        [
            "",
            "maybe",
            "[1, 2]",
            '{"reason": "x"}',
            '{"percent": true}',
            '{"percent": Infinity}',
            '{"percent": NaN}',
        ],
    )
    def test_no_answer_is_an_error_never_a_guess(self, content: str):
        with pytest.raises(ValueError):
            parse(content)

    async def test_it_asks_with_the_chosen_prompt_and_schema(self):
        server = Server(ok())
        verdict = await server.judge().judge(WISH, "Търся място ПД - София", context="Пътуващи")
        assert (verdict.percent, verdict.model, verdict.cost) == (
            92,
            "deepseek/deepseek-v4.1-flash",
            0.0002,
        )
        (request,) = server.requests
        assert request.headers["Authorization"] == "Bearer sk-or-v1-test"
        body = json.loads(request.content)
        assert body["model"] == "deepseek/deepseek-v4.1-flash"
        assert body["temperature"] == 0
        assert body["response_format"] == {"type": "json_schema", "json_schema": SCHEMA}
        assert body["messages"][0] == {"role": "system", "content": SYSTEM}
        user = body["messages"][1]["content"]
        assert f"«{WISH}»" in user and "Group: Пътуващи" in user and "Търся място" in user

    async def test_an_empty_answer_is_asked_again_once(self):
        server = Server(httpx.Response(200, json=answer("")), ok(5))
        assert (await server.judge().judge(WISH, "x")).percent == 5
        assert len(server.requests) == 2
        twice = Server(httpx.Response(200, json=answer("")), httpx.Response(200, json=answer("")))
        with pytest.raises(JudgeError) as caught:
            await twice.judge().judge(WISH, "x")
        assert caught.value.kind == "unparsable"

    @pytest.mark.parametrize(
        ("reply", "kind"),
        [
            (httpx.Response(401, json={"error": {"message": "User not found."}}), "http_401"),
            (httpx.Response(429, text="slow down"), "http_429"),
            (httpx.ReadTimeout("slow"), "timeout"),
            (httpx.ConnectError("refused"), "ConnectError"),
        ],
    )
    async def test_a_failed_call_names_its_cause_only(self, reply: Any, kind: str):
        with pytest.raises(JudgeError) as caught:
            await Server(reply).judge().judge(WISH, "x")
        assert caught.value.kind == kind

    def test_what_the_model_reads_of_a_post(self):
        post = item(
            text="Продавам къща",
            listing=Listing(title="Къща", price="BGN 150,000", location="Марково"),
            is_share=True,
        )
        assert post_text(post) == (
            "Продавам къща\n[marketplace listing] title: Къща; price: BGN 150,000; "
            "location: Марково\n[shared post]"
        )
        assert post_text(item(text="x" * 9000)) == "x" * 4000


@pytest.fixture
def ai(fb: FbStore) -> AiStore:
    return AiStore(fb)


async def keep(fb: FbStore, ai: AiStore, *posts: Any, now: int = EPOCH0) -> None:
    """What the ingest does for each post: keep its text, then queue it."""
    for post in posts:
        await fb.record_text(post, received_at=now)
        await ai.enqueue(int(post.group_id), int(post.post_id), created_at=post.created_at, now=now)


class TestQueue:
    async def test_a_post_is_queued_once_for_each_older_wish_on_its_group(
        self, ai: AiStore, fb: FbStore
    ):
        early = await ai.add_wish(OWNER, G1, WISH, now=EPOCH0 - 3600, threshold=70)
        await ai.add_wish(OWNER, G2, "дом", now=EPOCH0 - 3600, threshold=70)
        await ai.add_wish(MOD, G1, "later", now=EPOCH0, threshold=70)  # after the post: D10
        post = item(G1, 11, created_at=EPOCH0 - 60)
        await keep(fb, ai, post, post)  # a resend queues nothing twice
        due = await ai.due(now=EPOCH0, limit=10)
        assert [(q.wish_id, q.post_id) for q in due] == [(early.id, 11)]

    async def test_a_post_judged_already_is_not_queued_again_by_a_resend(
        self, ai: AiStore, fb: FbStore
    ):
        await ai.add_wish(OWNER, G1, WISH, now=EPOCH0 - 3600, threshold=70)
        await keep(fb, ai, item(G1, 11))
        (queued,) = await ai.due(now=EPOCH0, limit=10)
        await ai.record(queued, now=EPOCH0, verdict=Verdict(5, "нет", "m"))
        await keep(fb, ai, item(G1, 11))  # fb_items was busy: the collector sends it again
        assert await ai.due(now=EPOCH0, limit=10) == []

    async def test_a_deleted_wish_takes_its_waiting_posts_and_only_its_owners(
        self, ai: AiStore, fb: FbStore
    ):
        wish = await ai.add_wish(OWNER, G1, WISH, now=EPOCH0 - 3600, threshold=70)
        await keep(fb, ai, item(G1, 11))
        assert not await ai.delete_wish(wish.id, MOD)
        assert await ai.delete_wish(wish.id, OWNER)
        assert await ai.due(now=EPOCH0, limit=10) == []
        assert await ai.wishes() == []

    async def test_verdicts_matches_and_prune(self, ai: AiStore, fb: FbStore):
        wish = await ai.add_wish(OWNER, G1, WISH, now=EPOCH0 - 3600, threshold=70)
        await keep(fb, ai, item(G1, 11), item(G1, 12), item(G1, 13))
        q11, q12, q13 = await ai.due(now=EPOCH0, limit=10)
        model = "deepseek/deepseek-v4.1-flash"
        await ai.record(q11, now=EPOCH0, verdict=Verdict(92, "да", model, cost=0.001))
        await ai.record(q12, now=EPOCH0, verdict=Verdict(10, "водитель", model, cost=0.001))
        await ai.record(q13, now=EPOCH0, error="http_402")
        assert await ai.due(now=EPOCH0, limit=10) == []
        stats = await ai.stats(wish, since=EPOCH0 - 60)
        assert (stats.judged, stats.would_send, stats.failed, stats.queued) == (2, 1, 1, 0)
        assert stats.cost == pytest.approx(0.002)
        assert await ai.calls_since(EPOCH0 - 60) == 3  # the failed post's try counts too
        (found,) = await ai.matched(OWNER, since=EPOCH0 - 60, limit=10)
        assert (found.post_id, found.percent, found.reason) == (11, 92, "да")
        assert found.url == f"https://www.facebook.com/groups/{G1}/posts/11/"
        assert await ai.matched(MOD, since=EPOCH0 - 60, limit=10) == []
        assert await ai.prune(before=EPOCH0 + 1) == 3
        assert await ai.matched(OWNER, since=0, limit=10) == []

    async def test_the_ingest_queues_a_stored_post_and_not_an_unwatched_one(
        self,
        ai: AiStore,
        fb: FbStore,
        storage: Storage,
        book: GroupBook,
        clock: FakeClock,
    ):
        async def delivered(_row: Any) -> None:
            return None

        await seed_groups(book, G1, G2)
        await storage.subscribe(OWNER, G1)
        matcher = Matcher(storage, timeout=0.2)
        source = FacebookSource(storage, fb, matcher, deliver=delivered, ai=ai, clock=clock)
        await ai.add_wish(OWNER, G1, WISH, now=EPOCH0 - 3600, threshold=70)
        await ai.add_wish(OWNER, G2, WISH, now=EPOCH0 - 3600, threshold=70)
        assert await source.process(item(G1, 11)) is ItemOutcome.STORED
        assert await source.process(item(G2, 12)) is ItemOutcome.NOT_MONITORED
        assert [q.post_id for q in await ai.due(now=epoch(clock), limit=10)] == [11]


class FakeJudge:
    model = "fake/judge"

    def __init__(self, *results: Verdict | Exception) -> None:
        self.results = list(results)
        self.asked: list[tuple[str, str, str]] = []

    async def judge(self, wish: str, text: str, *, context: str = "") -> Verdict:
        self.asked.append((wish, text, context))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class Notes:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def __call__(self, user_id: int, text: str) -> None:
        self.sent.append((user_id, text))


def worker(
    ai: AiStore,
    fb: FbStore,
    storage: Storage,
    clock: FakeClock,
    *,
    judge: Any,
    notes: Notes,
    daily_calls: int = 100,
) -> AiWorker:
    return AiWorker(
        ai,
        fb,
        storage,
        judge,
        owner_id=OWNER,
        notify=notes,
        daily_calls=daily_calls,
        clock=clock,
    )


class TestWorker:
    @pytest.fixture
    async def wish(self, ai: AiStore, book: GroupBook) -> int:
        await seed_groups(book, G1)
        return (await ai.add_wish(OWNER, G1, WISH, now=EPOCH0 - 3600, threshold=70)).id

    async def test_due_posts_are_judged_and_recorded(
        self, ai: AiStore, fb: FbStore, storage: Storage, clock: FakeClock, wish: int
    ):
        judge = FakeJudge(Verdict(92, "да", "fake/judge"), Verdict(4, "нет", "fake/judge"))
        notes = Notes()
        await keep(fb, ai, item(G1, 11, text="Търся място"), item(G1, 12, text="Пътувам"))
        assert await worker(ai, fb, storage, clock, judge=judge, notes=notes).run_once() == 2
        assert sorted(text for _, text, _ in judge.asked) == ["Пътувам", "Търся място"]
        assert {context for _, _, context in judge.asked} == {f"Group {G1}"}
        assert await ai.due(now=epoch(clock), limit=10) == []
        assert [m.post_id for m in await ai.matched(OWNER, since=0, limit=10)] == [11]
        assert notes.sent == []

    async def test_a_post_no_longer_kept_is_dropped_unasked(
        self, ai: AiStore, fb: FbStore, storage: Storage, clock: FakeClock, wish: int
    ):
        await ai.enqueue(G1, 11, created_at=EPOCH0 - 60, now=EPOCH0)  # its text was pruned
        judge = FakeJudge()
        await worker(ai, fb, storage, clock, judge=judge, notes=Notes()).run_once()
        assert judge.asked == []
        assert await ai.due(now=epoch(clock) + 10**6, limit=10) == []

    async def test_a_failure_waits_longer_each_time_then_gives_up(
        self, ai: AiStore, fb: FbStore, storage: Storage, clock: FakeClock, wish: int
    ):
        await keep(fb, ai, item(G1, 11), now=epoch(clock))
        judge = FakeJudge(*(JudgeError("http_503") for _ in range(20)))
        work = worker(ai, fb, storage, clock, judge=judge, notes=Notes())
        for delay in BACKOFF[:3]:
            await work.run_once()
            assert await work.run_once() == 0  # not due yet
            clock.tick(delay)
        tries = len(judge.asked)
        assert tries == 3
        clock.tick(GIVE_UP_AFTER)
        await work.run_once()
        assert await ai.due(now=epoch(clock) + 10**6, limit=10) == []
        stats = await ai.stats((await ai.wishes())[0], since=0)
        assert (stats.failed, stats.judged) == (1, 0)

    async def test_the_owner_hears_once_when_the_model_fails_and_once_when_it_is_back(
        self, ai: AiStore, fb: FbStore, storage: Storage, clock: FakeClock, wish: int
    ):
        posts = [item(G1, pid) for pid in range(11, 11 + FAILURES_TO_ALERT + 1)]
        await keep(fb, ai, *posts)
        results: list[Verdict | JudgeError] = [JudgeError("http_401")] * (FAILURES_TO_ALERT + 1)
        judge = FakeJudge(*results, Verdict(5, "нет", "fake/judge"))
        notes = Notes()
        work = worker(ai, fb, storage, clock, judge=judge, notes=notes)
        await work.run_once()  # every one fails: one message, not one per failure
        assert notes.sent == [
            (
                OWNER,
                (
                    "🤖 The AI rule cannot reach its model (http_401). Posts wait in its queue "
                    "and are retried for six hours."
                ),
            )
        ]
        await keep(fb, ai, item(G1, 99))  # due now; the failed ones wait a minute
        await work.run_once()
        assert notes.sent[1:] == [(OWNER, "🤖 The AI rule's model answers again.")]

    async def test_the_daily_cap_stops_the_calls_and_is_told_once_a_day(
        self, ai: AiStore, fb: FbStore, storage: Storage, clock: FakeClock, wish: int
    ):
        await keep(fb, ai, *(item(G1, pid) for pid in range(11, 15)))
        judge = FakeJudge(*(Verdict(5, "нет", "fake/judge") for _ in range(4)))
        notes = Notes()
        work = worker(ai, fb, storage, clock, judge=judge, notes=notes, daily_calls=2)
        assert await work.run_once() == 2
        assert await work.run_once() == 0
        await work.run_once()
        assert len(judge.asked) == 2
        assert [text for _, text in notes.sent] == [
            (
                "🤖 The AI rule reached its cap of 2 model calls a day; posts wait until the "
                "day's calls fall below it (AI_DAILY_CALLS)."
            )
        ]
        clock.tick(86400)
        assert await work.run_once() == 2

    async def test_any_failure_of_the_judge_backs_off_and_counts(
        self, ai: AiStore, fb: FbStore, storage: Storage, clock: FakeClock, wish: int
    ):
        # An OverflowError out of the judge left the row due, asked every tick, uncounted.
        await keep(fb, ai, item(G1, 11), now=epoch(clock))
        judge = FakeJudge(OverflowError("cannot convert float infinity"))
        work = worker(ai, fb, storage, clock, judge=judge, notes=Notes())
        await work.run_once()
        assert await work.run_once() == 0
        assert await ai.calls_since(epoch(clock) - 60) == 1

    async def test_given_up_tries_still_count_against_the_cap(
        self, ai: AiStore, fb: FbStore, storage: Storage, clock: FakeClock, wish: int
    ):
        await keep(fb, ai, item(G1, 11), now=epoch(clock))
        judge = FakeJudge(*(JudgeError("http_402") for _ in range(10)))
        work = worker(ai, fb, storage, clock, judge=judge, notes=Notes())
        for delay in (*BACKOFF, GIVE_UP_AFTER):
            await work.run_once()
            clock.tick(delay)
        await work.run_once()
        assert await ai.due(now=epoch(clock) + 10**6, limit=10) == []
        assert await ai.calls_since(epoch(clock) - 86400) == len(judge.asked)

    async def test_a_notice_that_failed_is_tried_again(
        self, ai: AiStore, fb: FbStore, storage: Storage, clock: FakeClock, wish: int
    ):
        class Busy(Notes):
            async def __call__(self, user_id: int, text: str) -> None:
                if not self.sent and not getattr(self, "failed", False):
                    self.failed = True
                    raise sqlite3.OperationalError("database is locked")
                await super().__call__(user_id, text)

        await keep(fb, ai, *(item(G1, pid) for pid in range(11, 14)))
        notes = Busy()
        judge = FakeJudge(*(JudgeError("http_401") for _ in range(10)))
        work = worker(ai, fb, storage, clock, judge=judge, notes=notes, daily_calls=100)
        await work.run_once()  # the notice failed to be queued
        assert notes.sent == []
        await keep(fb, ai, item(G1, 99))
        await work.run_once()
        assert len(notes.sent) == 1

    async def test_without_a_key_nothing_is_asked(
        self, ai: AiStore, fb: FbStore, storage: Storage, clock: FakeClock, wish: int
    ):
        await keep(fb, ai, item(G1, 11))
        assert await worker(ai, fb, storage, clock, judge=None, notes=Notes()).run_once() == 0
        assert len(await ai.due(now=epoch(clock), limit=10)) == 1


class TestCommand:
    @pytest.fixture
    async def router(
        self,
        *,
        storage: Storage,
        monitor: CollectorMonitor,
        desk: ControlDesk,
        fb: FbStore,
        ai: AiStore,
        clock: FakeClock,
    ) -> Router:
        from ddiver_core.delivery import Delivery

        delivery = Delivery(FakeBotClient(), storage, clock=clock)
        judge = FakeJudge()
        return build_fb_router(
            storage,
            matcher=Matcher(storage, timeout=0.2),
            monitor=monitor,
            desk=desk,
            fb=fb,
            counters=lambda: StatusSources(SourceMetrics(), delivery, T0),
            regex_timeout=0.2,
            ai=ai,
            ai_worker=worker(ai, fb, storage, clock, judge=judge, notes=Notes()),
            clock=clock,
        )

    async def say(self, router: Router, user: int, text: str) -> str:
        (action,) = await router.dispatch(Incoming(user_id=user, text=text))
        return str(getattr(action, "key_or_text", ""))

    async def test_the_owner_adds_lists_and_deletes_a_wish(
        self, router: Router, ai: AiStore, fb: FbStore, book: GroupBook, storage: Storage
    ):
        await seed_groups(book, G1)
        await storage.subscribe(OWNER, G1)
        added = await self.say(router, OWNER, f"/ai add {G1}   {WISH}  ")
        assert added.startswith(f"🤖 Wish #1 for Group {G1}: «{WISH}».")
        (wish,) = await ai.wishes(OWNER)
        assert (wish.group_id, wish.wish, wish.threshold, wish.live) == (G1, WISH, 70, False)
        await keep(fb, ai, item(G1, 11, created_at=EPOCH0 + 5))  # written after the wish
        (queued,) = await ai.due(now=EPOCH0 + 10, limit=1)
        await ai.record(queued, now=EPOCH0, verdict=Verdict(95, "ищет место", "fake/judge"))
        shown = await self.say(router, OWNER, "/ai")
        assert shown.startswith("🤖 AI rule · model fake/judge · shadow:")
        assert f"#1 Group {G1}: «{WISH}» — sends from 70%" in shown
        assert "24 h: 1 judged, 1 would be sent, 0 failed, 0 waiting" in shown
        assert "• 95% #1 ищет место\n  https://www.facebook.com/groups/" in shown
        assert "Model calls in 24 h: 1 of 100" in shown
        assert await self.say(router, OWNER, "/ai del 1") == "🤖 Wish #1 deleted."
        assert await ai.wishes() == []
        assert (await self.say(router, OWNER, "/ai del 1")).startswith("You have no wish #1")

    async def test_a_wish_needs_a_watched_group_and_words(
        self, router: Router, ai: AiStore, book: GroupBook, storage: Storage
    ):
        await seed_groups(book, G1, G2)
        await storage.subscribe(OWNER, G1)
        assert (await self.say(router, OWNER, f"/ai add {G2} дом")).startswith(
            f"No watched group with id {G2}"
        )
        assert (await self.say(router, OWNER, f"/ai add {G1} ab")).startswith("Write the wish")
        assert (await self.say(router, OWNER, "/ai frobnicate")).startswith("/ai add")
        assert (await self.say(router, OWNER, "/ai add")).startswith("/ai add")
        assert (await self.say(router, OWNER, "/ai del")).startswith("/ai add")
        assert (await self.say(router, MOD, f"/ai add {G1} дом в Марково")) == (
            "bot.err.owner_only"
        )
        assert await ai.wishes() == []
