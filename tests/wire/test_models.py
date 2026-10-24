from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

import fbwire.models as wire
from fbwire import (
    MAX_BATCH_BYTES,
    MAX_MONITORED,
    MAX_TEXT_BYTES,
    WIRE_VERSION,
    Author,
    BlockInfo,
    BudgetKind,
    BudgetWindow,
    CollectorState,
    Control,
    ErrorResponse,
    Event,
    EventKind,
    EventsRequest,
    EventsResponse,
    GroupInfo,
    GroupsResponse,
    GroupsSnapshot,
    Item,
    ItemOutcome,
    ItemResult,
    ItemsRequest,
    ItemsResponse,
    Lease,
    Listing,
    MonitoredGroup,
    Profile,
    RefreshGroupsCommand,
    ResumeCommand,
    Status,
    StatusResponse,
    Via,
    accepted_versions,
    cap_text,
    items_body,
    items_overhead,
    parse_items_request,
    plan_batches,
    to_json,
)

T = 1_790_000_000
GID = "1234567890"
PID = "9876543210987654"


def item_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "v": 1,
        "collector_id": "home-1",
        "seq": 1843,
        "group_id": GID,
        "post_id": PID,
        "url": f"https://www.facebook.com/groups/{GID}/posts/{PID}/",
        "group_name": "Оренда Софія",
        "group_url": f"https://www.facebook.com/groups/{GID}/",
        "group_privacy": "private",
        "author": {
            "id": "100000000000001",
            "name": "Іван",
            "url": "https://www.facebook.com/profile.php?id=100000000000001",
            "anonymous": False,
        },
        "created_at": T,
        "first_seen_at": T + 240,
        "via": "feed",
        "late": False,
        "text": "Здається квартира, 2 кімнати",
        "truncated": False,
        "listing": {"id": "555", "title": "2-room", "price": "€600", "location": "Sofia"},
        "is_share": False,
    }
    data.update(overrides)
    return data


def make_item(**overrides: Any) -> Item:
    return Item.model_validate(item_data(**overrides))


