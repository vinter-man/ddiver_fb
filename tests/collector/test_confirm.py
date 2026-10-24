"""The owner's word on group membership (phase 0): the file, the rules, the page's last word."""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fbcollector import paths
from fbcollector.__main__ import EXIT_CONFIG, EXIT_NO_TTY, EXIT_OK, EXIT_USAGE, main
from fbcollector.clock import FakeClock
from fbcollector.confirm import (
    FLAG_CONFIRMED,
    KV_CONFIRMATIONS,
    Confirmation,
    ConfirmationState,
    GroupRefError,
    _check_edit,
    apply_confirmations,
    confirm_in_file,
    confirm_text,
    describe,
    load_confirmations,
    note_verified,
    parse_group_ref,
    withdraw,
)
from fbcollector.control import MonitoredEntry, MonitoredFileError, load_monitored, parse_monitored
from fbcollector.detectors import DetectorInput, classify, group_url
from fbcollector.joins import JoinedGroups
from fbcollector.ladder import Ladder
from fbcollector.model import GroupMeta, GroupState, PageState, Surface
from fbcollector.parser import PageInfo, ReadParser
from fbcollector.paths import CommandResult
from fbcollector.reader import Reader, ReaderConfig, page_facts
from fbcollector.runner import Ports, status_lines
from fbcollector.scheduler import FLAG_PAUSED, ActionKind
from fbcollector.store import KV_LAST_COMPLETE_SYNC, Store
from fbcollector.sync import apply_sync, local_monitored
from fbwire.models import MonitoredGroup

from .reader_support import (
    T0,
    FakeDriver,
    Recorder,
    StubScheduler,
    grant,
    group_body,
    group_document,
    html,
    nav,
    post,
    story,
    user_define,
)

START = ("390000000000001", "150000000000002", "1030000000000003", "200000000000004")
A, B, C, D = START
DONE = PageInfo("joined_groups", False, None)
UTC = dt.UTC


def joined(*ids: str) -> JoinedGroups:
    return JoinedGroups(tuple(GroupMeta(id=g, name=f"n{g[-1]}", join_state="MEMBER") for g in ids))


def stamp(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry(gid: str, *, at: float | None = None, confirmed: bool = True) -> MonitoredEntry:
    return MonitoredEntry(MonitoredGroup(group_id=gid), confirmed_member=confirmed, confirmed_at=at)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=T0)


@pytest.fixture
async def store(tmp_path: Path, clock: FakeClock) -> AsyncIterator[Store]:
    async with Store(tmp_path / "state.sqlite", clock=clock) as opened:
        yield opened


async def state_of(store: Store, gid: str) -> tuple[GroupState, frozenset[str]] | None:
    row = await store.get_group(gid)
    return None if row is None else (row.state, row.flags)


# -- monitored.toml ---------------------------------------------------------------------------


def test_the_file_carries_confirmations(tmp_path: Path) -> None:
    path = tmp_path / "monitored.toml"
    path.write_text(
        f'[[group]]\nid = "{A}"\nconfirmed_member = true\n'
        f'[[group]]\nid = "{B}"\nconfirmed_member = true\n'
        "confirmed_at = 2026-09-25T09:00:00+03:00\n"
        f'[[group]]\nid = "{C}"\nlistings = true\nconfirmed_member = false\n'
        f"[[group]]\nid = {D}\n"
    )
    entries = load_monitored(path)
    assert entries is not None
    assert [(e.group_id, e.confirmed_member) for e in entries] == [
        (A, True),
        (B, True),
        (C, False),
        (D, False),
    ]
    assert entries[0].confirmed_at is None
    assert entries[1].confirmed_at == dt.datetime(2026, 9, 25, 6, 0, tzinfo=UTC).timestamp()
    assert entries[2].group.listings


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[[group]]\nid = "1"\nconfirmed_member = "true"\n', "true or false"),
        ('[[group]]\nid = "1"\nconfirmed_member = 1\n', "true or false"),
        ('[[group]]\nid = "1"\nconfirmed_at = 2026-09-25T09:00:00\n', "offset"),
        ('[[group]]\nid = "1"\nconfirmed_at = 2026-09-25\n', "offset"),
        ('[[group]]\nid = "1"\nconfirmed_at = "2026-09-25T09:00:00Z"\n', "offset"),
    ],
)
def test_bad_confirmations_are_refused(text: str, message: str) -> None:
    with pytest.raises(MonitoredFileError, match=message):
        parse_monitored(text.encode())


