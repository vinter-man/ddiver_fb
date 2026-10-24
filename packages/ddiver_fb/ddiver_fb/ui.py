"""ddiver's bot UI with the Facebook changes, through the core's hooks rather than a fork.

- The bots toggle is hidden (``telegram_settings=False``): no Facebook post comes from a
  Telegram bot, so the switch would change nothing while claiming otherwise.
- The weekly summary and backup toggles are hidden too (``weekly_settings=False``): the core
  only stores them, and the weekly task that reads them is ddiver's, which the brain does not
  run. A forged ``set:weekly`` or ``set:backup`` changes nothing.
- The owner's chat lists get "🔄 Refresh groups", which queues a one-shot ``refresh_groups``
  command for the collector (at most three a day, like the collector's own limit).
- ``/status`` is this module's: the collector's state, the lease, the budget and — for everyone —
  the honest delay for their groups, instead of a Telegram listener card.
- Owner commands: ``/pause``, ``/resume`` (bound to the collector's *current* block, with a
  confirmation button that refuses to act on a block that has since changed; for an
  advanced-protection lock the button is the owner's "the lock is lifted"), ``/pace``,
  ``/lease`` (two-step, see ``control.py``; ``/lease <id> now`` for a holder switched off) and
  ``/listings <group id> on|off``, the per-group listings sort phase 0 may call for.
- ``/ai`` (owner only, for now): the AI rule's wishes — ``/ai add <group id> <wish>``, ``/ai del
  <n>`` — what each judged in a day, and what would have been sent (the rule runs in shadow).
- ``/export`` writes, and ``/import`` accepts, only ``ddiver-fb.config``: a ddiver export's chat
  ids are Telegram's and would be looked up here as Facebook group ids.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from ddiver_core.app import format_uptime
from ddiver_core.bot import menus
from ddiver_core.bot.handlers import UiHooks, build_router
from ddiver_core.bot.router import Action, Context, Edit, Reply, Router, Toast
from ddiver_core.delivery import Delivery
from ddiver_core.matcher import Matcher
from ddiver_core.storage import SQLITE_INT_MAX, Storage
from ddiver_core.timeutil import Clock, format_local, relative, utcnow
from ddiver_fb.ai import DAY, DEFAULT_THRESHOLD, MAX_WISH_CHARS, MAX_WISHES, AiStore, AiWorker
from ddiver_fb.control import (
    REFRESH_PER_DAY,
    ControlDesk,
    LeaseOutcome,
    RefreshOutcome,
    ResumeOutcome,
    ResumeVerdict,
    resume_verdict,
)
from ddiver_fb.monitor import CollectorMonitor, CollectorView, format_duration
from ddiver_fb.pacing import FEED_COUNTER, backstop_delay, feed_delay, shoulder_delay
from ddiver_fb.source import SourceMetrics
from ddiver_fb.store import CollectorRow, FbGroup, FbStore
from fbwire import MAX_MONITORED, CollectorState, GroupState, ItemOutcome, Profile, Status

EXPORT_FORMAT: Final = "ddiver-fb.config"
LOCK_LIFTED: Final = "lifted"
"""The resume button's suffix that carries the owner's "the advanced-protection lock is lifted"."""

DELAY_STATES: Final = frozenset({CollectorState.OK, CollectorState.FEED_NOT_CHRONO})
"""States in which posts are read, so a delay can be promised."""
PAUSE_STATES: Final = frozenset(
    {CollectorState.QUIET, CollectorState.BREAK, CollectorState.UNKNOWN}
)
"""Planned pauses (quiet hours, a break, a back-off): nothing is read now, and reading resumes
by itself — said as such, with the time the collector announced, not as a delay in minutes."""

MAX_LISTED: Final = 12
"""Group names per /status line; the rest are counted, so the reply stays under Telegram's cap."""