class TestItem:
    def test_design_example_roundtrips(self) -> None:
        item = make_item()
        again = Item.model_validate_json(to_json(item))
        assert again == item
        assert item.key == (GID, PID)
        assert item.via is Via.FEED
        assert item.author == Author(
            id="100000000000001",
            name="Іван",
            url="https://www.facebook.com/profile.php?id=100000000000001",
        )
        assert item.listing == Listing(id="555", title="2-room", price="€600", location="Sofia")

    def test_non_ascii_is_written_raw_not_escaped(self) -> None:
        body = to_json(make_item())
        assert "Здається".encode() in body
        assert b"\\u" not in body

    def test_unknown_fields_are_ignored_at_every_level(self) -> None:
        data = item_data(photo_url="https://x", source="fb")
        data["author"]["verified"] = True
        data["listing"]["url"] = "https://www.facebook.com/marketplace/item/1/"
        item = Item.model_validate(data)
        assert not hasattr(item, "photo_url")
        assert item.author.id == "100000000000001"

    def test_optional_parts_default(self) -> None:
        data = item_data()
        for key in ("author", "listing", "late", "truncated", "is_share", "group_privacy", "text"):
            del data[key]
        item = Item.model_validate(data)
        assert item.author == Author()
        assert item.listing is None
        assert item.group_privacy == "unknown"
        assert item.text == ""
        assert not item.truncated

    def test_anonymous_author(self) -> None:
        item = make_item(author={"anonymous": True})
        assert item.author.anonymous
        assert item.author.id is None

    def test_models_are_frozen(self) -> None:
        item = make_item()
        with pytest.raises(ValidationError):
            item.text = "changed"

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "0",
            "0123",
            "12a",
            " 123",
            "123 ",
            "-5",
            "1.5",
            "١٢٣",  # Arabic-Indic digits: str.isdigit() says yes, the wire says no
            "9223372036854775808",  # 2**63
            "12345678901234567890",  # 20 digits
        ],
    )
    def test_id_must_be_a_positive_int64_digit_string(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            make_item(post_id=bad)
        with pytest.raises(ValidationError):
            make_item(group_id=bad)

    def test_ids_are_never_coerced_from_numbers(self) -> None:
        with pytest.raises(ValidationError):
            make_item(post_id=9876543210987654)

    def test_int64_max_is_accepted(self) -> None:
        assert make_item(post_id="9223372036854775807").post_id == "9223372036854775807"

    @pytest.mark.parametrize(
        "url",
        [
            "http://www.facebook.com/groups/1/posts/2/",
            "https://evil.example/groups/1/posts/2/",
            "https://www.facebook.com.evil.example/x",
            "https://user:pw@www.facebook.com/x",
            "https://www.facebook.com:8443/x",
            "javascript:alert(1)",
            "https://www.facebook.com/" + "a" * 2100,
        ],
    )
    def test_links_must_point_at_facebook_over_https(self, url: str) -> None:
        with pytest.raises(ValidationError):
            make_item(url=url)
        with pytest.raises(ValidationError):
            make_item(author={"url": url})

    @pytest.mark.parametrize("when", [0, 1_000_000_000, 1_790_000_000_000, -1])
    def test_epoch_seconds_are_sane(self, when: int) -> None:
        with pytest.raises(ValidationError):
            make_item(created_at=when)

    def test_unknown_via_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_item(via="email")

    def test_long_text_is_capped_and_flagged(self) -> None:
        text = "я" * MAX_TEXT_BYTES  # 2 bytes each: twice the cap
        item = make_item(text=text)
        assert item.truncated
        assert len(item.text.encode()) <= MAX_TEXT_BYTES
        assert text.startswith(item.text)

    def test_text_at_the_cap_is_untouched(self) -> None:
        text = "a" * MAX_TEXT_BYTES
        item = make_item(text=text)
        assert item.text == text
        assert not item.truncated

    def test_already_truncated_flag_survives(self) -> None:
        assert make_item(text="short", truncated=True).truncated


class TestCapText:
    def test_cut_never_splits_a_character(self) -> None:
        text, cut = cap_text("€€€", limit=4)  # 3 bytes each
        assert (text, cut) == ("€", True)

    def test_emoji_at_the_boundary(self) -> None:
        text, cut = cap_text("ab😀", limit=5)
        assert (text, cut) == ("ab", True)

    @given(st.text(max_size=200), st.integers(min_value=0, max_value=300))
    def test_properties(self, text: str, limit: int) -> None:
        capped, cut = cap_text(text, limit)
        assert len(capped.encode()) <= max(limit, 0) or not cut
        assert text.startswith(capped)
        assert cut == (len(text.encode()) > limit)


class TestVersionPolicy:
    def test_current_and_previous_are_accepted(self) -> None:
        assert accepted_versions(1) == {1}
        assert accepted_versions(2) == {1, 2}
        assert accepted_versions(7) == {6, 7}
        assert accepted_versions() == accepted_versions(WIRE_VERSION)

    @pytest.mark.parametrize("v", [0, 2, 3, -1])
    def test_other_versions_are_rejected(self, v: int) -> None:
        with pytest.raises(ValidationError, match="unsupported wire version"):
            make_item(v=v)

    def test_missing_version_means_ours(self) -> None:
        data = item_data()
        del data["v"]
        assert Item.model_validate(data).v == WIRE_VERSION

    def test_a_v2_receiver_still_reads_v1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wire, "WIRE_VERSION", 2)
        assert make_item(v=1).v == 1
        assert make_item(v=2).v == 2
        with pytest.raises(ValidationError):
            make_item(v=3)
        assert ItemsResponse().v == 2  # and writes its own version

    def test_an_answer_is_written_in_the_version_it_was_asked_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round 2 F8: a brain one version ahead answered in its own version, which no
        collector still on the older one could read (no lease, items re-sent for ever)."""
        monkeypatch.setattr(wire, "WIRE_VERSION", 2)
        assert [wire.answer_version(v) for v in (1, 2)] == [1, 2]
        for odd in (3, 0, None, "1", True, 1.0):
            assert wire.answer_version(odd) == 2  # not spoken, or not a version: its own
        answer = ItemsResponse(v=wire.answer_version(1))
        monkeypatch.setattr(wire, "WIRE_VERSION", 1)  # the collector reads it
        assert ItemsResponse.model_validate_json(to_json(answer)).v == 1

    def test_version_skew_on_every_envelope(self) -> None:
        with pytest.raises(ValidationError):
            Control.model_validate(
                {"v": 9, "issued_at": T, "lease": {"collector_id": "a", "granted": True}}
            )
        with pytest.raises(ValidationError):
            ItemsResponse.model_validate({"v": 9, "results": []})


class TestResults:
    def test_rejected_needs_a_reason(self) -> None:
        with pytest.raises(ValidationError):
            ItemResult(group_id=GID, post_id=PID, result=ItemOutcome.REJECTED)
        ok = ItemResult(group_id=GID, post_id=PID, result=ItemOutcome.REJECTED, reason="bad")
        assert ok.key == (GID, PID)

    def test_response_by_key_and_roundtrip(self) -> None:
        results = (
            ItemResult(group_id=GID, post_id=PID, result=ItemOutcome.STORED),
            ItemResult(group_id=GID, post_id="11", result=ItemOutcome.DUPLICATE),
            ItemResult(group_id="7", post_id="12", result=ItemOutcome.NOT_MONITORED),
        )
        response = ItemsResponse(results=results)
        again = ItemsResponse.model_validate_json(to_json(response))
        assert again.by_key()[(GID, "11")].result is ItemOutcome.DUPLICATE
        assert set(again.by_key()) == {(GID, PID), (GID, "11"), ("7", "12")}

    def test_unknown_outcome_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ItemResult.model_validate({"group_id": GID, "post_id": PID, "result": "maybe"})


class TestItemsRequest:
    def test_items_must_share_the_collector(self) -> None:
        with pytest.raises(ValidationError, match="request's collector"):
            ItemsRequest(collector_id="home-2", items=(make_item(),))

    def test_empty_batch_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ItemsRequest(collector_id="home-1", items=())

    @pytest.mark.parametrize("cid", ["", "-lead", "has space", "x" * 65, "ü"])
    def test_collector_id_shape(self, cid: str) -> None:
        with pytest.raises(ValidationError):
            make_item(collector_id=cid)

    def test_body_from_payloads_matches_the_model(self) -> None:
        items = (make_item(seq=1), make_item(seq=2, post_id="3", text="ünïcödé"))
        request = ItemsRequest(collector_id="home-1", items=items)
        payloads = [item.model_dump_json() for item in items]
        assert items_body("home-1", payloads) == to_json(request)
        assert ItemsRequest.model_validate_json(items_body("home-1", payloads)) == request

    def test_overhead_is_the_empty_envelope(self) -> None:
        assert items_overhead("home-1") == len(b'{"v":1,"collector_id":"home-1","items":[]}')

    def test_predicted_size_equals_serialised_size(self) -> None:
        items = [make_item(seq=i, post_id=str(100 + i), text="щ" * (i * 37)) for i in range(1, 9)]
        sizes = [len(item.model_dump_json().encode()) for item in items]
        predicted = items_overhead("home-1") + sum(sizes) + len(sizes) - 1
        request = ItemsRequest(collector_id="home-1", items=tuple(items))
        assert predicted == len(to_json(request))


class TestPlanBatches:
    def test_everything_fits_in_one(self) -> None:
        assert plan_batches([10, 10, 10], overhead=5, limit=100) == [(0, 3)]

    def test_limit_is_inclusive_and_counts_commas(self) -> None:
        # 5 + 10 + 1 + 10 = 26: fits exactly; a third item would need 11 more.
        assert plan_batches([10, 10, 10], overhead=5, limit=26) == [(0, 2), (2, 3)]
        assert plan_batches([10, 10], overhead=5, limit=25) == [(0, 1), (1, 2)]

    def test_oversized_item_goes_alone(self) -> None:
        assert plan_batches([5, 500, 5], overhead=5, limit=100) == [(0, 1), (1, 2), (2, 3)]

    def test_empty(self) -> None:
        assert plan_batches([], overhead=5) == []

    @given(
        st.lists(st.integers(min_value=1, max_value=200), max_size=60),
        st.integers(min_value=0, max_value=50),
        st.integers(min_value=1, max_value=600),
    )
    def test_properties(self, sizes: list[int], overhead: int, limit: int) -> None:
        batches = plan_batches(sizes, overhead=overhead, limit=limit)
        # Contiguous, in order, covering everything, never empty.
        flat = [i for start, end in batches for i in range(start, end)]
        assert flat == list(range(len(sizes)))
        for start, end in batches:
            assert end > start
            body = overhead + sum(sizes[start:end]) + (end - start - 1)
            assert body <= limit or end - start == 1

    def test_real_batches_stay_under_512_kib(self) -> None:
        items = [make_item(seq=i, post_id=str(1000 + i), text="ї" * 8000) for i in range(1, 80)]
        payloads = [item.model_dump_json() for item in items]
        sizes = [len(p.encode()) for p in payloads]
        batches = plan_batches(sizes, overhead=items_overhead("home-1"))
        assert len(batches) > 1
        for start, end in batches:
            assert len(items_body("home-1", payloads[start:end])) <= MAX_BATCH_BYTES


class TestLenientParse:
    def test_good_items_pass_and_bad_ones_are_rejected_by_key(self) -> None:
        good = item_data()
        bad_url = item_data(post_id="2", url="https://evil.example/")
        future = item_data(post_id="3", v=99)
        other_collector = item_data(post_id="4", collector_id="home-2")
        no_key = item_data(post_id="not-digits")
        not_a_dict = "garbage"
        parsed = parse_items_request(
            {
                "v": 1,
                "collector_id": "home-1",
                "items": [good, bad_url, future, other_collector, no_key, not_a_dict],
            }
        )
        assert [i.post_id for i in parsed.items] == [PID]
        assert parsed.collector_id == "home-1"
        reasons = {r.post_id: r.reason or "" for r in parsed.rejected}
        assert set(reasons) == {"2", "3", "4"}
        assert all(r.result is ItemOutcome.REJECTED for r in parsed.rejected)
        assert reasons["2"].startswith("url")
        assert reasons["3"].startswith("v")
        assert "collector_id" in reasons["4"]
        assert parsed.unkeyed == 2

    def test_rejection_reason_never_quotes_the_input(self) -> None:
        secret_text = "private words of the post"
        data = item_data(post_id="2", text=secret_text, via="nope", author={"url": secret_text})
        parsed = parse_items_request({"collector_id": "home-1", "items": [data]})
        (result,) = parsed.rejected
        assert secret_text not in (result.reason or "")
        assert len(result.reason or "") <= wire.MAX_REASON_LEN

    def test_bad_envelope_raises(self) -> None:
        with pytest.raises(ValidationError):
            parse_items_request({"v": 5, "collector_id": "home-1", "items": []})
        with pytest.raises(ValidationError):
            parse_items_request({"items": []})
        with pytest.raises(ValueError, match="items must be a list"):
            parse_items_request({"collector_id": "home-1", "items": {"a": 1}})
        with pytest.raises(ValidationError):
            parse_items_request(["not", "a", "mapping"])


class TestGroups:
    def group(self, gid: str, **kw: Any) -> GroupInfo:
        return GroupInfo(
            id=gid, name=f"g{gid}", url=f"https://www.facebook.com/groups/{gid}/", **kw
        )

    def test_complete_snapshot_matches_the_header(self) -> None:
        groups = (
            self.group("1"),
            self.group("2"),
            self.group("3", listed=False, state="unverified"),
        )
        snap = GroupsSnapshot(
            collector_id="home-1", taken_at=T, complete=True, header_count=2, groups=groups
        )
        assert GroupsSnapshot.model_validate_json(to_json(snap)) == snap

    @pytest.mark.parametrize("header", [None, 1, 3])
    def test_complete_with_the_wrong_count_is_rejected(self, header: int | None) -> None:
        with pytest.raises(ValidationError, match="header_count"):
            GroupsSnapshot(
                collector_id="home-1",
                taken_at=T,
                complete=True,
                header_count=header,
                groups=(self.group("1"), self.group("2")),
            )

    def test_incomplete_snapshot_may_disagree(self) -> None:
        snap = GroupsSnapshot(
            collector_id="home-1", taken_at=T, complete=False, header_count=40, groups=()
        )
        assert not snap.complete

    def test_duplicate_groups_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="twice"):
            GroupsSnapshot(
                collector_id="home-1",
                taken_at=T,
                complete=False,
                groups=(self.group("1"), self.group("1")),
            )

    def test_slug_shape(self) -> None:
        assert self.group("1", slug="sofia.rent-2").slug == "sofia.rent-2"
        with pytest.raises(ValidationError):
            self.group("1", slug="../etc")


def status_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "collector_id": "home-1",
        "sent_at": T,
        "state": "ok",
        "profile": "cautious",
        "budget": [
            {"kind": "navigation", "window_seconds": 3600, "cap": 10, "used": 4},
            {"kind": "scroll", "window_seconds": 86400, "cap": 250, "used": 260},
        ],
        "gates": {"home": True, "quiet": False},
        "counters": {"stories_total": 12, "outbox_depth": 0},
        "versions": {"collector": "0.1.0", "chrome": "140"},
    }
    data.update(overrides)
    return data


class TestStatus:
    def test_roundtrip_and_remaining_budget(self) -> None:
        status = Status.model_validate(status_data())
        assert Status.model_validate_json(to_json(status)) == status
        nav, scroll = status.budget
        assert nav.remaining == 6
        assert scroll.remaining == 0  # over the cap reads as none left, never negative
        assert nav.kind is BudgetKind.NAVIGATION

    def test_blocked_state_carries_its_block(self) -> None:
        with pytest.raises(ValidationError, match="carries its block"):
            Status.model_validate(status_data(state="blocked"))
        block = {"id": 3, "opened_at": T, "until": T + 6 * 3600, "reason": "checkpoint", "rung": 0}
        status = Status.model_validate(status_data(state="blocked", block=block))
        assert status.block == BlockInfo(
            id=3, opened_at=T, until=T + 6 * 3600, reason="checkpoint", rung=0
        )

    def test_collector_cannot_claim_offline(self) -> None:
        with pytest.raises(ValidationError, match="offline"):
            Status.model_validate(status_data(state="offline"))

    def test_every_collector_state_but_offline_is_sendable(self) -> None:
        block = {"id": 1, "opened_at": T, "reason": "x", "rung": 2}
        for state in CollectorState:
            if state is CollectorState.OFFLINE:
                continue
            assert Status.model_validate(status_data(state=state, block=block)).state is state

    def test_counters_are_non_negative_and_keys_are_tame(self) -> None:
        with pytest.raises(ValidationError):
            Status.model_validate(status_data(counters={"x": -1}))
        with pytest.raises(ValidationError):
            Status.model_validate(status_data(gates={"Not A Key": True}))

    def test_budget_window_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            BudgetWindow(kind=BudgetKind.SCROLL, window_seconds=0, cap=1, used=0)


class TestEvents:
    def test_group_events_name_the_group(self) -> None:
        with pytest.raises(ValidationError, match="group_id"):
            Event(collector_id="home-1", event_id=1, at=T, kind=EventKind.GROUP_LOST)
        event = Event(
            collector_id="home-1", event_id=1, at=T, kind=EventKind.GROUP_LOST, group_id=GID
        )
        assert event.key == ("home-1", 1)

    def test_block_events_name_the_block(self) -> None:
        with pytest.raises(ValidationError, match="block_id"):
            Event(collector_id="home-1", event_id=2, at=T, kind=EventKind.BLOCK_OPENED)
        Event(collector_id="home-1", event_id=2, at=T, kind=EventKind.BLOCK_CLOSED, block_id=4)

    def test_batch_roundtrip(self) -> None:
        events = (
            Event(
                collector_id="home-1", event_id=5, at=T, kind=EventKind.OUTBOX_HIGH, detail="4100"
            ),
            Event(collector_id="home-1", event_id=6, at=T, kind=EventKind.PARSER_BROKEN),
        )
        request = EventsRequest(collector_id="home-1", events=events)
        assert EventsRequest.model_validate_json(to_json(request)) == request


class TestControl:
    def control(self, **overrides: Any) -> dict[str, Any]:
        data: dict[str, Any] = {
            "v": 1,
            "issued_at": T,
            "monitored": [{"group_id": GID, "tier": 2, "listings": True}, {"group_id": "7"}],
            "lease": {"collector_id": "home-1", "granted": True},
            "commands": [
                {"kind": "resume", "block_id": 3, "nonce": "n0nce-abcdef"},
                {"kind": "refresh_groups", "nonce": "fresh-123456"},
            ],
            "pace_cap": "cautious",
        }
        data.update(overrides)
        return data

    def test_roundtrip(self) -> None:
        control = Control.model_validate(self.control())
        assert Control.model_validate_json(to_json(control)) == control
        assert control.monitored[0] == MonitoredGroup(group_id=GID, tier=2, listings=True)
        assert control.monitored[1].tier == 1
        assert control.commands == (
            ResumeCommand(block_id=3, nonce="n0nce-abcdef"),
            RefreshGroupsCommand(nonce="fresh-123456"),
        )
        assert control.pace_cap is Profile.CAUTIOUS
        assert not control.paused

    def test_unknown_commands_are_skipped_not_fatal(self) -> None:
        control = Control.model_validate(
            self.control(
                commands=[
                    {"kind": "self_destruct", "x": 1},
                    {"kind": "resume", "block_id": 1, "nonce": "abcdefgh"},
                ]
            )
        )
        assert control.commands == (ResumeCommand(block_id=1, nonce="abcdefgh"),)

    def test_commands_must_be_a_list(self) -> None:
        with pytest.raises(ValidationError):
            Control.model_validate(self.control(commands="resume"))

    def test_known_command_with_bad_fields_fails(self) -> None:
        with pytest.raises(ValidationError):
            Control.model_validate(self.control(commands=[{"kind": "resume", "nonce": "abcdefgh"}]))
        with pytest.raises(ValidationError):
            Control.model_validate(
                self.control(commands=[{"kind": "refresh_groups", "nonce": "short"}])
            )

    def test_lease_must_name_this_collector(self) -> None:
        control = Control.model_validate(self.control())
        assert control.lease_held_by("home-1")
        assert not control.lease_held_by("home-2")
        denied = Control.model_validate(
            self.control(lease={"collector_id": "home-1", "granted": False})
        )
        assert not denied.lease_held_by("home-1")

    def test_monitored_is_capped_and_unique(self) -> None:
        too_many = [{"group_id": str(i)} for i in range(1, MAX_MONITORED + 2)]
        with pytest.raises(ValidationError):
            Control.model_validate(self.control(monitored=too_many))
        with pytest.raises(ValidationError, match="twice"):
            Control.model_validate(self.control(monitored=[{"group_id": "1"}, {"group_id": "1"}]))
        exactly = [{"group_id": str(i)} for i in range(1, MAX_MONITORED + 1)]
        assert len(Control.model_validate(self.control(monitored=exactly)).monitored) == 25

    def test_lease_needs_a_collector_id(self) -> None:
        with pytest.raises(ValidationError):
            Lease.model_validate({"granted": True})

    def test_minimal_control(self) -> None:
        control = Control.model_validate(
            {"issued_at": T, "lease": {"collector_id": "home-1", "granted": False}}
        )
        assert control.monitored == ()
        assert control.commands == ()
        assert control.pace_cap is None


class TestProfile:
    def test_remote_can_only_slow_down(self) -> None:
        assert Profile.most_cautious(Profile.NORMAL, Profile.CAUTIOUS) is Profile.CAUTIOUS
        assert Profile.most_cautious(Profile.CAUTIOUS, Profile.NORMAL) is Profile.CAUTIOUS
        assert Profile.most_cautious(Profile.NORMAL, None) is Profile.NORMAL
        assert Profile.most_cautious(Profile.NORMAL, Profile.NORMAL) is Profile.NORMAL

    def test_no_opinion_at_all_is_cautious(self) -> None:
        assert Profile.most_cautious() is Profile.CAUTIOUS
        assert Profile.most_cautious(None, None) is Profile.CAUTIOUS


def test_package_reexports_everything() -> None:
    import fbwire

    for name in fbwire.__all__:
        assert getattr(fbwire, name) is getattr(wire, name)


def test_json_is_compact_and_ordered() -> None:
    body = json.loads(to_json(ItemsResponse()))
    assert body == {"v": 1, "results": []}


class TestOrderingFields:
    """Round 1 of review: what lets the brain tell a delayed message from a newer one."""

    def test_status_and_snapshot_carry_their_order_but_do_not_need_to(self) -> None:
        old = Status.model_validate(status_data())
        assert (old.seq, old.incarnation, old.resumes_at) == (None, None, None)
        new = Status.model_validate(
            status_data(seq=41, incarnation="db-7f3a9c1e", resumes_at=T + 600, state="quiet")
        )
        assert Status.model_validate_json(to_json(new)) == new
        snap = GroupsSnapshot(
            collector_id="home-1",
            taken_at=T,
            complete=False,
            seq=42,
            incarnation="db-7f3a9c1e",
            nonce="refresh-000001",
        )
        assert GroupsSnapshot.model_validate_json(to_json(snap)) == snap

    @pytest.mark.parametrize(
        "bad", [{"seq": -1}, {"incarnation": "short"}, {"incarnation": "a b c d e f"}]
    )
    def test_bad_order_fields_are_refused(self, bad: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            Status.model_validate(status_data(**bad))

    def test_a_status_may_say_when_each_group_was_read_through(self) -> None:
        assert Status.model_validate(status_data()).group_checked == {}
        new = Status.model_validate(status_data(group_checked={"123456789": T - 600}))
        assert Status.model_validate_json(to_json(new)) == new
        many = {str(n): T for n in range(1, wire.MAX_MONITORED + 2)}
        for bad in ({"12x": T}, {"123": 0}, {"123": T * 1000}, many):
            with pytest.raises(ValidationError):
                Status.model_validate(status_data(group_checked=bad))

    def test_budget_windows_and_groups_are_bounded(self) -> None:
        window = {"kind": "scroll", "window_seconds": 60, "cap": 1, "used": 0}
        with pytest.raises(ValidationError):
            Status.model_validate(status_data(budget=[window] * (wire.MAX_BUDGET_WINDOWS + 1)))
        many = [
            {"id": str(n), "name": "g", "url": f"https://www.facebook.com/groups/{n}/"}
            for n in range(1, wire.MAX_GROUPS + 2)
        ]
        with pytest.raises(ValidationError):
            GroupsSnapshot.model_validate(
                {"collector_id": "home-1", "taken_at": T, "complete": False, "groups": many}
            )


class TestEventsEnvelope:
    def event(self, collector_id: str = "home-1", event_id: int = 1) -> Event:
        return Event(collector_id=collector_id, event_id=event_id, at=T, kind=EventKind.OUTBOX_HIGH)

    def test_every_event_comes_from_the_envelope_collector(self) -> None:
        with pytest.raises(ValidationError, match="request's collector"):
            EventsRequest(collector_id="home-2", events=(self.event("home-1"),))
        EventsRequest(collector_id="home-1", events=(self.event(), self.event(event_id=2)))

    def test_a_request_may_name_the_database_its_ids_count_in(self) -> None:
        plain = EventsRequest(collector_id="home-1", events=(self.event(),))
        assert plain.incarnation is None  # an older collector
        named = EventsRequest(collector_id="home-1", incarnation="Ab_3-x9Qz", events=plain.events)
        assert EventsRequest.model_validate_json(to_json(named)).incarnation == "Ab_3-x9Qz"
        for bad in ("short", "has space1", "x" * 65):
            with pytest.raises(ValidationError):
                EventsRequest(collector_id="home-1", incarnation=bad, events=plain.events)

    def test_a_request_holds_at_most_the_queue(self) -> None:
        events = tuple(self.event(event_id=n) for n in range(1, wire.MAX_EVENTS + 2))
        with pytest.raises(ValidationError):
            EventsRequest(collector_id="home-1", events=events)

    def test_the_answers_roundtrip(self) -> None:
        for answer in (
            EventsResponse(acked=(1, 2)),
            StatusResponse(stale=True),
            GroupsResponse(groups=3, changed=1),
            ErrorResponse(error="database busy"),
        ):
            assert type(answer).model_validate_json(to_json(answer)) == answer
        assert json.loads(to_json(StatusResponse())) == {"v": 1, "ok": True, "stale": False}


class TestTargetedCommands:
    def control(self, **lease: Any) -> dict[str, Any]:
        return {
            "issued_at": T,
            "lease": {"collector_id": "home-1", "granted": True, "generation": 3, **lease},
            "commands": [
                {
                    "kind": "resume",
                    "block_id": 3,
                    "nonce": "for-me-000",
                    "collector_id": "home-1",
                    "lease_generation": 3,
                    "lock_lifted": True,
                },
                {
                    "kind": "resume",
                    "block_id": 3,
                    "nonce": "for-them-00",
                    "collector_id": "laptop",
                    "lease_generation": 3,
                },
                {
                    "kind": "refresh_groups",
                    "nonce": "old-lease-0",
                    "collector_id": "home-1",
                    "lease_generation": 2,
                },
                {"kind": "refresh_groups", "nonce": "old-brain-0"},
            ],
        }

    def test_a_collector_applies_only_what_is_meant_for_it_and_this_lease(self) -> None:
        control = Control.model_validate(self.control())
        assert Control.model_validate_json(to_json(control)) == control
        mine = control.commands_for("home-1")
        assert [c.nonce for c in mine] == ["for-me-000", "old-brain-0"]
        assert isinstance(mine[0], ResumeCommand) and mine[0].lock_lifted
        assert control.commands_for("laptop") == ()  # no lease, no commands at all

    def test_without_a_grant_nothing_applies(self) -> None:
        control = Control.model_validate(self.control(granted=False))
        assert control.commands_for("home-1") == ()

    def test_defaults_are_an_older_brains_commands(self) -> None:
        command = ResumeCommand(block_id=1, nonce="abcdefgh")
        assert (command.collector_id, command.lease_generation, command.lock_lifted) == (
            None,
            None,
            False,
        )
        assert command.meant_for("anyone", None) and command.meant_for("anyone", 9)

    def test_a_lease_validity_is_at_least_a_minute(self) -> None:
        with pytest.raises(ValidationError):
            Lease(collector_id="home-1", granted=True, valid_for=30)
        with pytest.raises(ValidationError):
            Lease(collector_id="home-1", granted=True, generation=0)
        lease = Lease(collector_id="home-1", granted=True, generation=1, valid_for=3600)
        assert Lease.model_validate_json(to_json(lease)) == lease


def test_an_items_request_holds_a_bounded_number_of_entries() -> None:
    with pytest.raises(ValueError, match="at most"):
        parse_items_request({"collector_id": "home-1", "items": [{}] * (wire.MAX_ITEMS + 1)})