def test_the_example_file_is_exactly_the_four_start_groups() -> None:
    example = Path(__file__).resolve().parents[2] / "docs" / "monitored.example.toml"
    entries = load_monitored(example)
    assert entries is not None
    assert [e.group_id for e in entries] == list(START)
    assert all(e.confirmed_member for e in entries)
    assert all(not e.group.listings and e.group.tier == 1 for e in entries)


# -- the rules --------------------------------------------------------------------------------


async def test_a_confirmation_makes_a_member_before_any_sync(store: Store, tmp_path: Path) -> None:
    path = tmp_path / "monitored.toml"
    path.write_text(f'[[group]]\nid = "{A}"\nconfirmed_member = true\n[[group]]\nid = "{B}"\n')
    monitored = await local_monitored(store, path)
    assert [m.group_id for m in monitored] == [A]  # B is neither synced nor confirmed
    assert await state_of(store, A) == (GroupState.MEMBER, frozenset({FLAG_CONFIRMED}))
    assert await store.get_group(B) is None
    records = await load_confirmations(store)
    assert records[A].state is ConfirmationState.ACTIVE
    assert records[A].since == T0  # undated: counted from the first read
    before = await store.kv_get(KV_CONFIRMATIONS)
    again = await apply_confirmations(store, load_monitored(path) or ())
    assert again.applied == again.overridden == again.withdrawn == ()
    assert await store.kv_get(KV_CONFIRMATIONS) == before


async def test_a_complete_sync_overrides_older_confirmations(
    store: Store, clock: FakeClock, tmp_path: Path
) -> None:
    path = tmp_path / "monitored.toml"
    path.write_text("".join(f'[[group]]\nid = "{g}"\nconfirmed_member = true\n' for g in (A, B)))
    await local_monitored(store, path)
    clock.advance(3600)
    sync = await apply_sync(store, joined(A), header_count=1, page_info=DONE)
    assert sync.complete
    assert await store.kv_get(KV_LAST_COMPLETE_SYNC) == repr(T0 + 3600)
    # A is a member by the sync's word now; B, missing from it, is unverified.
    assert await state_of(store, A) == (GroupState.MEMBER, frozenset())
    assert await state_of(store, B) == (GroupState.UNVERIFIED, frozenset())
    report = await apply_confirmations(store, load_monitored(path) or ())
    assert set(report.overridden) == {A, B}
    assert report.applied == ()
    monitored = await local_monitored(store, path)
    assert [m.group_id for m in monitored] == [A]
    assert (await load_confirmations(store))[B].state is ConfirmationState.OVERRIDDEN


async def test_a_confirmation_newer_than_the_sync_counts_until_the_next_one(
    store: Store, clock: FakeClock, tmp_path: Path
) -> None:
    await apply_sync(store, joined(A), header_count=1, page_info=DONE)
    clock.advance(600)
    path = tmp_path / "monitored.toml"
    old, new = T0 - 60, T0 + 300
    path.write_text(
        f'[[group]]\nid = "{B}"\nconfirmed_member = true\nconfirmed_at = {stamp(old)}\n'
        f'[[group]]\nid = "{C}"\nconfirmed_member = true\nconfirmed_at = {stamp(new)}\n'
    )
    report = await apply_confirmations(store, load_monitored(path) or ())
    assert report.overridden == (B,)  # dated before the sync, which already spoke
    assert report.applied == (C,)
    assert await store.get_group(B) is None
    assert await state_of(store, C) == (GroupState.MEMBER, frozenset({FLAG_CONFIRMED}))
    # An incomplete sync that does not list C changes nothing.
    await apply_sync(store, joined(A), header_count=9, page_info=DONE)
    assert await state_of(store, C) == (GroupState.MEMBER, frozenset({FLAG_CONFIRMED}))
    clock.advance(600)
    await apply_sync(store, joined(A), header_count=1, page_info=DONE)
    assert await state_of(store, C) == (GroupState.UNVERIFIED, frozenset())


