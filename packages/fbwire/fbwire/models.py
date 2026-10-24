"""What the collector and the brain say to each other, and nothing else.

Both sides import these models, so a field renamed here is renamed for both at once; the version
number exists for the moment they are deployed apart (the collector at home, the brain on the
VPS), which is every day. The rules that keep that safe:

- Unknown fields are ignored (``extra="ignore"``), so adding an optional field is not a version
  bump. ``v`` changes only for an incompatible change, and a receiver accepts ``v`` and ``v - 1``.
  The brain answers in the version of the request (``answer_version``), so the safe deploy
  order after a bump is the brain first, then the collectors: an older collector still reads
  every answer. The other order is refused (an older brain cannot read a newer request).
- Facebook ids are digit strings on the wire. JSON numbers above 2**53 lose precision in many
  parsers, and a post id is 16-17 digits; the brain turns them into integers, so they must also
  fit a signed 64-bit column.
- URLs that end up as links in a notification must point at ``https://www.facebook.com``: the
  brain renders them into a Telegram message and should never be a way to send someone elsewhere.
- A post's text is capped at 16 KiB of UTF-8 and the cut is flagged, never silent; batches are
  sized by their serialised bytes (``ensure_ascii=False``, which is what pydantic emits), because
  a byte limit is what the brain's body cap enforces.

Validation errors quote the offending field, never the post text: a rejection reason travels
back to the collector's log (invariant 8).
"""

from __future__ import annotations

import enum
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

WIRE_VERSION: Final = 1
"""The version this code writes. Receivers accept this and the one before it."""

MAX_TEXT_BYTES: Final = 16 * 1024
MAX_BATCH_BYTES: Final = 512 * 1024
MAX_BODY_BYTES: Final = 1024 * 1024
MAX_MONITORED: Final = 25
"""D17: the hard limit of monitored groups per listener account."""
MAX_ITEMS: Final = 4096
"""Items per request. A 512 KiB batch of valid items holds far fewer (each carries two URLs);
the cap exists so a small body of tiny invalid entries cannot keep the brain validating."""
MAX_EVENTS: Final = 1000
"""Events per request: the collector's durable queue never holds more."""
MAX_GROUPS: Final = 5000
"""Groups per snapshot, beyond what "All groups you've joined" shows any account."""
MAX_BUDGET_WINDOWS: Final = 32

INT64_MAX: Final = 2**63 - 1
EPOCH_MIN: Final = 1_072_915_200
"""2004-01-01 UTC: nothing on Facebook is older. Catches a zero or a unit mix-up."""
EPOCH_MAX: Final = 4_102_444_800
"""2100-01-01 UTC: catches milliseconds sent where seconds were meant."""

FACEBOOK_HOST: Final = "www.facebook.com"
MAX_URL_LEN: Final = 2048
MAX_NAME_LEN: Final = 1024
MAX_REASON_LEN: Final = 300


def accepted_versions(current: int | None = None) -> frozenset[int]:
    """The versions a receiver at ``current`` accepts: itself and the one before, never 0."""
    now = WIRE_VERSION if current is None else current
    return frozenset(v for v in (now - 1, now) if v >= 1)


def answer_version(requested: object) -> int:
    """The version to answer a request in: the request's own when this side accepts it.

    A receiver refuses anything newer than itself, so a brain one version ahead that answered
    in its own version would be unreadable to every collector whose request it had just taken:
    no lease (reading stops when the grant lapses), items stored but never deleted at home and
    re-sent for ever, events never acknowledged.
    """
    wanted = requested if isinstance(requested, int) and not isinstance(requested, bool) else None
    return wanted if wanted in accepted_versions() else WIRE_VERSION


def _check_version(value: int) -> int:
    accepted = accepted_versions()
    if value not in accepted:
        wanted = ", ".join(str(v) for v in sorted(accepted))
        msg = f"unsupported wire version {value} (this side accepts {wanted})"
        raise ValueError(msg)
    return value


def _check_int64(value: str) -> str:
    if int(value) > INT64_MAX:
        msg = "id does not fit a signed 64-bit integer"
        raise ValueError(msg)
    return value


def _check_facebook_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or parts.hostname != FACEBOOK_HOST:
        msg = f"must be an https://{FACEBOOK_HOST}/ URL"
        raise ValueError(msg)
    if parts.username is not None or parts.password is not None or parts.port is not None:
        msg = "must not carry credentials or a port"
        raise ValueError(msg)
    return value


