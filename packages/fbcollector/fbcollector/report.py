"""The phase-0 report (``docs/phase0-report.md``), computed from the collector's own database.

Phase 0 ends on data, not dates (DESIGN, Phase 0): at least 48 at-home, awake daytime hours of
reading and at least 300 posts, 30 in every group a decision is made for. This module computes
every measure of the design's table from ``state.sqlite`` — read-only, with the standard
library's ``sqlite3``, so it can run next to a live collector — and renders Markdown with the
decision rule beside each number. It prints ids and counts only, never post text.

Where the data cannot answer a question (the display's sleep state, the feed cursor's
``feed_ordering``), the report says so instead of guessing.

Latency, the late rate, the volume and the "enough data" verdict count *delivered* posts only
(``seen.outcome = 'queued'``): a group's first read also marks seen its history from before the
baseline, posts older than 7 days and posts of groups nobody monitors, and first-seen minus
created for those says nothing about the collector. Rows from before the column existed are
judged the same way from the group's baseline and the novelty window. Latency and the late rate
count, of those, only posts seen within 30 min (``ON_DUTY``) or a feed or group read began within
30 min of: a post created before reading began, in quiet hours, a break or while the Mac slept
waits for the schedule, and its wait says nothing about how fast the reading finds posts.

The last section, the parser diagnostics, is for whoever fixes the parser and may be read by
an operator who must not see posts: the text-path histogram comes from the reads' own health
counters, and the shapes of stories without text from the saved steps
(``fbcollector.diagnostics``), as key names, typenames, lengths and counts only. The other
sections print group ids; ``diagnostics_only`` (``fbcollector report --diagnostics-only``, the
operator door's ``diagnostics`` verb) renders that last section alone.
"""

from __future__ import annotations

import bisect
import datetime as dt
import json
import math
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from fbcollector.diagnostics import (
    CLASS_NOTES,
    TEXT_PATH_ORDER,
    RawScan,
    render_scan,
    render_scripts,
    scan_raw,
    text_label,
)
from fbcollector.parser import NO_NAME
from fbcollector.stories import ATTACHMENT_LISTING, TRANSLATION_MARK, TextClass

HOURS_NEEDED: Final = 48
POSTS_NEEDED: Final = 300
PER_GROUP_NEEDED: Final = 30
FEED_MISS_OK: Final = 0.05
FILL_OK: Final = 0.95
LATE_SECONDS: Final = 2 * 3600
ON_DUTY: Final = 30 * 60
"""A post is measured only if it was seen, or a feed or group read began, within this long after
its creation (the feed's slowest cadence is 18 min)."""
DAY: Final = 86400.0
NOVELTY: Final = 7 * DAY
OLD_TEXT_PATHS: Final = {"listing": ATTACHMENT_LISTING}
"""Text paths under the names reads recorded before a rename."""


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def _pct(part: float, whole: float) -> str:
    return "-" if not whole else f"{100 * part / whole:.1f} %"


def _minutes(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds / 60:.1f} min"


@dataclass
class ReadRow:
    id: int
    surface: str
    group_id: str | None
    started_at: float
    newest: int | None
    oldest: int | None
    reached: bool
    steps: int
    outcome: str | None
    page_state: str | None
    stats: dict[str, Any]


@dataclass
class Data:
    reads: list[ReadRow] = field(default_factory=list)
    seen: list[tuple[str, str, int | None, float, str | None]] = field(default_factory=list)
    delivered: list[tuple[str, str, int | None, float, str | None]] = field(default_factory=list)
    """The ``seen`` rows that became items (the measures of latency and volume)."""
    read_starts: list[float] = field(default_factory=list)
    """When every feed and group read began, before ``since`` too: a post in the window may have
    been created before it."""
    sightings: dict[int, set[str]] = field(default_factory=dict)
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    blocks: list[dict[str, Any]] = field(default_factory=list)
    navigations: list[float] = field(default_factory=list)
    scrolls: list[float] = field(default_factory=list)