async def test_a_future_date_counts_from_the_first_read(store: Store, clock: FakeClock) -> None:
    await apply_confirmations(store, [entry(A, at=T0 + 86400)])
    record = (await load_confirmations(store))[A]
    assert record.since == T0
    clock.advance(60)
    await apply_sync(store, joined(A), header_count=1, page_info=DONE)
    report = await apply_confirmations(store, [entry(A, at=T0 + 86400)])
    assert report.overridden == (A,)


async def test_taking_the_word_back_unverifies_what_rested_on_it(store: Store) -> None:
    await apply_confirmations(store, [entry(A), entry(B), entry(C)])
    # A's entry is gone, C's says false now, B's stays.
    report = await apply_confirmations(store, [entry(B), entry(C, confirmed=False)])
    assert set(report.withdrawn) == {A, C}
    assert await state_of(store, A) == (GroupState.UNVERIFIED, frozenset())
    assert await state_of(store, C) == (GroupState.UNVERIFIED, frozenset())
    assert await state_of(store, B) == (GroupState.MEMBER, frozenset({FLAG_CONFIRMED}))
    records = await load_confirmations(store)
    assert records[A].reason == "taken back"
    # The same (undated) word again is spent; a dated one is a new confirmation.
    assert (await apply_confirmations(store, [entry(A)])).applied == ()
    assert (await apply_confirmations(store, [entry(A, at=T0)])).applied == (A,)


async def test_the_file_gone_takes_every_word_back(store: Store, tmp_path: Path) -> None:
    path = tmp_path / "monitored.toml"
    path.write_text(f'[[group]]\nid = "{A}"\nconfirmed_member = true\n')
    await local_monitored(store, path)
    path.unlink()
    assert await local_monitored(store, path) == []
    assert await state_of(store, A) == (GroupState.UNVERIFIED, frozenset())


async def test_withdrawal_is_spent_and_a_new_date_lifts_the_pause(
    store: Store, clock: FakeClock
) -> None:
    await apply_confirmations(store, [entry(A)])
    # What the reader does on GROUP_LOST: lost and paused first, then the word withdrawn.
    await store.upsert_group(GroupMeta(id=A), state=GroupState.LOST)
    await store.set_group_flags(A, {FLAG_CONFIRMED, FLAG_PAUSED})
    assert await withdraw(store, A, reason="its page did not say MEMBER")
    assert not await withdraw(store, A, reason="again")  # nothing left to withdraw
    assert await state_of(store, A) == (GroupState.LOST, frozenset({FLAG_PAUSED}))
    spent = await store.kv_get(KV_CONFIRMATIONS)
    clock.advance(60)
    report = await apply_confirmations(store, [entry(A)])
    assert report.applied == report.withdrawn == report.overridden == ()
    assert await store.kv_get(KV_CONFIRMATIONS) == spent  # nothing rewritten, nothing re-ended
    assert await state_of(store, A) == (GroupState.LOST, frozenset({FLAG_PAUSED}))
    clock.advance(3600)
    report = await apply_confirmations(store, [entry(A, at=T0 + 3000)])
    assert report.applied == (A,)
    assert await state_of(store, A) == (GroupState.MEMBER, frozenset({FLAG_CONFIRMED}))


async def test_a_lost_group_whose_word_was_not_withdrawn_is_reconciled(store: Store) -> None:
    """A crash between the lost verdict and the withdrawal: the next pass finishes it."""
    await apply_confirmations(store, [entry(A)])
    await store.upsert_group(GroupMeta(id=A), state=GroupState.LOST)
    await store.set_group_flags(A, {FLAG_CONFIRMED, FLAG_PAUSED})
    report = await apply_confirmations(store, [entry(A)])
    assert report.withdrawn == (A,)
    assert await state_of(store, A) == (GroupState.LOST, frozenset({FLAG_PAUSED}))


