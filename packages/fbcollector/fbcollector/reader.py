"""One granted read, step by step: the part of the read loop between two scheduler decisions.

The scheduler has already checked the gates and written the navigation's budget event to disk
when a ``Grant`` arrives here. From then on every step follows the same order (DESIGN, process
model): driver step → raw payload to disk → parser → **detectors** → ladder → store → stop rule →
the next step's own gate check and write-ahead budget event (``scroll_permit``) — or the final
look and ``park``.

What each verdict does:

- ``OK``: the step's stories are stored (``seen`` + sighting + outbox in one transaction, or
  deferred), group metadata refreshed, and the stop rule decides whether to scroll on. A story
  whose item does not fit the wire model is quarantined, not seen (retried on its next sighting
  and by ``replay``), and the owner is told once while any wait (``ITEMS_INVALID``). An unread
  login or password notice in Facebook's notifications panel (``detectors.panel_heads_up_items``)
  is told to the owner once per notice, and the read goes on. A notice is its words and its
  arrival (the look's time less the age it shows), so an old one is told once, not daily, and
  a new login in Facebook's same generic words is a notice of its own; a notice is recorded as
  told only once the bot delivered its alert (``_panel_notices``).
- ``NOT_FOLLOWED`` / ``SORT_IGNORED``: flagged on the group, the owner told once, reading goes on.
  DESIGN's second ``NOT_FOLLOWED`` trigger is checked after every backstop: a group that has not
  appeared in the Groups feed for 48 h (with the feed read all that time) while backstops find
  its posts is flagged ``feed_absent`` (followed but possibly snoozed) and the owner told; the
  flag is lifted when the feed shows the group again, and the report leaves such a group out of
  ``feed_miss``.
- ``FEED_NOT_CHRONO``: backstop-only mode (kv ``feed_not_chrono``), the owner told; the read ends.
- ``GROUP_LOST``: the group is paused as ``lost``; the owner told; the read ends. Two within 24 h
  are a block (the ladder's rule). A group that is a member on the owner's word alone
  (``fbcollector.confirm``) must also *say* ``MEMBER`` on its page — silence is lost too — and
  losing it withdraws that word; the first page that says ``MEMBER`` is noted.
- ``UNKNOWN``, ``BLOCKED``, ``LOGGED_OUT``: nothing from the step is stored, the ladder writes its
  block or backoff *first*, then a screenshot is taken and the loop closes Chrome. A driver
  failure mid-read (a crashed renderer, a closed target) is an ``UNKNOWN`` page, fail closed —
  unless Chrome is being closed under the read (``abort``: the machine is going to sleep, or
  the owner or the brain stopped reading): then nothing is judged at all.

Right before the navigation, after the read's own preparation, the scheduler confirms that the
control state is still the one the grant was made under (``still_granted``); a pause, a lease
change or another pace cap in between discards the grant.

The resume probe is a feed read with no scrolling: its verdict is taken after the ≥ 15 s dwell
(late dialogs), and the ladder closes the block only on that final, clean look. A feed that is
not chronological is a clean probe too (no warning): the block is lifted and backstop-only mode
applies as on any read.

After the read: coverage and health go into ``reads``; a capped read that still saw unseen
stories newer than its target queues gap backstops; the parser's health over the last three
reads can pause everything (``PARSER_BROKEN``); a backstop refreshes the group's
``no_posts_7d`` flag; a sync applies the joined-groups rules.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import hashlib
import json
import logging
import random
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from pydantic import ValidationError

from fbcollector.clock import Clock
from fbcollector.confirm import FLAG_CONFIRMED, note_verified, withdraw
from fbcollector.coverage import CoverageTracker, backstop_target, feed_target
from fbcollector.deadline import abandon, run_within
from fbcollector.detectors import (
    DetectorInput,
    FeedPreloader,
    PageFacts,
    StepKind,
    Verdict,
    age_seconds,
    age_unit,
    classify,
    panel_heads_up_items,
    panel_read_items,
    redact_url,
)
from fbcollector.driver import Driver
from fbcollector.driver.policy import UrlNotAllowedError
from fbcollector.events import outbox_edge, quarantine_edge
from fbcollector.ladder import PROBE_CLEAN, Ladder, Outcome
from fbcollector.model import (
    Coverage,
    DeferredReason,
    GroupMeta,
    PageState,
    ParsedStory,
    ReadOutcome,
    Sighting,
    Step,
    Surface,
)
from fbcollector.parser import ParseHealth, PostMark, ReadParser, StepParse, parser_verdict
from fbcollector.raw import save_quarantine, save_step
from fbcollector.scheduler import (
    FLAG_NO_RECENT_POSTS,
    FLAG_PAUSED,
    KV_BACKSTOP_ONLY,
    ActionKind,
    Grant,
    Hold,
    gap_targets,
)
from fbcollector.store import INVALID, GroupRow, Store
from fbcollector.sync import SyncOutcome, apply_sync
from fbwire.models import EventKind, GroupState, Item, Via

log = logging.getLogger(__name__)

DAY: Final = 86400.0
NOTICE_ONCE: Final = 365 * DAY
"""The alert bot's dedup of a panel notice's key. The key holds the notice's arrival, so it is
unique anyway: what tells a notice once is the record of told arrivals (``KV_PANEL_TOLD``)."""
KV_PANEL_TOLD: Final = "panel_told:"
"""Prefix of the kv row, per notice's words (``panel_notice_digest``), that holds the notices
of those words already told, as a JSON list of ``[arrival, told_at, read]``: epoch seconds,
and 1 once the panel showed the notice read (the owner, 2026-10-01: an old notice they cannot
find in the app must not come back every day, and a new login must always be told)."""
PANEL_TOLD_KEEP: Final = 20
"""The told notices kept per words, the latest told; and none told more than
``PANEL_TOLD_DAYS`` ago. The age is the told time, not the arrival: an unread item may show
"1y", and a told arrival dropped for its age would be told again at the next look."""
PANEL_TOLD_DAYS: Final = 400 * DAY
FINAL_DWELL: Final = (2.0, 5.0)
FLAG_NOT_FOLLOWED: Final = "not_followed"
FLAG_FEED_ABSENT: Final = "feed_absent"
"""Followed, yet missing from the Groups feed for 48 h while backstops find posts (snoozed?)."""
FEED_ABSENT_AFTER: Final = 48 * 3600.0
FEED_GRACE: Final = 3600.0
"""A post younger than this may simply not have reached the feed yet."""
FLAG_SORT_IGNORED: Final = "sort_ignored"
KV_PARSER_HISTORY: Final = "parser_history"
KV_PARSER_BROKEN: Final = "parser_broken"
PARSER_HISTORY: Final = 10
ELLIPSES: Final = ("…", "...")
SCREENSHOT_STATES: Final = frozenset({PageState.BLOCKED, PageState.LOGGED_OUT, PageState.UNKNOWN})
READ_DEADLINE: Final = 8 * 60.0
"""A read's time without its scroll steps: the navigation (a 45 s load, the wait for stories),
the final look and the checks around them take about two minutes at worst."""
STEP_DEADLINE: Final = 2 * 60.0
"""Each allowed scroll step's share: a wheel gesture, 28 s for pagination at most, a DOM read."""
CANCEL_GRACE: Final = 5.0
"""How long a hung read is given to unwind once cancelled, before it is left behind."""
STOP_GRACE: Final = 20.0
"""How long a shutdown waits for the step under way before it cuts the read: launchd allows the
whole stop 90 s (``ExitTimeOut``), and closing Chrome may take 40 s of it."""
PARK_DEADLINE: Final = 120.0
"""Parking the tab after a read: a 20 s wait for bodies in flight and a load of about:blank."""