def load(path: Path, *, since: float | None = None) -> Data:
    """Everything the report needs, from a read-only connection."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        start = since if since is not None else 0.0
        data = Data()
        for r in db.execute(
            "SELECT * FROM reads WHERE started_at>=? ORDER BY started_at", (start,)
        ):
            data.reads.append(
                ReadRow(
                    id=int(r["id"]),
                    surface=str(r["surface"]),
                    group_id=r["group_id"],
                    started_at=float(r["started_at"]),
                    newest=r["newest"],
                    oldest=r["oldest"],
                    reached=bool(r["target_reached"]),
                    steps=int(r["steps"]),
                    outcome=r["outcome"],
                    page_state=r["page_state"],
                    stats=json.loads(r["stats"] or "{}"),
                )
            )
        columns = {str(r[1]) for r in db.execute("PRAGMA table_info(seen)")}
        outcome = "outcome" if "outcome" in columns else "NULL"
        rows = list(
            db.execute(
                f"SELECT group_id, post_id, created_at, first_seen_at, via, {outcome} FROM seen"  # noqa: S608 - fixed column
                " WHERE first_seen_at>=?",
                (start,),
            )
        )
        data.seen = [(str(r[0]), str(r[1]), r[2], float(r[3]), r[4]) for r in rows]
        data.read_starts = [
            float(r[0])
            for r in db.execute(
                "SELECT started_at FROM reads WHERE surface IN ('feed', 'group')"
                " ORDER BY started_at"
            )
        ]
        for read_id, post_id in db.execute(
            "SELECT read_id, post_id FROM sightings WHERE seen_at>=? AND read_id IS NOT NULL",
            (start,),
        ):
            data.sightings.setdefault(int(read_id), set()).add(str(post_id))
        for r in db.execute("SELECT * FROM groups"):
            data.groups[str(r["id"])] = dict(r)
        data.delivered = [
            (str(r[0]), str(r[1]), r[2], float(r[3]), r[4])
            for r in rows
            if _delivered(r[5], r[2], float(r[3]), data.groups.get(str(r[0])))
        ]
        data.blocks = [
            dict(r) for r in db.execute("SELECT * FROM blocks WHERE opened_at>=?", (start,))
        ]
        for kind, at in db.execute(
            "SELECT kind, at FROM budget_events WHERE at>=? ORDER BY at", (start,)
        ):
            (data.navigations if kind == "navigation" else data.scrolls).append(float(at))
        return data
    finally:
        db.close()


def _delivered(
    outcome: str | None, created: int | None, first: float, group: dict[str, Any] | None
) -> bool:
    """Whether a seen post became an item (for rows without ``outcome``: the same rules)."""
    if outcome is not None:
        return str(outcome) == "queued"
    if created is None or group is None or not group.get("monitored"):
        return False
    return created >= float(group.get("baseline_at") or 0.0) and first - created <= NOVELTY


# -- measures -----------------------------------------------------------------------------------


def reading_hours(data: Data, zone: dt.tzinfo) -> int:
    """Distinct local hours with at least one read that got a recognised page."""
    hours = {
        dt.datetime.fromtimestamp(r.started_at, zone).strftime("%Y-%m-%d %H")
        for r in data.reads
        if r.outcome in {"complete", "gap"}
    }
    return len(hours)


def posts(data: Data) -> Counter[str]:
    """Delivered posts per group."""
    return Counter(group for group, _post, _c, _f, via in data.delivered if via is not None)


def _followed(group: dict[str, Any] | None) -> bool:
    """Followed and actually in the feed: a group flagged ``not_followed`` or ``feed_absent``
    (followed but missing from the feed for 48 h, e.g. snoozed) is left out of ``feed_miss``."""
    if group is None:
        return False
    flags = set(json.loads(group.get("flags") or "[]"))
    subscribed = group.get("subscribe_status") in {None, "IS_SUBSCRIBED"}
    return subscribed and not flags & {"not_followed", "feed_absent"}


@dataclass(frozen=True, slots=True)
class FeedMiss:
    group_id: str
    listings: bool
    covered: int
    missed: int

    @property
    def rate(self) -> float:
        return self.missed / self.covered if self.covered else 0.0


def feed_miss(data: Data) -> list[FeedMiss]:
    """DESIGN's ``feed_miss``: a post counts only if its creation time lies inside a completed
    feed read's coverage window, its group was followed, and that read did not contain it."""
    windows = [
        r
        for r in data.reads
        if r.surface == "feed" and r.outcome == "complete" and r.newest and r.oldest
    ]
    covered: Counter[str] = Counter()
    missed: Counter[str] = Counter()
    for group_id, post_id, created, _first, via in data.seen:
        if created is None or via is None or not _followed(data.groups.get(group_id)):
            continue
        reads = [r for r in windows if (r.oldest or 0) <= created <= (r.newest or 0)]
        if not reads:
            continue  # nothing covered it: feed latency, not a miss
        covered[group_id] += 1
        missed[group_id] += any(post_id not in data.sightings.get(r.id, set()) for r in reads)
    return [
        FeedMiss(g, bool((data.groups.get(g) or {}).get("listings")), covered[g], missed[g])
        for g in sorted(covered)
    ]