async def test_verification_is_noted_once_and_only_for_active_words(
    store: Store, clock: FakeClock
) -> None:
    await note_verified(store, A)  # nothing to verify
    assert await load_confirmations(store) == {}
    await apply_confirmations(store, [entry(A)])
    clock.advance(10)
    await note_verified(store, A)
    clock.advance(10)
    await note_verified(store, A)
    assert (await load_confirmations(store))[A].verified_at == T0 + 10


def test_records_round_trip_and_describe() -> None:
    zone = dt.UTC
    record = Confirmation(confirmed_at=T0 - 100, seen_at=T0, verified_at=T0 + 60)
    assert Confirmation.from_json(record.to_json()) == record
    assert "active, verified on its page" in describe(A, record, zone)
    fresh = Confirmation(confirmed_at=None, seen_at=T0)
    assert "not yet verified" in describe(A, fresh, zone)
    ended = Confirmation(
        None, T0, ConfirmationState.WITHDRAWN, ended_at=T0 + 5, reason="its page said so"
    )
    assert "withdrawn" in describe(A, ended, zone)
    assert "its page said so" in describe(A, ended, zone)


async def test_edges_of_taking_back_and_re_dating(store: Store, clock: FakeClock) -> None:
    await apply_confirmations(store, [entry(A), entry(B)])
    # Re-dated while active: a new confirmation, the group already as it should be.
    clock.advance(5)
    assert (await apply_confirmations(store, [entry(A, at=T0), entry(B)])).applied == (A,)
    assert await state_of(store, A) == (GroupState.MEMBER, frozenset({FLAG_CONFIRMED}))
    # B lost its flag some other way (its record still active): taking it back changes nothing
    # more than the record; A is lost (flagged) when the word is taken back: it stays lost.
    await store.set_group_flags(B, set())
    await store.upsert_group(GroupMeta(id=A), state=GroupState.LOST)
    report = await apply_confirmations(store, [])
    assert set(report.withdrawn) == {A, B}
    assert await state_of(store, A) == (GroupState.LOST, frozenset())
    assert await state_of(store, B) == (GroupState.MEMBER, frozenset())
    ended = Confirmation(None, T0, ConfirmationState.OVERRIDDEN)
    assert describe(A, ended, dt.UTC).endswith("overridden - (-)")


def test_the_edit_check_refuses_any_other_outcome() -> None:
    old = [entry(A, confirmed=False), entry(B, confirmed=False)]
    at = AT.replace(microsecond=0)
    good = [entry(A, at=at.timestamp()), entry(B, confirmed=False)]
    _check_edit(old, good, [A], AT)
    with pytest.raises(MonitoredFileError, match="order or number"):
        _check_edit(old, list(reversed(good)), [A], AT)
    with pytest.raises(MonitoredFileError, match=f"group {A} would not"):
        _check_edit(old, [entry(A, at=at.timestamp() - 1), good[1]], [A], AT)
    with pytest.raises(MonitoredFileError, match=f"group {B} would not"):
        _check_edit(old, [good[0], entry(B)], [A], AT)
    listed = MonitoredEntry(MonitoredGroup(group_id=A, listings=True), True, at.timestamp())
    with pytest.raises(MonitoredFileError, match=f"group {A} would not"):
        _check_edit(old, [listed, good[1]], [A], AT)


async def test_unreadable_records_count_as_none(store: Store) -> None:
    await store.kv_set_json(KV_CONFIRMATIONS, ["not", "a", "map"])
    assert await load_confirmations(store) == {}
    await store.kv_set_json(KV_CONFIRMATIONS, {A: "x", B: {"seen_at": 1.0}})
    assert set(await load_confirmations(store)) == {B}


# -- the page has the last word ---------------------------------------------------------------


def verdict_for(document: bytes, *, proof: bool, posts: bool = True) -> Any:
    parser = ReadParser(Surface.GROUP, group_id=A)
    bodies = [group_body(story(post(1), A, T0))] if posts else []
    step = nav(group_url(A), document, *bodies)
    parser.parse(step)
    return classify(
        DetectorInput(
            surface=Surface.GROUP,
            step=step,
            group_id=A,
            cookies=frozenset({"c_user", "xs"}),
            facts=page_facts(parser),
            needs_member_proof=proof,
        )
    )