@dataclass(frozen=True, slots=True)
class StatusSources:
    """The live counters /status reads; the composition root passes its own objects."""

    source: SourceMetrics
    delivery: Delivery | None
    started_at: dt.datetime


def build_fb_router(
    storage: Storage,
    *,
    matcher: Matcher,
    monitor: CollectorMonitor,
    desk: ControlDesk,
    fb: FbStore,
    counters: Callable[[], StatusSources],
    regex_timeout: float,
    ai: AiStore | None = None,
    ai_worker: AiWorker | None = None,
    clock: Clock = utcnow,
) -> Router:
    async def refresh_groups(_ctx: Context) -> list[Action]:
        outcome = await desk.request_refresh()
        if outcome is RefreshOutcome.LIMIT:
            return [Toast("fb.refresh.limit", {"n": REFRESH_PER_DAY}, alert=True)]
        if outcome is RefreshOutcome.NO_HOLDER:
            return [Toast("fb.refresh.no_holder", alert=True)]
        key = "fb.refresh.sent" if outcome is RefreshOutcome.SENT else "fb.refresh.pending"
        return [Toast(key, alert=True)]

    async def status_text(ctx: Context) -> str:
        view = await monitor.view()
        return await write_status(
            ctx,
            view,
            storage=storage,
            counters=counters(),
            pace_cap=await desk.pace_cap(),
            now=clock(),
        )

    router = build_router(
        storage,
        matcher=matcher,
        regex_timeout=regex_timeout,
        clock=clock,
        export_format=EXPORT_FORMAT,
        hooks=UiHooks(
            telegram_settings=False,
            weekly_settings=False,
            refresh_chats=refresh_groups,
            status_text=status_text,
        ),
    )

    _owner_commands(router, monitor=monitor, desk=desk, fb=fb, clock=clock)
    if ai is not None and ai_worker is not None:
        _ai_command(router, ai=ai, worker=ai_worker, desk=desk, fb=fb, clock=clock)
    return router


def _owner_only(ctx: Context) -> list[Action] | None:
    """The second layer: the router refuses these commands first (``owner_only=True``)."""
    return None if ctx.is_owner else [Reply("bot.err.owner_only")]


def _owner_commands(
    router: Router, *, monitor: CollectorMonitor, desk: ControlDesk, fb: FbStore, clock: Clock
) -> None:
    """``/pause``, ``/resume`` (and its confirmation), ``/pace``, ``/lease``, ``/listings``."""
    _resume_commands(router, monitor=monitor, desk=desk, clock=clock)
    _lease_command(router, monitor=monitor, desk=desk)

    @router.command("pause", owner_only=True)
    async def pause(ctx: Context) -> list[Action]:
        if refused := _owner_only(ctx):
            return refused
        return [Reply("fb.pause.done" if await desk.pause() else "fb.pause.already")]

    @router.command("listings", owner_only=True)
    async def listings(ctx: Context) -> list[Action]:
        if refused := _owner_only(ctx):
            return refused
        args = ctx.incoming.args.split()
        groups = {g.chat_id: g for g in await fb.groups()}
        if len(args) == 2 and args[1].lower() in {"on", "off"}:
            gid = _int(args[0])
            on = args[1].lower() == "on"
            if gid not in groups or not await fb.set_listings(gid, on):
                return [Reply(ctx.t("fb.listings.unknown", id=args[0][:32]), literal=True)]
            state = ctx.t("fb.listings.on" if on else "fb.listings.off")
            return [
                Reply(ctx.t("fb.listings.set", group=groups[gid].title, state=state), literal=True)
            ]
        watched = [groups[gid] for gid in (await desk.monitored_ids()) if gid in groups]
        on_names = [g.title for g in watched if g.listings]
        text = ctx.t(
            "fb.listings.show",
            on=", ".join(on_names) or ctx.t("fb.listings.none"),
            groups=", ".join(f"{g.title} ({g.chat_id})" for g in watched) or "—",
        )
        return [Reply(text, literal=True)]

    @router.command("pace", owner_only=True)
    async def pace(ctx: Context) -> list[Action]:
        if refused := _owner_only(ctx):
            return refused
        wanted = ctx.incoming.args.strip().lower()
        if wanted in {Profile.CAUTIOUS.value, Profile.NORMAL.value}:
            # "normal" as a cap never beats a local cautious (min), so it means "no cap".
            cap = Profile.CAUTIOUS if wanted == Profile.CAUTIOUS.value else None
            await desk.set_pace_cap(cap)
            return [Reply("fb.pace.set", {"profile": _cap_name(ctx, cap)})]
        return [Reply("fb.pace.show", {"profile": _cap_name(ctx, await desk.pace_cap())})]


