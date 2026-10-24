"""Which writes were fsynced: a statement trace of the store's connection, read back.

SQLite's trace expands bound values, so a test can tell which kv keys and which group flags
were written while ``PRAGMA synchronous=FULL`` was in force (a durable commit).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from fbcollector.store import Store

_KV_KEY = re.compile(r"INSERT INTO kv \(key, value, updated_at\) VALUES \('([^']*)'")
_FLAGS = re.compile(r"UPDATE groups SET flags='([^']*)', updated_at=\S+ WHERE id='([^']*)'")


def durable_writes(statements: Sequence[str]) -> set[str]:
    """kv keys (``kv:<key>``) and group flag sets (``flags:<id>:<json>``) written durably."""
    found: set[str] = set()
    full = False
    for raw in statements:
        sql = " ".join(raw.split())
        if sql == "PRAGMA synchronous=FULL":
            full = True
        elif sql == "PRAGMA synchronous=NORMAL":
            full = False
        elif full:
            if match := _KV_KEY.search(sql):
                found.add(f"kv:{match.group(1)}")
            elif match := _FLAGS.search(sql):
                found.add(f"flags:{match.group(2)}:{match.group(1)}")
    return found


@asynccontextmanager
async def traced(store: Store) -> AsyncIterator[list[str]]:
    statements: list[str] = []
    await store.db.set_trace_callback(statements.append)
    try:
        yield statements
    finally:
        await store.db.set_trace_callback(lambda _sql: None)