def test_a_page_that_says_nothing_about_membership_proves_nothing() -> None:
    silent = group_document(A, join_state=None)
    assert verdict_for(silent, proof=False).state is PageState.OK
    lost = verdict_for(silent, proof=True)
    assert lost.state is PageState.GROUP_LOST
    assert lost.reasons == ("join_state:unstated",)
    assert verdict_for(group_document(A), proof=True).state is PageState.OK
    refused = verdict_for(group_document(A, join_state="CAN_REQUEST"), proof=True)
    assert refused.reasons == ("join_state:CAN_REQUEST",)


BLANK = html({"define": [user_define()]})
"""Logged in, English, the group's own URL — and nothing else: no posts, no group header."""


@pytest.mark.parametrize(
    "document", [BLANK, group_document(A, join_state=None)], ids=["blank", "header only"]
)
def test_a_page_that_shows_nothing_stays_unknown_for_a_confirmed_group(document: bytes) -> None:
    # Silence about membership proves nothing lost when the page shows nothing at all: an empty
    # shell is UNKNOWN (DESIGN: before GROUP_LOST), with or without the owner's word.
    for proof in (False, True):
        verdict = verdict_for(document, proof=proof, posts=False)
        assert verdict.state is PageState.UNKNOWN, proof
        assert "signature:content" in verdict.reasons
    # A real signal still explains an empty page.
    refused = verdict_for(group_document(A, join_state="CAN_REQUEST"), proof=True, posts=False)
    assert refused.state is PageState.GROUP_LOST
    assert refused.reasons == ("join_state:CAN_REQUEST",)


@pytest.fixture
async def reading(
    store: Store, clock: FakeClock, tmp_path: Path
) -> tuple[Reader, FakeDriver, Recorder]:
    (tmp_path / "raw").mkdir()
    (tmp_path / "shots").mkdir()
    path = tmp_path / "monitored.toml"
    path.write_text("".join(f'[[group]]\nid = "{g}"\nconfirmed_member = true\n' for g in (A, B)))
    await local_monitored(store, path)
    clock.advance(600)
    events = Recorder()
    driver = FakeDriver()
    reader = Reader(
        store=store,
        driver=driver,
        scheduler=StubScheduler(),
        ladder=Ladder(store, clock, events=events),
        events=events,
        clock=clock,
        config=ReaderConfig("home-1", tmp_path / "raw", tmp_path / "shots"),
    )
    return reader, driver, events


async def test_a_confirmed_group_is_read_and_verified_by_its_page(
    store: Store, reading: tuple[Reader, FakeDriver, Recorder]
) -> None:
    reader, driver, events = reading
    driver.navigations.append(
        nav(group_url(A), group_document(A), group_body(story(post(1), A, T0 + 300)))
    )
    result = await reader.read(grant(ActionKind.BACKSTOP, group=A, steps=0))
    assert result.page_state is PageState.OK
    assert (await load_confirmations(store))[A].verified_at is not None
    rows = await store.next_batch("home-1")
    assert [r.post_id for r in rows] == [post(1)]  # its posts are items like any member's
    assert events.kinds() == []
    row = await store.get_group(A)
    assert row is not None
    assert row.name == "Flats"  # known by id alone until its page named it


def page_of(group: str, join_state: str | None) -> Any:
    """A group page that does not say MEMBER: silent (with posts, or it would be an empty
    shell, UNKNOWN) or a stated other state (no posts needed: the state explains the page)."""
    bodies = [group_body(story(post(9), group, T0))] if join_state is None else []
    return nav(group_url(group), group_document(group, join_state=join_state), *bodies)