AI_MATCHED_SHOWN: Final = 10
AI_REASON_CHARS: Final = 160
MESSAGE_CHARS: Final = 4000
"""Telegram takes 4096 characters; a long /ai answer is cut a little before that."""


def _ai_command(
    router: Router, *, ai: AiStore, worker: AiWorker, desk: ControlDesk, fb: FbStore, clock: Clock
) -> None:
    @router.command("ai", owner_only=True)
    async def ai_rule(ctx: Context) -> list[Action]:
        if refused := _owner_only(ctx):
            return refused
        verb, _, rest = ctx.incoming.args.strip().partition(" ")
        now = int(clock().timestamp())
        user_id = ctx.user_id
        groups = {g.chat_id: g for g in await fb.groups()}
        if verb.lower() in {"add", "del"} and not rest.strip():
            return [Reply(ctx.t("fb.ai.usage"), literal=True)]
        if verb.lower() == "add":
            raw_id, _, wish = rest.strip().partition(" ")
            wish = " ".join(wish.split())
            gid = _int(raw_id)
            if gid not in groups or gid not in await desk.monitored_ids():
                return [Reply(ctx.t("fb.ai.unknown_group", id=raw_id[:32]), literal=True)]
            if not 3 <= len(wish) <= MAX_WISH_CHARS:
                return [Reply(ctx.t("fb.ai.bad_wish", n=MAX_WISH_CHARS), literal=True)]
            if len(await ai.wishes(user_id)) >= MAX_WISHES:
                return [Reply(ctx.t("fb.ai.too_many", n=MAX_WISHES), literal=True)]
            added = await ai.add_wish(user_id, gid, wish, now=now, threshold=DEFAULT_THRESHOLD)
            text = ctx.t("fb.ai.added", n=added.id, group=groups[gid].title, wish=wish)
            return [Reply(text, literal=True)]
        if verb.lower() == "del":
            wish_id = _int(rest.strip().lstrip("#"))
            if not await ai.delete_wish(wish_id, user_id):
                return [Reply(ctx.t("fb.ai.not_found", n=rest.strip()[:32]), literal=True)]
            return [Reply(ctx.t("fb.ai.deleted", n=wish_id), literal=True)]
        if verb:
            return [Reply(ctx.t("fb.ai.usage"), literal=True)]
        overview = await _ai_overview(ctx, ai, worker, groups, now)
        return [Reply(overview[:MESSAGE_CHARS], literal=True)]