def field_fill(data: Data) -> dict[str, float]:
    found = accepted = no_time = no_group = no_text = no_post = 0
    for read in data.reads:
        if read.surface != "feed":
            continue
        health = read.stats.get("health") or {}
        found += int(health.get("found", 0))
        accepted += int(health.get("accepted", 0))
        missing = health.get("missing") or {}
        no_time += int(missing.get("no_created_at", 0))
        no_group += int(missing.get("no_group", 0))
        no_text += int(missing.get("no_text", 0))
        no_post += int((health.get("dropped") or {}).get("no_post_id", 0))
    if not found:
        return {}
    return {
        "post_id": 1 - no_post / found,
        "created_at": 1 - no_time / found,
        "group": 1 - no_group / found,
        "url": 1 - no_group / found,
        "text": 1 - no_text / found,
        "accepted": accepted / found,
    }


def text_completeness(data: Data) -> dict[str, int]:
    totals: Counter[str] = Counter()
    for read in data.reads:
        texts = read.stats.get("texts") or {}
        totals["texts"] += int(texts.get("n", 0))
        totals["ellipsis"] += int(texts.get("ellipsis", 0))
        dom = read.stats.get("dom") or {}
        totals["see_more"] += int(dom.get("see_more", 0))
        totals["reads_with_see_more"] += int(dom.get("see_more", 0) > 0)
    return dict(totals)


def group_paths(data: Data) -> dict[str, tuple[int, int]]:
    paths: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for read in data.reads:
        for path, (count, agree) in (read.stats.get("group_paths") or {}).items():
            paths[path][0] += int(count)
            paths[path][1] += int(agree)
    return {p: (v[0], v[1]) for p, v in sorted(paths.items(), key=lambda kv: -kv[1][0])}


def measured(data: Data) -> tuple[list[tuple[int, float, str]], int]:
    """The delivered posts that measure the reading, as (created, first seen, via), and how many
    were left out: not seen, and no feed or group read began, within ``ON_DUTY`` of their
    creation. A failed read counts: a delay behind a warning page is the collector's own."""
    starts = data.read_starts
    kept: list[tuple[int, float, str]] = []
    idle = 0
    for _group, _post, created, first, via in data.delivered:
        if created is None or via is None:
            continue
        after = bisect.bisect_left(starts, created)
        soon = after < len(starts) and starts[after] - created <= ON_DUTY
        if soon or first - created <= ON_DUTY:
            kept.append((created, first, via))
        else:
            idle += 1
    return kept, idle


def latencies(data: Data) -> dict[str, list[float]]:
    out: dict[str, list[float]] = defaultdict(list)
    for created, first, via in measured(data)[0]:
        out["feed" if via == "feed" else "backstop"].append(max(0.0, first - created))
    return out


def late_rate(data: Data) -> tuple[int, int]:
    kept = measured(data)[0]
    return sum(first - created > LATE_SECONDS for created, first, _via in kept), len(kept)


def steps_by_surface(data: Data) -> dict[str, list[int]]:
    out: dict[str, list[int]] = defaultdict(list)
    for read in data.reads:
        if read.outcome in {"complete", "gap"}:
            out[read.surface].append(read.steps)
    return out


def pagination(data: Data, zone: dt.tzinfo) -> tuple[int, int, dict[int, tuple[int, int]]]:
    scrolls = fired = 0
    by_hour: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for read in data.reads:
        info = read.stats.get("pagination") or {}
        s, f = int(info.get("scrolls", 0)), int(info.get("fired", 0))
        scrolls += s
        fired += f
        hour = dt.datetime.fromtimestamp(read.started_at, zone).hour
        by_hour[hour][0] += s
        by_hour[hour][1] += f
    return scrolls, fired, {h: (v[0], v[1]) for h, v in sorted(by_hour.items())}


