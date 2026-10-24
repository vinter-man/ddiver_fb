"""Build parser fixtures from captures by allow-list, so no account data reaches the repository.

DESIGN (Phase 0, fixtures): the builder emits only the JSON paths the parser reads (ids, times,
texts, actors, permalinks, group ids, listing fields, framing, labels, page_info) with names,
ids and text replaced consistently, and drops every other key and script block. An allow-list
and not a deny-list, because a logged-in capture is full of things nobody thought to deny
(tokens, session ids inside URLs, other people's names in member facepiles); what the parser
does not read cannot leak.

"Consistently" matters: one post id comes out the same wherever it appears (edge, content
section, permalink), and one text the same wherever it is repeated, or the fixture would stop
exercising the parser's cross-checks (group against permalink, a shared text "if different").

Subcommands::

    uv run python -m tools.make_fixture document CAPTURE.html OUT.html --source "..."
    uv run python -m tools.make_fixture steps MANIFEST.json OUT.json --source "..."
    uv run python -m tools.make_fixture research RESEARCH_DIR tests/fixtures
    uv run python -m tools.make_fixture synthetic tests/fixtures

``research`` regenerates every committed fixture from the 2026-09-24 research captures;
``synthetic`` only those built without them (``feed_attachments_synthetic.json``). The
Groups feed and the joined-groups page have no logged-in capture yet, so their fixtures (names
ending ``_synthetic``) are assembled from the real story shapes of the group captures, moved into
the paths the Groups Watcher extension reads (``data.viewer.news_feed.edges[].node``) and filled
with synthetic values; they go through the same allow-list as everything else.

A ``steps`` manifest is ``{"steps": [{"final_url", "status", "document", "graphql": [
{"friendly_name", "status", "body_file" | "frames"}], "dom": {...}}]}``; ``document`` names an
already built HTML fixture next to the output.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode, urlsplit

from fbcollector.frames import iter_document_json, iter_graphql_frames
from fbcollector.relay import WalkStats, dig, document_frames
from fbcollector.stories import own_text

log = logging.getLogger("make_fixture")

ALLOWED_KEYS: Final = frozenset(
    {
        # framing
        "require", "__bbox", "result", "data", "label", "path", "sequence_number", "complete",
        "expectedPreloaders", "preloaderID", "queryName", "variables", "extensions",
        "errors", "error", "errorSummary", "errorDescription", "code", "api_error_code",
        "severity", "summary", "description", "message",
        # connections
        "viewer", "group", "node", "edges", "cursor", "page_info", "has_next_page", "end_cursor",
        "news_feed", "group_feed", "cross_group_feed", "highlight_units",
        "if_viewer_can_see_highlight_units", "is_unit_pinned",
        # stories
        "__typename", "id", "post_id", "creation_time", "comet_sections", "content", "story",
        "context_layout", "title", "metadata", "timestamp", "to", "target_group", "feedback",
        "associated_group", "owning_profile", "name", "url", "permalink_url", "wwwURL",
        "profile_url", "text", "message_container", "rich_message", "attached_story",
        "actors", "attachments", "styles", "style_type_renderer", "attachment", "target",
        "title_with_entities", "marketplace_listing_title", "formatted_price", "location_text",
        "listing_price", "formatted_amount", "source", "style_infos", "comet_footer_renderer",
        "sponsored_data", "th_dat_spo", "ad_id", "is_anonymous",
        # group page
        "profile_header_renderer", "privacy_info", "icon_name", "viewer_join_state",
        "join_action", "if_viewer_can_change_follow_setting", "subscribe_status", "vanity",
        "group_address", "visibility",
        # joined-groups connections of the synthetic fixture (phase 0 adds the real ones)
        "groups_tab", "joined_groups", "suggested_groups",
    }
)  # fmt: skip
ALLOWED_VARIABLES: Final = frozenset(
    {
        "groupID", "sortingSetting", "feedStyle", "orderby", "feedInitialFetchSize",
        "feedLocation", "renderLocation", "regular_stories_count",
        "regular_stories_stream_initial_count", "count",
    }
)  # fmt: skip
FRAMING_STRINGS: Final = frozenset(
    {"ScheduledServerJS", "handle", "RelayPrefetchedStreamCache", "next"}
)
ID_KEYS: Final = frozenset({"id", "post_id", "groupID", "ad_id"})
URL_KEYS: Final = frozenset({"url", "permalink_url", "wwwURL", "profile_url"})
CURSOR_KEYS: Final = frozenset({"cursor", "end_cursor"})
SLUG_KEYS: Final = frozenset({"vanity", "group_address"})
ERROR_TEXT_KEYS: Final = frozenset({"summary", "errorSummary", "errorDescription", "description"})
KEPT_TEXTS: Final = frozenset(
    {
        "Public group",
        "Private group",
        "Anyone can see who's in the group and what they post.",
        "Only members can see who's in the group and what they post.",
    }
)
SAFE_SEGMENTS: Final = frozenset(
    {"groups", "posts", "permalink", "profile.php", "user", "photo", "commerce", "listing",
     "marketplace", "item", "story.php", "joins"}
)  # fmt: skip
SAFE_QUERY: Final = frozenset({"filter", "sk", "sorting_setting"})
FACEBOOK_HOSTS: Final = frozenset(
    {"facebook.com", "www.facebook.com", "web.facebook.com", "m.facebook.com"}
)
_ENUM: Final = re.compile(r"[A-Za-z_$][A-Za-z0-9_$.:-]{0,200}")
_LONG_DIGITS: Final = re.compile(r"[0-9]{5,}")
_PRELOADER: Final = re.compile(r"adp_([A-Za-z0-9]+?)RelayPreloader_")
_LOREM: Final = [
    "lorem", "ipsum", "dolor", "sit", "amet", "consectetur", "adipiscing", "elit", "sed", "do",
    "eiusmod", "tempor", "incididunt", "ut", "labore", "et", "dolore", "magna", "aliqua",
]  # fmt: skip


class _Drop:
    """Marker: the value was pruned away."""


DROP: Final = _Drop()


class Anonymizer:
    """Consistent replacement maps. Values in ``keep`` (a synthetic recipe's own) pass as is."""

    def __init__(self, keep: Iterable[Any] = ()) -> None:
        self.keep: set[str] = {str(v) for v in keep}
        self._maps: dict[str, dict[str, str]] = {}

    def _get(self, kind: str, value: str, make: Callable[[int], str]) -> str:
        table = self._maps.setdefault(kind, {})
        if value not in table:
            table[value] = make(len(table) + 1)
        return table[value]

    def id(self, value: str) -> str:
        if value in self.keep:
            return value
        width = max(len(value), 9)
        return self._get("id", value, lambda n: "1" + str(n).zfill(width - 1))

    def opaque(self, value: str) -> str:
        return value if value in self.keep else self._get("opaque", value, lambda n: f"opaque{n}")

    def name(self, value: str, *, group: bool) -> str:
        if value in self.keep:
            return value
        return self._get("name", value, lambda n: f"{'Group' if group else 'Person'} {n}")

    def slug(self, value: str) -> str:
        return value if value in self.keep else self._get("slug", value, lambda n: f"slug-{n}")

    def cursor(self, value: str) -> str:
        return value if value in self.keep else self._get("cursor", value, lambda n: f"cursor-{n}")

    def preloader(self, value: str) -> str:
        match = _PRELOADER.match(value)
        query = match.group(1) if match else "Unknown"
        return self._get("preloader", value, lambda n: f"adp_{query}RelayPreloader_{n}")

    def text(self, value: str) -> str:
        if value in self.keep or value in KEPT_TEXTS:
            return value

        def make(n: int) -> str:
            lines = []
            for i, line in enumerate(value.split("\n")):
                if not line.strip():
                    lines.append("")
                    continue
                words = max(1, min(len(line), 120) // 6)
                lorem = " ".join(_LOREM[(n + i + k) % len(_LOREM)] for k in range(words))
                lines.append(f"t{n}.{i} {lorem}")
            return "\n".join(lines)

        return self._get("text", value, make)

    def digits(self, value: str) -> str:
        """Free text (error summaries): only long digit runs are ids worth replacing."""
        if value in self.keep:
            return value
        return _LONG_DIGITS.sub(lambda m: self.id(m.group()), value)

    def url(self, value: str) -> str:
        if value in self.keep:
            return value
        try:
            parts = urlsplit(value)
        except ValueError:
            return "https://example.invalid/"
        if (parts.hostname or "").lower() not in FACEBOOK_HOSTS:
            return "https://example.invalid/"
        segments = [s for s in parts.path.split("/") if s]
        out: list[str] = []
        for segment in segments:
            if segment.isascii() and segment.isdigit():
                out.append(self.id(segment))
            elif segment in SAFE_SEGMENTS:
                out.append(segment)
            elif segment.startswith("pfbid"):
                out.append(self.opaque(segment))
            else:
                out.append(self.slug(segment))  # a group's or a person's vanity name
        query = [
            (k, self.id(v) if k == "id" else v)
            for k, v in parse_qsl(parts.query)
            if k in SAFE_QUERY or (k == "id" and segments == ["profile.php"] and v.isdigit())
        ]
        tail = "/" if out and parts.path.endswith("/") else ""
        return (
            "https://www.facebook.com/"
            + "/".join(out)
            + tail
            + (f"?{urlencode(query)}" if query else "")
        )


class Pruner:
    """Keep only allow-listed keys, replace their values through the anonymizer."""

    def __init__(self, anonymizer: Anonymizer, extra_keys: Iterable[str] = ()) -> None:
        self.anon = anonymizer
        self.allowed = ALLOWED_KEYS | frozenset(extra_keys)

    def prune(self, value: Any) -> Any:
        return self._prune(value, None, None, ())

    def _prune(
        self,
        value: Any,
        key: str | None,
        holder: Mapping[str, Any] | None,
        parents: tuple[str, ...],
    ) -> Any:
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for k, v in value.items():
                if not isinstance(k, str) or k not in self.allowed:
                    continue
                if k == "variables" and isinstance(v, Mapping):
                    kept = {vk: vv for vk, vv in v.items() if vk in ALLOWED_VARIABLES}
                    out[k] = {
                        vk: pruned
                        for vk, vv in kept.items()
                        if (pruned := self._prune(vv, vk, v, (*parents, k, vk))) is not DROP
                    }
                    continue
                pruned = self._prune(v, k, value, (*parents, k))
                if pruned is not DROP:
                    out[k] = pruned
            return DROP if value and not out else out
        if isinstance(value, list):
            items: list[Any] = []
            for item in value:
                pruned = self._list_item(item, key, holder, parents)
                if pruned is DROP or (hollow(pruned) and not _empty(item)):
                    continue
                items.append(pruned)
            return DROP if value and not items else items
        return self._scalar(key, value, holder, parents)

    def _list_item(
        self, item: Any, key: str | None, holder: Mapping[str, Any] | None, parents: tuple[str, ...]
    ) -> Any:
        if isinstance(item, Mapping | list):
            return self._prune(item, key, holder, parents)
        if key == "path" and isinstance(item, int | str) and not isinstance(item, bool):
            return item if isinstance(item, int) or _ENUM.fullmatch(item) else DROP
        if key == "orderby" and isinstance(item, str):
            return item if _ENUM.fullmatch(item) else DROP
        if isinstance(item, str) and item in FRAMING_STRINGS:
            return item
        if isinstance(item, str) and item.startswith("adp_"):
            return self.anon.preloader(item)
        return DROP  # module names, numbers and flags of the page's require lists

    def _scalar(
        self,
        key: str | None,
        value: Any,
        holder: Mapping[str, Any] | None,
        parents: tuple[str, ...],
    ) -> Any:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int | float):
            if key in ID_KEYS and isinstance(value, int):
                return int(self.anon.id(str(value)))
            return value
        if not isinstance(value, str) or key is None:
            return DROP
        return self._string(key, value, holder, parents)

    def _string(
        self, key: str, value: str, holder: Mapping[str, Any] | None, parents: tuple[str, ...]
    ) -> str:
        anon = self.anon
        if key in ID_KEYS:
            return anon.id(value) if value.isascii() and value.isdigit() else anon.opaque(value)
        if key == "name":
            return anon.name(value, group=_is_group(holder, parents))
        if key in URL_KEYS:
            return anon.url(value)
        if key in CURSOR_KEYS:
            return anon.cursor(value)
        if key in SLUG_KEYS:
            return anon.slug(value) if value else value
        if key == "preloaderID":
            return anon.preloader(value)
        if key in ERROR_TEXT_KEYS or key == "message":
            # Error strings are Facebook's boilerplate and worth keeping; the same keys on
            # anything else (a group's description) are someone's words.
            return anon.digits(value) if _is_error(holder) else anon.text(value)
        if key in {"__typename", "label", "queryName", "severity"} and _ENUM.fullmatch(value):
            return value
        if key == "text" and "privacy_info" in parents and value in KEPT_TEXTS:
            return value
        if key not in {"text", "marketplace_listing_title"} and _enum_value(value):
            return value
        return anon.text(value)


def _enum_value(value: str) -> bool:
    return bool(_ENUM.fullmatch(value)) and not _LONG_DIGITS.search(value)


def _is_group(holder: Mapping[str, Any] | None, parents: tuple[str, ...]) -> bool:
    if holder is not None and holder.get("__typename") == "Group":
        return True
    return len(parents) >= 2 and parents[-2] in {"to", "group", "target_group", "associated_group"}


def _is_error(holder: Mapping[str, Any] | None) -> bool:
    return holder is not None and not {"severity", "code", "error"}.isdisjoint(holder)


def _empty(value: Any) -> bool:
    return isinstance(value, Mapping | list) and not value


def hollow(value: Any) -> bool:
    """No content left: only empty containers, nulls and framing strings."""
    if isinstance(value, Mapping):
        return not value
    if isinstance(value, list):
        return all(v is None or (isinstance(v, str) and _framing(v)) or hollow(v) for v in value)
    return False


def _framing(value: str) -> bool:
    return value in FRAMING_STRINGS or value.startswith("adp_")


def script_json(value: Any) -> str:
    # Facebook escapes "<" in data-sjs blocks; so do we, so a text cannot close the script.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003C")


def render_html(blocks: Sequence[Any], *, source: str, synthetic: bool) -> str:
    note = re.sub(r"-{2,}|[<>]", "-", source)
    scripts = "\n".join(
        f'<script type="application/json" data-sjs>{script_json(b)}</script>' for b in blocks
    )
    return (
        "<!DOCTYPE html>\n"
        f"<!-- fixture: built by tools/make_fixture.py (allow-list); synthetic: "
        f"{'yes' if synthetic else 'no'}; source: {note} -->\n"
        '<html lang="en"><head><meta charset="utf-8"></head><body>\n'
        f"{scripts}\n</body></html>\n"
    )


def build_document(values: Iterable[Any], pruner: Pruner, *, source: str, synthetic: bool) -> str:
    """An HTML fixture holding only the allow-listed part of each JSON script block."""
    blocks = []
    for value in values:
        pruned = pruner.prune(value)
        if pruned is not DROP and not hollow(pruned):
            blocks.append(pruned)
    return render_html(blocks, source=source, synthetic=synthetic)


def body_text(frames: Iterable[Any], pruner: Pruner, *, guard: bool = True) -> str:
    """A GraphQL body: ``for (;;);`` and one allow-listed frame per line (NDJSON)."""
    lines = []
    for frame in frames:
        pruned = pruner.prune(frame)
        if pruned is not DROP:
            lines.append(json.dumps(pruned, ensure_ascii=False, separators=(",", ":")))
    return ("for (;;);" if guard else "") + "\n".join(lines)


def build_steps(
    manifest: Mapping[str, Any], base: Path, pruner: Pruner, *, source: str, synthetic: bool
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    for step in manifest["steps"]:
        bodies = []
        for body in step.get("graphql", []):
            if "frames" in body:
                frames = list(body["frames"])
            else:
                frames = list(iter_graphql_frames((base / body["body_file"]).read_bytes()))
            entry: dict[str, Any] = {
                "friendly_name": body.get("friendly_name"),
                "status": body.get("status", 200),
                "body": body_text(frames, pruner, guard=body.get("guard", True)),
            }
            if body.get("capped"):
                entry["capped"] = True
            bodies.append(entry)
        steps.append(
            {
                "final_url": pruner.anon.url(step["final_url"]),
                "status": step.get("status", 200),
                "document": step.get("document"),
                "graphql": bodies,
                "dom": dict(step.get("dom", {})),
                "pagination_fired": bool(step.get("pagination_fired", bool(bodies))),
            }
        )
    return {
        "_fixture": {
            "builder": "tools/make_fixture.py (allow-list)",
            "synthetic": synthetic,
            "source": source,
        },
        "steps": steps,
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


# -- research recipes ---------------------------------------------------------------------------

GROUP_CAPTURE: Final = "wgroup.html"
SHARE_CAPTURE: Final = (
    "c4probe/www__desk_chrome145__https_www_facebook_com_groups_Jerseyask_sorting_setting_"
    "CHRONOLOGICAL.html"
)
ADBERTRAM: Final = "repos/adbertram__cli-tools/facebook/tests/fixtures"
ADBERTRAM_NOTICE: Final = (
    "adbertram/cli-tools facebook/tests/fixtures/{name} (MIT License, "
    "Copyright (c) 2026 CLI Tools contributors)"
)

T0: Final = 1_790_300_000
"""2026-09-25T00:53:20Z: "now" in the synthetic fixtures."""
G1, G2, G3, G9 = "400000000000001", "400000000000002", "400000000000003", "400000000000009"
G4, G5 = "400000000000004", "400000000000005"
GROUP_NAMES: Final = {
    G1: "Synthetic Flats Sofia",
    G2: "Synthetic Rentals Varna",
    G3: "Synthetic Buy Sell Plovdiv",
    G4: "Synthetic Roommates Burgas",
    G5: "Synthetic Studios Ruse",
    G9: "Synthetic Suggested Group",
}
SLUG_G2: Final = "synthetic-rentals-varna"


def post(n: int) -> str:
    return f"50000000000{n:05d}"


def user(n: int) -> str:
    return f"60000000000{n:04d}"


@dataclass
class Templates:
    """Real story shapes from the group captures, with the values they carry."""

    plain: dict[str, Any]
    share: dict[str, Any]
    highlight: dict[str, Any]

    @classmethod
    def load(cls, research: Path) -> Templates:
        plain_frames = _frames((research / GROUP_CAPTURE).read_bytes())
        share_frames = _frames((research / SHARE_CAPTURE).read_bytes())
        return cls(
            plain=copy.deepcopy(_stream_node(plain_frames)),
            share=copy.deepcopy(_stream_node(share_frames)),
            highlight=copy.deepcopy(
                dig(
                    _root_group(plain_frames),
                    "if_viewer_can_see_highlight_units",
                    "highlight_units",
                    "edges",
                    0,
                    "node",
                )
            ),
        )


def _frames(document: bytes) -> list[Mapping[str, Any]]:
    return [f.raw for f in document_frames(list(iter_document_json(document)), WalkStats())]


def _stream_node(frames: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    for frame in frames:
        if "$stream$" in str(frame.get("label")) and "group_feed" in str(frame.get("path")):
            node = dig(frame, "data", "node")
            if isinstance(node, dict):
                return node
    raise LookupError("no streamed story in the capture")


def _root_group(frames: Sequence[Mapping[str, Any]]) -> Any:
    for frame in frames:
        group = dig(frame, "data", "group")
        if isinstance(group, dict) and "if_viewer_can_see_highlight_units" in group:
            return group
    raise LookupError("no root group frame in the capture")


def substitute(value: Any, whole: Mapping[Any, Any], parts: Sequence[tuple[str, str]]) -> Any:
    """Replace exact values (``whole``) and substrings (``parts``, for URLs) everywhere."""
    if isinstance(value, dict):
        return {k: substitute(v, whole, parts) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, whole, parts) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if value in whole:
        return whole[value]
    if isinstance(value, str):
        for old, new in parts:
            value = value.replace(old, new)
    return value


@dataclass
class StoryValues:
    post_id: str
    group_id: str
    created_at: int
    text: str | None
    author_id: str
    author_name: str
    group_ref: str | None = None  # the permalink's group segment (a slug) if not the id


class Synth:
    """Assembles synthetic stories out of the real templates and remembers every value it
    introduced, so the anonymizer keeps them and the tests can name them."""

    def __init__(self, templates: Templates) -> None:
        self.t = templates
        self.keep: set[str] = set(GROUP_NAMES) | set(GROUP_NAMES.values()) | {SLUG_G2}
        old = templates.plain
        self._old_plain = {
            "post_id": old["post_id"],
            "created_at": old["creation_time"],
            "group_id": dig(old, "comet_sections", "content", "story", "target_group", "id"),
            "slug": "Jerseyask",
            "text": dig(old, "comet_sections", "content", "story", "message", "text"),
            "actor_id": dig(old, "comet_sections", "content", "story", "actors", 0, "id"),
            "actor_name": dig(old, "comet_sections", "content", "story", "actors", 0, "name"),
            "group_name": dig(
                old,
                "comet_sections",
                "context_layout",
                "story",
                "comet_sections",
                "title",
                "story",
                "to",
                "name",
            ),
        }

    def remember(self, *values: Any) -> None:
        self.keep.update(str(v) for v in values if v is not None)

    def plain(self, v: StoryValues) -> dict[str, Any]:
        o = self._old_plain
        ref = v.group_ref or v.group_id
        self.remember(v.post_id, v.group_id, v.text, v.author_id, v.author_name, ref)
        whole: dict[Any, Any] = {
            o["post_id"]: v.post_id,
            o["group_id"]: v.group_id,
            o["created_at"]: v.created_at,
            o["actor_id"]: v.author_id,
            o["actor_name"]: v.author_name,
            o["group_name"]: GROUP_NAMES[v.group_id],
        }
        if v.text is not None:
            whole[o["text"]] = v.text
        parts = [
            (f"/groups/{o['slug']}/", f"/groups/{ref}/"),
            (o["post_id"], v.post_id),
            (o["group_id"], v.group_id),
        ]
        node: dict[str, Any] = substitute(self.t.plain, whole, parts)
        return node

    def share(self, v: StoryValues, shared: StoryValues) -> dict[str, Any]:
        old = self.t.share
        content = dig(old, "comet_sections", "content", "story")
        attached = content["attached_story"]
        self.remember(v.post_id, v.group_id, v.author_id, v.author_name, shared.post_id)
        self.remember(shared.text, shared.author_id, shared.author_name)
        old_time = dig(
            attached,
            "comet_sections",
            "context_layout",
            "story",
            "comet_sections",
            "metadata",
            0,
            "story",
            "creation_time",
        )
        whole: dict[Any, Any] = {
            old["post_id"]: v.post_id,
            old["creation_time"]: v.created_at,
            content["actors"][0]["name"]: v.author_name,
            content["actors"][0]["id"]: v.author_id,
            content["target_group"]["id"]: v.group_id,
            attached["post_id"]: shared.post_id,
            attached["message"]["text"]: shared.text,
            attached["actors"][0]["id"]: shared.author_id,
            attached["actors"][0]["name"]: shared.author_name,
            old_time: shared.created_at,
            self._old_plain["group_name"]: GROUP_NAMES[v.group_id],
        }
        parts = [
            ("/groups/Jerseyask/", f"/groups/{v.group_ref or v.group_id}/"),
            (old["post_id"], v.post_id),
            (content["target_group"]["id"], v.group_id),
        ]
        node: dict[str, Any] = substitute(old, whole, parts)
        return node

    def highlight(self, v: StoryValues) -> dict[str, Any]:
        unit = self.t.highlight
        story = unit["story"]
        content = story["comet_sections"]["content"]["story"]
        old_time = dig(
            story,
            "comet_sections",
            "context_layout",
            "story",
            "comet_sections",
            "metadata",
            0,
            "story",
            "creation_time",
        )
        self.remember(v.post_id, v.group_id, v.text, v.author_id, v.author_name)
        whole: dict[Any, Any] = {
            unit["id"]: v.post_id,
            content["post_id"]: v.post_id,
            old_time: v.created_at,
            content["actors"][0]["id"]: v.author_id,
            own_text(story)[0]: v.text,
            self._old_plain["group_id"]: v.group_id,
            self._old_plain["group_name"]: GROUP_NAMES[v.group_id],
        }
        parts = [
            ("/groups/Jerseyask/", f"/groups/{v.group_id}/"),
            (unit["id"], v.post_id),
            (self._old_plain["group_id"], v.group_id),
        ]
        node: dict[str, Any] = substitute(unit, whole, parts)
        for actors in _actor_lists(node["story"]):
            for actor in actors:
                if isinstance(actor, dict) and "name" in actor:
                    actor["name"] = v.author_name
        return node


def _actor_lists(node: Any) -> list[list[Any]]:
    found: list[list[Any]] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key == "attached_story":
                    continue
                if key == "actors" and isinstance(value, list):
                    found.append(value)
                stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    return found


def _content(node: dict[str, Any]) -> dict[str, Any]:
    content: dict[str, Any] = node["comet_sections"]["content"]["story"]
    return content


def _edge(node: Mapping[str, Any], cursor: str) -> dict[str, Any]:
    return {"node": node, "cursor": cursor}


def _rpsc(query: str, sequence: int, result: Mapping[str, Any]) -> list[Any]:
    """A RelayPrefetchedStreamCache entry, the way the SSR embeds a query's frames."""
    return [
        "RelayPrefetchedStreamCache",
        "next",
        [],
        [
            f"adp_{query}RelayPreloader_1",
            {"__bbox": {"complete": False, "result": result, "sequence_number": sequence}},
        ],
    ]


def _ssr(entries: Sequence[list[Any]]) -> dict[str, Any]:
    return {
        "require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"require": list(entries)}}]]]
    }


def _preloaders(*preloaders: Mapping[str, Any]) -> dict[str, Any]:
    return _ssr([["CometPreloaders", "init", [], [{"expectedPreloaders": list(preloaders)}]]])


def feed_synthetic(synth: Synth) -> tuple[list[Any], dict[str, Any]]:
    """The Groups feed (``/?filter=groups&sk=h_chr``): SSR, then two pagination responses."""
    s = synth
    a = s.plain(
        StoryValues(
            post(1),
            G1,
            T0 - 120,
            "Synthetic text: 2-room flat in Lozenets, 650 EUR.",
            user(1),
            "Synthetic Author One",
        )
    )
    b = s.share(
        StoryValues(post(2), G2, T0 - 400, None, user(2), "Synthetic Author Two", SLUG_G2),
        StoryValues(
            post(9001),
            G2,
            T0 - 10 * 86400,
            "Synthetic shared text: studio near the sea.",
            user(3),
            "Synthetic Page",
        ),
    )
    anchor = s.plain(
        StoryValues(
            post(3),
            G1,
            T0 - 2 * 86400,
            "Synthetic text: old anchor post.",
            user(4),
            "Synthetic Author Four",
        )
    )
    listing = s.plain(
        StoryValues(
            post(4), G3, T0 - 900, "Synthetic text: selling a sofa.", user(5), "Synthetic Seller"
        )
    )
    listing_card = {
        "styles": {
            "__typename": "StoryAttachmentGroupCommerceProductItemStyleRenderer",
            "attachment": {
                "title_with_entities": {"text": "Synthetic listing: grey sofa"},
                "target": {
                    "__typename": "GroupCommerceProductItem",
                    "id": "700000000000001",
                    "marketplace_listing_title": "Synthetic listing: grey sofa",
                    "formatted_price": {"text": "BGN 120"},
                    "location_text": {"text": "Plovdiv, Bulgaria"},
                },
            },
        }
    }
    _content(listing)["attachments"] = [listing_card]
    s.remember("700000000000001", "Synthetic listing: grey sofa", "BGN 120", "Plovdiv, Bulgaria")
    anonymous = s.plain(
        StoryValues(
            post(5),
            G1,
            T0 - 1000,
            "Synthetic text: looking for a room.",
            user(6),
            "Anonymous participant 4821",
        )
    )
    for actors in _actor_lists(anonymous):
        for actor in actors:
            if isinstance(actor, dict):
                actor["__typename"] = "GroupAnonAuthorProfile"
    anonymous.get("feedback", {}).pop("owning_profile", None)
    sponsored = s.plain(
        StoryValues(
            post(6), G1, T0 - 1100, "Synthetic text: an advert.", user(7), "Synthetic Advertiser"
        )
    )
    sponsored["sponsored_data"] = {"ad_id": "800000000000001"}
    s.remember("800000000000001")
    no_time = s.plain(
        StoryValues(
            post(7),
            G1,
            T0 - 1200,
            "Synthetic text: no timestamp.",
            user(8),
            "Synthetic Author Eight",
        )
    )
    no_time.pop("creation_time")
    no_time["comet_sections"].pop("timestamp")
    no_time["comet_sections"]["context_layout"]["story"]["comet_sections"]["metadata"] = []
    conflict = s.plain(
        StoryValues(
            post(8),
            G1,
            T0 - 1300,
            "Synthetic text: two groups disagree.",
            user(9),
            "Synthetic Author Nine",
        )
    )
    conflict["comet_sections"]["content"]["story"]["target_group"]["id"] = G2
    no_text = s.plain(StoryValues(post(10), G1, T0 - 1400, None, user(10), "Synthetic Author Ten"))
    content = _content(no_text)
    content["message"] = {"__typename": "TextWithEntities"}
    content["comet_sections"]["message"] = {"__typename": "CometFeedStoryFutureMessageStrategy"}
    content["comet_sections"]["message_container"] = None
    non_story = {"__typename": "GroupsYouShouldJoinFeedUnit", "id": "opaque-suggestions"}
    old_anchor = s.plain(
        StoryValues(
            post(11),
            G2,
            T0 - 3 * 86400,
            "Synthetic text: second anchor.",
            user(11),
            "Synthetic Author Eleven",
            SLUG_G2,
        )
    )
    nested = s.share(
        StoryValues(post(12), G3, T0 - 2000, None, user(12), "Synthetic Author Twelve"),
        StoryValues(
            post(9002),
            G3,
            T0 - 20 * 86400,
            "Synthetic middle text: shared once.",
            user(13),
            "Synthetic Middle",
        ),
    )
    _content(nested)["attached_story"]["attached_story"] = {
        "__typename": "Story",
        "post_id": post(9003),
        "message": {
            "__typename": "TextWithEntities",
            "text": "Synthetic nested text: the original.",
        },
    }
    s.remember(post(9003), "Synthetic nested text: the original.")

    feed_style = {
        "preloaderID": "adp_CometNewsFeedRootQueryRelayPreloader_1",
        "queryName": "CometNewsFeedRootQuery",
        "variables": {
            "feedStyle": "MOST_RECENT_GROUPS_FEED",
            "orderby": ["MOST_RECENT"],
            "feedInitialFetchSize": 3,
            "feedLocation": "NEWSFEED",
        },
    }
    label = "CometNewsFeed_viewerConnection$stream$CometNewsFeed_viewer_news_feed"
    defer = "CometNewsFeed_viewerConnection$defer$CometNewsFeed_viewer_news_feed$page_info"
    s.remember(*(f"cursor-feed-{n}" for n in range(20)))
    ssr = [
        _preloaders(feed_style),
        _ssr(
            [
                _rpsc(
                    "CometNewsFeedRootQuery",
                    0,
                    {"data": {"viewer": {"news_feed": {"edges": [_edge(a, "cursor-feed-1")]}}}},
                )
            ]
        ),
        _ssr(
            [
                _rpsc(
                    "CometNewsFeedRootQuery",
                    1,
                    {
                        "label": label,
                        "path": ["viewer", "news_feed", "edges", 1],
                        "data": _edge(b, "cursor-feed-2"),
                    },
                )
            ]
        ),
        _ssr(
            [
                _rpsc(
                    "CometNewsFeedRootQuery",
                    2,
                    {
                        "label": defer,
                        "path": ["viewer", "news_feed"],
                        "data": {
                            "page_info": {"has_next_page": True, "end_cursor": "cursor-feed-2"}
                        },
                    },
                )
            ]
        ),
    ]
    page1 = [
        {
            "data": {
                "viewer": {
                    "news_feed": {
                        "edges": [_edge(anchor, "cursor-feed-3"), _edge(listing, "cursor-feed-4")]
                    }
                }
            }
        },
        {
            "label": label,
            "path": ["viewer", "news_feed", "edges", 2],
            "data": _edge(anonymous, "cursor-feed-5"),
        },
        {
            "label": label,
            "path": ["viewer", "news_feed", "edges", 3],
            "data": _edge(sponsored, "cursor-feed-6"),
        },
        {
            "label": label,
            "path": ["viewer", "news_feed", "edges", 4],
            "data": _edge(no_time, "cursor-feed-7"),
        },
        {
            "label": label,
            "path": ["viewer", "news_feed", "edges", 5],
            "data": _edge(conflict, "cursor-feed-8"),
        },
        {
            "label": label,
            "path": ["viewer", "news_feed", "edges", 6],
            "data": _edge(no_text, "cursor-feed-9"),
        },
        {
            "label": label,
            "path": ["viewer", "news_feed", "edges", 7],
            "data": _edge(non_story, "cursor-feed-10"),
        },
        {
            "label": defer,
            "path": ["viewer", "news_feed"],
            "data": {"page_info": {"has_next_page": True, "end_cursor": "cursor-feed-10"}},
        },
    ]
    page2 = [
        {
            "data": {
                "viewer": {
                    "news_feed": {
                        "edges": [_edge(old_anchor, "cursor-feed-11"), _edge(b, "cursor-feed-12")]
                    }
                }
            }
        },
        {
            "label": label,
            "path": ["viewer", "news_feed", "edges", 2],
            "data": _edge(nested, "cursor-feed-13"),
        },
        {
            "label": defer,
            "path": ["viewer", "news_feed"],
            "data": {"page_info": {"has_next_page": False, "end_cursor": "cursor-feed-13"}},
        },
    ]
    url = "https://www.facebook.com/?filter=groups&sk=h_chr"
    s.keep.add(url)
    manifest = {
        "steps": [
            {
                "final_url": url,
                "document": "feed_hchr_synthetic.html",
                "dom": {"html_lang": "en", "posinset_count": 2},
            },
            {
                "final_url": url,
                "graphql": [{"friendly_name": "CometNewsFeedPaginationQuery", "frames": page1}],
                "dom": {"html_lang": "en", "posinset_count": 9},
            },
            {
                "final_url": url,
                "graphql": [{"friendly_name": "CometNewsFeedPaginationQuery", "frames": page2}],
                "dom": {"html_lang": "en", "posinset_count": 11},
            },
            {"final_url": url, "dom": {"html_lang": "en", "posinset_count": 11}},
        ]
    }
    return ssr, manifest


def group_pagination_synthetic(synth: Synth) -> dict[str, Any]:
    """Scroll steps on a group page (``GroupsCometFeedRegularStoriesPaginationQuery``)."""
    s = synth
    stories = [
        s.plain(
            StoryValues(
                post(100 + n),
                G4,
                T0 - 600 * n,
                f"Synthetic text: group post {n}.",
                user(100 + n),
                f"Synthetic Member {n}",
            )
        )
        for n in range(1, 7)
    ]
    anchor = s.plain(
        StoryValues(
            post(199),
            G4,
            T0 - 5 * 86400,
            "Synthetic text: injected anchor.",
            user(199),
            "Synthetic Member Anchor",
        )
    )
    pinned = s.highlight(
        StoryValues(
            post(198),
            G4,
            T0 - 90 * 86400,
            "Synthetic text: pinned rules.",
            user(198),
            "Synthetic Admin",
        )
    )
    label = (
        "GroupsCometFeedRegularStories_paginationGroup$stream$"
        "GroupsCometFeedRegularStories_group_group_feed"
    )
    defer = f"{label.replace('$stream$', '$defer$')}$page_info"
    s.remember(*(f"cursor-group-{n}" for n in range(20)))
    header = {"__typename": "GroupsSectionHeaderUnit", "title": {"text": "New posts"}}
    s.remember("New posts")
    body1 = [
        {
            "data": {
                "node": {
                    "__typename": "Group",
                    "id": G4,
                    "group_feed": {
                        "edges": [
                            _edge(header, "cursor-group-0"),
                            _edge(stories[0], "cursor-group-1"),
                            _edge(stories[1], "cursor-group-2"),
                        ]
                    },
                }
            }
        },
        {
            "label": label,
            "path": ["node", "group_feed", "edges", 3],
            "data": _edge(stories[2], "cursor-group-3"),
        },
        {
            "label": defer,
            "path": ["node", "group_feed"],
            "data": {"page_info": {"has_next_page": True, "end_cursor": "cursor-group-3"}},
        },
    ]
    body2 = [
        {
            "data": {
                "node": {
                    "__typename": "Group",
                    "id": G4,
                    "group_feed": {
                        "edges": [
                            _edge(anchor, "cursor-group-4"),
                            _edge(stories[2], "cursor-group-5"),
                            _edge(stories[3], "cursor-group-6"),
                        ]
                    },
                }
            }
        },
        {
            "label": label,
            "path": ["node", "group_feed", "edges", 3],
            "data": _edge(stories[4], "cursor-group-7"),
        },
        {
            "label": label,
            "path": ["node", "group_feed", "edges", 4],
            "data": _edge(stories[5], "cursor-group-8"),
        },
        {
            "label": defer,
            "path": ["node", "group_feed"],
            "data": {"page_info": {"has_next_page": False, "end_cursor": "cursor-group-8"}},
        },
    ]
    highlights = [
        {
            "data": {
                "node": {
                    "__typename": "Group",
                    "id": G4,
                    "if_viewer_can_see_highlight_units": {
                        "highlight_units": {"edges": [{"node": pinned, "cursor": None}]}
                    },
                }
            }
        },
    ]
    url = f"https://www.facebook.com/groups/{G4}/?sorting_setting=CHRONOLOGICAL"
    s.keep.add(url)
    fn = "GroupsCometFeedRegularStoriesPaginationQuery"
    return {
        "steps": [
            {
                "final_url": url,
                "graphql": [
                    {"friendly_name": "GroupsCometHighlightUnitsQuery", "frames": highlights},
                    {"friendly_name": fn, "frames": body1},
                ],
                "dom": {"posinset_count": 3},
            },
            {
                "final_url": url,
                "graphql": [{"friendly_name": fn, "frames": body2}],
                "dom": {"posinset_count": 6},
            },
        ]
    }


def joins_synthetic(synth: Synth) -> tuple[list[Any], dict[str, Any]]:
    """``/groups/joins/``: Group-shaped dicts under a joined-groups connection (shape assumed)."""

    def group(gid: str, *, slug: str | None = None, state: str | None = None) -> dict[str, Any]:
        node: dict[str, Any] = {
            "__typename": "Group",
            "id": gid,
            "name": GROUP_NAMES[gid],
            "url": f"https://www.facebook.com/groups/{slug or gid}/",
        }
        if state is not None:
            node["viewer_join_state"] = state
        return node

    s = synth
    for gid in GROUP_NAMES:
        s.keep.add(f"https://www.facebook.com/groups/{gid}/")
    s.keep.add(f"https://www.facebook.com/groups/{SLUG_G2}/")
    s.remember(*(f"cursor-joins-{n}" for n in range(10)))
    stream = "GroupsCometJoinedGroups_viewer$stream$GroupsCometJoinedGroups_viewer_joined_groups"
    defer = f"{stream.replace('$stream$', '$defer$')}$page_info"
    ssr = [
        _ssr(
            [
                _rpsc(
                    "GroupsCometJoinsRootQuery",
                    0,
                    {
                        "data": {
                            "viewer": {
                                "groups_tab": {
                                    "joined_groups": {
                                        "edges": [
                                            _edge(group(G1), "cursor-joins-1"),
                                            _edge(group(G2, slug=SLUG_G2), "cursor-joins-2"),
                                        ]
                                    },
                                    "suggested_groups": {
                                        "edges": [
                                            _edge(group(G9, state="CAN_JOIN"), "cursor-joins-9")
                                        ]
                                    },
                                }
                            }
                        }
                    },
                )
            ]
        ),
        _ssr(
            [
                _rpsc(
                    "GroupsCometJoinsRootQuery",
                    1,
                    {
                        "label": stream,
                        "path": ["viewer", "groups_tab", "joined_groups", "edges", 2],
                        "data": _edge(group(G3, state="MEMBER"), "cursor-joins-3"),
                    },
                )
            ]
        ),
        _ssr(
            [
                _rpsc(
                    "GroupsCometJoinsRootQuery",
                    2,
                    {
                        "label": defer,
                        "path": ["viewer", "groups_tab", "joined_groups"],
                        "data": {
                            "page_info": {"has_next_page": True, "end_cursor": "cursor-joins-3"}
                        },
                    },
                )
            ]
        ),
    ]
    page = [
        {
            "data": {
                "node": {
                    "joined_groups": {
                        "edges": [
                            _edge(group(G4), "cursor-joins-4"),
                            _edge(group(G5), "cursor-joins-5"),
                            _edge(group(G1), "cursor-joins-6"),
                        ]
                    }
                }
            }
        },
        {
            "label": defer,
            "path": ["node", "joined_groups"],
            "data": {"page_info": {"has_next_page": True, "end_cursor": "cursor-joins-6"}},
        },
    ]
    last = [
        {
            "label": defer,
            "path": ["node", "joined_groups"],
            "data": {"page_info": {"has_next_page": False, "end_cursor": None}},
        },
    ]
    url = "https://www.facebook.com/groups/joins/"
    s.keep.add(url)
    fn = "GroupsCometJoinedGroupsPaginationQuery"
    manifest = {
        "steps": [
            {
                "final_url": url,
                "document": "groups_joins_synthetic.html",
                "dom": {"html_lang": "en", "joined_header_count": 5},
            },
            {
                "final_url": url,
                "graphql": [{"friendly_name": fn, "frames": page}],
                "dom": {"html_lang": "en", "joined_header_count": 5},
            },
            {
                "final_url": url,
                "graphql": [{"friendly_name": fn, "frames": last}],
                "dom": {"html_lang": "en", "joined_header_count": 5},
            },
        ]
    }
    return ssr, manifest


def errors_synthetic() -> dict[str, Any]:
    """In-body errors the detectors act on, and a query whose ``viewer`` came back null."""
    rate_limited = [
        {
            "errors": [
                {
                    "message": "Rate limit exceeded",
                    "severity": "CRITICAL",
                    "code": 1675004,
                    "summary": "Rate limit exceeded",
                    "description": "You have been temporarily blocked from performing this action.",
                }
            ],
            "data": None,
        }
    ]
    logged_out = [
        {
            "error": 1357001,
            "errorSummary": "Not logged in",
            "errorDescription": "Please log in to continue.",
        }
    ]
    viewer_null = [{"data": {"viewer": None}}]
    field_error = [
        {
            "data": {"viewer": {"news_feed": {"edges": []}}},
            "errors": [
                {
                    "message": "A server error field_exception occured.",
                    "severity": "ERROR",
                    "path": ["viewer", "news_feed", "edges", 0, "node", "feature_intervention"],
                }
            ],
        }
    ]
    url = "https://www.facebook.com/?filter=groups&sk=h_chr"
    return {
        "steps": [
            {
                "final_url": url,
                "graphql": [
                    {"friendly_name": "CometNewsFeedPaginationQuery", "frames": rate_limited},
                    {"friendly_name": "CometNotificationsDropdownQuery", "frames": logged_out},
                    {"friendly_name": "CometNewsFeedPaginationQuery", "frames": viewer_null},
                    {"friendly_name": "CometNewsFeedPaginationQuery", "frames": field_error},
                ],
            },
        ]
    }


ATTACHMENTS_FIXTURE: Final = "feed_attachments_synthetic.json"
ATTACHMENTS_SOURCE: Final = (
    "the empty-text shapes A-D of the phase-0 report of 2026-09-30 (their typenames), with the"
    " attachment paths of the research notes (Groups Watcher, recon.id, fbscrape) and synthetic"
    " values; synthetic until a capture confirms the paths"
)
ATTACHMENT_EXTRA_KEYS: Final = frozenset(
    {
        "web_link", "external_url", "fbclid", "action_links", "media", "large_share_image",
        "image", "uri", "accessibility_caption", "tracking", "deduplication_key",
        "marketplace_listing_seller",
    }
)  # fmt: skip
"""Keys the parser must never read, kept in this fixture so the tests can say it does not."""


def _attachment_story(
    n: int,
    *,
    created_at: int,
    text: str | None = None,
    attachments: Sequence[Mapping[str, Any]] = (),
    attached: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A feed story in the shape of the group captures' (content and context sections), with
    no message unless ``text``, the given attachments and shared story."""
    pid = post(n)
    permalink = f"https://www.facebook.com/groups/{G1}/posts/{pid}/"
    actor = {"__typename": "User", "id": user(n), "name": f"Synthetic Member {n}"}
    content: dict[str, Any] = {
        "__typename": "Story",
        "id": f"opaque-story-{n}",
        "post_id": pid,
        "actors": [actor],
        "message": None if text is None else {"__typename": "TextWithEntities", "text": text},
        "comet_sections": {"message": None, "message_container": None, "attached_story": None},
        "attachments": list(attachments),
        "attached_story": attached,
        "target_group": {"id": G1},
        "wwwURL": permalink,
    }
    return {
        "__typename": "Story",
        "id": f"opaque-story-{n}",
        "post_id": pid,
        "creation_time": created_at,
        "sponsored_data": None,
        "permalink_url": permalink,
        "to": {"__typename": "Group", "id": G1, "name": GROUP_NAMES[G1]},
        "actors": [actor],
        "attached_story": attached,
        "comet_sections": {
            "__typename": "CometStorySections",
            "content": {"__typename": "CometFeedStoryDefaultContentStrategy", "story": content},
            "context_layout": {
                "__typename": "CometFeedStoryDefaultContextLayoutStrategy",
                "story": {
                    "comet_sections": {
                        "metadata": [
                            {
                                "__typename": "CometFeedStoryMinimizedTimestampStrategy",
                                "story": {"creation_time": created_at, "url": permalink},
                            }
                        ]
                    }
                },
            },
        },
    }


def _link_card(title: str, description: str, source: str, target_url: str) -> dict[str, Any]:
    """Shape C: a link share (``ExternalUrl`` target, ``ExternalWebLink``, the share style
    info, an article-context button, the generic share footer, a preview picture)."""
    wrapped = "https://l.facebook.com/l.php?u=" + target_url.replace("/", "%2F")
    return {
        "__typename": "StoryAttachment",
        "deduplication_key": "c3ludGhldGljLWRlZHVwbGljYXRpb24ta2V5LWMx",
        "target": {
            "__typename": "ExternalUrl",
            "id": "710000000000001",
            "external_url": target_url,
        },
        "styles": {
            "__typename": "StoryAttachmentShareStyleRenderer",
            "attachment": {
                "url": wrapped,
                "web_link": {
                    "__typename": "ExternalWebLink",
                    "url": target_url,
                    "fbclid": "IwZXh0bgNhZW0CMTEAAR2synthetic0fbclid0token0value",
                },
                "title_with_entities": {"__typename": "TextWithEntities", "text": title},
                "description": {"__typename": "TextWithEntities", "text": description},
                "source": {"__typename": "TextWithEntities", "text": source},
                "style_infos": [{"__typename": "ExternalShareAttachmentStyleInfo"}],
                "action_links": [
                    {
                        "__typename": "ArticleContextActionLink",
                        "title": {"text": "Synthetic button: about this website"},
                        "url": wrapped,
                    }
                ],
                "comet_footer_renderer": {
                    "__typename": "CometStoryAttachmentFooterGenericShareStrategy",
                    "attachment": {
                        "source": {"text": source},
                        "title_with_entities": {"text": title},
                    },
                },
                "media": {
                    "__typename": "GenericAttachmentMedia",
                    "accessibility_caption": "Synthetic caption: may be an image of a building",
                    "large_share_image": {"uri": "https://external.example.invalid/preview.jpg"},
                },
                "tracking": "AZXsynthetic0tracking0token0value0AAAA",
            },
        },
    }


def attachments_synthetic() -> tuple[dict[str, Any], set[str]]:
    """Stories whose text is in an attachment or nowhere (the phase-0 shapes), and the values
    they introduce (kept as they are by the anonymizer)."""
    t = T0 - 3000
    link_title = "Synthetic link title: two-room flat in Lozenets, 650 EUR"
    link_description = "Synthetic link description: furnished, near the metro, free from November."
    link_source = "SYNTHETIC-HOMES.BG"
    link_url = "https://synthetic-homes.example.invalid/offer/2-room-lozenets"
    link_share = _attachment_story(
        301,
        created_at=t,
        attachments=[_link_card(link_title, link_description, link_source, link_url)],
    )
    link_with_words = _attachment_story(
        302,
        created_at=t - 60,
        text="Synthetic text: have a look at this one.",
        attachments=[_link_card(link_title, link_description, link_source, link_url)],
    )
    listing_title = "Synthetic listing: corner sofa, grey"
    marketplace_share = _attachment_story(
        303,
        created_at=t - 120,
        attachments=[
            {
                "__typename": "StoryAttachment",
                "target": {"__typename": "MarketplaceListing", "id": "700000000000011"},
                "styles": {
                    "__typename": "StoryAttachmentFeedStandardStyleRenderer",
                    "attachment": {
                        "url": "https://www.facebook.com/marketplace/item/700000000000011/",
                        "title_with_entities": {"text": listing_title},
                        "style_infos": [
                            {"__typename": "CommerceProductItemStyleInfo"},
                            {"__typename": "FeedStandardAttachmentStyleInfo"},
                            {"__typename": "FutureOfFeedAttachmentStyleInfo"},
                        ],
                        "media": {
                            "__typename": "GenericAttachmentMedia",
                            "image": {"uri": "https://scontent.example.invalid/sofa.jpg"},
                        },
                        "comet_footer_renderer": {
                            "__typename": "CometStoryAttachmentFooterGHLMockedGenericShareStrategy"
                        },
                        "target": {
                            "__typename": "MarketplaceListing",
                            "id": "700000000000011",
                            "listing_price": {"formatted_amount": "250 лв."},
                            "location_text": {"text": "Plovdiv, Bulgaria"},
                            "marketplace_listing_seller": {
                                "__typename": "User",
                                "id": user(399),
                                "name": "Synthetic Seller Name",
                            },
                        },
                    },
                },
            }
        ],
    )
    group_listing_title = "Synthetic listing: bicycle, 26 inch"
    shared_listing = _attachment_story(
        304,
        created_at=t - 180,
        attached={
            "__typename": "Story",
            "id": "opaque-story-9304",
            "post_id": post(9304),
            "message": None,
            "attachments": [
                {
                    "__typename": "StoryAttachment",
                    "styles": {
                        "__typename": "StoryAttachmentGroupCommerceProductItemStyleRenderer",
                        "attachment": {
                            "title_with_entities": {"text": group_listing_title},
                            "target": {
                                "__typename": "GroupCommerceProductItem",
                                "id": "700000000000012",
                                "marketplace_listing_title": group_listing_title,
                                "formatted_price": {"text": "BGN 90"},
                                "location_text": {"text": "Varna"},
                            },
                        },
                    },
                }
            ],
        },
    )
    empty_share = _attachment_story(
        305,
        created_at=t - 240,
        attached={"__typename": "Story", "id": "opaque-story-9305"},
    )
    unavailable = _attachment_story(
        306,
        created_at=t - 300,
        attachments=[
            {
                "__typename": "StoryAttachment",
                "styles": {
                    "__typename": "StoryAttachmentUnavailableStyleRenderer",
                    "attachment": {
                        "title_with_entities": {"text": "Synthetic notice: content unavailable"},
                        "description": {"text": "Synthetic notice: the owner may have removed it."},
                        "comet_footer_renderer": {
                            "__typename": "CometStoryAttachmentFooterGHLMockedGenericShareStrategy"
                        },
                    },
                },
            }
        ],
    )
    photo_only = _attachment_story(
        307,
        created_at=t - 360,
        attachments=[
            {
                "__typename": "StoryAttachment",
                "target": {"__typename": "Photo", "id": "720000000000001"},
                "styles": {
                    "__typename": "StoryAttachmentPhotoStyleRenderer",
                    "attachment": {
                        "media": {
                            "__typename": "Photo",
                            "accessibility_caption": "Synthetic caption: may be an image of a car",
                        }
                    },
                },
            }
        ],
    )
    stories = [
        link_share,
        link_with_words,
        marketplace_share,
        shared_listing,
        empty_share,
        unavailable,
        photo_only,
    ]
    label = "CometNewsFeed_viewerConnection$stream$CometNewsFeed_viewer_news_feed"
    frames = [
        {"data": {"viewer": {"news_feed": {"edges": [_edge(stories[0], "cursor-att-0")]}}}},
        *(
            {
                "label": label,
                "path": ["viewer", "news_feed", "edges", i],
                "data": _edge(story, f"cursor-att-{i}"),
            }
            for i, story in enumerate(stories[1:], start=1)
        ),
    ]
    url = "https://www.facebook.com/?filter=groups&sk=h_chr"
    manifest = {
        "steps": [
            {
                "final_url": url,
                "graphql": [{"friendly_name": "CometNewsFeedPaginationQuery", "frames": frames}],
                "dom": {"html_lang": "en", "posinset_count": len(stories)},
            }
        ]
    }
    keep: set[str] = {url, *GROUP_NAMES.values()}
    stack: list[Any] = [manifest]
    while stack:  # every value the recipe wrote is its own, and kept as written
        value = stack.pop()
        if isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, str):
            keep.add(value)
    return manifest, keep


def synthetic(out: Path) -> list[Path]:
    """The fixtures built without the research captures."""
    manifest, keep = attachments_synthetic()
    pruner = Pruner(Anonymizer(keep), ATTACHMENT_EXTRA_KEYS)
    path = out / ATTACHMENTS_FIXTURE
    write_json(path, build_steps(manifest, out, pruner, source=ATTACHMENTS_SOURCE, synthetic=True))
    return [path]


def research(src: Path, out: Path) -> list[Path]:
    """Regenerate every committed fixture from the research captures."""
    written: list[Path] = []

    def emit(name: str, content: str) -> None:
        path = out / name
        path.write_text(content, encoding="utf-8")
        written.append(path)

    for name, rel, what in (
        ("group_chrono_loggedout.html", GROUP_CAPTURE, "logged-out group page, CHRONOLOGICAL"),
        ("group_share_loggedout.html", SHARE_CAPTURE, "logged-out group page with a share"),
    ):
        values = list(iter_document_json((src / rel).read_bytes()))
        pruner = Pruner(Anonymizer())
        emit(
            name,
            build_document(
                values, pruner, source=f"research capture 2026-09-24, {what}", synthetic=False
            ),
        )
    for name, rel in (
        ("group_header_member.html", "group_header_private_member.script.txt"),
        ("group_header_pending.html", "group_header_private_pending.script.txt"),
    ):
        values = [json.loads((src / ADBERTRAM / rel).read_text(encoding="utf-8"))]
        pruner = Pruner(Anonymizer())
        emit(
            name,
            build_document(
                values, pruner, source=ADBERTRAM_NOTICE.format(name=rel), synthetic=False
            ),
        )

    synth = Synth(Templates.load(src))
    feed_ssr, feed_steps = feed_synthetic(synth)
    group_steps = group_pagination_synthetic(synth)
    joins_ssr, joins_steps = joins_synthetic(synth)
    shapes = "story shapes of the 2026-09-24 group captures, synthetic values"
    pruner = Pruner(Anonymizer(synth.keep))
    emit(
        "feed_hchr_synthetic.html", build_document(feed_ssr, pruner, source=shapes, synthetic=True)
    )
    emit(
        "groups_joins_synthetic.html",
        build_document(joins_ssr, pruner, source="assumed shape, synthetic", synthetic=True),
    )
    for name, manifest, source in (
        ("feed_hchr_synthetic.json", feed_steps, shapes),
        ("group_pagination_synthetic.json", group_steps, shapes),
        ("groups_joins_synthetic.json", joins_steps, "assumed shape, synthetic"),
        ("graphql_errors_synthetic.json", errors_synthetic(), "synthetic"),
    ):
        built = build_steps(manifest, out, pruner, source=source, synthetic=True)
        path = out / name
        write_json(path, built)
        written.append(path)
    return written + synthetic(out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    doc = sub.add_parser("document", help="an HTML capture -> an HTML fixture")
    doc.add_argument("capture", type=Path)
    doc.add_argument("out", type=Path)
    doc.add_argument("--source", required=True)
    doc.add_argument("--extra-key", action="append", default=[], dest="extra_keys")
    steps = sub.add_parser("steps", help="a steps manifest -> a JSON fixture")
    steps.add_argument("manifest", type=Path)
    steps.add_argument("out", type=Path)
    steps.add_argument("--source", required=True)
    steps.add_argument("--extra-key", action="append", default=[], dest="extra_keys")
    res = sub.add_parser("research", help="regenerate the committed fixtures")
    res.add_argument("research_dir", type=Path)
    res.add_argument("out_dir", type=Path)
    syn = sub.add_parser("synthetic", help="regenerate the fixtures built without captures")
    syn.add_argument("out_dir", type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.command == "document":
        values = list(iter_document_json(args.capture.read_bytes()))
        pruner = Pruner(Anonymizer(), args.extra_keys)
        args.out.write_text(
            build_document(values, pruner, source=args.source, synthetic=False), encoding="utf-8"
        )
        written = [args.out]
    elif args.command == "steps":
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        pruner = Pruner(Anonymizer(), args.extra_keys)
        write_json(
            args.out,
            build_steps(
                manifest, args.manifest.parent, pruner, source=args.source, synthetic=False
            ),
        )
        written = [args.out]
    elif args.command == "synthetic":
        written = synthetic(args.out_dir)
    else:
        written = research(args.research_dir, args.out_dir)
    for path in written:
        sys.stdout.write(f"{path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
