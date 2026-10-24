"""Fixtures for the brain: a real SQLite file, a clock that moves by hand, recorded messages."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from ddiver_core import i18n
from ddiver_core.storage import Role, Storage
from ddiver_core_testing import FakeClock
from ddiver_fb import schema, strings
from ddiver_fb.control import ControlDesk
from ddiver_fb.groups import GroupBook
from ddiver_fb.monitor import CollectorMonitor, MonitorSettings
from ddiver_fb.store import FbStore
from tests.brain.helpers import MOD, OWNER, T0, Invalidations, Nonces, Outbox


@pytest.fixture(autouse=True)
def no_ai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's shell may export the AI rule's key; no test may use it (or call out)."""
    for name in ("OPENROUTER_API_KEY", "AI_MODEL", "AI_DAILY_CALLS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def overlays() -> Iterator[None]:
    strings.install()
    yield
    i18n.clear_overlays()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
async def storage(tmp_path: Path, clock: FakeClock) -> AsyncIterator[Storage]:
    s = Storage(
        tmp_path / "fb.sqlite",
        owner_id=OWNER,
        clock=clock,
        tracks={schema.TRACK: schema.MIGRATIONS},
    )
    await s.open()
    await s.ensure_user(OWNER)
    await s.update_user(OWNER, lang="en", tz="Europe/Kyiv")
    await s.ensure_user(MOD)
    await s.update_user(MOD, role=Role.ALLOWED, lang="uk", tz="Europe/Kyiv")
    yield s
    await s.close()


@pytest.fixture
async def fb(storage: Storage) -> AsyncIterator[FbStore]:
    store = FbStore(storage)
    yield store
    await store.close()


@pytest.fixture
def told() -> Outbox:
    return Outbox()


@pytest.fixture
def invalidations() -> Invalidations:
    return Invalidations()


@pytest.fixture
def desk(storage: Storage, fb: FbStore, clock: FakeClock) -> ControlDesk:
    return ControlDesk(storage, fb, clock=clock, nonce=Nonces())


@pytest.fixture
def book(
    storage: Storage, fb: FbStore, told: Outbox, invalidations: Invalidations, clock: FakeClock
) -> GroupBook:
    return GroupBook(storage, fb, tell=told, invalidate=invalidations, clock=clock)


@pytest.fixture
async def monitor(
    storage: Storage,
    *,
    fb: FbStore,
    desk: ControlDesk,
    book: GroupBook,
    told: Outbox,
    clock: FakeClock,
) -> AsyncIterator[CollectorMonitor]:
    watcher = CollectorMonitor(
        storage,
        fb,
        desk,
        book,
        owner_id=OWNER,
        tell=told,
        clock=clock,
        settings=MonitorSettings(tz="Europe/Kyiv"),
    )
    yield watcher
    await watcher.close()  # before the database closes: its sender must not outlive it