class ReadScheduler(Protocol):
    """The scheduler's door for the steps inside a read (``Scheduler`` is one)."""

    async def scroll_permit(
        self, grant: Grant, *, steps_done: int, read_id: int | None
    ) -> Hold | None: ...

    async def read_finished(
        self, grant: Grant, *, outcome: ReadOutcome, gap_groups: Sequence[str] = ()
    ) -> None: ...

    async def recheck(self, grant: Grant) -> Hold | None: ...

    async def still_granted(self, grant: Grant) -> Hold | None: ...


class EventSink(Protocol):
    async def __call__(
        self,
        kind: EventKind,
        *,
        block_id: int | None = None,
        detail: str | None = None,
        group_id: str | None = None,
        group_name: str | None = None,
    ) -> None: ...

    async def notify(
        self,
        key: str,
        text: str,
        *,
        dedup: float | None = None,
        on_sent: Callable[[], Awaitable[None]] | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ReaderConfig:
    collector_id: str
    raw_dir: Path
    shots_dir: Path
    novelty_days: float = 7
    late_after_hours: float = 2
    outbox_cap: int = 5000
    final_dwell: tuple[float, float] = FINAL_DWELL
    deadline: float = READ_DEADLINE
    step_deadline: float = STEP_DEADLINE
    cancel_grace: float = CANCEL_GRACE
    stop_grace: float = STOP_GRACE
    park_deadline: float = PARK_DEADLINE

    @property
    def novelty(self) -> float:
        return self.novelty_days * DAY

    def read_deadline(self, steps: int, min_dwell: float) -> float:
        """Seconds a read with ``steps`` scroll steps allowed may take before it counts as hung."""
        return self.deadline + steps * self.step_deadline + min_dwell


@dataclass(frozen=True, slots=True)
class ReadResult:
    read_id: int | None
    outcome: ReadOutcome
    page_state: PageState | None = None
    close_chrome: bool = False
    reason: str = ""
    new_items: int = 0
    sync: SyncOutcome | None = None
    planned_close: bool = False
    """``close_chrome`` for a hold or a stop, not a failure: a gate closed mid-read, the grant's
    terms changed, the machine is going to sleep. Every other close — a page the detectors did
    not pass, a browser failure — is unplanned, and so is any close nobody classified: the
    crash-loop gate counts the relaunch after it (fail closed)."""


@dataclass
class _Run:
    """Everything one read carries between its steps."""

    grant: Grant
    read_id: int
    parser: ReadParser
    tracker: CoverageTracker | None
    groups: dict[str, GroupRow]
    via: Via
    started: float
    steps: int = 0
    index: int = 0
    capped: bool = False
    stalled: bool = False
    """The last step was a scroll that loaded nothing new."""
    verdict: Verdict | None = None
    pending: list[tuple[StepParse, str | None]] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)
    stale: Counter[str] = field(default_factory=Counter)
    group_paths: dict[str, list[int]] = field(default_factory=dict)
    latencies: list[int] = field(default_factory=list)
    verdicts: list[str] = field(default_factory=list)
    fired: int = 0
    scrolls: int = 0
    lost_bodies: int = 0
    see_more: int = 0
    posinset: int = 0
    texts: Counter[str] = field(default_factory=Counter)
    joined_sizes: list[int] = field(default_factory=list)
    header: int | None = None
    """N of "All groups you've joined (N)", from the page (joins surface)."""
    meta_saved: bool = False
    feed_absent: set[str] = field(default_factory=set)
    """Groups flagged absent from the feed (a feed sighting lifts the flag)."""
    abort: asyncio.Event | None = None
    """Set when Chrome is being closed under the read (an ``Abort`` says why)."""
    held_dialogs: list[tuple[str, float]] = field(default_factory=list)
    """Dialog texts of a probe's steps, with the time each was read, whose panel notices wait
    for its final look."""
    told_now: dict[str, list[int]] = field(default_factory=dict)
    """Arrivals of the panel notices told in this read, per words: their alerts are held
    until Chrome is closed, and recorded only once delivered."""

    def aborted(self) -> bool:
        return self.abort is not None and self.abort.is_set()

    @property
    def action_kind(self) -> ActionKind:
        return self.grant.action.kind

    @property
    def surface(self) -> Surface:
        return self.grant.action.surface

    @property
    def group_id(self) -> str | None:
        return self.grant.action.group_id


SLEEP_ABORT: Final = "the machine is going to sleep"


class Abort(asyncio.Event):
    """Set when Chrome is being closed under a read, with why: the machine is going to sleep
    (the default reason), or reading was stopped — the owner's or the brain's pause, the lease
    gone. Whatever a step brings back after it is set is not judged."""

    def __init__(self) -> None:
        super().__init__()
        self.reason = SLEEP_ABORT

    def stop(self, reason: str) -> None:
        """Set it, with ``reason``; the first reason stays."""
        if not self.is_set():
            self.reason = reason
            self.set()


class _Stop(Exception):  # noqa: N818 - control flow inside the read, not an error
    def __init__(self, result: ReadResult) -> None:
        self.result = result


class ReadHung(Exception):  # noqa: N818 - named for what the read did
    """A read ran past its deadline: some browser call never answered."""


def await_chain(coro: Any) -> list[str]:
    """Where a suspended coroutine waits: ``file:line function`` of every coroutine in its
    ``await`` chain, outermost first (the chain ends at the future nobody resolved)."""
    where: list[str] = []
    while coro is not None and len(where) < 50:
        frame = getattr(coro, "cr_frame", None) or getattr(coro, "ag_frame", None)
        frame = frame or getattr(coro, "gi_frame", None)
        if frame is not None:
            code = frame.f_code
            where.append(f"{Path(code.co_filename).name}:{frame.f_lineno} {code.co_name}")
        coro = (
            getattr(coro, "cr_await", None)
            or getattr(coro, "ag_await", None)
            or getattr(coro, "gi_yieldfrom", None)
        )
    return where