async def _ai_overview(
    ctx: Context, ai: AiStore, worker: AiWorker, groups: dict[int, FbGroup], now: int
) -> str:
    """Blocks: the model (or why the rule is off), the wishes with their day, the calls
    against the cap, what would have been sent this week, and how to change the wishes."""
    judge = worker.judge
    blocks = [ctx.t("fb.ai.head", model=judge.model) if judge is not None else ctx.t("fb.ai.off")]
    wishes = await ai.wishes(ctx.user_id)
    lines = []
    for wish in wishes:
        stats = await ai.stats(wish, since=now - DAY)
        group = groups.get(wish.group_id)
        lines.append(
            ctx.t(
                "fb.ai.wish",
                n=wish.id,
                group=group.title if group is not None else str(wish.group_id),
                wish=wish.wish,
                threshold=wish.threshold,
                judged=stats.judged,
                would=stats.would_send,
                failed=stats.failed,
                queued=stats.queued,
                cost=f"{stats.cost:.3f}",
            )
        )
    blocks.append("\n".join(lines) if lines else ctx.t("fb.ai.none"))
    if judge is not None:
        calls = await ai.calls_since(now - DAY)
        blocks.append(ctx.t("fb.ai.calls", calls=calls, cap=worker.daily_calls))
    if wishes:
        matched = await ai.matched(ctx.user_id, since=now - 7 * DAY, limit=AI_MATCHED_SHOWN)
        lines = [ctx.t("fb.ai.matched_head") if matched else ctx.t("fb.ai.matched_none")]
        for found in matched:
            line = ctx.t(
                "fb.ai.matched",
                percent=found.percent,
                n=found.wish_id,
                reason=found.reason[:AI_REASON_CHARS],
            )
            lines.append(f"{line}\n  {found.url}" if found.url else line)
        blocks.append("\n".join(lines))
    blocks.append(ctx.t("fb.ai.usage"))
    return "\n\n".join(blocks)


def _resume_commands(
    router: Router, *, monitor: CollectorMonitor, desk: ControlDesk, clock: Clock
) -> None:
    @router.command("resume", owner_only=True)
    async def resume(ctx: Context) -> list[Action]:
        if refused := _owner_only(ctx):
            return refused
        actions: list[Action] = []
        if await desk.unpause():
            actions.append(Reply("fb.resume.unpaused"))
        row = await monitor.holder_row()
        block = row.status.block if row is not None and row.status is not None else None
        verdict = resume_verdict(block, now=int(clock().timestamp()))
        if block is None or verdict is ResumeVerdict.NO_BLOCK:
            return actions or [Reply("fb.resume.nothing")]
        if verdict is ResumeVerdict.NO_PROBE:
            # The collector arms no probe for this lock until the owner says it is lifted;
            # the button carries that confirmation, bound to this block like any resume.
            card = ctx.t("fb.resume.lock_card", id=block.id, reason=block.reason)
            button = menus.literal(ctx.t("fb.resume.lock_button", id=block.id))
            callback = menus.check_callback(f"fbresume:{block.id}:{LOCK_LIFTED}")
            actions.append(Reply(card, literal=True, buttons=[[(button, callback)]]))
            return actions
        if verdict is not ResumeVerdict.OK:
            actions.append(_refusal(ctx, verdict, block.id, block.until))
            return actions
        opened = format_local(dt.datetime.fromtimestamp(block.opened_at, dt.UTC), _tz(ctx))
        card = ctx.t(
            "fb.resume.card", id=block.id, reason=block.reason, opened=opened, rung=block.rung
        )
        button = menus.literal(ctx.t("fb.resume.button", id=block.id))
        actions.append(
            Reply(
                card,
                literal=True,
                buttons=[[(button, menus.check_callback(f"fbresume:{block.id}"))]],
            )
        )
        return actions

    @router.callback("fbresume", owner_only=True)
    async def resume_confirmed(ctx: Context) -> list[Action]:
        _, _, rest = (ctx.incoming.data or "").partition(":")
        raw_id, _, flag = rest.partition(":")
        block_id, lifted = _int(raw_id), flag == LOCK_LIFTED
        row = await monitor.holder_row()
        current = row.status.block if row is not None and row.status is not None else None
        outcome = await desk.request_resume(block_id, current, lock_lifted=lifted)
        if outcome is ResumeOutcome.SENT:
            return [Edit("fb.resume.sent")]
        if outcome is ResumeOutcome.PENDING:
            return [Edit("fb.resume.pending", {"id": block_id})]
        if outcome is ResumeOutcome.NO_HOLDER:
            return [Edit("fb.resume.no_holder")]
        if outcome is ResumeOutcome.STALE or current is None:
            return [Edit("fb.resume.stale")]
        verdict = resume_verdict(current, now=int(clock().timestamp()), lock_lifted=lifted)
        refusal = _refusal(ctx, verdict, current.id, current.until)
        return [Edit(refusal.key_or_text, refusal.params, literal=refusal.literal)]