@pytest.mark.parametrize("join_state", [None, "NOT_MEMBER"])
async def test_a_page_that_does_not_say_member_withdraws_the_word(
    store: Store, reading: tuple[Reader, FakeDriver, Recorder], join_state: str | None
) -> None:
    reader, driver, events = reading
    driver.navigations.append(page_of(A, join_state))
    result = await reader.read(grant(ActionKind.BACKSTOP, group=A))
    assert result.page_state is PageState.GROUP_LOST
    assert await state_of(store, A) == (GroupState.LOST, frozenset({FLAG_PAUSED}))
    record = (await load_confirmations(store))[A]
    assert record.state is ConfirmationState.WITHDRAWN
    assert events.kinds() == ["group_lost"]
    detail = events.events[0][1]["detail"]
    assert "confirmation is withdrawn" in detail
    assert f"groups confirm {A}" in detail
    assert await store.next_batch("home-1") == []
    # A second lost confirmed group within 24 h is a block, and its word is withdrawn too.
    driver.navigations.append(page_of(B, join_state))
    second = await reader.read(grant(ActionKind.BACKSTOP, group=B))
    assert second.close_chrome
    assert await store.current_block() is not None
    assert (await load_confirmations(store))[B].state is ConfirmationState.WITHDRAWN
    assert await state_of(store, B) == (GroupState.LOST, frozenset({FLAG_PAUSED}))


async def test_a_synced_member_needs_no_page_proof(
    store: Store, reading: tuple[Reader, FakeDriver, Recorder]
) -> None:
    reader, driver, _events = reading
    await apply_sync(store, joined(A, B), header_count=2, page_info=DONE)
    driver.navigations.append(
        nav(group_url(A), group_document(A, join_state=None), group_body(story(post(2), A, T0)))
    )
    result = await reader.read(grant(ActionKind.BACKSTOP, group=A, steps=0))
    assert result.page_state is PageState.OK


async def test_status_lists_the_confirmations(store: Store, tmp_path: Path) -> None:
    from fbcollector.config import Settings

    await apply_confirmations(store, [entry(A)])
    settings = Settings(timezone="Europe/Sofia", root=tmp_path)
    lines = await status_lines(settings, store, FakeClock(start=T0))
    assert "membership confirmed by hand (monitored.toml):" in lines
    assert any(line.startswith(f"  {A} confirmed") and "active" in line for line in lines)


# -- group references -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "group_id", "slug"),
    [
        (A, A, None),
        (f" {A} ", A, None),
        (f"https://www.facebook.com/groups/{A}/", A, None),
        (f"https://www.facebook.com/groups/{A}/posts/5/?ref=share", A, None),
        (f"facebook.com/groups/{A}", A, None),
        (f"https://m.facebook.com/groups/{A}/", A, None),
        ("https://www.facebook.com/groups/Jerseyask/", None, "Jerseyask"),
    ],
)
def test_group_refs(text: str, group_id: str | None, slug: str | None) -> None:
    ref = parse_group_ref(text)
    assert (ref.group_id, ref.slug) == (group_id, slug)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "abc",
        "https://example.com/groups/1/",
        "https://www.facebook.com/profile.php?id=4",
        "https://www.facebook.com/groups/",
        "https://www.facebook.com/groups/a%20b/",
    ],
)
def test_bad_group_refs(text: str) -> None:
    with pytest.raises(GroupRefError):
        parse_group_ref(text)


# -- editing the file -------------------------------------------------------------------------


AT = dt.datetime(2026, 9, 25, 7, 30, 12, 999, tzinfo=UTC)


def test_confirm_keeps_comments_and_changes_only_its_lines() -> None:
    text = (
        "# my groups\n"
        "[[group]]  # flats\n"
        f'id = "{A}"   # the big one\n'
        "confirmed_member = false\n"
        "listings = true\n"
        "confirmed_at = 2026-01-01T00:00:00Z\n"
        "\n"
        "[[group]]\n"
        f"id = {B}\n"
        "tier = 2\n"
    )
    result = confirm_text(text, [A, C, A], AT)
    assert result == (
        "# my groups\n"
        "[[group]]  # flats\n"
        f'id = "{A}"   # the big one\n'
        "confirmed_member = true\n"
        "confirmed_at = 2026-09-25T07:30:12Z\n"
        "listings = true\n"
        "\n"
        "[[group]]\n"
        f"id = {B}\n"
        "tier = 2\n"
        "\n"
        "[[group]]\n"
        f'id = "{C}"\n'
        "confirmed_member = true\n"
        "confirmed_at = 2026-09-25T07:30:12Z\n"
    )
    entries = parse_monitored(result.encode())
    assert [(e.group_id, e.confirmed_member) for e in entries] == [(A, True), (B, False), (C, True)]
    assert entries[1].group.tier == 2


