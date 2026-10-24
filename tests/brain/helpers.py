"""Builders and fakes for the brain's tests (fixtures live in ``conftest.py``).

Everything runs offline: the bot is ddiver-core's ``FakeBotClient`` or an ``Outbox`` standing in
for ``tell``, the collector is a test calling the API, and time is ``FakeClock``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ddiver_core_testing import FakeClock
from ddiver_fb.config import FbSettings
from ddiver_fb.groups import GroupBook
from fbwire import (
    Author,
    BlockInfo,
    CollectorState,
    GroupInfo,
    GroupPrivacy,
    GroupsSnapshot,
    Item,
    Profile,
    Status,
    Via,
)

OWNER = 111
MOD = 222
STRANGER = 333
CID = "home-1"
TOKEN = "k" * 40
# 2026-09-22 12:00 UTC is 15:00 in Kyiv: daytime, which most tests want.
T0 = dt.datetime(2026, 9, 22, 12, 0, 0, tzinfo=dt.UTC)
EPOCH0 = int(T0.timestamp())
G1, G2, G3 = 1_234_567_890, 2_345_678_901, 3_456_789_012


def epoch(clock: FakeClock) -> int:
    return int(clock().timestamp())


async def until(predicate: Callable[[], bool]) -> None:
    """Yield to the loop until ``predicate`` holds: a bounded number of turns, no time."""
    for _ in range(10_000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("never got there")


@dataclass
class Invalidations:
    calls: list[int | None] = field(default_factory=list)

    async def __call__(self, chat_id: int | None = None) -> None:
        self.calls.append(chat_id)


@dataclass
class Outbox:
    """Messages the brain sent through ``tell`` (owner alerts, access notices)."""

    sent: list[tuple[int, str]] = field(default_factory=list)
    fail: bool = False

    async def __call__(self, user_id: int, text: str) -> bool:
        if self.fail:
            return False
        self.sent.append((user_id, text))
        return True

    def to(self, user_id: int) -> list[str]:
        return [text for uid, text in self.sent if uid == user_id]


class Nonces:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"nonce-{self.n:04d}"


# -- wire builders ---------------------------------------------------------------------------


def group_url(gid: int) -> str:
    return f"https://www.facebook.com/groups/{gid}/"


def info(gid: int, name: str | None = None, **kw: Any) -> GroupInfo:
    return GroupInfo(id=str(gid), name=name or f"Group {gid}", url=group_url(gid), **kw)


def snapshot(
    *groups: GroupInfo, complete: bool = True, taken_at: int = EPOCH0, collector_id: str = CID
) -> GroupsSnapshot:
    listed = sum(1 for g in groups if g.listed)
    return GroupsSnapshot(
        collector_id=collector_id,
        taken_at=taken_at,
        complete=complete,
        header_count=listed if complete else None,
        groups=groups,
    )


def item(
    gid: int = G1,
    pid: int = 9_876_543_210_987_654,
    text: str = "Flat for rent in Varna, 400 EUR",
    *,
    created_at: int = EPOCH0 - 120,
    author: Author | None = None,
    **kw: Any,
) -> Item:
    data: dict[str, Any] = {
        "collector_id": CID,
        "seq": 1,
        "group_id": str(gid),
        "post_id": str(pid),
        "url": f"https://www.facebook.com/groups/{gid}/posts/{pid}/",
        "group_name": f"Group {gid}",
        "group_url": group_url(gid),
        "group_privacy": GroupPrivacy.PRIVATE,
        "author": author
        or Author(
            id="100000000000001",
            name="Olena K.",
            url="https://www.facebook.com/profile.php?id=100000000000001",
        ),
        "created_at": created_at,
        "first_seen_at": created_at + 60,
        "via": Via.FEED,
        "text": text,
    }
    data.update(kw)
    return Item.model_validate(data)


def status(
    state: CollectorState = CollectorState.OK,
    *,
    sent_at: int = EPOCH0,
    collector_id: str = CID,
    block: BlockInfo | None = None,
    profile: Profile = Profile.NORMAL,
    **kw: Any,
) -> Status:
    return Status(
        collector_id=collector_id,
        sent_at=sent_at,
        state=state,
        profile=profile,
        block=block,
        **kw,
    )


def block(
    block_id: int = 17,
    *,
    until: int | None = EPOCH0 - 60,
    reason: str = "suspect automated behaviour",
    no_probe: bool = False,
    rung: int = 0,
) -> BlockInfo:
    return BlockInfo(
        id=block_id,
        opened_at=EPOCH0 - 6 * 3600,
        until=until,
        reason=reason,
        rung=rung,
        no_probe=no_probe,
    )


async def seed_groups(book: GroupBook, *ids: int, **kw: Any) -> None:
    await book.apply_snapshot(snapshot(*(info(gid, **kw) for gid in ids)), now=EPOCH0)


def settings_for(tmp_path: Path, **kw: Any) -> FbSettings:
    values: dict[str, Any] = {
        "api_id": 1,
        "api_hash": "0" * 32,
        "bot_token": "123456789:" + "A" * 35,
        "owner_id": OWNER,
        "ingest_token": TOKEN,
        "data_dir": tmp_path,
        "ingest_port": 0,
    }
    values.update(kw)
    return FbSettings(_env_file=None, **values)  # type: ignore[call-arg]


class FakeTelethon:
    """The bot client as ``BrainApp`` touches it: connect, start, handlers, requests, a sender."""

    def __init__(self) -> None:
        self.disconnected = False
        self.calls: list[str] = []
        self._sender: Any = None
        self.handlers: list[tuple[Any, Any]] = []
        self.requests: list[Any] = []
        self.fail_requests = False

    async def connect(self) -> None:
        self.calls.append("connect")

    async def start(self, *, bot_token: str) -> None:
        self.calls.append(f"start {bot_token.split(':', maxsplit=1)[0]}")

    async def disconnect(self) -> None:
        self.disconnected = True

    def add_event_handler(self, callback: Any, event: Any) -> None:
        self.handlers.append((callback, event))

    async def __call__(self, request: Any) -> Any:
        if self.fail_requests:
            raise ConnectionError("down")
        self.requests.append(request)
        return None