def stories_per_response(data: Data) -> float | None:
    """Posts per pagination response: the regular stories of the GraphQL bodies that carried
    stories, over the number of those bodies (the SSR, highlights and other queries excluded)."""
    stories = responses = 0
    for read in data.reads:
        health = read.stats.get("health") or {}
        responses += int(health.get("story_bodies", 0))
        stories += int(health.get("batch_stories", 0))
    return stories / responses if responses else None


@dataclass
class Measured:
    """A per-read health counter over the reads that record it, as ``[feed reads, group
    reads, last 24 h]``. A read saved before the counter existed says nothing either way: it
    is left out of the count and of the stories it is counted against, never read as none."""

    found: list[int] = field(default_factory=lambda: [0, 0, 0])
    of: list[int] = field(default_factory=lambda: [0, 0, 0])
    """The stories of the reads that record it: the counter's own denominator."""
    without: int = 0
    """Feed and group reads that do not record it."""

    def add(
        self, health: Mapping[str, Any], key: str, stories: int, columns: Iterable[int]
    ) -> None:
        if key not in health:
            self.without += 1
            return
        for column in columns:
            self.found[column] += int(health[key])
            self.of[column] += stories

    def counts(self) -> str:
        found, of = self.found, self.of
        shown = (
            f"feed {found[0]} of {of[0]}, group {found[1]} of {of[1]},"
            f" last 24 h {found[2]} of {of[2]}"
        )
        if not self.without:
            return shown
        reads = "read" if self.without == 1 else "reads"
        return (
            f"{shown} (of the stories of the reads that record it; {self.without} older"
            f" {reads} without it left out)"
        )


@dataclass(frozen=True, slots=True)
class TextPaths:
    """Stories per text path the parser took, as ``[feed reads, group reads, last 24 h]``."""

    rows: dict[str, list[int]]
    unread: list[int]
    """Stories with a message section the parser could not read (``missing.no_text``)."""
    classes: dict[str, list[int]] = field(default_factory=dict)
    """Stories without a message of their own nor a shared text, by class
    (``health.text_classes``)."""
    card_beside_words: Measured = field(default_factory=Measured)
    """Stories with a message of their own whose text also got a card's lines
    (``health.card_beside_words``)."""
    translation_fields: Measured = field(default_factory=Measured)
    """Stories whose tree holds a key that speaks of a translation
    (``health.translation_fields``)."""

    @property
    def stories(self) -> list[int]:
        return [sum(row[i] for row in self.rows.values()) for i in range(3)]

    @property
    def empty(self) -> list[int]:
        return self.rows.get(NO_NAME, [0, 0, 0])


def text_paths(data: Data, *, now: float) -> TextPaths:
    """Where the stories' text came from (``health.text_paths``), and why the ones without a
    message of their own have the text they have (``health.text_classes``); a post read again
    counts again (the parser's alert counts each post once, from its own history)."""
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    classes: dict[str, list[int]] = {kind.value: [0, 0, 0] for kind in TextClass}
    unread = [0, 0, 0]
    beside, translation = Measured(), Measured()
    for read in data.reads:
        if read.surface not in {"feed", "group"}:
            continue
        health = read.stats.get("health") or {}
        columns = (
            0 if read.surface == "feed" else 1,
            *((2,) if read.started_at >= now - DAY else ()),
        )
        read_stories = 0
        for label, count in (health.get("text_paths") or {}).items():
            read_stories += int(count)
            for column in columns:
                counts[text_label(OLD_TEXT_PATHS.get(label, label))][column] += int(count)
        no_text = int((health.get("missing") or {}).get("no_text", 0))
        for column in columns:
            unread[column] += no_text
        beside.add(health, "card_beside_words", read_stories, columns)
        translation.add(health, "translation_fields", read_stories, columns)
        for kind, count in (health.get("text_classes") or {}).items():
            row = classes.setdefault(text_label(kind), [0, 0, 0])
            for column in columns:
                row[column] += int(count)
    order = [*TEXT_PATH_ORDER, *sorted(set(counts) - {*TEXT_PATH_ORDER, NO_NAME}), NO_NAME]
    return TextPaths(
        {label: list(counts.get(label, [0, 0, 0])) for label in order},
        unread,
        classes,
        beside,
        translation,
    )