def test_confirm_creates_a_file_with_a_header() -> None:
    result = confirm_text("", [A], AT)
    assert result.startswith("# Groups the collector reads in phase 0")
    assert [e.group_id for e in parse_monitored(result.encode())] == [A]


@pytest.mark.parametrize(
    "text",
    [
        f'group = [{{id = "{A}"}}]\n',  # an inline array: nothing to edit line by line
        f'[[group]]\n"id" = "{A}"\n',  # a quoted key the line editor does not look for
    ],
)
def test_a_shape_the_editor_cannot_change_is_refused(text: str) -> None:
    parse_monitored(text.encode())  # valid TOML, and a valid monitored file
    with pytest.raises(MonitoredFileError, match="cannot edit safely"):
        confirm_text(text, [A], AT)


def test_a_sub_table_ends_the_block_the_lines_go_into() -> None:
    text = f'[[group]]\nid = "{A}"\n[group.extra]\nx = 1\n'
    result = confirm_text(text, [A], AT)
    assert result == (
        f'[[group]]\nid = "{A}"\nconfirmed_member = true\nconfirmed_at = 2026-09-25T07:30:12Z\n'
        "[group.extra]\nx = 1\n"
    )


def test_confirm_refuses_an_invalid_file_and_the_hard_limit(tmp_path: Path) -> None:
    with pytest.raises(MonitoredFileError, match="not valid TOML"):
        confirm_text("[[group]\n", [A], AT)
    path = tmp_path / "monitored.toml"
    path.write_text("".join(f'[[group]]\nid = "{100 + i}"\n' for i in range(25)))
    with pytest.raises(MonitoredFileError, match="more than 25"):
        confirm_in_file(path, [A], AT)
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(MonitoredFileError, match="not valid TOML"):
        confirm_in_file(path, [A], AT)


def test_confirm_in_file_writes_privately_and_reports_additions(tmp_path: Path) -> None:
    path = tmp_path / "monitored.toml"
    assert confirm_in_file(path, [A, B], AT) == [A, B]
    assert path.stat().st_mode & 0o777 == 0o600
    assert confirm_in_file(path, [B], AT + dt.timedelta(hours=1)) == []
    entries = load_monitored(path)
    assert entries is not None
    assert (
        entries[1].confirmed_at == (AT + dt.timedelta(hours=1)).replace(microsecond=0).timestamp()
    )
    assert not list(tmp_path.glob("*.fbc-tmp"))
    with pytest.raises(MonitoredFileError, match="cannot be written"):
        confirm_in_file(tmp_path / "missing" / "monitored.toml", [A], AT)


ids = st.sampled_from([str(100 + i) for i in range(8)])
comment = st.sampled_from(["", "  # note", " # [[group]] in a comment"])


@st.composite
def monitored_files(draw: st.DrawFn) -> tuple[str, list[str]]:
    listed = draw(st.lists(ids, unique=True, max_size=6))
    parts = [draw(st.sampled_from(["", "# header\n"]))]
    for gid in listed:
        lines = [f"[[group]]{draw(comment)}"]
        quoted = draw(st.sampled_from([f'"{gid}"', f"'{gid}'", gid]))
        fields = [f"id = {quoted}{draw(comment)}"]
        if draw(st.booleans()):
            fields.append(f"listings = {draw(st.sampled_from(['true', 'false']))}")
        if draw(st.booleans()):
            fields.append(f"confirmed_member = {draw(st.sampled_from(['true', 'false']))}")
        if draw(st.booleans()):
            fields.append("confirmed_at = 2026-01-02T03:04:05Z")
        if draw(st.booleans()):
            fields.append(f"tier = {draw(st.integers(1, 3))}")
        lines += draw(st.permutations(fields))
        parts.append("\n".join(lines) + "\n" + draw(st.sampled_from(["", "\n", "# gap\n"])))
    return "".join(parts), listed


