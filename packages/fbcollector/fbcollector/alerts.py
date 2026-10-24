"""The owner-only alert bot (D19): critical states go straight to the owner's Telegram.

In phase 0 this is the only alert path, and later it stays the path of last resort, because the
brain may be what is down. It is deliberately narrow:

- **One method, one chat.** The client can only call ``sendMessage``, only to ``OWNER_ID``
  fixed at construction; there is no parameter for another chat or another method. It has its
  own BotFather token; the user-facing bot's token never reaches the collector.
- **Never post text** (invariant 8). Alerts are built by the functions below from states,
  reasons, ids and group names — never from a post — and are capped in length.
- **Dedup and rate limits.** An alert carries an event key; the same key is not sent again
  within its window (persisted, so a restart does not repeat it). At most 20 alerts an hour
  go out, 3 s apart; critical ones (blocks, logouts, crash loops) ignore the hourly cap.
- **The token stays out of logs.** It is part of the request URL, which ``httpx`` logs at
  INFO; that logger is held at WARNING, and failures are logged by exception type only.

Tests use ``httpx.MockTransport``; nothing here is ever called for real in tests.
"""

from __future__ import annotations

import datetime as dt
import enum
import json
import logging
from collections import deque
from collections.abc import Sequence
from typing import Any, Final, Protocol

import httpx

from fbcollector.clock import Clock

log = logging.getLogger(__name__)

API_ROOT: Final = "https://api.telegram.org"
METHOD: Final = "sendMessage"
MAX_TEXT: Final = 1000
PER_HOUR: Final = 20
MIN_GAP: Final = 3.0
DEFAULT_DEDUP: Final = 6 * 3600.0
KV_PREFIX: Final = "alert:"
PREFIX: Final = "🛰 fbcollector: "
HOUR: Final = 3600.0


class AlertLedger(Protocol):
    async def kv_get(self, key: str) -> str | None: ...

    async def kv_set(self, key: str, value: str) -> None: ...


class AlertResult(enum.StrEnum):
    SENT = "sent"
    DUPLICATE = "duplicate"
    RATE_LIMITED = "rate_limited"
    FAILED = "failed"
    DISABLED = "disabled"


def quiet_http_logs() -> None:
    """``httpx`` logs every request URL at INFO — with a bot token in it. Keep it at WARNING."""
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        if logger.getEffectiveLevel() < logging.WARNING:
            logger.setLevel(logging.WARNING)


class Alerts(Protocol):
    async def alert(
        self, key: str, text: str, *, critical: bool = False, dedup: float | None = None
    ) -> AlertResult: ...


