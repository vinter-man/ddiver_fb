"""Writes src/captured.json: what the bot says on each README screen, rendered by its own code.

The screenshots are drawn by Remotion, but their words are not typed by hand: this script seeds a
throwaway database with made-up groups, rules, posts and verdicts, drives the real bot router
(``/status``, ``/test``, ``/ai``, the groups screen) and the real notification renderer, and
saves the text and buttons each one produced. A wording change in the bot is one re-run away
from the pictures, and a picture cannot show a message the bot would never send.

    uv run python -m tools.readme_shots && (cd docs/remotion && npm run build)

Offline: no Telegram, no Facebook, no model. The AI verdicts below are the answers DeepSeek V4.1
Flash gave to these exact posts and wishes on 2026-10-24 through ddiver_fb.judge, copied here
so that a re-run costs nothing.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import html
import itertools
import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tests.brain.helpers import Invalidations, Nonces, Outbox, info, item, snapshot, status

from ddiver_core import i18n
from ddiver_core.bot import menus
from ddiver_core.bot.router import Incoming, Router
from ddiver_core.delivery import Delivery
from ddiver_core.formatter import Payload, render_notification
from ddiver_core.i18n import t
from ddiver_core.matcher import Matcher
from ddiver_core.rules import RuleKind
from ddiver_core.storage import Storage
from ddiver_core.timeutil import format_local
from ddiver_core_testing import FakeBotClient, FakeClock
from ddiver_fb import schema, strings
from ddiver_fb.ai import AiStore, AiWorker
from ddiver_fb.control import ControlDesk
from ddiver_fb.groups import GroupBook
from ddiver_fb.judge import Verdict
from ddiver_fb.monitor import CollectorMonitor, MonitorSettings
from ddiver_fb.pacing import FEED_COUNTER
from ddiver_fb.source import SOURCE_FACEBOOK, SourceMetrics
from ddiver_fb.store import FbStore
from ddiver_fb.ui import StatusSources, build_fb_router
from fbwire import BudgetKind, BudgetWindow, ItemOutcome

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "remotion" / "src" / "captured.json"
OWNER = 41_700_000
TZ = "Europe/Sofia"
NOW = dt.datetime(2026, 10, 24, 8, 3, 5, tzinfo=dt.UTC)  # 11:03 in Sofia
EPOCH = int(NOW.timestamp())
COLLECTOR = "mac-1"

SECOND_HAND, MOMS, FREE, RIDES, FLATS, BIKES = (
    480_112_093_517_204,
    1_293_550_871_064_118,
    361_048_227_915_530,
    207_845_119_336_801,
    1_510_377_402_268_945,
    905_662_184_027_713,
)
NAMES = {
    SECOND_HAND: "Second-hand · Sofia",
    MOMS: "Sofia Moms",
    FREE: "Free stuff Sofia",
    RIDES: "Rides Plovdiv ⇄ Sofia",
    FLATS: "Flats for rent Sofia, no agencies",
    BIKES: "Bikes & scooters BG",
}
WATCHED = (SECOND_HAND, MOMS, FREE, RIDES, FLATS)

WISHES = (
    (RIDES, "people who need a ride from Plovdiv to Sofia, not drivers offering seats"),
    (
        FLATS,
        (
            "a two-room flat to rent in Lozenets or Ivan Vazov, up to 700 euro, "
            "straight from the owner"
        ),
    ),
)
# (wish index, post id, minutes ago, text, percent, reason): the model's own answers.
VERDICTS = (
    (
        0,
        3_118_204_779_501,
        14,
        "Търся място за утре сутрин от Пловдив за София, 1 човек, малък багаж",
        100,
        "Passenger seeks ride from Plovdiv to Sofia, exactly matching wish.",
    ),
    (
        0,
        3_118_191_002_845,
        52,
        "Пътувам Пловдив - София днес 18:00, 3 свободни места, 15лв",
        0,
        "Driver offers seats from Plovdiv to Sofia, not someone needing a ride.",
    ),
    (
        0,
        3_117_950_337_160,
        190,
        "Има ли някой за София от Пловдив сега? Плащам гориво",
        100,
        "Post is a passenger seeking ride from Plovdiv to Sofia, paying fuel.",
    ),
    (
        0,
        3_117_802_514_473,
        260,
        "Търся място София - Пловдив в петък вечерта",
        0,
        "Post seeks Sofia to Plovdiv, opposite direction from desired Plovdiv to Sofia.",
    ),
    (
        1,
        2_204_617_330_918,
        35,
        "Давам под наем двустаен в Лозенец, 650 евро, от собственик, без комисионна",
        100,
        "Two-room flat in Lozenets, 650 euro, directly from owner, no commission.",
    ),
    (
        1,
        2_204_598_116_022,
        80,
        "Агенция Дом+ предлага двустайни апартаменти в Лозенец, 680 евро",
        0,
        "Agency offer, not straight from owner, so it does not fit.",
    ),
    (
        1,
        2_204_411_920_457,
        300,
        "Тристаен в Лозенец, 1100 евро",
        0,
        "Three-room flat in Lozenets for 1100 euro, not two-room or under 700.",
    ),
)
MODEL = "deepseek/deepseek-v4.1-flash"


class _NoJudge:
    model = MODEL

    async def judge(self, wish: str, text: str, *, context: str = "") -> Verdict:
        del wish, text, context
        raise AssertionError("capture never asks a model")


async def _quiet(user_id: int, text: str) -> None:
    del user_id, text


def entities_to_html(text: str, entities: list[Any]) -> str:
    """Telethon's entities (UTF-16 offsets) as the HTML the README pictures draw."""
    units = text.encode("utf-16-le")
    marks: list[tuple[int, int, str, str]] = []
    for e in entities:
        kind = type(e).__name__
        if kind == "MessageEntityBold":
            opening, closing = "<b>", "</b>"
        elif kind == "MessageEntityCode":
            opening, closing = "<code>", "</code>"
        elif kind in {"MessageEntityTextUrl", "MessageEntityUrl"}:
            opening, closing = '<a href="#">', "</a>"
        else:
            continue
        marks.append((e.offset, e.offset + e.length, opening, closing))
    cuts = sorted({0, len(units) // 2, *(m[0] for m in marks), *(m[1] for m in marks)})
    out: list[str] = []
    for a, b in itertools.pairwise(cuts):
        piece = html.escape(units[a * 2 : b * 2].decode("utf-16-le"), quote=False)
        for start, end, opening, closing in marks:
            if start <= a and b <= end:
                piece = f"{opening}{piece}{closing}"
        out.append(piece)
    return "".join(out)


def buttons(rows: Any, lang: str = "en") -> list[list[str]]:
    return [
        [menus.unwrap(label) if menus.is_literal(label) else t(lang, label) for label, _ in row]
        for row in rows or []
    ]


def text_of(action: Any, lang: str = "en") -> str:
    raw = action.key_or_text if action.literal else t(lang, action.key_or_text, **action.params)
    return raw if action.html else html.escape(raw, quote=False)


async def say(router: Router, text: str | None = None, data: str | None = None) -> Any:
    actions = await router.dispatch(Incoming(user_id=OWNER, text=text, data=data))
    return actions[0]


def notification(
    gid: int,
    pid: int,
    author: tuple[int, str],
    text: str,
    keyword: str,
    *,
    posted: dt.datetime,
    seen: dt.datetime,
    private: bool,
) -> dict[str, Any]:
    """One notification as ddiver-core renders it for a Facebook post (``build_payload``)."""
    start = text.index(keyword)
    author_id, name = author
    payload = Payload(
        chat_id=gid,
        chat_title=NAMES[gid],
        chat_username=None,
        chat_kind="group",
        msg_id=pid,
        topic_id=None,
        date=posted,
        text=text,
        spans=[(start, start + len(keyword))],
        rule_pattern=keyword,
        rule_kind=RuleKind.INCLUDE.value,
        rule_is_global=False,
        sender_id=author_id,
        sender_name=name,
        sender_username=None,
        source=SOURCE_FACEBOOK,
        url=f"https://www.facebook.com/groups/{gid}/posts/{pid}/",
        chat_url=f"https://www.facebook.com/groups/{gid}/",
        sender_url=f"https://www.facebook.com/profile.php?id={author_id}",
        chat_private=private,
    )
    rendered = render_notification(payload, lang="en", tz=TZ, now=seen)
    local = seen.astimezone(ZoneInfo(TZ))
    return {
        "html": entities_to_html(rendered.text, rendered.entities),
        "buttons": buttons(menus.notification_buttons(gid, pid, author_id, 1)),
        "time": local.strftime("%H:%M"),
    }


def notifications() -> dict[str, dict[str, Any]]:
    """Today's notification, and the end of last night's above it."""
    return {
        "notification": notification(
            SECOND_HAND,
            1_042_877_310_662_905,
            (100_012_345_678_901, "Maria Petrova"),
            "Selling a stroller, barely used, 80 eur. Pickup in Mladost 4, can send more photos 🙂",
            "stroller",
            posted=NOW - dt.timedelta(minutes=3, seconds=12),
            seen=NOW,
            private=True,
        ),
        "earlier": notification(
            FREE,
            8_830_164_502_771,
            (61_550_872_014_336, "Ivo Dimitrov"),
            "Giving away a stroller rain cover and two baby bottles, pickup Lyulin 7 tonight",
            "stroller",
            posted=NOW - dt.timedelta(hours=15, minutes=49),
            seen=NOW - dt.timedelta(hours=15, minutes=44),
            private=False,
        ),
    }


def alerts() -> list[dict[str, str]]:
    """A soft block on a Saturday morning and how it ends, as the owner's chat shows it."""
    blocked_at = dt.datetime(2026, 10, 17, 5, 12, tzinfo=dt.UTC)
    until = format_local(blocked_at + dt.timedelta(hours=6), TZ)
    lines: list[tuple[str, str, str, dict[str, object]]] = [
        (
            "bot",
            "08:12",
            "fb.alert.blocked",
            {"id": COLLECTOR, "block": 1, "reason": "graphql:rate_limit_text", "until": until},
        ),
        ("me", "14:25", "/resume", {}),
        ("bot", "14:25", "fb.resume.sent", {}),
        ("bot", "14:31", "fb.alert.block_closed", {"block": 1}),
        ("bot", "14:31", "fb.alert.back", {"id": COLLECTOR}),
        ("bot", "19:40", "fb.alert.silence", {"group": NAMES[MOMS], "silent": "3 h", "gap": "1 h"}),
    ]
    return [
        {
            "who": who,
            "time": hhmm,
            "html": key if who == "me" else html.escape(t("en", key, **params), quote=False),
        }
        for who, hhmm, key, params in lines
    ]


async def seed(storage: Storage, fb: FbStore, monitor: CollectorMonitor) -> None:
    """The groups the account is in, the owner's subscriptions and rules, a fresh status."""
    groups = [info(gid, NAMES[gid]) for gid in NAMES]
    await monitor.on_snapshot(snapshot(*groups, taken_at=EPOCH - 3600, collector_id=COLLECTOR))
    for gid in WATCHED:
        await storage.subscribe(OWNER, gid)
    stroller = await storage.add_rule(OWNER, SECOND_HAND, RuleKind.INCLUDE, "stroller")
    crib = await storage.add_rule(OWNER, SECOND_HAND, RuleKind.INCLUDE, "crib")
    await storage.add_rule(OWNER, SECOND_HAND, RuleKind.EXCLUDE, "broken")
    free = await storage.add_rule(OWNER, FREE, RuleKind.INCLUDE, "stroller")
    doctor = await storage.add_rule(OWNER, MOMS, RuleKind.INCLUDE, "pediatrician")
    await storage.flush_rule_hits({stroller.id: 23, crib.id: 9, free.id: 6, doctor.id: 4})
    await fb.touch_collector(COLLECTOR, now=EPOCH)
    budget = (
        BudgetWindow(kind=BudgetKind.NAVIGATION, window_seconds=3600, cap=20, used=7),
        BudgetWindow(kind=BudgetKind.SCROLL, window_seconds=86400, cap=500, used=164),
    )
    await monitor.on_status(
        status(
            collector_id=COLLECTOR,
            sent_at=EPOCH,
            budget=budget,
            detail="reading the Groups feed",
            counters={FEED_COUNTER: 9 * 60},
        )
    )


async def seed_verdicts(ai: AiStore, fb: FbStore) -> None:
    """Two wishes made three days ago, and the posts the model has judged since."""
    for gid, words in WISHES:
        await ai.add_wish(OWNER, gid, words, now=EPOCH - 3 * 86400, threshold=70)
    wishes = await ai.wishes(OWNER)
    for index, pid, minutes, text, percent, reason in VERDICTS:
        wish = wishes[index]
        posted = EPOCH - minutes * 60
        post = item(
            wish.group_id,
            pid,
            text,
            created_at=posted,
            collector_id=COLLECTOR,
            group_name=NAMES[wish.group_id],
        )
        await fb.record_text(post, received_at=posted + 60)
        await ai.enqueue(wish.group_id, pid, created_at=posted, now=posted + 60)
        (queued,) = [q for q in await ai.due(now=EPOCH, limit=50) if q.post_id == pid]
        await ai.record(
            queued, now=posted + 75, verdict=Verdict(percent, reason, MODEL, cost=0.00022)
        )


async def capture(tmp: Path) -> dict[str, Any]:
    clock = FakeClock(NOW)
    storage = Storage(
        tmp / "fb.sqlite", owner_id=OWNER, clock=clock, tracks={schema.TRACK: schema.MIGRATIONS}
    )
    await storage.open()
    await storage.ensure_user(OWNER)
    await storage.update_user(OWNER, lang="en", tz=TZ)
    fb = FbStore(storage)
    desk = ControlDesk(storage, fb, clock=clock, nonce=Nonces())
    book = GroupBook(storage, fb, tell=Outbox(), invalidate=Invalidations(), clock=clock)
    monitor = CollectorMonitor(
        storage,
        fb,
        desk,
        book,
        owner_id=OWNER,
        tell=Outbox(),
        clock=clock,
        settings=MonitorSettings(tz=TZ),
    )
    ai = AiStore(fb)
    worker = AiWorker(
        ai, fb, storage, _NoJudge(), owner_id=OWNER, notify=_quiet, daily_calls=1000, clock=clock
    )
    metrics = SourceMetrics()
    metrics.count(ItemOutcome.STORED, 1284)
    metrics.count(ItemOutcome.DUPLICATE, 211)
    metrics.count(ItemOutcome.NOT_MONITORED, 57)
    delivery = Delivery(FakeBotClient(), storage, clock=clock)
    delivery.metrics.delivered = 38
    delivery.metrics.digested = 4
    router = build_fb_router(
        storage,
        matcher=Matcher(storage, timeout=0.5),
        monitor=monitor,
        desk=desk,
        fb=fb,
        counters=lambda: StatusSources(metrics, delivery, NOW - dt.timedelta(days=6, hours=3)),
        regex_timeout=0.5,
        ai=ai,
        ai_worker=worker,
        clock=clock,
    )
    try:
        await seed(storage, fb, monitor)

        clock.tick(75)  # the owner looks a little after the collector's last report
        groups_screen = await say(router, data="chats:0")
        test = await say(router, "/test Selling a stroller and a baby crib, barely used, Mladost")
        status_reply = await say(router, "/status")

        await seed_verdicts(ai, fb)
        ai_reply = await say(router, "/ai")
    finally:
        await monitor.close()
        await fb.close()
        await storage.close()

    return {
        **notifications(),
        "groups": {"html": text_of(groups_screen), "buttons": buttons(groups_screen.buttons)},
        "test": {
            "command": "/test Selling a stroller and a baby crib, barely used, Mladost",
            "html": text_of(test),
        },
        "status": {"command": "/status", "html": text_of(status_reply)},
        "ai": {"command": "/ai", "html": text_of(ai_reply)},
        "alerts": alerts(),
        "wish": WISHES[0][1],
        "ids": {"rides": str(RIDES)},
        "verdicts": [
            {"text": text, "percent": percent, "reason": reason}
            for index, _, _, text, percent, reason in VERDICTS
            if index == 0
        ],
        "names": NAMES,
    }


def main() -> None:
    strings.install()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            data = asyncio.run(capture(Path(tmp)))
    finally:
        i18n.clear_overlays()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(f"wrote {OUT.relative_to(ROOT)}\n")


if __name__ == "__main__":
    main()