def resources(data: Data) -> list[float]:
    return [
        float(r.stats["resources"]["chrome_rss_mb"])
        for r in data.reads
        if isinstance(r.stats.get("resources"), dict) and "chrome_rss_mb" in r.stats["resources"]
    ]


def per_day(times: Iterable[float], zone: dt.tzinfo) -> dict[str, int]:
    days: Counter[str] = Counter()
    for at in times:
        days[dt.datetime.fromtimestamp(at, zone).strftime("%Y-%m-%d")] += 1
    return dict(sorted(days.items()))


# -- rendering ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class View:
    data: Data
    zone: dt.tzinfo
    now: float
    raw: RawScan | None = None

    @property
    def span(self) -> tuple[float | None, float | None]:
        times = [r.started_at for r in self.data.reads]
        return min(times, default=None), max(times, default=None)

    def local(self, epoch: float, fmt: str = "%Y-%m-%d %H:%M") -> str:
        return dt.datetime.fromtimestamp(epoch, self.zone).strftime(fmt)


def _done(view: View) -> list[str]:
    counts = posts(view.data)
    hours = reading_hours(view.data, view.zone)
    total = sum(counts.values())
    start, end = view.span
    period = "-" if start is None or end is None else f"{view.local(start)} → {view.local(end)}"
    decidable = [g for g, n in counts.items() if n >= PER_GROUP_NEEDED]
    enough = hours >= HOURS_NEEDED and total >= POSTS_NEEDED
    return [
        "## Is phase 0 done?",
        "",
        f"- Reading period: {period}",
        f"- Hours with a recognised read: **{hours}** (needed ≥ {HOURS_NEEDED})",
        f"- Posts: **{total}** (needed ≥ {POSTS_NEEDED})",
        f"- Groups with ≥ {PER_GROUP_NEEDED} posts (decidable): {len(decidable)}",
        f"- Verdict: **{'enough data' if enough else 'keep observing'}**",
    ]


def _feed_miss(view: View) -> list[str]:
    out = [
        "## Does the Groups feed miss posts? (feed_miss)",
        "",
        f"Rule: ≤ {FEED_MISS_OK:.0%} per group keeps the 5-10 min target; above it, find why.",
        "",
        "| Group | Listings | Covered by a feed read | Missed | Rate |",
        "|---|---|---|---|---|",
    ]
    for miss in feed_miss(view.data):
        mark = " ⚠️" if miss.rate > FEED_MISS_OK else ""
        listings = "yes" if miss.listings else "no"
        out.append(
            f"| {miss.group_id} | {listings} | {miss.covered} | {miss.missed}"
            f" | {miss.rate:.1%}{mark} |"
        )
    return out


def _fill(view: View) -> list[str]:
    out = ["## Is the feed's JSON what we think? (field fill)", ""]
    fill = field_fill(view.data)
    if not fill:
        out.append("No feed reads yet.")
    for name, value in fill.items():
        mark = "" if value >= FILL_OK else " ⚠️ below 95 %: fix the parser first"
        out.append(f"- {name}: {value:.1%}{mark}")
    return out


def _text(view: View) -> list[str]:
    text = text_completeness(view.data)
    texts, ellipsis = text.get("texts", 0), text.get("ellipsis", 0)
    return [
        "## Is the text complete without “See more”?",
        "",
        f"- Post texts read from JSON: {texts}",
        f"- Ending in an ellipsis: {ellipsis} ({_pct(ellipsis, texts)})",
        f"- “See more” buttons on the page (max per read, summed): {text.get('see_more', 0)}",
        "- Truncated JSON texts: the owner chooses one allowed click, or a marker.",
    ]


def _groups(view: View) -> list[str]:
    out = [
        "## CHRONOLOGICAL in private groups; follow state",
        "",
        "| Group | Privacy | Sorting | Followed | Flags |",
        "|---|---|---|---|---|",
    ]
    for gid, group in sorted(view.data.groups.items()):
        if not group.get("monitored"):
            continue
        flags = ", ".join(json.loads(group.get("flags") or "[]")) or "-"
        out.append(
            f"| {gid} | {group.get('privacy')} | {group.get('sorting_setting') or '-'}"
            f" | {group.get('subscribe_status') or '-'} | {flags} |"
        )
    out += [
        "",
        "## Which field names a feed story's group?",
        "",
        "| Path | Stories | Agrees with the chosen group |",
        "|---|---|---|",
    ]
    out += [
        f"| `{path}` | {count} | {_pct(agree, count)} |"
        for path, (count, agree) in group_paths(view.data).items()
    ]
    out += ["", "## Listings", ""]
    for gid, group in sorted(view.data.groups.items()):
        if group.get("listings"):
            vias = Counter(v for g, _p, _c, _f, v in view.data.seen if g == gid and v is not None)
            summary = ", ".join(f"{k} {n}" for k, n in sorted(vias.items())) or "-"
            out.append(f"- {gid}: {summary}")
    out.append("- Groups without the listings switch are not compared.")
    return out


