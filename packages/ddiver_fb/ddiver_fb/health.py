"""The container's health: the bot is connected and the ingest server is up. Nothing else.

``ddiver-fb health`` runs in a separate process (Docker's HEALTHCHECK), so the running brain
writes what it knows to a small JSON file every thirty seconds and the check reads it back —
the same arrangement as ddiver's heartbeat, with the brain's own two facts in it.

The collector's condition is deliberately absent. A blocked, offline or logged-out collector is
reported to the owner and shown in /status; restarting the brain would fix none of it, and a
health check that failed on it would turn every Facebook warning into a restart loop of the one
process that can tell the owner about the warning.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from ddiver_core.timeutil import utcnow

MAX_AGE_SECONDS: Final = 120
MAX_PING_AGE_SECONDS: Final = 300


@dataclass(frozen=True, slots=True)
class BrainBeat:
    ts: str
    bot_connected: bool
    ingest_up: bool
    bot_ping_ok_at: str | None = None
    queue: int = 0


@dataclass(frozen=True, slots=True)
class HealthReport:
    healthy: bool
    reason: str


def _parse(text: str | None) -> dt.datetime | None:
    if not text:
        return None
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


def write_beat(path: Path, beat: BrainBeat) -> None:
    """Atomically, so a concurrent check never reads half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(beat)))
    tmp.replace(path)


def read_beat(path: Path) -> BrainBeat | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("ts"), str):
        return None
    ping = data.get("bot_ping_ok_at")
    return BrainBeat(
        ts=data["ts"],
        bot_connected=data.get("bot_connected") is True,
        ingest_up=data.get("ingest_up") is True,
        bot_ping_ok_at=ping if isinstance(ping, str) else None,
        queue=data["queue"] if isinstance(data.get("queue"), int) else 0,
    )


def check(
    path: Path,
    *,
    now: dt.datetime | None = None,
    max_age: float = MAX_AGE_SECONDS,
    max_ping_age: float = MAX_PING_AGE_SECONDS,
) -> HealthReport:
    moment = now or utcnow()
    beat = read_beat(path)
    written = _parse(beat.ts) if beat else None
    if beat is None or written is None:
        return HealthReport(False, "no heartbeat file")
    age = (moment - written).total_seconds()
    if age < 0 or age > max_age:
        return HealthReport(False, f"heartbeat is {age:.0f}s old")
    if not beat.ingest_up:
        return HealthReport(False, "the ingest server is not serving")
    if not beat.bot_connected:
        return HealthReport(False, "the bot is not connected to Telegram")
    pinged = _parse(beat.bot_ping_ok_at)
    if pinged is not None and (moment - pinged).total_seconds() > max_ping_age:
        return HealthReport(False, "the bot has not answered a ping for a while")
    return HealthReport(True, f"ok ({age:.0f}s)")