def _lease_command(router: Router, *, monitor: CollectorMonitor, desk: ControlDesk) -> None:
    @router.command("lease", owner_only=True)
    async def lease(ctx: Context) -> list[Action]:
        if refused := _owner_only(ctx):
            return refused
        args = ctx.incoming.args.split()
        if not args:
            state = await desk.lease()
            view = await monitor.view()
            seen = [r.collector_id for r in (view.holder, *view.others) if r is not None]
            text = ctx.t(
                "fb.lease.show",
                holder=state.holder or ctx.t("fb.lease.nobody"),
                ids=", ".join(seen) or "—",
            )
            if state.moving_to is not None:
                moving = ctx.t("fb.lease.moving", id=state.moving_to, old=state.holder or "?")
                text = f"{text}\n{moving}"
            return [Reply(text, literal=True)]
        wanted = args[0][:64]
        at_once = len(args) > 1 and args[1].lower() == "now"
        outcome, old = await desk.transfer_lease(wanted, now=at_once)
        nobody = ctx.t("fb.lease.nobody")
        if outcome is LeaseOutcome.SAME:
            return [Reply(ctx.t("fb.lease.same", id=wanted), literal=True)]
        if outcome is LeaseOutcome.UNKNOWN:
            return [Reply(ctx.t("fb.lease.unknown", id=wanted), literal=True)]
        if outcome is LeaseOutcome.CANCELLED:
            return [Reply(ctx.t("fb.lease.cancelled", id=wanted), literal=True)]
        if outcome is LeaseOutcome.REVOKING:
            text = ctx.t("fb.lease.revoking", id=wanted, old=old or nobody)
            return [Reply(text, literal=True)]
        if outcome in (LeaseOutcome.REPLACING, LeaseOutcome.REPLACED):
            # The same name, another state database: a machine replaced, or its state recreated.
            return [Reply(ctx.t(f"fb.lease.{outcome.value}", id=wanted), literal=True)]
        return [Reply(ctx.t("fb.lease.moved", id=wanted, old=old or nobody), literal=True)]


def _refusal(ctx: Context, verdict: ResumeVerdict, block_id: int, until: int | None) -> Reply:
    if verdict is ResumeVerdict.TOO_EARLY and until is not None:
        moment = format_local(dt.datetime.fromtimestamp(until, dt.UTC), _tz(ctx))
        return Reply(ctx.t("fb.resume.too_early", id=block_id, until=moment), literal=True)
    if verdict is ResumeVerdict.FINAL:
        return Reply(ctx.t("fb.resume.final", id=block_id), literal=True)
    if verdict is ResumeVerdict.NO_PROBE:
        return Reply(ctx.t("fb.resume.no_probe", id=block_id), literal=True)
    return Reply("fb.resume.nothing")


def _tz(ctx: Context) -> str:
    return ctx.user.tz or "UTC"


def _cap_name(ctx: Context, cap: Profile | None) -> str:
    return ctx.t(f"fb.profile.{cap.value}") if cap is not None else ctx.t("fb.pace.none")


def _int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if 0 < value <= SQLITE_INT_MAX else 0


def _names(groups: Sequence[FbGroup]) -> str:
    titles = [g.title for g in groups[:MAX_LISTED]]
    if len(groups) > MAX_LISTED:
        titles.append(f"+{len(groups) - MAX_LISTED}")
    return ", ".join(titles)