def _volume(view: View) -> list[str]:
    data = view.data
    late, seen_total = late_rate(data)
    late_share = _pct(late, seen_total)
    out = [
        "## Late approvals",
        "",
        f"- Posts first seen > 2 h after creation: {late} of {seen_total} ({late_share})",
        (
            f"- Not measured: {measured(data)[1]} posts neither seen nor a feed or group read"
            f" began within {ON_DUTY // 60} min of (created before reading began, in quiet"
            " hours, a break or while the Mac slept): they wait for the schedule, not for the"
            " reading."
        ),
        "- The feed cursor's `feed_ordering` is not recorded; the late rate stands in for it.",
        "",
        "## Volume",
        "",
    ]
    start, end = view.span
    days = max(((end or view.now) - (start or view.now)) / DAY, 1 / 24)
    out += [f"- {gid}: {n} posts, {n / days:.1f} per day" for gid, n in posts(data).most_common()]
    for surface, values in steps_by_surface(data).items():
        mean = sum(values) / len(values) if values else 0.0
        out.append(f"- steps per {surface} read: mean {mean:.2f}, p95 {percentile(values, 0.95)}")
    ratio = stories_per_response(data)
    out.append(f"- stories per GraphQL response: {'-' if ratio is None else f'{ratio:.1f}'}")
    navs, scrolls = per_day(data.navigations, view.zone), per_day(data.scrolls, view.zone)
    out += [
        f"- {day}: {navs.get(day, 0)} navigations, {scrolls.get(day, 0)} scroll steps"
        for day in sorted(set(navs) | set(scrolls))
    ]
    return out


def _timing(view: View) -> list[str]:
    scrolls, fired, by_hour = pagination(view.data, view.zone)
    hours = ", ".join(f"{h:02d}h {f}/{s}" for h, (s, f) in by_hour.items()) or "-"
    out = [
        "## Pagination (screen locked / display asleep)",
        "",
        f"- pagination fired on {fired} of {scrolls} scroll steps ({_pct(fired, scrolls)})",
        f"- by local hour: {hours}",
        "- The display's sleep state is not recorded; compare night-adjacent hours by hand.",
        "",
        "## Latency (first seen − created)",
        "",
    ]
    lat = latencies(view.data)
    for via in ("feed", "backstop"):
        values = lat.get(via, [])
        out.append(
            f"- {via}: {len(values)} posts, p50 {_minutes(percentile(values, 0.5))},"
            f" p95 {_minutes(percentile(values, 0.95))}"
        )
    out.append(
        f"- Only posts seen or a read began within {ON_DUTY // 60} min of, as the late rate."
    )
    return out


def _health(view: View) -> list[str]:
    out = ["## Account health", ""]
    if not view.data.blocks:
        out.append("- No warning, logout or checkpoint.")
    out.extend(
        (
            f"- block #{block['id']} {view.local(float(block['opened_at']))} rung {block['rung']}"
            f" ({block['state']}): {block['reason']} — closed: {block['close_reason'] or 'open'}"
        )
        for block in view.data.blocks
    )
    unknown = sum(1 for r in view.data.reads if r.page_state == "unknown")
    rss = resources(view.data)
    median = percentile(rss, 0.5)
    p50 = "-" if median is None else f"{median:.0f}"
    peak = "-" if not rss else f"{max(rss):.0f}"
    out += [
        f"- unrecognised pages: {unknown}",
        "",
        "## Resources",
        "",
        f"- Chrome RSS: p50 {p50} MiB, max {peak} MiB (target < 1.5 GB)",
    ]
    return out