class Reader:
    def __init__(
        self,
        *,
        store: Store,
        driver: Driver,
        scheduler: ReadScheduler,
        ladder: Ladder,
        events: EventSink,
        clock: Clock,
        config: ReaderConfig,
        rng: random.Random | None = None,
        resources: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._store = store
        self._driver = driver
        self._scheduler = scheduler
        self._ladder = ladder
        self._events = events
        self._clock = clock
        self._config = config
        self._rng = rng or random.Random()
        self._resources = resources

    # -- the read ------------------------------------------------------------------------------

    async def read(
        self,
        grant: Grant,
        *,
        stop: asyncio.Event | None = None,
        abort: asyncio.Event | None = None,
    ) -> ReadResult:
        """One read. ``stop`` (shutdown) ends it between steps; ``abort`` (Chrome is being
        closed under it: the machine is going to sleep, or reading was stopped) ends it too,
        and whatever a step brings back after it is set is not judged at all: a tab torn down
        mid-step is not a page. Nor is a step whose cookies were read after it was set — even a
        step that came back whole before: a closing Chrome has no cookies to show, and "no
        ``c_user``" would be judged a logout or an unrecognised page."""
        run = await self._begin(grant)
        run.abort = abort
        try:
            result = await self._bounded(run, stop)
        except _Stop as stopped:
            result = stopped.result
        except UrlNotAllowedError:
            # A bug upstream asked for a URL outside the allow-list; nothing was sent.
            log.error("read %d refused by the URL allow-list", run.read_id)  # noqa: TRY400
            result = ReadResult(run.read_id, ReadOutcome.FAILED, reason="url_not_allowed")
        except Exception as exc:  # noqa: BLE001 - a browser failure is an unrecognised page
            if run.aborted():
                result = _sleep_abort(run)
            else:
                result = await self._driver_failure(run, exc)
        if not result.close_chrome:
            try:
                await run_within(self._driver.park(), self._config.park_deadline)
            except TimeoutError:
                log.warning("read %d: parking the tab did not end; Chrome is closed", run.read_id)
                result = dataclasses.replace(
                    result, close_chrome=True, planned_close=False, reason="parking hung"
                )
            except Exception:  # noqa: BLE001, S110 - a failed park leaves the page as it was
                pass
        return await self._finish(run, result)

    async def _bounded(self, run: _Run, stop: asyncio.Event | None) -> ReadResult:
        """``_steps`` under a deadline. No browser call has a timeout of its own (a CDP command,
        a wheel event): one Chrome never answered held the read, the loop and even the shutdown
        (a stop is looked at between steps) for almost eight hours on 2026-10-07. Past the
        deadline the stuck call's await chain is logged, the steps are cancelled and the read
        fails like any browser failure: Chrome is closed under it.

        A shutdown gives the step under way ``stop_grace`` to end; then the read is cut and
        Chrome closed, a planned close and no verdict: a slow step is no strike on the ladder.
        """
        action = run.grant.action
        limit = self._config.read_deadline(action.scroll_allowance, action.min_dwell)
        loop = asyncio.get_running_loop()
        ends = loop.time() + limit
        task = asyncio.create_task(self._steps(run, stop), name=f"read-{run.read_id}")
        stopped = asyncio.ensure_future(stop.wait()) if stop is not None else None
        try:
            waits = {task} if stopped is None else {task, stopped}
            await asyncio.wait(waits, timeout=limit, return_when=asyncio.FIRST_COMPLETED)
            if not task.done() and stopped is not None and stopped.done():
                grace = min(self._config.stop_grace, max(0.0, ends - loop.time()))
                await asyncio.wait({task}, timeout=grace)
                if not task.done():
                    log.warning(
                        "read %d: cut by the shutdown in the middle of a step, waiting at: %s",
                        run.read_id,
                        " > ".join(await_chain(task.get_coro())) or "unknown",
                    )
                    await abandon(task, self._config.cancel_grace)
                    return ReadResult(
                        run.read_id,
                        ReadOutcome.ABORTED,
                        close_chrome=True,
                        reason="shutting down",
                        planned_close=True,
                    )
        except asyncio.CancelledError:
            await abandon(task, self._config.cancel_grace)
            raise
        finally:
            if stopped is not None:
                stopped.cancel()
        if task.done():
            return task.result()
        log.error(
            "read %d hung for %.0f s, waiting at: %s",
            run.read_id,
            limit,
            " > ".join(await_chain(task.get_coro())) or "unknown",
        )
        if not await abandon(task, self._config.cancel_grace):
            log.warning("read %d did not unwind when cancelled; left behind", run.read_id)
        msg = f"no answer from the browser in {limit:.0f} s"
        raise ReadHung(msg)

    async def _begin(self, grant: Grant) -> _Run:
        action = grant.action
        now = self._clock.time()
        rows = {g.id: g for g in await self._store.list_groups()}
        slugs = {g.slug: g.id for g in rows.values() if g.slug}
        tracker: CoverageTracker | None = None
        target: float | None = None
        if action.surface is Surface.FEED:
            if action.deep:
                last = await self._store.last_read(Surface.FEED, target_reached=True)
            else:
                last = await self._store.last_covering_read(Surface.FEED)
            baselines = [g.baseline_at for g in rows.values() if g.monitored]
            target, bootstrap = feed_target(
                last_covered_start=last.started_at if last else None,
                baselines=baselines,
                now=now,
            )
            target = max(target, now - self._config.novelty)
            tracker = CoverageTracker(Surface.FEED, target, now, bootstrap=bootstrap)
        elif action.surface is Surface.GROUP and action.group_id is not None:
            last = await self._store.last_read(
                Surface.GROUP, group_id=action.group_id, target_reached=True
            )
            group = rows.get(action.group_id)
            target = backstop_target(
                last_covered_start=last.started_at if last else None,
                baseline=group.baseline_at if group else now,
                now=now,
                novelty=self._config.novelty,
            )
            tracker = CoverageTracker(Surface.GROUP, target, now)
        read_id = await self._store.start_read(
            action.surface, group_id=action.group_id, target=target
        )
        via = Via.FEED
        if action.surface is Surface.GROUP:
            group = rows.get(action.group_id or "")
            via = Via.BACKSTOP_LISTINGS if group is not None and group.listings else Via.BACKSTOP
        parser = ReadParser(action.surface, group_id=action.group_id, slugs=slugs)
        run = _Run(grant, read_id, parser, tracker, rows, via, now)
        run.feed_absent = {g.id for g in rows.values() if FLAG_FEED_ABSENT in g.flags}
        return run

    async def _steps(self, run: _Run, stop: asyncio.Event | None) -> ReadResult:
        action = run.grant.action
        # The grant was made before the read prepared itself: a pause, a lease change or a
        # stricter pace cap that landed since discards it, before anything is sent.
        hold = await self._scheduler.still_granted(run.grant)
        if hold is not None:
            reason = "; ".join(hold.reasons) or "the control state changed"
            log.warning("read %d discarded before its navigation: %s", run.read_id, reason)
            return ReadResult(
                run.read_id,
                ReadOutcome.ABORTED,
                close_chrome=hold.close_chrome,
                reason=reason,
                planned_close=True,
            )
        if run.aborted():
            return _sleep_abort(run)
        step = await self._driver.navigate(action.url)
        if run.aborted():
            return _sleep_abort(run)
        await self._handle(run, step, StepKind.NAVIGATE)
        while not self._done(run):
            if run.aborted():
                return _sleep_abort(run)
            if stop is not None and stop.is_set():
                return ReadResult(run.read_id, ReadOutcome.ABORTED, reason="shutting down")
            if run.steps >= action.scroll_allowance:
                run.capped = True
                break
            hold = await self._scheduler.scroll_permit(
                run.grant, steps_done=run.steps, read_id=run.read_id
            )
            if hold is not None:
                if hold.close_chrome:
                    reason = "; ".join(hold.reasons) or "a gate closed"
                    log.warning("read %d aborted mid-read: %s", run.read_id, reason)
                    return ReadResult(
                        run.read_id,
                        ReadOutcome.ABORTED,
                        close_chrome=True,
                        reason=reason,
                        planned_close=True,
                    )
                run.capped = True
                break
            step = await self._driver.scroll_step()
            if run.aborted():
                return _sleep_abort(run)
            run.steps += 1
            await self._handle(run, step, StepKind.SCROLL)
        # The final look stays on the page for seconds more: a read whose steps ran out right
        # after a sleep or a network change must not take it without the gates.
        hold = await self._scheduler.recheck(run.grant)
        if hold is not None:
            reason = "; ".join(hold.reasons) or "a gate closed"
            log.warning("read %d aborted before its final look: %s", run.read_id, reason)
            return ReadResult(
                run.read_id,
                ReadOutcome.ABORTED,
                close_chrome=True,
                reason=reason,
                planned_close=True,
            )
        low, high = self._config.final_dwell
        dwell = max(action.min_dwell, self._rng.uniform(low, high))
        step = await self._driver.dwell(dwell)
        if run.aborted():
            return _sleep_abort(run)
        await self._handle(run, step, StepKind.DWELL)
        return ReadResult(run.read_id, self._outcome(run))

    def _done(self, run: _Run) -> bool:
        if run.action_kind is ActionKind.SYNC:
            info = run.parser.page_info
            if info is not None and info.has_next_page is False:
                return True
            sizes = run.joined_sizes
            return len(sizes) >= 2 and sizes[-1] <= sizes[-2]
        return run.tracker is not None and run.tracker.reached

    def _outcome(self, run: _Run) -> ReadOutcome:
        """A read that ran out of steps short of its target has a gap — unless the list ended.

        Unseen stories newer than the target in its last batch are a gap; so is coverage that
        never got below the target (the last batch all seen already, or undated), except when
        the last scroll step loaded nothing (the group's list ended, or pagination stalled:
        DESIGN, ``pagination_fired=False`` is its own outcome, never a gap).
        """
        tracker = run.tracker
        if tracker is None or tracker.reached or not run.capped:
            return ReadOutcome.COMPLETE
        if tracker.gap:
            return ReadOutcome.GAP
        if run.stalled:
            return ReadOutcome.COMPLETE
        return ReadOutcome.GAP if tracker.short_of_target() else ReadOutcome.COMPLETE

    # -- one step ------------------------------------------------------------------------------

    async def _handle(self, run: _Run, step: Step, kind: StepKind) -> None:
        index = run.index
        run.index += 1
        parsed = run.parser.parse(step)
        raw_path = await self._save_raw(run, step, index, parsed)
        self._note_step(run, step, kind, parsed)
        cookies = await self._driver.cookie_names()
        if run.aborted():
            # The OS announced a sleep while this step was saved or its cookies read: Chrome
            # is being closed, and a closing Chrome has no cookies to show. Nothing is judged.
            raise _Stop(_sleep_abort(run))
        verdict = classify(
            DetectorInput(
                surface=run.surface,
                step=step,
                kind=kind,
                group_id=run.group_id,
                group_slug=self._slug(run),
                listings=run.via is Via.BACKSTOP_LISTINGS,
                cookies=cookies,
                facts=page_facts(run.parser),
                needs_member_proof=_needs_member_proof(run),
            )
        )
        run.verdict = verdict
        run.verdicts.append(verdict.state.value)
        probe = run.action_kind is ActionKind.PROBE
        if probe and kind is not StepKind.DWELL and verdict.state in PROBE_CLEAN:
            run.pending.append((parsed, raw_path))  # stored once the final look is clean
            now = self._clock.time()  # told then too: "reading goes on"
            run.held_dialogs += [(text, now) for text in step.dom.dialog_texts]
            return
        shot = self._shot_path(run, verdict)
        outcome = await self._ladder.on_verdict(
            verdict,
            probe_block_id=run.grant.action.block_id if probe else None,
            screenshot=str(shot) if shot else None,
        )
        if shot is not None:
            await self._driver.screenshot(shot)  # after the block is on disk (invariant 3)
        match outcome.outcome:
            case Outcome.CONTINUE | Outcome.RESUMED:
                await self._panel_notices(run, step)
                await self._group_meta(run, verdict)
                for earlier, path in run.pending:
                    await self._store_stories(run, earlier.stories, path)
                run.pending.clear()
                if verdict.state is PageState.OK:
                    await self._store_stories(run, parsed.stories, raw_path)
                else:  # a clean probe on a flagged page: lifted, and the flag still applies
                    await self._flag(run, verdict, parsed, raw_path)
            case Outcome.FLAG:
                await self._panel_notices(run, step)
                await self._flag(run, verdict, parsed, raw_path)
            case Outcome.GROUP_PAUSED:
                await self._group_lost(run, verdict)
                raise _Stop(
                    ReadResult(run.read_id, ReadOutcome.FAILED, PageState.GROUP_LOST, reason="lost")
                )
            case Outcome.BACKOFF | Outcome.BLOCKED | Outcome.IGNORED:
                if verdict.state is PageState.GROUP_LOST:
                    await self._mark_lost(run)
                raise _Stop(
                    ReadResult(
                        run.read_id,
                        ReadOutcome.FAILED,
                        verdict.state,
                        close_chrome=True,
                        reason=outcome.outcome.value,
                    )
                )

    def _slug(self, run: _Run) -> str | None:
        group = run.groups.get(run.group_id or "")
        if group is not None and group.slug:
            return group.slug
        meta = run.parser.group
        return meta.slug if meta is not None else None

    async def _save_raw(self, run: _Run, step: Step, index: int, parsed: StepParse) -> str | None:
        now = self._clock.time()
        try:
            path = await asyncio.to_thread(
                save_step,
                self._config.raw_dir,
                read_id=run.read_id,
                index=index,
                surface=run.surface,
                group_id=run.group_id,
                at=now,
                step=step,
            )
            for n, quarantined in enumerate(parsed.quarantined):
                await asyncio.to_thread(
                    save_quarantine,
                    self._config.raw_dir,
                    read_id=run.read_id,
                    index=index * 100 + n,
                    at=now,
                    post_id=quarantined.post_id,
                    raw=quarantined.raw,
                    surface=run.surface,
                    group_id=run.group_id,
                )
        except OSError as exc:
            log.warning("raw payload not saved: %s", type(exc).__name__)
            return None
        return str(path)

    def _note_step(self, run: _Run, step: Step, kind: StepKind, parsed: StepParse) -> None:
        if step.latency_ms is not None:
            run.latencies.append(step.latency_ms)
        if kind is StepKind.SCROLL:
            run.scrolls += 1
            run.fired += step.pagination_fired
            run.stalled = not step.pagination_fired
        run.lost_bodies += step.lost_bodies
        run.see_more = max(run.see_more, step.dom.see_more)
        run.posinset = max(run.posinset, step.dom.posinset_count)
        if step.dom.joined_header_count is not None:
            run.header = step.dom.joined_header_count
        if run.parser.joined is not None:
            run.joined_sizes.append(len(run.parser.joined.groups))
        for story in parsed.stories:
            for path, gid in story.group_candidates:
                entry = run.group_paths.setdefault(path, [0, 0])
                entry[0] += 1
                entry[1] += gid == story.group_id

    def _shot_path(self, run: _Run, verdict: Verdict) -> Path | None:
        if verdict.state not in SCREENSHOT_STATES:
            return None
        stamp = int(self._clock.time())
        return self._config.shots_dir / f"{stamp}-r{run.read_id}-{verdict.state.value}.jpg.gz"

    # -- storing -------------------------------------------------------------------------------

    async def _store_stories(
        self, run: _Run, stories: tuple[ParsedStory, ...], raw_path: str | None
    ) -> None:
        if not stories:
            return
        keys = [(s.group_id, s.post_id) for s in stories if s.group_id]
        if run.surface is Surface.FEED and run.feed_absent:
            for group_id in run.feed_absent & {g for g, _ in keys}:
                await self._set_flag(group_id, FLAG_FEED_ABSENT, on=False)  # back in the feed
                run.feed_absent.discard(group_id)
        seen = await self._store.seen_among(keys)
        if run.tracker is not None:
            # A quarantined post was read before, as a seen one was: it counts as seen for the
            # stop rule and the gap, or one bad post would look new to every read for a week.
            before = seen | await self._store.quarantined_among(keys)
            run.tracker.add(stories, {post for _group, post in before})
        now = self._clock.time()
        for story in stories:
            await self._store_story(run, story, seen, raw_path, now)

    async def _store_story(
        self,
        run: _Run,
        story: ParsedStory,
        seen: set[tuple[str, str]],
        raw_path: str | None,
        now: float,
    ) -> None:
        sighting = Sighting.of(story, run.surface, run.read_id)
        if story.deferred_reason is not None or story.group_id is None:
            reason = story.deferred_reason or DeferredReason.NO_GROUP
            await self._store.defer(
                story.post_id,
                reason,
                group_id=story.group_id,
                raw_path=raw_path,
                sighting=sighting,
            )
            run.counts["deferred"] += 1
            return
        if (story.group_id, story.post_id) in seen:
            await self._store.record_sighting(sighting)
            run.counts["seen_again"] += 1
            return
        built = self._item(run, story, now)
        why = built.outcome
        if why == "not_member":
            # A monitored group that is lost or unverified right now (possibly a glitch): its
            # posts are not consumed without an item; once a sync lists the group again they
            # become items, inside the novelty window.
            await self._store.record_sighting(sighting)
            run.counts["not_member"] += 1
            return
        if why == INVALID:
            # Never seen without an item: kept for the next sighting and for replay.
            await self._store.quarantine(sighting, errors=built.errors, raw_path=raw_path)
            run.counts[INVALID] += 1
            detail = await quarantine_edge(self._store)
            if detail is not None:
                await self._events(EventKind.ITEMS_INVALID, detail=detail)
            return
        result = await self._store.mark_seen(sighting, via=run.via, item=built.item, outcome=why)
        self._note_text(run, story)
        if not result.new:
            run.counts["seen_again"] += 1
            return
        run.counts["new"] += 1
        run.counts[why] += 1
        if why == "stale":
            run.stale[story.group_id] += 1
        if result.dropped:
            run.counts["outbox_dropped"] += result.dropped
        depth, cap = result.outbox_depth, self._config.outbox_cap
        if await outbox_edge(self._store, depth, cap):
            await self._events(
                EventKind.OUTBOX_HIGH, detail=f"outbox holds {depth} items (cap {cap})"
            )

    def _note_text(self, run: _Run, story: ParsedStory) -> None:
        text = (story.text or "").rstrip()
        run.texts["n"] += 1
        run.texts["chars"] += len(text)
        run.texts["ellipsis"] += text.endswith(ELLIPSES)

    def _item(self, run: _Run, story: ParsedStory, now: float) -> Built:
        return build_item(
            self._config, story, run.groups.get(story.group_id or ""), via=run.via, now=now
        )

    # -- verdict handling ----------------------------------------------------------------------

    async def _panel_notices(self, run: _Run, step: Step) -> None:
        """Tell the owner of each unread login or password notice in a notifications panel
        the reading passes (``detectors.panel_heads_up_items``), with the probe's held steps,
        once per notice. A notice is known by its words and its arrival, the time of the look
        less its age. An item is a notice already told when its words have a told arrival
        within ``age_unit(age) + DAY`` of its own (Facebook's ages are whole units and grow,
        "5d", "6d", "1w"), each told arrival standing for one item of a look, the nearest
        first (``_pair``). The panel's read items of the same words (``panel_read_items``)
        take part: one that pairs with a told arrival marks that notice read, and a read
        notice stands for no unread item again; on a tie a read item pairs first, so that an
        ambiguous unread item is told rather than taken for an old one. Any other unread item
        is a new notice (another login in Facebook's same generic words) and is told; its
        arrival is recorded in kv (``KV_PANEL_TOLD``), durably, only once the bot delivered
        the alert (``on_sent``: after the read, since alerts are held until Chrome is
        closed). An alert that failed, was rate-limited, or died with the process is not
        recorded, and the next look tells it again; within one read it is told once
        (``_Run.told_now``). Whatever raises here (a notify, the kv record, a row of a shape
        it does not know) is logged by its type and never fails the read: the heads-up is
        lost for this look and tried again at the next (the final review of 2026-10-01)."""
        now = self._clock.time()
        looks = [*run.held_dialogs, *((text, now) for text in step.dom.dialog_texts)]
        run.held_dialogs.clear()
        for text, seen_at in looks:
            try:
                await self._panel_look(run, text, seen_at)
            except Exception as exc:  # noqa: BLE001 - a heads-up never stops a read
                log.warning("panel notices of a look skipped: %s", type(exc).__name__)

    async def _panel_look(self, run: _Run, text: str, seen_at: float) -> None:
        """The heads-up notices of one dialog text seen at ``seen_at`` (``_panel_notices``)."""
        unread = panel_heads_up_items(text)
        items = [*unread, *panel_read_items(text)]
        digests = [panel_notice_digest(item.text) for item in items]
        dated = [(int(seen_at - age_seconds(i.age)), age_unit(i.age) + DAY) for i in items]
        told: dict[str, list[int]] = {}  # the arrivals each item may be paired with
        new: list[int] = []
        for digest in dict.fromkeys(digests):
            rows = await self._told_rows(digest)
            open_rows = [k for k, row in enumerate(rows) if not row[2]]
            told[digest] = [rows[k][0] for k in open_rows] + run.told_now.get(digest, [])
            mine = [n for n, d in enumerate(digests) if d == digest]
            paired = _pair([(*dated[n], n >= len(unread)) for n in mine], told[digest])
            read = [paired[j] for j, n in enumerate(mine) if n >= len(unread) and j in paired]
            new += [n for j, n in enumerate(mine) if n < len(unread) and j not in paired]
            for k in (k for k in read if k < len(open_rows)):
                rows[open_rows[k]][2] = 1  # the owner has read it: it stands for no item
            if any(k < len(open_rows) for k in read):
                await self._store.kv_set_json(KV_PANEL_TOLD + digest, rows, durable=True)
        for n in sorted(new):  # in panel order
            item, digest, (arrival, _) = items[n], digests[n], dated[n]
            pending = run.told_now.setdefault(digest, [])
            nth = sum(1 for a in [*told[digest], *pending] if a == arrival) + 1
            key = panel_notice_key(item.text, arrival, nth=nth)
            try:
                body = panel_notice_text(item.text, item.age)
                record = functools.partial(self._record_told, digest, arrival)
                await self._events.notify(key, body, dedup=NOTICE_ONCE, on_sent=record)
            except Exception as exc:  # noqa: BLE001 - a heads-up never stops a read
                log.warning("panel notice not sent: %s", type(exc).__name__)
                continue
            pending.append(arrival)

    async def _told_rows(self, digest: str) -> list[list[int]]:
        """The told notices of one notice's words: ``[arrival, told_at, read]`` each."""
        rows = await self._store.kv_get_json(KV_PANEL_TOLD + digest) or []
        return [[int(a), int(at), int(bool(rest and rest[0]))] for a, at, *rest in rows]

    async def _record_told(self, digest: str, arrival: int) -> None:
        """A panel notice was delivered: its arrival is told, durably (``KV_PANEL_TOLD``)."""
        now = self._clock.time()
        rows = [*await self._told_rows(digest), [arrival, int(now), 0]]
        recent = [row for row in rows if row[1] >= now - PANEL_TOLD_DAYS]
        kept = sorted(recent, key=lambda row: row[1])[-PANEL_TOLD_KEEP:]
        await self._store.kv_set_json(KV_PANEL_TOLD + digest, kept, durable=True)

    async def _flag(
        self, run: _Run, verdict: Verdict, parsed: StepParse, raw_path: str | None
    ) -> None:
        detail = ", ".join(verdict.reasons)[:300] or verdict.state.value
        await self._group_meta(run, verdict)
        await self._store_stories(run, parsed.stories, raw_path)
        if verdict.state is PageState.FEED_NOT_CHRONO:
            if await self._store.kv_get(KV_BACKSTOP_ONLY) != "1":
                await self._store.kv_set(KV_BACKSTOP_ONLY, "1", durable=True)
                await self._events(EventKind.FEED_NOT_CHRONO, detail=detail)
            raise _Stop(
                ReadResult(
                    run.read_id,
                    ReadOutcome.FAILED,
                    PageState.FEED_NOT_CHRONO,
                    reason="feed not chronological",
                )
            )
        group_id = run.group_id
        if group_id is None:
            return
        kinds = {
            PageState.NOT_FOLLOWED: (FLAG_NOT_FOLLOWED, EventKind.NOT_FOLLOWED),
            PageState.SORT_IGNORED: (FLAG_SORT_IGNORED, EventKind.SORT_IGNORED),
        }
        if verdict.state in kinds:
            flag, kind = kinds[verdict.state]
            if await self._set_flag(group_id, flag, on=True):
                group = run.groups.get(group_id)
                await self._events(
                    kind,
                    group_id=group_id,
                    group_name=group.name if group else None,
                    detail=detail,
                )

    async def _group_meta(self, run: _Run, verdict: Verdict) -> None:
        """Refresh privacy, membership, follow and sort state from the group page (once a read)."""
        meta = run.parser.group
        if run.surface is not Surface.GROUP or meta is None or run.meta_saved:
            return
        if run.group_id is not None and meta.id != run.group_id:
            return
        run.meta_saved = True
        row = await self._store.upsert_group(meta)
        run.groups[row.id] = row
        if FLAG_CONFIRMED in row.flags and meta.is_member:
            await note_verified(self._store, row.id)
        if meta.is_followed and verdict.state is not PageState.NOT_FOLLOWED:
            await self._set_flag(row.id, FLAG_NOT_FOLLOWED, on=False)
        if meta.sorting_setting is not None and verdict.state is not PageState.SORT_IGNORED:
            await self._set_flag(row.id, FLAG_SORT_IGNORED, on=False)

    async def _set_flag(self, group_id: str, flag: str, *, on: bool) -> bool:
        """Set or clear one group flag; ``True`` when it changed."""
        row = await self._store.get_group(group_id)
        if row is None or (flag in row.flags) == on:
            return False
        flags = row.flags | {flag} if on else row.flags - {flag}
        return await self._store.set_group_flags(group_id, flags, durable=flag == FLAG_PAUSED)

    async def _mark_lost(self, run: _Run) -> tuple[GroupRow, bool] | None:
        """Lost and paused, then (never before: fail closed) the owner's word withdrawn.

        Returns the row and whether a membership confirmation was withdrawn.
        """
        group_id = run.group_id
        if group_id is None:
            return None
        row = await self._store.upsert_group(GroupMeta(id=group_id), state=GroupState.LOST)
        await self._set_flag(group_id, FLAG_PAUSED, on=True)
        withdrawn = FLAG_CONFIRMED in row.flags and await withdraw(
            self._store, group_id, reason="its page did not say MEMBER"
        )
        run.groups[group_id] = await self._store.get_group(group_id) or row
        return run.groups[group_id], withdrawn

    async def _group_lost(self, run: _Run, verdict: Verdict) -> None:
        lost = await self._mark_lost(run)
        if lost is None:
            return
        row, withdrawn = lost
        detail = ", ".join(verdict.reasons)[:300] or "group lost"
        if withdrawn:
            detail += (
                "; the owner's membership confirmation is withdrawn (after checking by hand: "
                f"`fbcollector groups confirm {row.id}`)"
            )
        await self._events(
            EventKind.GROUP_LOST, group_id=row.id, group_name=row.name, detail=detail
        )

    async def _driver_failure(self, run: _Run, exc: Exception) -> ReadResult:
        log.warning("read %d: the browser failed (%s)", run.read_id, type(exc).__name__)
        verdict = Verdict(
            state=PageState.UNKNOWN,
            reasons=(f"driver:{type(exc).__name__}",),
            excerpt=f"unknown driver:{type(exc).__name__} at {redact_url(run.grant.action.url)}",
        )
        run.verdict = verdict
        run.verdicts.append(verdict.state.value)
        probe = run.action_kind is ActionKind.PROBE
        with contextlib.suppress(Exception):
            await self._ladder.on_verdict(
                verdict, probe_block_id=run.grant.action.block_id if probe else None
            )
        return ReadResult(
            run.read_id,
            ReadOutcome.FAILED,
            PageState.UNKNOWN,
            close_chrome=True,
            reason=f"driver failure: {type(exc).__name__}",
        )

    # -- after the read ------------------------------------------------------------------------

    async def _finish(self, run: _Run, result: ReadResult) -> ReadResult:
        tracker = run.tracker
        coverage = tracker.coverage() if tracker is not None else Coverage()
        sync: SyncOutcome | None = None
        recognised = run.verdict is not None and run.verdict.state not in SCREENSHOT_STATES
        if (
            run.action_kind is ActionKind.SYNC
            and recognised
            and result.outcome is not (ReadOutcome.ABORTED)
        ):
            sync = await apply_sync(
                self._store,
                run.parser.joined,
                header_count=run.header,
                page_info=run.parser.page_info,
            )
            await self._announce_sync(sync, run)
        stats = await self._stats(run, result, sync)
        page_state = result.page_state or (run.verdict.state if run.verdict else None)
        await self._store.finish_read(
            run.read_id,
            coverage=coverage,
            steps=run.steps,
            outcome=result.outcome,
            page_state=page_state,
            stats=stats,
        )
        gap_groups: list[str] = []
        if result.outcome is ReadOutcome.GAP and tracker is not None:
            rates = await self._store.posting_rates(self._clock.time() - 7 * DAY)
            seen_in_gap = tracker.gap_groups()
            if run.surface is Surface.GROUP and run.group_id is not None:
                seen_in_gap = [run.group_id]  # a backstop's gap is its own group's
            readable = [g for g in seen_in_gap if _backstoppable(run.groups.get(g))]
            gap_groups = gap_targets(readable, rates)
        await self._scheduler.read_finished(
            run.grant, outcome=result.outcome, gap_groups=gap_groups
        )
        if recognised and run.surface is not Surface.JOINS and run.parser.health is not None:
            await self._parser_health(run.parser.health)
        if (
            run.surface is Surface.GROUP
            and run.group_id is not None
            and result.outcome in {ReadOutcome.COMPLETE, ReadOutcome.GAP}
        ):
            await self._quiet_flag(run.group_id)
            await self._feed_absence(run, run.group_id)
        return ReadResult(
            result.read_id,
            result.outcome,
            page_state,
            result.close_chrome,
            result.reason,
            new_items=run.counts["queued"],
            sync=sync,
            planned_close=result.planned_close,
        )

    async def _announce_sync(self, sync: SyncOutcome, run: _Run) -> None:
        if sync.pending:
            await self._events.notify(
                "sync_incomplete",
                f"ℹ️ SYNC: the joined-groups list was incomplete ({sync.listed} listed, header "
                f"{sync.header_count}); {len(sync.pending)} new group(s) wait for a complete "
                "sync before they are read.",
                dedup=DAY,
            )
        for group_id in sync.unverified:
            group = run.groups.get(group_id)
            text = (
                f"⚠️ UNVERIFIED: {(group.name if group else None) or group_id} ({group_id}) is "
                "missing from the joined-groups list; it is not read until a sync lists it again."
            )
            await self._events.notify(f"unverified:{group_id}", text, dedup=7 * DAY)
        for group_id in sync.left:
            await self._events.notify(
                f"left:{group_id}", f"ℹ️ LEFT: the account is no longer in group {group_id}."
            )

    async def _stats(
        self, run: _Run, result: ReadResult, sync: SyncOutcome | None
    ) -> dict[str, Any]:
        action = run.grant.action
        info = run.parser.page_info
        stats: dict[str, Any] = {
            "kind": action.kind.value,
            "reason": action.reason,
            "profile": run.grant.profile.value,
            "allowance": action.scroll_allowance,
            "result": result.reason,
            "verdicts": run.verdicts,
            "reasons": list(run.verdict.reasons) if run.verdict else [],
            "health": run.parser.health.as_stats() if run.parser.health else {},
            "preloader": run.parser.preloader.summary(),
            "page_info": None
            if info is None
            else {"connection": info.connection, "has_next_page": info.has_next_page},
            "latency_ms": run.latencies,
            "pagination": {"scrolls": run.scrolls, "fired": run.fired},
            "lost_bodies": run.lost_bodies,
            "dom": {"posinset": run.posinset, "see_more": run.see_more},
            "counts": dict(run.counts),
            "stale_dropped": dict(run.stale),
            "group_paths": run.group_paths,
            "texts": dict(run.texts),
            "gap": bool(run.tracker and run.tracker.gap),
        }
        if run.parser.group is not None:
            group = run.parser.group
            stats["group"] = {
                "privacy": group.privacy.value,
                "join_state": group.join_state,
                "subscribe_status": group.subscribe_status,
                "sorting_setting": group.sorting_setting,
            }
        if sync is not None:
            stats["sync"] = sync.summary()
        if run.parser.joined is not None:
            stats["joins_connections"] = dict(run.parser.joined.connections)
        if self._resources is not None:
            with contextlib.suppress(Exception):
                stats["resources"] = await self._resources()
        return stats

    async def _parser_health(self, health: ParseHealth) -> None:
        """Judge the parser over the last reads. The history keeps each read's posts by id
        (how the read saw each one), so a post read again counts once; entries written before
        it kept them are dropped (the window starts afresh, as after ``pause --off``)."""
        raw = await self._store.kv_get(KV_PARSER_HISTORY)
        loaded = json.loads(raw) if raw else []
        history = [h for h in loaded if isinstance(h, dict) and isinstance(h.get("posts"), dict)]
        history.append(
            {
                "found": health.found,
                "accepted": health.accepted,
                "dom_posts": health.dom_posts,
                "failures": health.failures,
                "posts": {pid: mark.as_json() for pid, mark in health.posts.items()},
            }
        )
        history = history[-PARSER_HISTORY:]
        await self._store.kv_set(KV_PARSER_HISTORY, json.dumps(history))
        verdict = parser_verdict([_history_health(h) for h in history])
        if verdict.broken and await self._store.kv_get(KV_PARSER_BROKEN) is None:
            await self._store.kv_set(
                KV_PARSER_BROKEN, json.dumps(list(verdict.reasons)), durable=True
            )
            await self._events(EventKind.PARSER_BROKEN, detail="; ".join(verdict.reasons)[:300])
        for kind, warning in verdict.warnings.items():
            await self._events.notify(
                f"parser_warning:{kind}", f"⚠️ PARSER: {warning} over the last reads.", dedup=DAY
            )

    async def _feed_absence(self, run: _Run, group_id: str) -> None:
        """Followed, but missing from the Groups feed for 48 h while backstops find posts."""
        now = self._clock.time()
        if group_id in run.feed_absent or await self._store.kv_get(KV_BACKSTOP_ONLY) == "1":
            return
        first_feed = await self._store.first_read_at(Surface.FEED)
        if first_feed is None or first_feed > now - FEED_ABSENT_AFTER:
            return  # the feed has not been read for 48 h yet
        in_feed = await self._store.last_sighting_at(group_id, Surface.FEED)
        since = now - FEED_ABSENT_AFTER
        if in_feed is not None and in_feed >= since:
            return
        found = await self._store.found_by_backstop(group_id, since=since, until=now - FEED_GRACE)
        if not found:
            return
        if await self._set_flag(group_id, FLAG_FEED_ABSENT, on=True):
            group = run.groups.get(group_id)
            await self._events(
                EventKind.NOT_FOLLOWED,
                group_id=group_id,
                group_name=group.name if group else None,
                detail=f"absent from the Groups feed for 48 h while backstops found {found} "
                "post(s): snoozed or unfollowed?",
            )

    async def _quiet_flag(self, group_id: str) -> None:
        last = await self._store.last_created_at(group_id)
        quiet = last is None or last < self._clock.time() - 7 * DAY
        await self._set_flag(group_id, FLAG_NO_RECENT_POSTS, on=quiet)


def panel_notice_digest(notice: str) -> str:
    """A panel notice's words, case and spacing ignored, as 12 hex digits of their SHA-1."""
    words = " ".join(notice.casefold().split())
    return hashlib.sha1(words.encode()).hexdigest()[:12]  # noqa: S324 - a key


def panel_notice_key(notice: str, arrival: int, *, nth: int = 1) -> str:
    """The alert key of one panel notice: its words' digest and its arrival (epoch seconds),
    and ``nth`` when alike items of one look came at the same second by their ages."""
    return f"panel_notice:{panel_notice_digest(notice)}:{arrival}{f':{nth}' if nth > 1 else ''}"


def _pair(dated: Sequence[tuple[int, float, bool]], told: Sequence[int]) -> dict[int, int]:
    """Which of a look's alike items (each its arrival, its slack and whether it is read)
    pair with which told arrival: within the item's slack, the nearest pairs first, a read
    item first on a tie, and each told arrival standing for one item only, so two logins are
    two notices."""
    pairs = sorted(
        (abs(arrival - t), not read, n, k)
        for n, (arrival, slack, read) in enumerate(dated)
        for k, t in enumerate(told)
        if abs(arrival - t) <= slack
    )
    paired: dict[int, int] = {}
    used: set[int] = set()
    for _, _, n, k in pairs:
        if n not in paired and k not in used:
            paired[n] = k
            used.add(k)
    return paired


def panel_notice_text(notice: str, age: str | None) -> str:
    when = f"{age} ago" if age else "just now (or no age shown)"
    return (
        f"🔔 FB NOTICE: unread on the account, {when}: «{notice}»\n"
        "Reading goes on. This notice is told once. A later one in the same words (a new "
        "login) is told on its own, but one first seen a week or more old, close to this one, "
        "may not be: look at the account's notifications now and then. If the login or the "
        "change was not yours, `fbcollector pause` and secure the account by hand."
    )


def _history_health(entry: dict[str, Any]) -> ParseHealth:
    """One read of the parser history, as the verdict wants it."""
    marks = {pid: PostMark.from_json(raw) for pid, raw in entry["posts"].items()}
    return ParseHealth(
        found=int(entry.get("found", 0)),
        accepted=int(entry.get("accepted", 0)),
        dom_posts=int(entry.get("dom_posts", 0)),
        dropped={"no_post_id": int(entry.get("failures", 0))},
        posts={pid: mark for pid, mark in marks.items() if mark is not None},
    )


@dataclass(frozen=True, slots=True)
class Built:
    """What ``build_item`` made of a story: the item, or why not (a counter name)."""

    item: Item | None
    outcome: str
    errors: tuple[str, ...] = ()
    """For ``INVALID``: where and how the wire model refused it (``url:value_error``), from
    the last attempt; never a value, which may be post text."""


def validation_errors(exc: ValidationError) -> tuple[str, ...]:
    """``field.path:error_type`` for each error, sorted and unique, without any input value."""
    found = set()
    for error in exc.errors(include_input=False, include_url=False, include_context=False):
        where = ".".join(str(p) for p in error.get("loc", ())) or "item"
        found.add(f"{where}:{error.get('type', 'invalid')}")
    return tuple(sorted(found))


def build_item(
    config: ReaderConfig, story: ParsedStory, group: GroupRow | None, *, via: Via, now: float
) -> Built:
    """The wire item for a story that is new, or ``None`` and why not.

    Only posts of monitored member groups become items, and only when they are inside the
    novelty window: created at or after the group's baseline and within the last 7 days
    (DESIGN, Seen set). An item that does not fit the wire model is retried without its
    author and listing (a name too long, a link that is not facebook.com); failing that it is
    ``INVALID``, with the categories of the errors: the caller quarantines it. The identifiers,
    links and times it failed on are not "repaired": that would send a post that is not the
    one read.
    """
    group_id = story.group_id
    if group is None or group_id is None:
        return Built(None, "unknown_group")
    if not group.monitored:
        return Built(None, "not_monitored")
    if group.state is not GroupState.MEMBER:
        return Built(None, "not_member")
    created = story.created_at
    if created is None:  # pragma: no cover - such a story is deferred by the parser
        return Built(None, "no_created_at")
    if created < group.baseline_at:
        return Built(None, "before_baseline")
    if created < now - config.novelty:
        return Built(None, "stale")
    fields: dict[str, Any] = {
        "collector_id": config.collector_id,
        "seq": 0,
        "group_id": group_id,
        "post_id": story.post_id,
        "url": story.url or f"https://www.facebook.com/groups/{group_id}/posts/{story.post_id}/",
        "group_name": group.name or group_id,
        "group_url": f"https://www.facebook.com/groups/{group_id}/",
        "group_privacy": group.privacy,
        "created_at": created,
        "first_seen_at": int(now),
        "via": via,
        "late": now - created > config.late_after_hours * 3600,
        "text": story.text or "",
        "is_share": story.is_share,
    }
    author = story.author
    if author.id or author.name or author.anonymous:
        fields["author"] = {
            "id": author.id,
            "name": author.name,
            "url": author.url,
            "anonymous": author.anonymous,
        }
    if story.listing is not None:
        fields["listing"] = {
            "id": story.listing.id,
            "title": story.listing.title,
            "price": story.listing.price,
            "location": story.listing.location,
        }
    errors: tuple[str, ...] = ()
    for attempt in (fields, _without(fields, "author", "listing")):
        try:
            return Built(Item.model_validate(attempt), "queued")
        except ValidationError as exc:
            errors = validation_errors(exc)
            log.warning(
                "post %s/%s does not fit the wire model (%s)",
                group_id,
                story.post_id,
                ", ".join(errors),
            )
    return Built(None, INVALID, errors)


def _sleep_abort(run: _Run) -> ReadResult:
    """The read's end when Chrome is being closed under it: nothing judged, with the reason."""
    reason = run.abort.reason if isinstance(run.abort, Abort) else SLEEP_ABORT
    return ReadResult(
        run.read_id, ReadOutcome.ABORTED, close_chrome=True, reason=reason, planned_close=True
    )


def _needs_member_proof(run: _Run) -> bool:
    """A backstop of a group that is a member only by the owner's word (``confirm``)."""
    group = run.groups.get(run.group_id or "")
    return run.surface is Surface.GROUP and group is not None and FLAG_CONFIRMED in group.flags


def _backstoppable(group: GroupRow | None) -> bool:
    return group is not None and group.monitored and group.state is GroupState.MEMBER


def _without(fields: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if k not in keys}


def page_facts(parser: ReadParser) -> PageFacts:
    """``StepParse`` → ``PageFacts``, the mapping A2 documented (read-level, all steps so far)."""
    markers = parser.markers
    preloader = parser.preloader
    group = parser.group
    feed: FeedPreloader | None = None
    if preloader.feed_style or preloader.orderby:
        feed = FeedPreloader(feed_style=preloader.feed_style, orderby=preloader.orderby)
    if parser.surface is Surface.JOINS:
        stories = len(parser.joined.groups) if parser.joined is not None else 0
    else:
        stories = len(parser.stories)
    return PageFacts(
        user_id=markers.user_id,
        viewer_null=bool(markers.viewer_null),
        error_codes=frozenset(e.code for e in parser.errors if e.code and not e.partial),
        rate_limited=any(e.rate_limited for e in parser.errors),
        feed_preloader=feed,
        group=group,
        sorting_setting=group.sorting_setting if group else None,
        stories=stories,
        locale=markers.locale,
    )