# ``[0-9]`` and not ``\d``: Python's str.isdigit() and \d accept Arabic-Indic and other digits,
# which int() happily parses — a different string for the same number would defeat dedupe.
DigitId = Annotated[
    str, StringConstraints(pattern=r"^[1-9][0-9]{0,18}$"), AfterValidator(_check_int64)
]
CollectorId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
Nonce = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,128}$")]
Incarnation = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,64}$")]
Seq = Annotated[int, Field(ge=0, le=2**63 - 1)]
Generation = Annotated[int, Field(ge=1, le=2**63 - 1)]
FacebookUrl = Annotated[str, Field(max_length=MAX_URL_LEN), AfterValidator(_check_facebook_url)]
Epoch = Annotated[int, Field(ge=EPOCH_MIN, le=EPOCH_MAX)]
Name = Annotated[str, Field(max_length=MAX_NAME_LEN)]
Reason = Annotated[str, Field(min_length=1, max_length=MAX_REASON_LEN)]
Slug = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,128}$")]
Key = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,40}$")]


class Via(enum.StrEnum):
    """Which read found the post."""

    FEED = "feed"
    BACKSTOP = "backstop"
    BACKSTOP_LISTINGS = "backstop_listings"


class GroupPrivacy(enum.StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    UNKNOWN = "unknown"


class GroupState(enum.StrEnum):
    """The collector's verdict on its membership (the brain maps it to ``chats.available``)."""

    MEMBER = "member"
    UNVERIFIED = "unverified"  # missing from a sync, not yet confirmed gone
    LEFT = "left"
    LOST = "lost"  # the group page said we cannot read it


class Profile(enum.StrEnum):
    """Pacing profile. Ordered from most to least cautious."""

    CAUTIOUS = "cautious"
    NORMAL = "normal"

    @classmethod
    def most_cautious(cls, *profiles: Profile | None) -> Profile:
        """``effective = min(local, remote)``: a remote side can only slow the collector down.

        ``None`` (no opinion) is skipped; with no opinion at all the answer is cautious.
        """
        order = list(cls)
        present = [p for p in profiles if p is not None]
        if not present:
            return cls.CAUTIOUS
        return min(present, key=order.index)


class CollectorState(enum.StrEnum):
    """What the collector is doing, as the owner sees it in ``/status`` and alerts.

    ``OFFLINE`` is computed by the brain when statuses stop arriving; a collector never says it.
    """

    OK = "ok"
    WARMUP = "warmup"
    QUIET = "quiet"
    BREAK = "break"
    NOT_HOME = "not_home"
    BLOCKED = "blocked"
    LOGGED_OUT = "logged_out"
    UNKNOWN = "unknown"
    CRASH_LOOP = "crash_loop"
    PARSER_BROKEN = "parser_broken"
    FEED_NOT_CHRONO = "feed_not_chrono"
    NEEDS_LOGIN = "needs_login"
    PAUSED = "paused"
    OFFLINE = "offline"


class BudgetKind(enum.StrEnum):
    NAVIGATION = "navigation"
    SCROLL = "scroll"


class ItemOutcome(enum.StrEnum):
    """Every outcome means *processed*: the collector deletes the outbox row for any of them."""

    STORED = "stored"
    DUPLICATE = "duplicate"
    NOT_MONITORED = "not_monitored"
    REJECTED = "rejected"


class EventKind(enum.StrEnum):
    BLOCK_OPENED = "block_opened"
    BLOCK_CLOSED = "block_closed"
    LOGGED_OUT = "logged_out"
    GROUP_LOST = "group_lost"
    NOT_FOLLOWED = "not_followed"
    SORT_IGNORED = "sort_ignored"
    FEED_NOT_CHRONO = "feed_not_chrono"
    PARSER_BROKEN = "parser_broken"
    CRASH_LOOP = "crash_loop"
    OUTBOX_HIGH = "outbox_high"
    ITEMS_INVALID = "items_invalid"
    """Posts whose items did not fit the wire model wait in the collector's quarantine."""


_GROUP_EVENTS: Final = frozenset(
    {EventKind.GROUP_LOST, EventKind.NOT_FOLLOWED, EventKind.SORT_IGNORED}
)
_BLOCK_EVENTS: Final = frozenset({EventKind.BLOCK_OPENED, EventKind.BLOCK_CLOSED})


def cap_text(text: str, limit: int = MAX_TEXT_BYTES) -> tuple[str, bool]:
    """Cut ``text`` to at most ``limit`` UTF-8 bytes on a character boundary.

    Returns the text and whether anything was cut.
    """
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    # A cut inside a multi-byte sequence leaves a partial character at the end; "ignore" drops
    # exactly that tail and nothing else, because everything before it is valid UTF-8.
    return raw[:limit].decode("utf-8", errors="ignore"), True


class WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Envelope(WireModel):
    """A top-level message: carries the version it was written in."""

    v: int = Field(default_factory=lambda: WIRE_VERSION)

    @field_validator("v")
    @classmethod
    def _version(cls, value: int) -> int:
        return _check_version(value)


class Author(WireModel):
    id: DigitId | None = None
    name: Name | None = None
    url: FacebookUrl | None = None
    anonymous: bool = False


class Listing(WireModel):
    """A marketplace card attached to a post. Its id is a listing id, not a post id."""

    id: Annotated[str, Field(max_length=64)] | None = None
    title: Name | None = None
    price: Name | None = None
    location: Name | None = None


class Item(Envelope):
    """One new post, as the collector saw it."""

    collector_id: CollectorId
    seq: int = Field(ge=0)
    group_id: DigitId
    post_id: DigitId
    url: FacebookUrl
    group_name: Name
    group_url: FacebookUrl
    group_privacy: GroupPrivacy = GroupPrivacy.UNKNOWN
    author: Author = Field(default_factory=Author)
    created_at: Epoch
    first_seen_at: Epoch
    via: Via
    late: bool = False
    text: str = ""
    truncated: bool = False
    listing: Listing | None = None
    is_share: bool = False

    @model_validator(mode="before")
    @classmethod
    def _cap_text(cls, data: Any) -> Any:
        # Cap instead of rejecting: an over-long post is still a post, and the flag tells the
        # reader the rest is on Facebook.
        if isinstance(data, Mapping) and isinstance(data.get("text"), str):
            text, cut = cap_text(data["text"])
            if cut:
                return {**data, "text": text, "truncated": True}
        return data

    @property
    def key(self) -> tuple[str, str]:
        return (self.group_id, self.post_id)


class ItemResult(WireModel):
    group_id: DigitId
    post_id: DigitId
    result: ItemOutcome
    reason: Reason | None = None

    @model_validator(mode="after")
    def _reason_for_rejections(self) -> ItemResult:
        if self.result is ItemOutcome.REJECTED and self.reason is None:
            msg = "a rejected item needs a reason"
            raise ValueError(msg)
        return self

    @property
    def key(self) -> tuple[str, str]:
        return (self.group_id, self.post_id)


class ItemsRequest(Envelope):
    collector_id: CollectorId
    items: tuple[Item, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_collector(self) -> ItemsRequest:
        if any(item.collector_id != self.collector_id for item in self.items):
            msg = "every item must come from the request's collector"
            raise ValueError(msg)
        return self


class ItemsResponse(Envelope):
    results: tuple[ItemResult, ...] = ()

    def by_key(self) -> dict[tuple[str, str], ItemResult]:
        return {result.key: result for result in self.results}


class GroupInfo(WireModel):
    id: DigitId
    name: Name
    slug: Slug | None = None
    url: FacebookUrl
    privacy: GroupPrivacy = GroupPrivacy.UNKNOWN
    state: GroupState = GroupState.MEMBER
    followed: bool | None = None
    """Whether the Groups feed shows this group's posts: ``False`` when the account does not
    follow it, or the feed has not shown it for 48 h (users get the backstop delay then)."""
    listed: bool = True
    """Seen in the joined-groups list of this sync (``False``: known from earlier syncs only)."""
    sort_ignored: bool | None = None
    """The group page ignores ``CHRONOLOGICAL``; ``None``: not known (the brain keeps its own).
    It is how the brain learns the sort is honoured again: no event says so."""


class GroupsSnapshot(Envelope):
    collector_id: CollectorId
    taken_at: Epoch
    complete: bool
    header_count: int | None = Field(default=None, ge=0)
    groups: tuple[GroupInfo, ...] = Field(default=(), max_length=MAX_GROUPS)
    seq: Seq | None = None
    """Orders this collector's snapshots and statuses (see :class:`Status`); the brain ignores
    a snapshot older than the last one it applied, so a delayed resend cannot undo a newer one."""
    incarnation: Incarnation | None = None
    """Names the counter behind ``seq`` (see :class:`Status`)."""
    nonce: Nonce | None = None
    """The ``refresh_groups`` command this sync answers, echoed, so the brain can close exactly
    that command instead of guessing from two machines' clocks."""

    @model_validator(mode="after")
    def _consistent(self) -> GroupsSnapshot:
        ids = [g.id for g in self.groups]
        if len(ids) != len(set(ids)):
            msg = "a group appears twice"
            raise ValueError(msg)
        # The brain marks groups gone only on complete snapshots, so "complete" has to mean
        # what the design says: every group of the "All groups you've joined (N)" header.
        if self.complete:
            listed = sum(1 for g in self.groups if g.listed)
            if self.header_count is None or listed != self.header_count:
                msg = "a complete snapshot lists exactly header_count groups"
                raise ValueError(msg)
        return self


class BudgetWindow(WireModel):
    kind: BudgetKind
    window_seconds: int = Field(gt=0)
    cap: int = Field(ge=0)
    used: int = Field(ge=0)

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.used)


class BlockInfo(WireModel):
    id: int = Field(ge=1)
    opened_at: Epoch
    until: Epoch | None = None
    """``None``: stopped until a human re-plans (the last rung)."""
    reason: Reason
    rung: int = Field(ge=0)
    no_probe: bool = False
    """``checkpoint/advanced_protection``: no probe until the owner confirms the lock is lifted."""


class Status(Envelope):
    collector_id: CollectorId
    sent_at: Epoch
    state: CollectorState
    state_since: Epoch | None = None
    detail: Reason | None = None
    profile: Profile
    budget: tuple[BudgetWindow, ...] = Field(default=(), max_length=MAX_BUDGET_WINDOWS)
    gates: dict[Key, bool] = Field(default_factory=dict, max_length=64)
    counters: dict[Key, Annotated[int, Field(ge=0)]] = Field(default_factory=dict, max_length=128)
    block: BlockInfo | None = None
    versions: dict[Key, Annotated[str, Field(max_length=64)]] = Field(
        default_factory=dict, max_length=32
    )
    seq: Seq | None = None
    """Increases with every status or snapshot this collector sends. The brain applies a status
    only if it is newer than the last one it applied, so one delayed by a retry cannot overwrite
    a newer block or state. ``None`` (an older collector): ``sent_at`` orders them instead."""
    incarnation: Incarnation | None = None
    """Names the counter behind ``seq``; it changes only when the collector's state database is
    new (and ``seq`` starts again). Random, not secret."""
    resumes_at: Epoch | None = None
    """For a planned idle state (quiet hours, a break, a back-off): when reading is due to
    resume, by the collector's clock. The brain shows it and bounds how long it waits before
    calling a silent collector offline."""
    group_checked: dict[DigitId, Epoch] = Field(default_factory=dict, max_length=MAX_MONITORED)
    """Per monitored group, when its last backstop that reached its target began, by the
    collector's clock: the group's own page was read back past everything it had, so the group
    had no post until then that the collector has not seen (and queued, if new). It tells the
    brain a group that is quiet from one the collector has stopped seeing (wire change 17)."""

    @model_validator(mode="after")
    def _consistent(self) -> Status:
        if self.state is CollectorState.OFFLINE:
            msg = "offline is the brain's verdict, not a collector state"
            raise ValueError(msg)
        if self.state is CollectorState.BLOCKED and self.block is None:
            msg = "a blocked status carries its block"
            raise ValueError(msg)
        return self


class Event(Envelope):
    """A one-off transition, kept in the collector's queue until the brain has it."""

    collector_id: CollectorId
    event_id: int = Field(ge=1)
    at: Epoch
    kind: EventKind
    group_id: DigitId | None = None
    block_id: int | None = Field(default=None, ge=1)
    detail: Reason | None = None

    @model_validator(mode="after")
    def _subject(self) -> Event:
        if self.kind in _GROUP_EVENTS and self.group_id is None:
            msg = f"{self.kind.value} needs a group_id"
            raise ValueError(msg)
        if self.kind in _BLOCK_EVENTS and self.block_id is None:
            msg = f"{self.kind.value} needs a block_id"
            raise ValueError(msg)
        return self

    @property
    def key(self) -> tuple[str, int]:
        return (self.collector_id, self.event_id)


class EventsRequest(Envelope):
    collector_id: CollectorId
    events: tuple[Event, ...] = Field(min_length=1, max_length=MAX_EVENTS)
    incarnation: Incarnation | None = None
    """The state database the events were queued in (as :attr:`Status.incarnation`). Event ids
    count per database and start again at 1 in a new one, so the brain keys events by it; without
    it, a collector's new database had its first events taken for old ones and dropped.
    ``None``: an older collector, whose ids the brain can only take at their word."""

    @model_validator(mode="after")
    def _one_collector(self) -> EventsRequest:
        # The brain authorises a request by its envelope; an event naming another collector
        # would otherwise borrow that collector's lease.
        if any(event.collector_id != self.collector_id for event in self.events):
            msg = "every event must come from the request's collector"
            raise ValueError(msg)
        return self


class EventsResponse(Envelope):
    """``POST /v1/events``: the ids now stored *and* applied; the collector deletes those."""

    acked: tuple[int, ...] = ()


class StatusResponse(Envelope):
    """``POST /v1/status``. ``stale``: older than one already applied, so it changed nothing."""

    ok: bool = True
    stale: bool = False


class GroupsResponse(Envelope):
    """``POST /v1/groups`` (409 without the lease). ``stale`` as for :class:`StatusResponse`."""

    ok: bool = True
    groups: int = Field(default=0, ge=0)
    changed: int = Field(default=0, ge=0)
    stale: bool = False


class ErrorResponse(Envelope):
    """Any 4xx/5xx answer; ``error`` names the problem, never echoes the input."""

    error: str


class MonitoredGroup(WireModel):
    group_id: DigitId
    tier: int = Field(default=1, ge=1, le=9)
    listings: bool = False


class Lease(WireModel):
    collector_id: CollectorId
    granted: bool
    generation: Generation | None = None
    """Increases every time the lease moves. A command carries the generation it was issued
    under, so one meant for an earlier holder's view of the account is never applied."""
    valid_for: int | None = Field(default=None, ge=60)
    """Seconds a grant stays good without a newer answer, measured by the collector's monotonic
    clock from the moment it received this one. ``None``: until told otherwise."""


class _Targeted(WireModel):
    collector_id: CollectorId | None = None
    """The collector this command is for; another one must ignore it (and keep the nonce
    unused). ``None``: an older brain, which sent commands only to the lease holder."""
    lease_generation: Generation | None = None
    """The lease generation it was issued under; see :attr:`Lease.generation`."""

    def meant_for(self, collector_id: str, generation: int | None) -> bool:
        if self.collector_id is not None and self.collector_id != collector_id:
            return False
        return self.lease_generation is None or self.lease_generation == generation


class ResumeCommand(_Targeted):
    """Clear the block with this id, once. A stale id never clears a newer block."""

    kind: Literal["resume"] = "resume"
    block_id: int = Field(ge=1)
    nonce: Nonce
    lock_lifted: bool = False
    """The owner confirmed that Facebook's ``advanced_protection`` lock is lifted; only this lets
    a resume arm the probe for a ``no_probe`` block (the collector's ``--lock-lifted``)."""


class RefreshGroupsCommand(_Targeted):
    kind: Literal["refresh_groups"] = "refresh_groups"
    nonce: Nonce


Command = Annotated[ResumeCommand | RefreshGroupsCommand, Field(discriminator="kind")]
_COMMAND_KINDS: Final = frozenset({"resume", "refresh_groups"})


class Control(Envelope):
    """The brain's answer to ``GET /v1/control``.

    ``monitored``, ``lease``, ``pace_cap`` and ``paused`` are state and are re-sent every poll;
    ``commands`` are edges, each applied at most once (its nonce is remembered).
    """

    issued_at: Epoch
    monitored: tuple[MonitoredGroup, ...] = Field(default=(), max_length=MAX_MONITORED)
    lease: Lease
    commands: tuple[Command, ...] = ()
    pace_cap: Profile | None = None
    paused: bool = False

    @field_validator("commands", mode="before")
    @classmethod
    def _skip_unknown_commands(cls, value: Any) -> Any:
        # A newer brain may know commands this collector does not. Ignoring them is the same
        # rule as ignoring unknown fields; failing the whole poll would also drop the lease.
        if isinstance(value, list | tuple):
            return [
                c for c in value if not isinstance(c, Mapping) or c.get("kind") in _COMMAND_KINDS
            ]
        return value

    @model_validator(mode="after")
    def _unique_groups(self) -> Control:
        ids = [m.group_id for m in self.monitored]
        if len(ids) != len(set(ids)):
            msg = "a monitored group appears twice"
            raise ValueError(msg)
        return self

    def lease_held_by(self, collector_id: str) -> bool:
        return self.lease.granted and self.lease.collector_id == collector_id

    def commands_for(self, collector_id: str) -> tuple[Command, ...]:
        """The commands this collector may apply: its lease, and meant for it and this lease."""
        if not self.lease_held_by(collector_id):
            return ()
        generation = self.lease.generation
        return tuple(c for c in self.commands if c.meant_for(collector_id, generation))


# -- sizes and lenient parsing -------------------------------------------------------------------


def to_json(model: BaseModel) -> bytes:
    """Serialise for the wire. pydantic writes UTF-8 as is (the ``ensure_ascii=False`` size)."""
    return model.model_dump_json().encode("utf-8")


def items_overhead(collector_id: str, v: int | None = None) -> int:
    """Bytes of an ``ItemsRequest`` envelope with an empty item list."""
    return len(items_body(collector_id, (), v=v))


def items_body(collector_id: str, payloads: Sequence[str], v: int | None = None) -> bytes:
    """An ``ItemsRequest`` body built from already-serialised items (the outbox's payloads).

    Byte-identical to ``to_json(ItemsRequest(...))`` for the same items, so a batch planned on
    stored sizes is exactly the batch that is sent.
    """
    version = WIRE_VERSION if v is None else v
    head = f'{{"v":{version},"collector_id":{json.dumps(collector_id, ensure_ascii=False)},'
    return f'{head}"items":[{",".join(payloads)}]}}'.encode()


def plan_batches(
    sizes: Sequence[int], *, overhead: int, limit: int = MAX_BATCH_BYTES
) -> list[tuple[int, int]]:
    """Split items (in order) into ``[start, end)`` runs whose request body fits ``limit``.

    An item too large to fit even alone gets a batch of its own rather than blocking the
    queue; with the text cap and field limits above that cannot happen for a valid item.
    """
    batches: list[tuple[int, int]] = []
    start = 0
    total = overhead
    for index, size in enumerate(sizes):
        extra = size if index == start else size + 1  # a comma between items
        if index > start and total + extra > limit:
            batches.append((start, index))
            start, total = index, overhead + size
        else:
            total += extra
    if start < len(sizes):
        batches.append((start, len(sizes)))
    return batches


@dataclass(frozen=True, slots=True)
class ParsedItems:
    """An items request read item by item, so one bad item does not sink the batch."""

    collector_id: str
    items: tuple[Item, ...]
    rejected: tuple[ItemResult, ...]
    unkeyed: int
    """Invalid items without a usable (group_id, post_id): nothing can be answered for them."""


def describe_validation(exc: ValidationError) -> str:
    """Where validation failed and why, without the input value (it may be post text)."""
    parts = []
    for error in exc.errors(include_input=False, include_url=False):
        where = ".".join(str(p) for p in error.get("loc", ())) or "item"
        parts.append(f"{where}: {error.get('type', 'invalid')}")
    return "; ".join(parts)[:MAX_REASON_LEN] or "invalid"


class _Head(Envelope):
    collector_id: CollectorId


class _KeyOnly(WireModel):
    group_id: DigitId
    post_id: DigitId


def parse_items_request(data: Any) -> ParsedItems:
    """Validate the envelope strictly and each item on its own.

    Raises ``ValidationError`` or ``ValueError`` only when the envelope itself is unusable (the
    whole request is then a 4xx). An item that fails validation becomes a ``rejected`` result
    keyed by its ids, so the collector can delete it instead of resending it forever.
    """
    head = _Head.model_validate(data)
    raw_items = data.get("items") if isinstance(data, Mapping) else None
    if not isinstance(raw_items, list):
        msg = "items must be a list"
        raise ValueError(msg)  # noqa: TRY004 - a malformed body, reported as a value error
    if len(raw_items) > MAX_ITEMS:
        msg = f"at most {MAX_ITEMS} items per request"
        raise ValueError(msg)
    items: list[Item] = []
    rejected: list[ItemResult] = []
    unkeyed = 0
    for raw in raw_items:
        try:
            item = Item.model_validate(raw)
        except ValidationError as exc:
            reason = describe_validation(exc)
        else:
            if item.collector_id == head.collector_id:
                items.append(item)
                continue
            reason = "collector_id: differs from the request"
        try:
            key = _KeyOnly.model_validate(raw)
        except ValidationError:
            unkeyed += 1
            continue
        rejected.append(
            ItemResult(
                group_id=key.group_id,
                post_id=key.post_id,
                result=ItemOutcome.REJECTED,
                reason=reason,
            )
        )
    return ParsedItems(
        collector_id=head.collector_id,
        items=tuple(items),
        rejected=tuple(rejected),
        unkeyed=unkeyed,
    )