def _diagnostics(view: View) -> list[str]:
    paths = text_paths(view.data, now=view.now)
    stories, empty, unread = paths.stories, paths.empty, paths.unread
    beside, translation = paths.card_beside_words, paths.translation_fields
    out = [
        "## Parser diagnostics (structure only)",
        "",
        (
            "Key names, `__typename` values, string lengths and counts only: no post text, id,"
            " name, URL or token. A key that looks like an id reads `<id>`, another odd key"
            " `<key>`, an odd typename `<?>`."
        ),
        "",
        "### Where the text came from",
        "",
        (
            "Stories per text path the parser took, in feed and group reads (a post read again"
            " counts again; the parser's alert counts each post once):"
        ),
        "",
        "| Text path | Feed reads | Group reads | Last 24 h |",
        "|---|---|---|---|",
    ]
    out += [f"| `{label}` | {row[0]} | {row[1]} | {row[2]} |" for label, row in paths.rows.items()]
    out += [
        f"| **all stories** | {stories[0]} | {stories[1]} | {stories[2]} |",
        "",
        (
            f"- No text at all (`{NO_NAME}`): feed {empty[0]} of {stories[0]}"
            f" ({_pct(empty[0], stories[0])}), group {empty[1]} of {stories[1]}"
            f" ({_pct(empty[1], stories[1])}), last 24 h {empty[2]} of {stories[2]}"
            f" ({_pct(empty[2], stories[2])})."
        ),
        (
            f"- A message section the parser could not read (deferred as `no_text`): feed"
            f" {unread[0]}, group {unread[1]}, last 24 h {unread[2]}."
        ),
        (
            "- With a message of their own and a card's lines after it (a link's title,"
            " description and source, a listing's title, price and location):"
            f" {beside.counts()}. Their text path stays the message's."
        ),
        (
            f"- With a key that speaks of a translation (`{TRANSLATION_MARK}` in its name,"
            f" whatever it holds) anywhere in the story: {translation.counts()}. The script"
            " of the texts is below, from the saved payloads."
        ),
        "",
        (
            "Stories without a message of their own (nor a shared text), by what explains it"
            " (a post read again counts again). The empty-text alert counts only `unexplained`"
            " posts, each once, over the last three reads, from 10 posts and above 20 %:"
        ),
        "",
        "| Class | Feed reads | Group reads | Last 24 h |",
        "|---|---|---|---|",
    ]
    out += [
        f"| `{kind}`{CLASS_NOTES.get(kind, '')} | {row[0]} | {row[1]} | {row[2]} |"
        for kind, row in paths.classes.items()
    ]
    return [
        *out,
        "",
        *render_scan(view.raw, zone=view.zone),
        "",
        *render_scripts(view.raw),
    ]


Section = Callable[[View], list[str]]
SECTIONS: Final[tuple[Section, ...]] = (
    _done,
    _feed_miss,
    _fill,
    _text,
    _groups,
    _volume,
    _timing,
    _health,
    _diagnostics,
)


def render(
    data: Data,
    *,
    zone: dt.tzinfo,
    now: float,
    raw: RawScan | None = None,
    diagnostics_only: bool = False,
) -> str:
    """The report; ``diagnostics_only``: its parser diagnostics alone, without the sections
    that print group ids."""
    view = View(data, zone, now, raw)
    stamp = view.local(now, "%Y-%m-%d %H:%M %Z")
    if diagnostics_only:
        title = "# Parser diagnostics"
        about = (
            "from the collector's database and saved steps: the phase-0 report's last section"
            " alone (structure and counts, no ids)"
        )
        sections: tuple[Section, ...] = (_diagnostics,)
    else:
        title = "# Phase-0 report"
        about = "from the collector's database (ids and counts only, no post text)"
        sections = SECTIONS
    lines = [title, "", f"Generated {stamp} {about}."]
    for section in sections:
        lines += ["", *section(view)]
    return "\n".join(lines) + "\n"


def generate(
    db: Path,
    *,
    zone: dt.tzinfo,
    now: float,
    since: float | None = None,
    raw_dir: Path | None = None,
    diagnostics_only: bool = False,
) -> str:
    """The report; with ``raw_dir``, the diagnostics also read the newest saved steps."""
    raw = scan_raw(raw_dir, since=since) if raw_dir is not None else None
    data = load(db, since=since)
    return render(data, zone=zone, now=now, raw=raw, diagnostics_only=diagnostics_only)
