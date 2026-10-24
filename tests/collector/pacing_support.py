"""Small builders shared by the pacing tests: settings, wall-clock times, verdicts, events.

Nothing here touches the network, the system's commands or Facebook.
"""

from __future__ import annotations

import datetime as dt
import os
import zoneinfo
from typing import Any

from fbcollector.config import Settings
from fbcollector.detectors import Verdict
from fbcollector.model import GroupMeta, PageState
from fbcollector.store import Store
from fbwire.models import EventKind, MonitoredGroup

ZONE = "Europe/Sofia"
DAY = 86400.0
HOUR = 3600.0


def make_settings(**overrides: Any) -> Settings:
    """Settings with the design's defaults, whatever ``FBC_*`` the developer's shell exports."""
    saved = {k: v for k, v in os.environ.items() if k.upper().startswith("FBC_")}
    for key in saved:
        del os.environ[key]
    try:
        return Settings(**{"timezone": ZONE, **overrides})
    finally:
        os.environ.update(saved)


def local(y: int, m: int, d: int, hh: int = 0, mm: int = 0, *, zone: str = ZONE) -> float:
    """Epoch seconds of a wall-clock time in ``zone`` (the first one, on an ambiguous hour)."""
    return dt.datetime(y, m, d, hh, mm, tzinfo=zoneinfo.ZoneInfo(zone)).timestamp()


def verdict(state: PageState, *reasons: str, advanced: bool = False) -> Verdict:
    return Verdict(state=state, reasons=reasons, advanced_protection=advanced, excerpt=state.value)


class Events:
    """Collects the ladder's events as the store's queue would."""

    def __init__(self) -> None:
        self.seen: list[tuple[EventKind, int | None, str | None]] = []

    async def __call__(
        self, kind: EventKind, *, block_id: int | None = None, detail: str | None = None
    ) -> None:
        self.seen.append((kind, block_id, detail))


async def seed_groups(store: Store, ids: list[str], *, monitored: bool = True) -> None:
    for gid in ids:
        await store.upsert_group(
            GroupMeta(id=gid, name=f"g{gid}", join_state="MEMBER"), synced=True
        )
    if monitored:
        await store.set_monitored([MonitoredGroup(group_id=g) for g in ids])
