"""Raw payloads: every step's HTML and GraphQL bodies, kept 48 h for ``replay`` (invariant 8).

A parser fix is only worth something if the posts it would have caught are still there. Each
step is written as one gzip'd JSON file, 0600, under ``<root>/raw`` and pruned by age with the
rest of the state; ``fbcollector replay --since`` reads them back into ``Step`` objects and runs
the same parser and store logic again. Stories that raised while being read are kept next to
them (``quarantine-*``).

Bodies are bytes and may not be valid UTF-8, so they are stored as text with
``surrogateescape`` and restored byte for byte.
"""

from __future__ import annotations

import gzip
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from fbcollector.model import DomFacts, GraphqlBody, Step, Surface

FORMAT: Final = 1
STEP_SUFFIX: Final = ".step.json.gz"
QUARANTINE_PREFIX: Final = "quarantine-"


def _text(raw: bytes | None) -> str | None:
    return None if raw is None else raw.decode("utf-8", errors="surrogateescape")


def _bytes(text: str | None) -> bytes | None:
    return None if text is None else text.encode("utf-8", errors="surrogateescape")


def write_private_gz(path: Path, data: dict[str, Any]) -> Path:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb") as handle:
        handle.write(json.dumps(data, ensure_ascii=True).encode("ascii"))
    return path


@dataclass(frozen=True, slots=True)
class RawStep:
    read_id: int
    index: int
    surface: Surface
    group_id: str | None
    at: float
    step: Step


def save_step(
    directory: Path,
    *,
    read_id: int,
    index: int,
    surface: Surface,
    group_id: str | None,
    at: float,
    step: Step,
) -> Path:
    data = {
        "v": FORMAT,
        "read_id": read_id,
        "index": index,
        "surface": surface.value,
        "group_id": group_id,
        "at": at,
        "final_url": step.final_url,
        "status": step.status,
        "document": _text(step.document),
        "graphql": [
            {
                "friendly_name": b.friendly_name,
                "status": b.status,
                "label": b.label,
                "capped": b.capped,
                "body": _text(b.body),
            }
            for b in step.graphql
        ],
        "dom": {
            "html_lang": step.dom.html_lang,
            "posinset_count": step.dom.posinset_count,
            "dialog_texts": list(step.dom.dialog_texts),
            "dialog_count": step.dom.dialog_count,
            "joined_header_count": step.dom.joined_header_count,
            "has_password_field": step.dom.has_password_field,
            "see_more": step.dom.see_more,
        },
        "lost_bodies": step.lost_bodies,
        "pagination_fired": step.pagination_fired,
        "latency_ms": step.latency_ms,
    }
    return write_private_gz(directory / step_name(read_id, index, at), data)


def step_name(read_id: int, index: int, at: float) -> str:
    """The file a step is saved in (``replay`` names it in a quarantine row it writes)."""
    return f"{int(at):010d}-r{read_id:08d}-s{index:02d}{STEP_SUFFIX}"


def save_quarantine(
    directory: Path,
    *,
    read_id: int,
    index: int,
    at: float,
    post_id: str | None,
    raw: bytes,
    surface: Surface,
    group_id: str | None,
) -> Path:
    name = f"{QUARANTINE_PREFIX}{int(at):010d}-r{read_id:08d}-{index:03d}.json.gz"
    data = {
        "post_id": post_id,
        "raw": _text(raw),
        "surface": surface.value,
        "group_id": group_id,
        "at": at,
        "read_id": read_id,
    }
    return write_private_gz(directory / name, data)


@dataclass(frozen=True, slots=True)
class RawQuarantine:
    read_id: int
    surface: Surface
    group_id: str | None
    at: float
    node: bytes


def iter_quarantined(directory: Path, *, since: float) -> Iterator[RawQuarantine]:
    """Stories that raised while being read, at or after ``since``."""
    if not directory.is_dir():
        return
    for path in sorted(directory.glob(f"{QUARANTINE_PREFIX}*.json.gz")):
        try:
            with gzip.open(path, "rb") as handle:
                data = json.loads(handle.read())
            item = RawQuarantine(
                read_id=int(data["read_id"]),
                surface=Surface(data["surface"]),
                group_id=data.get("group_id"),
                at=float(data["at"]),
                node=_bytes(data["raw"]) or b"",
            )
        except (OSError, ValueError, KeyError, EOFError):
            continue
        if item.at >= since:
            yield item


def load_step(path: Path) -> RawStep:
    with gzip.open(path, "rb") as handle:
        data = json.loads(handle.read())
    dom = data.get("dom") or {}
    step = Step(
        final_url=str(data["final_url"]),
        status=data.get("status"),
        document=_bytes(data.get("document")),
        graphql=tuple(
            GraphqlBody(
                friendly_name=b.get("friendly_name"),
                body=_bytes(b.get("body")) or b"",
                status=b.get("status"),
                label=b.get("label"),
                capped=bool(b.get("capped")),
            )
            for b in data.get("graphql", ())
        ),
        dom=DomFacts(
            html_lang=dom.get("html_lang"),
            posinset_count=int(dom.get("posinset_count", 0)),
            dialog_texts=tuple(dom.get("dialog_texts", ())),
            dialog_count=dom.get("dialog_count"),
            joined_header_count=dom.get("joined_header_count"),
            has_password_field=bool(dom.get("has_password_field")),
            see_more=int(dom.get("see_more", 0)),
        ),
        lost_bodies=int(data.get("lost_bodies", 0)),
        pagination_fired=bool(data.get("pagination_fired")),
        latency_ms=data.get("latency_ms"),
    )
    return RawStep(
        read_id=int(data["read_id"]),
        index=int(data["index"]),
        surface=Surface(data["surface"]),
        group_id=data.get("group_id"),
        at=float(data["at"]),
        step=step,
    )


def iter_steps(directory: Path, *, since: float) -> Iterator[RawStep]:
    """Saved steps at or after ``since``, in the order they were taken; unreadable files skipped."""
    if not directory.is_dir():
        return
    for path in sorted(directory.glob(f"*{STEP_SUFFIX}")):
        stamp = path.name.split("-", 1)[0]
        if stamp.isdigit() and int(stamp) + 1 < since:
            continue
        try:
            saved = load_step(path)
        except (OSError, ValueError, KeyError, EOFError):
            continue
        if saved.at >= since:
            yield saved
