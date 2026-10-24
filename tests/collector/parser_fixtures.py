"""Load the parser fixtures in ``tests/fixtures`` as driver ``Step`` objects.

The fixtures are built by ``tools/make_fixture.py``; a ``.json`` fixture is a list of steps whose
GraphQL bodies are stored framed (``for (;;);`` plus NDJSON), exactly as the driver captures
them, and whose ``document`` names an ``.html`` fixture next to it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fbcollector.model import DomFacts, GraphqlBody, Step

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def load_document(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def document_step(name: str, *, url: str = "https://www.facebook.com/", **dom: Any) -> Step:
    return Step(final_url=url, status=200, document=load_document(name), dom=DomFacts(**dom))


def load_steps(name: str) -> list[Step]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    steps = []
    for raw in data["steps"]:
        document = load_document(raw["document"]) if raw.get("document") else None
        bodies = tuple(
            GraphqlBody(
                friendly_name=body.get("friendly_name"),
                body=body["body"].encode("utf-8"),
                status=body.get("status"),
                capped=bool(body.get("capped", False)),
            )
            for body in raw.get("graphql", [])
        )
        steps.append(
            Step(
                final_url=raw["final_url"],
                status=raw.get("status"),
                document=document,
                graphql=bodies,
                dom=DomFacts(**raw.get("dom", {})),
                pagination_fired=bool(raw.get("pagination_fired", False)),
            )
        )
    return steps


def fixture_meta(name: str) -> dict[str, Any]:
    meta: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))["_fixture"]
    return meta