class NullAlerts:
    """No bot configured (tests, ``check-config``): alerts are logged, never sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def alert(
        self,
        key: str,
        text: str,
        *,
        critical: bool = False,  # noqa: ARG002 - one interface
        dedup: float | None = None,  # noqa: ARG002 - one interface
    ) -> AlertResult:
        self.sent.append((key, text))
        log.warning("alert (no bot configured) %s: %s", key, text)
        return AlertResult.DISABLED


class AlertBot:
    def __init__(
        self,
        token: str,
        owner_id: int,
        *,
        clock: Clock,
        ledger: AlertLedger | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
        per_hour: int = PER_HOUR,
        min_gap: float = MIN_GAP,
    ) -> None:
        if owner_id <= 0:
            msg = "the owner id must be a positive Telegram user id"
            raise ValueError(msg)
        self._url = f"{API_ROOT}/bot{token}/{METHOD}"
        self._owner = owner_id
        self._clock = clock
        self._ledger = ledger
        self._transport = transport
        self._timeout = timeout
        self._per_hour = per_hour
        self._min_gap = min_gap
        self._sent_at: deque[float] = deque()
        self._memory: dict[str, float] = {}
        self.suppressed = 0
        quiet_http_logs()

    async def alert(
        self, key: str, text: str, *, critical: bool = False, dedup: float | None = None
    ) -> AlertResult:
        now = self._clock.time()
        window = DEFAULT_DEDUP if dedup is None else dedup
        last = await self._last(key)
        if last is not None and 0 <= now - last < window:
            return AlertResult.DUPLICATE
        while self._sent_at and now - self._sent_at[0] >= HOUR:
            self._sent_at.popleft()
        if not critical and len(self._sent_at) >= self._per_hour:
            self.suppressed += 1
            log.warning("alert %s not sent: %d alerts in the last hour", key, self._per_hour)
            return AlertResult.RATE_LIMITED
        if self._sent_at and now - self._sent_at[-1] < self._min_gap:
            await self._clock.sleep(self._min_gap - (now - self._sent_at[-1]))
        body = self._compose(text)
        ok = await self._post(body)
        if not ok:
            return AlertResult.FAILED
        sent_at = self._clock.time()
        self._sent_at.append(sent_at)
        await self._remember(key, sent_at)
        return AlertResult.SENT

    def _compose(self, text: str) -> str:
        suffix = f"\n({self.suppressed} earlier alerts were not sent)" if self.suppressed else ""
        self.suppressed = 0
        full = PREFIX + text.strip() + suffix
        return full if len(full) <= MAX_TEXT else full[: MAX_TEXT - 1] + "…"

    async def _post(self, text: str) -> bool:
        payload = {"chat_id": self._owner, "text": text, "disable_web_page_preview": True}
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = await client.post(self._url, json=payload)
            data: Any = response.json() if response.content else {}
        except (httpx.HTTPError, ValueError) as exc:
            # The exception's message may contain the URL, and so the token: type only.
            log.warning("alert not sent: %s", type(exc).__name__)
            return False
        if response.status_code != 200 or not (isinstance(data, dict) and data.get("ok")):
            log.warning("alert not sent: Telegram answered %d", response.status_code)
            return False
        return True

    async def _last(self, key: str) -> float | None:
        if key in self._memory:
            return self._memory[key]
        if self._ledger is None:
            return None
        raw = await self._ledger.kv_get(KV_PREFIX + key)
        try:
            return None if raw is None else float(json.loads(raw))
        except (ValueError, TypeError):
            return None

    async def _remember(self, key: str, at: float) -> None:
        self._memory[key] = at
        if self._ledger is not None:
            await self._ledger.kv_set(KV_PREFIX + key, json.dumps(at))


# -- the texts -------------------------------------------------------------------------------
# Built from states, reasons, ids and group names only: never from a post (invariant 8).


def _when(epoch: float | None, zone: dt.tzinfo | None) -> str:
    if epoch is None:
        return "until you re-plan"
    return dt.datetime.fromtimestamp(epoch, zone or dt.UTC).strftime("%a %d %b %H:%M")


def block_opened(
    block_id: int,
    state: str,
    reason: str,
    until: float | None,
    *,
    no_probe: bool = False,
    zone: dt.tzinfo | None = None,
) -> str:
    lines = [
        f"⛔ BLOCKED ({state}), block #{block_id}: {reason}",
        "Chrome is closed; nothing will touch Facebook.",
        f"Earliest resume: {_when(until, zone)} — then `fbcollector resume {block_id}`.",
    ]
    if no_probe:
        lines.append("advanced_protection: confirm the lock is lifted (--lock-lifted) first.")
    return "\n".join(lines)


def probe_result(block_id: int, ok: bool, detail: str) -> str:
    if ok:
        return f"✅ Block #{block_id} lifted after a clean probe. Cautious pacing for a while."
    return f"⛔ The resume probe of block #{block_id} failed: {detail}"


def group_event(kind: str, group_id: str, name: str | None, detail: str) -> str:
    fixes = {
        "group_lost": "The group is paused. Check the membership by hand in `fbcollector login`.",
        "not_followed": "Follow the group by hand in `fbcollector login`; until then only the "
        "backstop reads it.",
        "sort_ignored": "The group did not honour CHRONOLOGICAL; it relies on the feed.",
        "left": "The account is no longer a member; the group is no longer read.",
        "unverified": "Missing from the joined-groups list; the group is not read until a "
        "sync lists it again.",
    }
    label = f"{name} ({group_id})" if name else group_id
    return f"⚠️ {kind.upper()}: {label} — {detail}\n{fixes.get(kind, '')}".strip()


def simple(kind: str, detail: str, *, hint: str = "") -> str:
    return f"⚠️ {kind.upper()}: {detail}" + (f"\n{hint}" if hint else "")


def reasons_text(reasons: Sequence[str]) -> str:
    return "; ".join(reasons)[:300]
