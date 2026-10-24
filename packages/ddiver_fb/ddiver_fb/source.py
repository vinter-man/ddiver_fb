"""FacebookSource: one post from the collector becomes zero or more durable notifications.

This is the brain's counterpart of ddiver's ``Listener.handle``, with the same rules and one
difference in how durability is proven. ddiver's listener holds a cursor back until every match
is written; here the collector holds the post in its outbox until the brain answers, so the
answer is the cursor:

1. unknown or unwatched group → ``not_monitored`` (the collector drops it);
2. already recorded in ``fb_items`` → ``duplicate``;
3. match against every subscriber's rules (ignore list and exclusions included; keyword rules
   read the text, ``from:`` rules the author's id or name), skip users who subscribed after
   the post was written (D10: no backfill), and ``claim_delivery`` each — the outbox key is
   ``(user, group, post_id)``, so a resend claims nothing twice;
4. keep the post itself for seven days (``fb_texts``, the AI rule's input; a post that broke a
   step above is not kept), and queue it for the AI rule's wishes on its group (``ai.py``);
5. only then record the post in ``fb_items`` and answer ``stored``.

A busy database raises out of :meth:`process` before step 5, the ingest API answers 503, and the
collector resends: the claims already made are duplicates on the second pass and the rest are
made then (invariant 5). Every claim is handed to delivery the moment it exists, because the
outbox row already makes it durable; a claim whose hand-off was lost is picked up by the
periodic outbox rescan.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final

from ddiver_core.formatter import Payload
from ddiver_core.matcher import Match, Matcher
from ddiver_core.rules import PreparedText, prepare_text
from ddiver_core.storage import OutboxItem, Storage
from ddiver_core.timeutil import Clock, utcnow
from ddiver_fb.ai import AiStore
from ddiver_fb.store import FbGroup, FbStore
from fbwire import GroupPrivacy, Item, ItemOutcome

log = logging.getLogger(__name__)

SOURCE_FACEBOOK: Final = "fb"
"""The ``Payload.source`` of every notification this app writes: no derived ``t.me`` links."""

DeliverHook = Callable[[OutboxItem], Awaitable[Any]]


class PoisonItem(Exception):  # noqa: N818 - a verdict on the item, not an error of ours
    """The post's own content broke a step that reads nothing else: retrying cannot help, so
    the ingest API rejects it at once (every other failure is retried, see ``api.py``)."""


@dataclass(slots=True)
class SourceMetrics:
    """Counted per processed item, and per claim; read by /status and /metrics."""

    results: dict[ItemOutcome, int] = field(default_factory=lambda: dict.fromkeys(ItemOutcome, 0))
    matched: int = 0
    claimed: int = 0
    claim_duplicates: int = 0
    skipped_before_subscription: int = 0

    def count(self, outcome: ItemOutcome, n: int = 1) -> None:
        self.results[outcome] = self.results.get(outcome, 0) + n


def build_payload(item: Item, group: FbGroup, match: Match, prepared: PreparedText) -> Payload:
    """The notification snapshot for one match, with Facebook's own links."""
    author = item.author
    anonymous = author.anonymous
    spans = [prepared.to_original(span) for span in match.spans]
    private = item.group_privacy is GroupPrivacy.PRIVATE or (
        item.group_privacy is GroupPrivacy.UNKNOWN and group.private
    )
    return Payload(
        chat_id=group.chat_id,
        chat_title=group.title or item.group_name,
        chat_username=None,
        chat_kind="group",
        msg_id=int(item.post_id),
        topic_id=None,
        date=dt.datetime.fromtimestamp(item.created_at, dt.UTC),
        text=item.text,
        spans=[(s.start, s.end) for s in spans],
        rule_pattern=match.rule.pattern,
        rule_kind=match.rule.kind.value,
        rule_is_global=match.rule.is_global,
        sender_id=author_id(item),
        sender_name="" if anonymous else (author.name or ""),
        sender_username=None,
        # The core labels a nameless "channel" sender as anonymous; the overlay says
        # "Anonymous participant", which is what Facebook calls it.
        sender_is_channel=anonymous,
        is_forwarded=item.is_share,
        source=SOURCE_FACEBOOK,
        url=item.url,
        chat_url=item.group_url,
        sender_url=None if anonymous else author.url,
        chat_private=private,
        late=item.late,
    )


def author_id(item: Item) -> int | None:
    """The author as the ignore list and the matcher know them; ``None`` when anonymous."""
    if item.author.anonymous or item.author.id is None:
        return None
    return int(item.author.id)


