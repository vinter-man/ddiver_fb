"""The brain's own tables: the ``fb`` migration track on top of ddiver-core's schema.

The core versions its tables with ``PRAGMA user_version``; these live on a named track
(``schema_tracks``), so a core upgrade and a brain upgrade never renumber each other. The list is
append-only, like the core's: a released script is never edited, a change is a new entry.

Times in these tables are epoch seconds (INTEGER, UTC). The wire speaks epoch seconds, and every
use here is arithmetic — silence gaps, lease ages, command expiry — so converting to the core's
ISO text and back would buy nothing. The core's own tables keep their ISO text.

``fb_items`` keeps ids and times, which is all the dedupe and the silence statistics need. Post
text lives in the outbox payload of a match and, since v4, in ``fb_texts`` for every post of a
watched group (the AI rule judges them from there, and its models are compared on them): both
are pruned after seven days (invariant 8), the text is never logged above DEBUG. So are the AI
rule's verdicts (v5), whose reasons retell the posts.
"""

from __future__ import annotations

from typing import Final

TRACK: Final = "fb"

MIGRATIONS: Final[tuple[str, ...]] = (
    # v1
    """
    CREATE TABLE fb_groups (
      chat_id INTEGER PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
      slug TEXT,
      url TEXT NOT NULL,
      privacy TEXT NOT NULL DEFAULT 'unknown'
        CHECK (privacy IN ('public','private','unknown')),
      state TEXT NOT NULL DEFAULT 'member'
        CHECK (state IN ('member','unverified','left','lost')),
      followed INTEGER,
      last_synced_at INTEGER,
      last_listed_at INTEGER,
      state_changed_at INTEGER NOT NULL,
      listings INTEGER NOT NULL DEFAULT 0,
      tier INTEGER NOT NULL DEFAULT 1 CHECK (tier BETWEEN 1 AND 9),
      sort_ignored INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE fb_items (
      group_id INTEGER NOT NULL,
      post_id INTEGER NOT NULL,
      created_at INTEGER NOT NULL,
      received_at INTEGER NOT NULL,
      via TEXT NOT NULL,
      late INTEGER NOT NULL DEFAULT 0,
      claimed INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (group_id, post_id)
    );
    CREATE INDEX fb_items_received ON fb_items(received_at);
    CREATE INDEX fb_items_group_created ON fb_items(group_id, created_at);
    CREATE TABLE collector_state (
      collector_id TEXT PRIMARY KEY,
      first_seen_at INTEGER NOT NULL,
      last_seen_at INTEGER NOT NULL,
      last_status_at INTEGER,
      status TEXT,
      last_control_at INTEGER
    );
    CREATE TABLE collector_events (
      collector_id TEXT NOT NULL,
      event_id INTEGER NOT NULL,
      kind TEXT NOT NULL,
      at INTEGER NOT NULL,
      group_id INTEGER,
      block_id INTEGER,
      detail TEXT,
      received_at INTEGER NOT NULL,
      alerted_at INTEGER,
      PRIMARY KEY (collector_id, event_id)
    );
    CREATE INDEX collector_events_unalerted ON collector_events(alerted_at, received_at);
    CREATE TABLE fb_commands (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT NOT NULL CHECK (kind IN ('resume','refresh_groups')),
      block_id INTEGER,
      nonce TEXT NOT NULL UNIQUE,
      created_at INTEGER NOT NULL,
      expires_at INTEGER NOT NULL,
      done_at INTEGER,
      done_reason TEXT
    );
    CREATE TABLE fb_kv (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );
    CREATE TABLE fb_alerts (
      key TEXT PRIMARY KEY,
      episode TEXT NOT NULL,
      sent_at INTEGER NOT NULL
    );
    """,
    # v2 — review round 1: an event is acked only once applied; statuses
    # and snapshots are ordered per collector; a group remembers the collector time of the
    # evidence behind its state; a command names the collector and lease generation it is for;
    # notices about a group change are kept until the bot has delivered them.
    """
    ALTER TABLE collector_events ADD COLUMN applied_at INTEGER;
    UPDATE collector_events SET applied_at = received_at;
    ALTER TABLE collector_state ADD COLUMN status_seq INTEGER;
    ALTER TABLE collector_state ADD COLUMN status_incarnation TEXT;
    ALTER TABLE collector_state ADD COLUMN status_sent_at INTEGER;
    ALTER TABLE collector_state ADD COLUMN snapshot_seq INTEGER;
    ALTER TABLE collector_state ADD COLUMN snapshot_incarnation TEXT;
    ALTER TABLE collector_state ADD COLUMN snapshot_taken_at INTEGER;
    ALTER TABLE fb_groups ADD COLUMN evidence_at INTEGER;
    ALTER TABLE fb_groups ADD COLUMN evidence_by TEXT;
    ALTER TABLE fb_commands ADD COLUMN collector_id TEXT;
    ALTER TABLE fb_commands ADD COLUMN lease_generation INTEGER;
    ALTER TABLE fb_commands ADD COLUMN lock_lifted INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE fb_notices (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      body TEXT NOT NULL,
      created_at INTEGER NOT NULL,
      sent_at INTEGER,
      attempts INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX fb_notices_pending ON fb_notices(sent_at, id);
    """,
    # v3 — review round 2: what the owner must hear about a collector's
    # check-in is written down for the monitor to send, never sent while the collector waits;
    # a collector is its id and its state database (incarnation), for the lease and for events:
    # event ids count per database, so the events table is rebuilt with the incarnation in its
    # key ('' for rows and collectors without one).
    """
    ALTER TABLE collector_state ADD COLUMN refused_by TEXT;
    ALTER TABLE collector_state ADD COLUMN refused_incarnation TEXT;
    ALTER TABLE collector_state ADD COLUMN incarnation TEXT;
    CREATE TABLE collector_events_v3 (
      collector_id TEXT NOT NULL,
      incarnation TEXT NOT NULL DEFAULT '',
      event_id INTEGER NOT NULL,
      kind TEXT NOT NULL,
      at INTEGER NOT NULL,
      group_id INTEGER,
      block_id INTEGER,
      detail TEXT,
      received_at INTEGER NOT NULL,
      alerted_at INTEGER,
      applied_at INTEGER,
      PRIMARY KEY (collector_id, incarnation, event_id)
    );
    INSERT INTO collector_events_v3 (collector_id, incarnation, event_id, kind, at, group_id,
      block_id, detail, received_at, alerted_at, applied_at)
    SELECT collector_id, '', event_id, kind, at, group_id, block_id, detail, received_at,
      alerted_at, applied_at
    FROM collector_events;
    DROP TABLE collector_events;
    ALTER TABLE collector_events_v3 RENAME TO collector_events;
    CREATE INDEX collector_events_unalerted ON collector_events(alerted_at, received_at);
    """,
    # v4 — the AI rule (2026-10-10): every stored post as the collector
    # sent it (the wire ``Item``), kept seven days, for the models to judge and be compared on.
    """
    CREATE TABLE fb_texts (
      group_id INTEGER NOT NULL,
      post_id INTEGER NOT NULL,
      received_at INTEGER NOT NULL,
      item TEXT NOT NULL,
      PRIMARY KEY (group_id, post_id)
    );
    CREATE INDEX fb_texts_received ON fb_texts(received_at);
    """,
    # v5 — the AI rule itself (2026-10-12): a user's wish
    # for a group, the posts waiting for the model, and what it said. A verdict keeps the
    # model's reason, which retells the post: pruned with the texts, never logged.
    """
    CREATE TABLE fb_ai_wishes (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      group_id INTEGER NOT NULL,
      wish TEXT NOT NULL,
      threshold INTEGER NOT NULL DEFAULT 70 CHECK (threshold BETWEEN 1 AND 100),
      live INTEGER NOT NULL DEFAULT 0,
      created_at INTEGER NOT NULL
    );
    CREATE INDEX fb_ai_wishes_group ON fb_ai_wishes(group_id);
    CREATE TABLE fb_ai_queue (
      wish_id INTEGER NOT NULL,
      group_id INTEGER NOT NULL,
      post_id INTEGER NOT NULL,
      queued_at INTEGER NOT NULL,
      due_at INTEGER NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 0,
      last_error TEXT,
      PRIMARY KEY (wish_id, group_id, post_id)
    );
    CREATE INDEX fb_ai_queue_due ON fb_ai_queue(due_at);
    CREATE TABLE fb_ai_verdicts (
      wish_id INTEGER NOT NULL,
      group_id INTEGER NOT NULL,
      post_id INTEGER NOT NULL,
      judged_at INTEGER NOT NULL,
      percent INTEGER,
      reason TEXT,
      error TEXT,
      model TEXT,
      cost REAL,
      calls INTEGER NOT NULL DEFAULT 1,
      PRIMARY KEY (wish_id, group_id, post_id)
    );
    CREATE INDEX fb_ai_verdicts_judged ON fb_ai_verdicts(judged_at);
    """,
)