@given(monitored_files(), st.lists(ids, min_size=1, max_size=4))
def test_confirming_changes_exactly_what_was_asked(
    case: tuple[str, list[str]], wanted: list[str]
) -> None:
    text, listed = case
    before = {e.group_id: e for e in parse_monitored(text.encode())}
    after = parse_monitored(confirm_text(text, wanted, AT).encode())
    assert [e.group_id for e in after] == listed + [
        g for g in dict.fromkeys(wanted) if g not in listed
    ]
    for got in after:
        if got.group_id in wanted:
            assert got.confirmed_member
            assert got.confirmed_at == AT.replace(microsecond=0).timestamp()
            if got.group_id in before:
                assert got.group == before[got.group_id].group
        else:
            assert got == before[got.group_id]


# -- the command ------------------------------------------------------------------------------


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def fake_tmutil(args: Any) -> CommandResult:
    if list(args[:2]) == ["/usr/bin/tmutil", "isexcluded"]:
        return CommandResult(0, f"[Excluded]    {args[2]}\n")
    return CommandResult(0)


@pytest.fixture
def cli_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in [k for k in os.environ if k.upper().startswith("FBC_")]:
        monkeypatch.delenv(key)
    root = tmp_path / "root"
    monkeypatch.setenv("FBC_ROOT", str(root))
    monkeypatch.setenv("FBC_TIMEZONE", "Europe/Sofia")
    return root


def run_cli(*argv: str, tty: bool = True) -> tuple[int, str]:
    out = io.StringIO()
    ports = Ports(clock=FakeClock(start=AT.timestamp()), platform="darwin", runner=fake_tmutil)
    code = main(list(argv), ports=ports, stdin=Tty() if tty else io.StringIO(), out=out)
    return code, out.getvalue()


def test_groups_confirm_writes_the_file(cli_root: Path) -> None:
    code, out = run_cli("groups", "confirm", A, f"https://www.facebook.com/groups/{B}/")
    assert code == EXIT_OK, out
    assert f"confirmed {A} (added to the monitored groups)" in out
    entries = load_monitored(cli_root / "monitored.toml")
    assert entries is not None
    assert [(e.group_id, e.confirmed_member) for e in entries] == [(A, True), (B, True)]
    assert entries[0].confirmed_at == AT.replace(microsecond=0).timestamp()
    code, out = run_cli("groups", "confirm", A)
    assert code == EXIT_OK
    assert f"confirmed {A}\n" in out


def test_groups_confirm_needs_a_terminal_and_local_mode(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, out = run_cli("groups", "confirm", A, tty=False)
    assert code == EXIT_NO_TTY
    assert "needs a local terminal" in out
    monkeypatch.setenv("FBC_INGEST_URL", "http://127.0.0.1:18781")
    monkeypatch.setenv("FBC_INGEST_TOKEN", "t" * 32)
    code, out = run_cli("groups", "confirm", A)
    assert code == EXIT_USAGE
    assert "monitored.toml is not read" in out
    assert not (cli_root / "monitored.toml").exists()


def test_groups_confirm_refuses_what_it_cannot_name(cli_root: Path) -> None:
    code, out = run_cli("groups", "confirm", "not-a-group")
    assert code == EXIT_USAGE
    assert "neither a group id" in out
    code, out = run_cli("groups", "confirm", "https://www.facebook.com/groups/Flats/")
    assert code == EXIT_USAGE
    assert "has not seen yet" in out
    (cli_root / "monitored.toml").write_text("[[group]\n")
    code, out = run_cli("groups", "confirm", A)
    assert code == EXIT_CONFIG
    assert "not valid TOML" in out


def test_groups_confirm_resolves_a_name_the_collector_knows(cli_root: Path) -> None:
    layout = paths.prepare(cli_root, platform="darwin", runner=fake_tmutil)

    async def seed_slug() -> None:
        async with Store(layout.db, clock=FakeClock(start=T0)) as known:
            await known.upsert_group(GroupMeta(id=C, slug="Flats"), state=GroupState.UNVERIFIED)

    asyncio.run(seed_slug())
    code, out = run_cli("groups", "confirm", "facebook.com/groups/Flats")
    assert code == EXIT_OK, out
    entries = load_monitored(cli_root / "monitored.toml")
    assert entries is not None
    assert [e.group_id for e in entries] == [C]
