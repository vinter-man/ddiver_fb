"""The composition root: ddiver-core's pieces with a Facebook source instead of a listener.

ddiver's own ``Application`` stays in ddiver; this is the brain's, and it is only wiring. There
is no user session and no supervisor: nothing here talks to Facebook (invariant 4), posts
arrive over the ingest API, and a collector problem is reported rather than restarted around.
What remains is the core's machinery — storage with the ``fb`` track, the matcher, delivery
without Telegram name lookups (``senders=None``), the bot UI with the ``fb`` overlay and hooks —
plus the ingest server, the collector monitor, health, metrics and the dead-man's switch.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import os
from typing import Any, Final

from telethon import errors
from telethon.tl.functions.updates import GetStateRequest

from ddiver_core.app import EXIT_ERROR, EXIT_OK, PeriodicTask
from ddiver_core.botclient import connect_with_timeout, make_bot_client
from ddiver_core.botruntime import BotAdapter, notification_buttons_factory
from ddiver_core.delivery import BotPort, Delivery
from ddiver_core.i18n import DEFAULT_LANG, t
from ddiver_core.matcher import Matcher
from ddiver_core.metrics import MetricsServer
from ddiver_core.storage import Rule, Storage, safe_write
from ddiver_core.timeutil import Clock, utcnow
from ddiver_core.watchdog import Watchdog
from ddiver_fb import schema, strings
from ddiver_fb.ai import AiStore, AiWorker
from ddiver_fb.api import IngestApi
from ddiver_fb.botio import BotFront
from ddiver_fb.config import FbSettings
from ddiver_fb.control import ControlDesk
from ddiver_fb.groups import GroupBook
from ddiver_fb.health import BrainBeat, write_beat
from ddiver_fb.ingest import IngestServer
from ddiver_fb.judge import OpenRouterJudge
from ddiver_fb.metrics import FbMetricsSource
from ddiver_fb.monitor import CollectorMonitor, MonitorSettings
from ddiver_fb.source import FacebookSource
from ddiver_fb.store import FbStore
from ddiver_fb.ui import StatusSources, build_fb_router

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS: Final = 30.0
PING_SECONDS: Final = 60.0
PING_TIMEOUT: Final = 15.0
TICK_SECONDS: Final = 60.0
FLUSH_SECONDS: Final = 60.0
RESCAN_SECONDS: Final = 300.0
AI_SECONDS: Final = 15.0
PRUNE_SECONDS: Final = 24 * 3600.0
EVENT_RETENTION: Final = 30 * 86400
TEXT_RETENTION: Final = 7 * 86400
"""Post texts (``fb_texts``) are kept as long as a match's outbox payload: ddiver's seven days."""
COMMAND_RETENTION: Final = 30 * 86400
TELL_TIMEOUT: Final = 5.0
"""How long one owner alert or notice may wait for Telegram. Nothing on the ingest path waits
for one (the monitor sends from its own task), so this bounds that task, not a request."""
TELL_ERRORS: Final = (errors.RPCError, ValueError, ConnectionError, OSError)


class BrainApp:
    """Everything wired together, with a lifecycle the CLI drives."""

    def __init__(
        self,
        settings: FbSettings,
        *,
        clock: Clock = utcnow,
        bot_client: Any = None,
        bot: BotPort | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.storage = Storage(
            settings.db_path,
            owner_id=settings.owner_id,
            clock=clock,
            digest_threshold=settings.digest_threshold,
            tracks={schema.TRACK: schema.MIGRATIONS},
        )
        self.fb = FbStore(self.storage)
        self.bot_client = bot_client if bot_client is not None else make_bot_client(settings)
        self.adapter = BotAdapter(self.bot_client)
        self.bot: BotPort = bot if bot is not None else self.adapter
        self.watchdog = Watchdog(
            settings.ping_url,
            timeout=settings.ping_timeout,
            min_interval=settings.ping_interval,
            clock=clock,
        )
        self.started_at = clock()
        self.bot_ping_ok_at: dt.datetime | None = None
        self.tell_timeout = TELL_TIMEOUT
        self._pings_ok = 0
        self._stalled_at_ping: int | None = None
        """``_pings_ok`` when a message last timed out; ``None``: Telegram took the last one."""
        self._stop = asyncio.Event()
        self._tasks: list[PeriodicTask] = []
        self.metrics_server: MetricsServer | None = None

        s = settings
        self.matcher = Matcher(
            self.storage, timeout=s.regex_timeout, on_rule_disabled=self._rule_disabled
        )
        self.delivery = Delivery(
            self.bot,
            self.storage,
            clock=clock,
            buttons=notification_buttons_factory(),
            senders=None,  # Facebook authors are not Telegram accounts; never look them up
            on_user_state_changed=self.invalidate,
        )
        self.ai = AiStore(self.fb)
        self.source = FacebookSource(
            self.storage,
            self.fb,
            self.matcher,
            deliver=self.delivery.enqueue,
            ai=self.ai,
            clock=clock,
        )
        self.desk = ControlDesk(self.storage, self.fb, clock=clock)
        self.groups = GroupBook(
            self.storage,
            self.fb,
            tell=self.tell,
            invalidate=self.invalidate,
            clock=clock,
        )
        self.monitor = CollectorMonitor(
            self.storage,
            self.fb,
            self.desk,
            self.groups,
            owner_id=s.owner_id,
            tell=self.tell,
            clock=clock,
            settings=MonitorSettings(
                offline_after=s.offline_after_minutes * 60,
                day_start_hour=s.day_start_hour,
                day_end_hour=s.day_end_hour,
                tz=s.collector_tz,
                silence_factor=s.silence_factor,
                silence_floor=int(s.silence_floor_hours * 3600),
            ),
        )
        key = s.openrouter_api_key
        self.judge = (
            OpenRouterJudge(key.get_secret_value(), model=s.ai_model) if key is not None else None
        )
        self.ai_worker = AiWorker(
            self.ai,
            self.fb,
            self.storage,
            self.judge,
            owner_id=s.owner_id,
            notify=self.notice,
            daily_calls=s.ai_daily_calls,
            clock=clock,
        )
        self.api = IngestApi(self.source, self.monitor, self.desk, self.fb, clock=clock)
        self.ingest = IngestServer(
            self.api,
            host=s.ingest_host,
            port=s.ingest_port,
            token=s.ingest_token.get_secret_value(),
        )
        self.router = build_fb_router(
            self.storage,
            matcher=self.matcher,
            monitor=self.monitor,
            desk=self.desk,
            fb=self.fb,
            counters=lambda: StatusSources(self.source.metrics, self.delivery, self.started_at),
            regex_timeout=s.regex_timeout,
            ai=self.ai,
            ai_worker=self.ai_worker,
            clock=clock,
        )
        self.front = BotFront(self.bot_client, self.adapter, self.router, self.storage)

    # -- lifecycle -------------------------------------------------------------------------

    async def setup(self) -> None:
        strings.install()
        await self.storage.open()
        await self.storage.ensure_user(self.settings.owner_id)
        await self.matcher.reload()
        await self.source.prime()

    async def run(self) -> int:
        await connect_with_timeout(self.bot_client)
        await self.bot_client.start(bot_token=self.settings.bot_token.get_secret_value())
        self.front.register()
        log.info("ddiver-fb starting, build %s", os.environ.get("DDIVER_COMMIT", "unknown")[:12])
        await self.front.publish_commands()
        await self.storage.record_boot()
        await self.delivery.replay_pending()
        try:
            await self.ingest.start()
        except OSError as exc:
            # Unlike metrics, the ingest server is the job: without it nothing arrives. Exit, so
            # the health check and the restart policy see it, rather than run deaf.
            log.error(  # noqa: TRY400 - the reason is the message; a bind's traceback adds nothing
                "cannot serve ingest on %s:%d: %s", self.ingest.host, self.ingest.port, exc
            )
            return EXIT_ERROR
        await self._start_metrics()
        self.start_tasks()
        await self._stop.wait()
        return EXIT_OK

    def request_stop(self) -> None:
        """Called from a signal handler: nothing here may await."""
        self._stop.set()

    async def close(self) -> None:
        """Stop taking posts first, then drain what was taken, then let go of the database."""
        if self.metrics_server is not None:
            await self.metrics_server.stop()
        self.api.closing = True  # items still in flight are answered 503, never rejected
        await self.ingest.stop()
        for task in self._tasks:
            await task.stop()
        await self.monitor.close()  # its sender may be mid-message; what is unsent stays queued
        # Each step on its own: a full disk failing the hit flush must not skip the drain, the
        # disconnect or closing the database (aiosqlite's thread would hang the exit).
        try:
            await self.matcher.flush_hits()
        except Exception:
            log.exception("rule-hit counters were not written")
        try:
            left = await self.delivery.drain()
        except Exception:
            log.exception("delivery drain failed during shutdown")
        else:
            if left:
                log.warning("%d notifications stay pending for the next start", left)
        with contextlib.suppress(Exception):
            await self.bot_client.disconnect()
        if self.judge is not None:
            with contextlib.suppress(Exception):
                await self.judge.close()
        await self.fb.close()
        await self.storage.close()

    # -- background work -------------------------------------------------------------------

    def start_tasks(self) -> None:
        self._tasks = [
            PeriodicTask("heartbeat", self.heartbeat, HEARTBEAT_SECONDS),
            PeriodicTask("bot-ping", self.ping_bot, PING_SECONDS, first_delay=PING_SECONDS),
            PeriodicTask("collector-tick", self.monitor.tick, TICK_SECONDS, first_delay=30.0),
            PeriodicTask("hits-flush", self.flush_hits, FLUSH_SECONDS),
            PeriodicTask("outbox-rescan", self.rescan, RESCAN_SECONDS, first_delay=RESCAN_SECONDS),
            PeriodicTask("prune", self.prune, PRUNE_SECONDS),
            PeriodicTask("ai-rule", self.ai_worker.run_once, AI_SECONDS, first_delay=AI_SECONDS),
        ]
        for task in self._tasks:
            task.start()

    def bot_connected(self) -> bool:
        sender = getattr(self.bot_client, "_sender", None)
        with contextlib.suppress(Exception):  # private API; never break a check over it
            return bool(sender is not None and sender._transport_connected())  # noqa: SLF001
        return False

    async def heartbeat(self) -> None:
        if self.watchdog.enabled:
            await self.watchdog.ping()
        write_beat(
            self.settings.health_path,
            BrainBeat(
                ts=self.clock().isoformat(),
                bot_connected=self.bot_connected(),
                ingest_up=self.ingest.serving,
                bot_ping_ok_at=self.bot_ping_ok_at.isoformat() if self.bot_ping_ok_at else None,
                queue=self.delivery.total_queued(),
            ),
        )

    async def ping_bot(self) -> None:
        try:
            async with asyncio.timeout(PING_TIMEOUT):
                await self.bot_client(GetStateRequest())
            self.bot_ping_ok_at = self.clock()
            self._pings_ok += 1
        except (TimeoutError, errors.RPCError, ConnectionError):
            log.warning("bot ping failed")

    async def flush_hits(self) -> None:
        await safe_write(self.matcher.flush_hits(), "rule hits")

    async def rescan(self) -> None:
        """Queue outbox rows nothing holds (a full queue, a paused user, a claim whose hand-off
        was lost to a busy database), and put the matcher's cache right if it drifted."""
        await self.delivery.replay_pending(quiet=True)
        await self.reconcile_matcher()

    async def reconcile_matcher(self) -> bool:
        """Reload the matcher when its monitored set differs from the database's; ``True``
        when it did. A refresh after a group change can fail after the change committed."""
        if await self.storage.monitored_chat_ids() == set(self.matcher.monitored):
            return False
        log.warning("the matcher's monitored set had drifted from the database; reloading")
        await self.matcher.invalidate(None)
        return True

    async def prune(self) -> None:
        now = int(self.clock().timestamp())
        removed = await self.storage.prune_outbox()
        items = await self.fb.prune_items(
            received_before=now - self.settings.items_retention_days * 86400
        )
        texts = await self.fb.prune_texts(received_before=now - TEXT_RETENTION)
        await self.ai.prune(before=now - TEXT_RETENTION)  # the reasons retell the posts
        await self.fb.prune_events(received_before=now - EVENT_RETENTION)
        await self.fb.prune_commands(created_before=now - COMMAND_RETENTION)
        if removed or items or texts:
            log.info("pruned %d outbox rows, %d post ids and %d post texts", removed, items, texts)

    async def _start_metrics(self) -> None:
        s = self.settings
        if not s.metrics_port:
            return
        source = FbMetricsSource(
            items=self.source.metrics,
            status_age=self._status_age,
            group_ages=self._group_ages,
            bot_connected=self.bot_connected,
            ingest_up=lambda: self.ingest.serving,
            delivery=self.delivery,
            matcher=self.matcher,
            watchdog=self.watchdog,
            uptime=self._uptime,
            collector=lambda: (self.monitor.last_holder_state, self.monitor.last_holder_status),
        )
        server = MetricsServer(source, s.metrics_host, s.metrics_port)
        try:
            await server.start()
        except OSError as exc:
            log.warning("could not serve metrics on port %d: %s", s.metrics_port, exc)
            return
        self.metrics_server = server

    def _uptime(self) -> float:
        return (self.clock() - self.started_at).total_seconds()

    def _status_age(self) -> float | None:
        # Scrapes are synchronous; the monitor's tick keeps this snapshot fresh enough.
        at = self.monitor.last_holder_status_at
        return None if at is None else self.clock().timestamp() - at

    def _group_ages(self) -> dict[int, float]:
        now = self.clock().timestamp()
        monitored = self.matcher.monitored
        return {gid: now - at for gid, at in self.source.last_item_at.items() if gid in monitored}

    # -- callbacks -------------------------------------------------------------------------

    async def invalidate(self, chat_id: int | None = None) -> None:
        await self.matcher.invalidate(chat_id)

    async def tell(self, user_id: int, text: str) -> bool:
        """Send a plain message; ``False`` when it did not go out (every caller retries later).

        Bounded by :attr:`tell_timeout`: Telethon queues a request while it reconnects and never
        gives up (``connection_retries=None``), so an unbounded send waits out the whole outage.
        After one timed out, nothing more is tried until the bot's ping has succeeded again:
        Telethon keeps the request of a caller that gave up and sends it once it reconnects, so
        retrying every minute would hand the owner one copy of the message per minute of
        outage. At most that first copy arrives twice.
        """
        if self._stalled_at_ping is not None:
            if self._pings_ok == self._stalled_at_ping:
                return False
            self._stalled_at_ping = None
        try:
            async with asyncio.timeout(self.tell_timeout):
                await self.bot.send_message(user_id, text)
        except TimeoutError:
            self._stalled_at_ping = self._pings_ok
            log.warning(
                "Telegram took no message for %.0f s; holding messages until the bot's ping "
                "succeeds",
                self.tell_timeout,
            )
            return False
        except TELL_ERRORS:
            log.debug("could not message %s", user_id, exc_info=True)
            return False
        return True

    async def notice(self, user_id: int, text: str) -> None:
        """Queue a message for the monitor's sender: durable, and never awaited on Telegram."""
        await self.fb.add_notices([(user_id, text)], now=int(self.clock().timestamp()))
        self.monitor.nudge()

    async def _rule_disabled(self, rule: Rule, reason: str) -> None:
        """Queued, not sent: the matcher calls this while it evaluates a post inside an ingest
        request, and nothing there may wait for Telegram (the monitor's sender delivers it)."""
        user = await self.storage.get_user(rule.user_id)
        lang = (user.lang if user else None) or DEFAULT_LANG
        text = t(lang, "bot.rules.disabled", pattern=rule.pattern, reason=reason)
        await self.fb.add_notices([(rule.user_id, text)], now=int(self.clock().timestamp()))
        self.monitor.nudge()