async def write_status(
    ctx: Context,
    view: CollectorView,
    *,
    storage: Storage,
    counters: StatusSources,
    pace_cap: Profile | None,
    now: dt.datetime,
) -> str:
    """The whole /status body, in the reader's language; the owner sees the machinery too."""
    owner = ctx.is_owner
    lines = [ctx.t("fb.status.title"), ""]
    lines.extend(_collector_lines(ctx, view, pace_cap, now))

    by_id = {g.chat_id: g for g in view.groups}
    read = [by_id[gid] for gid in view.monitored if gid in by_id]
    if not owner:
        mine = {c.id for c in await storage.list_subscriptions(ctx.user_id)}
        read = [g for g in read if g.chat_id in mine]
        lines.append(ctx.t("fb.status.your_groups", n=len(mine)))
        over = [by_id[gid] for gid in view.over_cap if gid in mine and gid in by_id]
        if over:
            lines.append(ctx.t("fb.status.your_over_cap", cap=MAX_MONITORED, groups=_names(over)))
    lines.extend(_delay_lines(ctx, view, read, pace_cap))

    if owner:
        lines.append("")
        lines.extend(_group_lines(ctx, view, by_id))
        lines.append("")
        results = counters.source.results
        lines.append(
            ctx.t(
                "fb.status.items",
                stored=results.get(ItemOutcome.STORED, 0),
                duplicates=results.get(ItemOutcome.DUPLICATE, 0),
                not_monitored=results.get(ItemOutcome.NOT_MONITORED, 0),
                rejected=results.get(ItemOutcome.REJECTED, 0),
            )
        )
        delivery = counters.delivery
        if delivery is not None:
            m = delivery.metrics
            lines.append(
                ctx.t(
                    "fb.status.delivery",
                    delivered=m.delivered,
                    digested=m.digested,
                    failed=m.failed,
                    queue=delivery.total_queued(),
                )
            )
        lines.append(ctx.t("fb.status.uptime", uptime=format_uptime(counters.started_at, now)))
        lines.append(ctx.t("fb.status.owner_help"))
    lines.append("")
    lines.append(ctx.t("bot.status.lifetime", hits=await storage.total_rule_hits(ctx.user_id)))
    return "\n".join(lines)


def _collector_lines(
    ctx: Context, view: CollectorView, pace_cap: Profile | None, now: dt.datetime
) -> list[str]:
    lines: list[str] = []
    status = view.status
    if view.holder is None or view.state is None or status is None:
        lines.append(ctx.t("fb.status.never"))
    elif not ctx.is_owner:
        lines.append(ctx.t("fb.status.collector", state=ctx.t(f"fb.state.{view.state.value}")))
    else:
        lines.extend(
            _holder_lines(
                ctx, holder=view.holder, state=view.state, status=status, pace_cap=pace_cap, now=now
            )
        )
    if view.paused:
        lines.append(ctx.t("fb.status.paused" if ctx.is_owner else "fb.status.paused_user"))
    if ctx.is_owner and pace_cap is not None:
        lines.append(ctx.t("fb.status.pace_cap", profile=ctx.t(f"fb.profile.{pace_cap.value}")))
    if ctx.is_owner and view.others:
        lines.append(ctx.t("fb.status.others", ids=", ".join(r.collector_id for r in view.others)))
    return lines