def author_name(item: Item) -> str:
    """The name a ``from:`` rule may match; empty for an anonymous participant.

    An anonymous participant's name is a per-group pseudonym (or Facebook's own "Anonymous
    participant"), which identifies nobody: a ``from:`` rule on it would watch every anonymous
    post of the group, or a stranger who happens to get the same pseudonym elsewhere.
    """
    if item.author.anonymous:
        return ""
    return item.author.name or ""


class FacebookSource:
    def __init__(
        self,
        storage: Storage,
        fb: FbStore,
        matcher: Matcher,
        *,
        deliver: DeliverHook,
        ai: AiStore | None = None,
        clock: Clock = utcnow,
    ) -> None:
        self._storage = storage
        self._ai = ai
        self._fb = fb
        self._matcher = matcher
        self._deliver = deliver
        self._clock = clock
        self.metrics = SourceMetrics()
        self.last_item_at: dict[int, int] = {}
        """Newest ``created_at`` per group, for ``fbb_group_last_item_age_seconds``."""

    async def prime(self) -> None:
        self.last_item_at = await self._fb.last_item_times()

    async def process(self, item: Item) -> ItemOutcome:
        """Handle one valid item. Raises ``sqlite3.OperationalError`` when the database is busy;
        the item is then not acknowledged and comes back."""
        outcome = await self._process(item)
        self.metrics.count(outcome)
        return outcome

    async def _process(self, item: Item) -> ItemOutcome:
        group_id, post_id = int(item.group_id), int(item.post_id)
        if not await self._monitored(group_id):
            return ItemOutcome.NOT_MONITORED
        group = await self._fb.group(group_id)
        if group is None:  # monitored implies a synced group; a hand-made chat row is not one
            return ItemOutcome.NOT_MONITORED
        if await self._fb.has_item(group_id, post_id):
            return ItemOutcome.DUPLICATE

        try:
            prepared = prepare_text(item.text)
        except Exception as exc:
            raise PoisonItem from exc
        # ``from:`` rules see the author's id and name. Facebook shows no username, so a
        # ``from:@name`` rule has nothing to compare with and matches no post (the bot's help
        # says so); the id is the one every notification shows next to the author's name.
        matches = await self._matcher.evaluate(
            group_id,
            prepared.shadow,
            author_id(item),
            username=None,
            display_name=author_name(item),
        )
        claimed = 0
        if matches:
            self.metrics.matched += 1
            since = await self._storage.subscribed_since(group_id)
            posted = dt.datetime.fromtimestamp(item.created_at, dt.UTC)
            for match in matches:
                started = since.get(match.user_id)
                if started is None or posted < started:
                    # Subscribed after it was posted (or unsubscribed while it was matched):
                    # a late-approved or late-seen post is not a backfill ticket.
                    self.metrics.skipped_before_subscription += 1
                    continue
                try:
                    payload = build_payload(item, group, match, prepared).to_json()
                except Exception as exc:
                    raise PoisonItem from exc
                row = await self._storage.claim_delivery(
                    match.user_id, group_id, post_id, match.rule.id, payload
                )
                if row is None:
                    self.metrics.claim_duplicates += 1
                    continue
                claimed += 1
                self.metrics.claimed += 1
                self._matcher.note_hit(match.rule.id)
                await self._deliver(row)

        received_at = int(self._clock().timestamp())
        await self._fb.record_text(item, received_at=received_at)
        if self._ai is not None:
            await self._ai.enqueue(group_id, post_id, created_at=item.created_at, now=received_at)
        await self._fb.record_item(
            group_id,
            post_id,
            created_at=item.created_at,
            received_at=received_at,
            via=item.via.value,
            late=item.late,
            claimed=claimed > 0,
        )
        if item.created_at > self.last_item_at.get(group_id, 0):
            self.last_item_at[group_id] = item.created_at
        return ItemOutcome.STORED

    async def _monitored(self, group_id: int) -> bool:
        """The matcher's cached answer, confirmed against the database before a "no".

        ``not_monitored`` makes the collector delete the post, so it must never come from a
        stale cache: a refresh after a group change can fail (the change itself is durable),
        and ``/v1/control`` computes the monitored set from the database, so the collector
        would be told to read a group whose posts the brain then drops.
        """
        if self._matcher.is_monitored(group_id):
            return True
        if group_id not in await self._storage.monitored_chat_ids():
            return False
        log.warning("the matcher's monitored set was stale for group %d; reloading", group_id)
        await self._matcher.invalidate(None)
        return self._matcher.is_monitored(group_id)