def _holder_lines(
    ctx: Context,
    *,
    holder: CollectorRow,
    state: CollectorState,
    status: Status,
    pace_cap: Profile | None,
    now: dt.datetime,
) -> list[str]:
    """The owner's view of the lease holder: state, last report, block and budget."""
    lines = [
        ctx.t(
            "fb.status.collector_owner",
            id=holder.collector_id,
            state=ctx.t(f"fb.state.{state.value}"),
        )
    ]
    reported = dt.datetime.fromtimestamp(holder.last_status_at or 0, dt.UTC)
    profile = Profile.most_cautious(status.profile, pace_cap)
    lines.append(
        ctx.t(
            "fb.status.report",
            ago=relative(reported, now, ctx.lang),
            profile=ctx.t(f"fb.profile.{profile.value}"),
        )
    )
    if status.detail:
        lines.append(ctx.t("fb.status.detail", detail=status.detail))
    block = status.block
    if block is not None and block.until is None:
        lines.append(ctx.t("fb.status.block_final", id=block.id, reason=block.reason))
    elif block is not None and block.until is not None:
        until = format_local(dt.datetime.fromtimestamp(block.until, dt.UTC), _tz(ctx))
        lines.append(ctx.t("fb.status.block", id=block.id, reason=block.reason, until=until))
    if status.budget:
        items = "; ".join(
            ctx.t(
                "fb.status.budget_item",
                kind=ctx.t(f"fb.budget.{w.kind.value}"),
                remaining=w.remaining,
                cap=w.cap,
                window=format_duration(w.window_seconds, ctx.lang),
            )
            for w in status.budget
        )
        lines.append(ctx.t("fb.status.budget", items=items))
    return lines


def _delay_lines(
    ctx: Context, view: CollectorView, read: list[FbGroup], pace_cap: Profile | None
) -> list[str]:
    status = view.status
    if view.paused or status is None or view.state is None:
        return [ctx.t("fb.status.delay_stopped")]
    if view.state in PAUSE_STATES:
        state = ctx.t(f"fb.state.{view.state.value}")
        if status.resumes_at is None:
            return [ctx.t("fb.status.delay_idle_soon", state=state)]
        at = format_local(dt.datetime.fromtimestamp(status.resumes_at, dt.UTC), _tz(ctx))
        return [ctx.t("fb.status.delay_idle", state=state, time=at)]
    if view.state not in DELAY_STATES:
        return [ctx.t("fb.status.delay_stopped")]
    profile = Profile.most_cautious(status.profile, pace_cap)
    counters = status.counters
    backstop = format_duration(
        backstop_delay(profile, len(view.monitored), counters).total_seconds(), ctx.lang, bound=True
    )
    if view.state is CollectorState.FEED_NOT_CHRONO:
        return [ctx.t("fb.status.delay_feed_off", backstop=backstop)]
    feed = format_duration(feed_delay(profile, counters).total_seconds(), ctx.lang, bound=True)
    lines = [ctx.t("fb.status.delay", feed=feed)]
    if FEED_COUNTER not in counters:
        # Without the collector's own current interval, the slower shoulder cadence around
        # quiet hours has to be promised too: the figure above is not a bound then.
        shoulder = format_duration(shoulder_delay().total_seconds(), ctx.lang, bound=True)
        lines.append(ctx.t("fb.status.delay_shoulder", shoulder=shoulder))
    slow = [g for g in read if g.followed is False]
    if slow:
        lines.append(ctx.t("fb.status.delay_backstop", backstop=backstop, groups=_names(slow)))
    return lines


def _group_lines(ctx: Context, view: CollectorView, by_id: dict[int, FbGroup]) -> list[str]:
    groups = view.groups
    unavailable = [g for g in groups if not g.available]
    lines = [
        ctx.t(
            "fb.status.groups",
            monitored=len(view.monitored),
            known=len(groups),
            unavailable=len(unavailable),
        )
    ]
    sections = (
        ("fb.status.lost", [g for g in groups if g.state is GroupState.LOST]),
        ("fb.status.unverified", [g for g in groups if g.state is GroupState.UNVERIFIED]),
        ("fb.status.not_followed", [g for g in groups if g.available and g.followed is False]),
        ("fb.status.sort_ignored", [g for g in groups if g.available and g.sort_ignored]),
    )
    lines.extend(ctx.t(key, groups=_names(found)) for key, found in sections if found)
    if view.over_cap:
        over = [by_id[gid] for gid in view.over_cap if gid in by_id]
        lines.append(ctx.t("fb.status.over_cap", cap=MAX_MONITORED, groups=_names(over)))
    return lines
