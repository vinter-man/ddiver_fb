# ddiver-fb — design

Facebook-group keyword monitor built on ddiver's core. A **collector** at the owner's home drives
a real Google Chrome logged into a secondary Facebook account, reads the groups that account is a
member of, and pushes new posts to a **brain** on the VPS. The brain is ddiver's bot, rules,
matcher and delivery with a Facebook source instead of a Telegram listener; it notifies each user
in a private chat through a separate BotFather bot.

Revision 2 (2026-09-25). Revision 1 went through four adversarial reviews (account safety,
Facebook data path, ddiver-core extraction, operations/security/testability) that raised 67
findings, 7 of them critical; every one is either fixed here or listed under *Open*. Claims
marked *verified* were checked during the research against code, captures or dated primary
sources; claims marked *phase 0* can only be settled by a live run on the real account, and the
design names what happens under each outcome.

## Decisions (fixed, from the requirements sessions of 2026-09-24)

| # | Decision |
|---|---|
| D1 | Long-lived tool ("ddiver for Facebook"), not a one-off apartment script. Multi-user like ddiver: owner (`OWNER_ID`) + allow-listed users, each with own language, timezone, subscriptions, rules, ignore list. |
| D2 | Listener = a **secondary, old** Facebook account (2FA on, already a member of the target groups, has logged in from the home network before, not linked to the owner's main account). UI language switched to **English** on a device the account already trusts, days before the collector's first login. |
| D3 | Groups are **mixed public and private**. All are read the same way: through the account, as a member. |
| D4 | The browser runs **at home** (residential IP): first on the owner's Mac (development and phase 0), then on a Linux x86_64 laptop (8+ GB) running 24/7. The move is a **fresh manual login** on the laptop — the profile is never copied. On the Mac, phase 0 runs under a **dedicated Standard macOS user** in a background fast-user-switching session, with the ordinary Google Chrome (not Chrome Beta) — owner decision of 2026-09-25, see Operations. |
| D5 | Brain + bot run on **a VPS** next to ddiver, in Docker. The VPS never contacts facebook.com. |
| D6 | Collector → brain over an **ssh `-L`** tunnel opened by the collector — on the Mac `/usr/bin/ssh` under a LaunchAgent of the collector's user, on Linux autossh — restricted in sshd to one local forward. Nothing new listens publicly on the VPS. |
| D7 | **Separate bot, shared code.** This repo holds ddiver-fb; the reusable part of ddiver is extracted into `packages/ddiver-core` inside the ddiver repo (uv workspace) and consumed here as a git dependency pinned to a tag. ddiver's behaviour, database and deploy scripts do not change. |
| D8 | Groups are **synced from the account's memberships** (the Facebook analogue of ddiver's dialog list). The owner joins groups by hand; the owner decides which groups other users may see, exactly like ddiver's shared chats. |
| D9 | Matching = **ddiver's rule syntax only** (`word`, `=word`, `re:`, `from:` on the author's id or name, exclusions, ignore list). No price parser. Listing cards (title, price, location) and the text of a shared post are appended to the matched text. A model came later, beside the rules rather than instead of them: the AI rule (Brain), in shadow since 2026-10-12. |
| D10 | Notification = ddiver's format with Facebook links: group (🔒 if private), author (profile link), post link, time in user tz + "N min ago", full text with the match in bold, the rule that fired. Buttons: ignore author, remove rule. **No photo, no cross-post merging, no backfill on subscribe.** |
| D11 | Latency target 5–10 minutes in the daytime, under a hard page-load budget. **Quiet hours** at night (≈ 01:00–07:00 local, edges jittered daily). The first 14 days after every fresh login run a **cautious profile**. |
| D12 | Never write to Facebook from automation: no posts, comments, reactions, joins, follows, settings, clicks or typing. Every human action on the listener account happens by hand, inside the collector's own browser profile. |
| D13 | Languages uk / ru / en for the bot; posts may be ru/uk/en/bg/es. Code, comments, this document and the README in English. |
| D14 | Python 3.12, MIT, uv. Tests from the first commit; ruff + mypy strict + coverage gate in CI — the same bar as ddiver. |

Decided by the design (the owner may override):

| # | Decision |
|---|---|
| D15 | Collector driver: **Patchright** (Playwright fork without the `Runtime.enable` / `--enable-automation` leaks) driving **installed Google Chrome, headed**, persistent profile, no custom UA, no stealth plugins, `chromium_sandbox=True`. **One launcher for login and for reading**, with identical arguments. Passive capture only (invariant 1). *Verified on this Mac (2026-09-24, local test page): `navigator.webdriver=false`, no `HeadlessChrome`, the CDP stack-getter probe does not fire, streamed NDJSON bodies are captured whole.* |
| D16 | Primary surface: the **Feeds → Groups** view `https://www.facebook.com/?filter=groups&sk=h_chr` (newest-first posts of every *followed* group). Backstop: each monitored group at `https://www.facebook.com/groups/<id>/?sorting_setting=CHRONOLOGICAL`. Data comes from the JSON the page itself receives, never from the DOM. |
| D17 | Capacity: **25 monitored groups per listener account** (hard), 20 soft; the budget table below shows what that costs in latency. Beyond it: a second account/collector (`collector_id` is in the wire format; the brain grants one lease per account). |
| D18 | Not built in v1: IMAP source, vendor webhook source, NetLog driver, photos, cross-post merging, backfill, a browser on the VPS. The `Source` seam in the brain and the `Driver` seam in the collector keep each a contained addition. |
| D19 | Collector alerts go through a **separate owner-only alert bot** (its own token; code can only `sendMessage` to `OWNER_ID`). The user-facing bot's token never leaves the VPS. |

## Non-goals (v1)

Anything that acts on Facebook; reading groups the account is not a member of; logged-out
scraping; Marketplace search; two collectors on one account; a web UI; notifications for edits
or comments; Xvfb or headless reading.

## Why this shape (short version of the research)

- **No official or semi-official feed exists.** Graph API Groups API removed 2024-04-22; Zapier /
  Make integrations gone; RSS-Bridge broken (maintainer, 2026-08-01). *Verified.*
- **Push "All posts" is not a listener.** Meta's help page promises every post only for in-app
  notifications; 2025–2026 reports show push offering only Highlights/Off, reverting to
  Highlights, and zero delivery even for admins. *Verified (C1 refuted).*
- **Email is not a listener.** The one real 2026 IMAP dataset saw 1 group-post email in 10 weeks
  across ~35 groups, subject snippet only. *Verified (C3).*
- **Light HTML surfaces are gone.** mbasic is dead (since 2024-12); m.facebook.com with a mobile
  UA shows 2–3 ranked posts of public groups only. *Verified (C4).*
- **Hosted services read private groups only through a member account** — yours or theirs after an
  admin lets it in, with no guarantee. *Verified (C10).*
- **What works:** a real browser session of a member account. The group page carries Relay JSON
  with `post_id`, `creation_time`, full `message.text`, permalink and author, and more stories
  arrive in the page's own `…PaginationQuery` responses. *Verified for group pages (C5)*, with
  caveats handled below: pinned/highlight units out of order, one regular story in the SSR,
  one out-of-order anchor story per pagination batch. **The Groups feed's JSON shape is
  phase 0**: C6 established its query and ordering from extension code (mid-2024), not from a
  logged-in capture.
- **Ban risk is driven by rhythm and volume, then by location and device continuity.** A real
  Chrome with a real profile on its owner's machine got "We suspect automated behavior"
  (carousell/sellee PR #79, 2026-09-09) on an unjittered 300 s grid with deep scrolling and
  writes. Sessions moved to datacenter IPs die in hours to days; new-device logins triggered a
  wave of `checkpoint/advanced_protection` locks in Aug–Sep 2026. *Verified (C7, C8), both
  partially: no controlled study exists.*

## Invariants

1. **The collector never writes to Facebook and never speaks for the page.** Allowed: navigating
   the tab to an allow-listed URL, `mouse.move` to non-interactive coordinates, mouse-wheel
   events, reading response bodies the page itself requested, reading the DOM/SSR from an
   *isolated* world, taking a screenshot, reading cookies. Nothing else. Enforced three ways:
   only `fbcollector/driver/_patchright.py` may import Patchright (import-boundary test); an AST
   test allows only a fixed set of attribute calls in that module, bans `getattr` with a
   non-literal name and requires `isolated_context=True` as a literal; the raw CDP session is
   wrapped in a runtime allow-list (`Network.enable`, `Network.getResponseBody`,
   `Page.captureScreenshot`) that raises on anything else, and a driver test records the
   protocol traffic on a local page and asserts only allow-listed methods were sent.
2. **No Facebook traffic unless every gate is open**: home network, outside quiet hours and
   breaks, inside the budget, no block, no crash loop, profile lock held, on AC power (macOS),
   OS timezone equal to the pinned one. Gates are checked before **launching Chrome**, before
   every navigation and before every scroll step. Chrome is **not running** while any gate is
   closed; between reads inside an open window the tab is parked on `about:blank`.
3. **A warning is terminal until a human says otherwise.** Any checkpoint, logout,
   "suspect automated behaviour", "temporarily blocked", rate-limit code, captcha or unrecognised
   page (twice) → the block is written to disk *before* anything else happens, Chrome is closed,
   the owner is alerted. Resuming needs the block's time to pass **and** a `/resume` bound to that
   block's id **and** one clean probe. Never auto-login, never type a password or code, never
   press "This was me".
4. **The VPS never contacts facebook.com or fbcdn.net.** The brain only receives items.
5. **A post is durable before it is acknowledged.** The collector writes the parsed item to its
   outbox and its sighting to `seen` in one transaction; an outbox row is deleted only when the
   brain answered *processed* for it (stored, duplicate, not monitored, or permanently rejected);
   the brain acknowledges an item only after every `claim_delivery` for it succeeded.
6. **Budget caps are hard, persistent and write-ahead.** Every navigation and scroll step is
   recorded in SQLite and fsynced *before* it is issued; caps are counted over sliding UTC
   windows from that table; minimum spacing after a restart is computed from the last recorded
   event; process starts and Chrome launches are counted too (crash-loop gate).
7. **The browser profile is the account.** It lives under one 0700 root with the collector's
   state, excluded from Time Machine / backups / Spotlight; it is never copied, archived,
   exported or synced; one process per profile; the login is done by a human.
8. **Post text is never logged above DEBUG.** Raw payloads and screenshots are kept 48 h (for the
   whole of phase 0 plus 7 days in observe mode), 0600, gzip; matched posts are pruned after 7
   days on the brain (ddiver's rule), and so is every post of a watched group, kept since
   2026-10-10 for the AI rule (`fb_texts`), and the AI rule's verdicts, whose reasons retell
   the posts (`fb_ai_verdicts`, since 2026-10-12). Wishes and reasons are not logged either.

## Process model

```
 Home (Mac → Linux laptop)                                   VPS (Docker)
┌──────────────────────────── fbcollector ────────────────┐   ┌──────────────── ddiver-fb ───────────────────┐
│ Gates: home · quiet/break · budget · block · crash-loop │   │ Ingest API (127.0.0.1:8781, bearer)           │
│        · lock · AC · timezone                           │   │  POST /v1/items /v1/groups /v1/status         │
│ Scheduler — owns the read loop, step by step             │   │       /v1/events   GET /v1/control            │
│   gate → write-ahead budget event → driver step          │   │        │                                      │
│   → detectors → parser → stop rule                       │   │        ▼                                      │
│ Driver (Patchright → Google Chrome, headed, profile)     │   │ FacebookSource → Matcher → outbox → Delivery  │
│ Parser (edge-level stories → Item) · Detectors (fail-    │   │        (ddiver-core)                ▲         │
│   closed)                                                │   │ Bot UI (ddiver-core + FB overlays)  │         │
│ SQLite: seen · sightings · reads · outbox · budget ·     │   │ Collector state/lease → /status, alerts ─┘    │
│   blocks · groups · kv          raw/ (gzip, 0600)        │   │ SQLite fb.sqlite (core schema + fb track)     │
│ Uplink ── HTTP over ssh -L socket → VPS 127.0.0.1:8781    ┼───────────────────────▲─────────────────────────┘
│ Alert bot (sendMessage → OWNER_ID only)                  │
└──────────────────────────────────────────────────────────┘
```

Two supervised processes on the collector host (launchd on macOS, systemd `--user` on Linux):
`fbcollector run` and the tunnel (`ssh -N` under its own LaunchAgent on macOS, `autossh` on
Linux). The brain is one container.

## Repositories and packages

```
ddiver/                                    (existing repo, becomes a uv workspace)
  packages/ddiver-core/src/ddiver_core/    everything that is not Telegram *user-session* I/O:
                                           rules, matcher, storage, links, senders, formatter,
                                           richtext, delivery, timeutil, i18n (+ overlays), bot/
                                           (router, menus, handlers), portable, app, health,
                                           metrics, watchdog, config base, tgerrors, bot client
                                           factory, botruntime (BotAdapter, notification buttons),
                                           process (InstanceLock, harden_data_dir, JsonFormatter),
                                           testing (FakeClock, FakeBotClient)
  ddiver/                                  Telegram app: listener, catchup, login, runtime
                                           (Application), CLI, user-client half of clients
ddiver_facebook/                           (this repo, uv workspace)
  packages/fbwire/      pydantic wire models shared by both sides (versioned)
  packages/fbcollector/ the home collector (Patchright, aiosqlite, httpx) — no Telethon, no core
  packages/ddiver_fb/   the brain: composition root on ddiver-core + FacebookSource + ingest API
  deploy/               brain compose, sshd snippet, launchd plist, systemd units, bootstrap
  scripts/              e2e_live.py, phase-0 report
```

ddiver-core depends on Telethon and cryptg (the bot side) and contains no user-session I/O.

### Extracting ddiver-core

The base is a named, committed ddiver head: every test at that commit (554 at `a277d62`) stays
green after each step, unedited except for import paths and patch strings. Rules:

- **Pure moves, no shims.** Each step is `git mv` plus rewriting every `ddiver.X` import and patch
  string (`"ddiver.delivery.QUEUE_MAXSIZE"`, `storage_module._MIGRATIONS`, …) to `ddiver_core.X`
  in the same commit. Re-export shims are forbidden: they turn module-global patches into patches
  of the shim. `ddiver.clients` keeps importing `make_bot_client` by name (test_cli patches it
  there).
- **Order**: (1) workspace scaffold: empty `ddiver_core` with `py.typed`; coverage source, mypy
  files, ruff src / known-first-party / per-file-ignores; Dockerfile (workspace-aware: copy member
  pyprojects, `uv sync --frozen --no-dev --no-install-workspace`, then copy sources and sync
  `--no-editable`); a CI job that builds the image. (2) Leaves: timeutil, rules, richtext, i18n,
  watchdog, health, metrics. (3) links, storage. (4) matcher, portable, bot/, formatter, senders,
  delivery. (5) tgerrors (the classifiers, unchanged), bot client factory, app, config. (6) the
  module-level pieces of runtime.py and `__main__.py` (botruntime, process), and `testing`.
  (7) Only then behaviour-neutral additions with their own new tests: `Payload` url fields
  honoured by notification **and** digest rendering; `i18n.register_overlay`; a named
  migration track (`schema_tracks`) so app tables version independently of core's
  `user_version`; a format tag parameter for `/export`/`/import`; menu hooks.
- **No migration of ddiver's live database**; ddiver registers no overlay and no extra track, so
  its behaviour is unchanged.
- The mechanical part (moves and import rewrites) is a script committed with the branch, so it can
  be replayed onto a newer ddiver head if ddiver moves on meanwhile.
- **Consuming a private repo.** Until a tag existed this repo used a `path` source. Since the
  owner merged the extraction and tagged `main`, it is pinned (`core-v0.1.0` today):
  `ddiver-core = { git = "https://github.com/vinter-man/ddiver", tag = "core-vX.Y.Z",
  subdirectory = "packages/ddiver-core" }`, and the core's fakes (`ddiver-core-testing`) come
  from the same tag as a dev dependency. CI reads it with a fine-grained read-only token passed
  only to the sync step via `GIT_CONFIG_COUNT/KEY/VALUE`. The VPS image build installs `git
  openssh-client` in the builder stage, rewrites `https://github.com/` to ssh there, and uses
  `RUN --mount=type=ssh` with a pinned `known_hosts` and an ssh-agent holding the ddiver
  repository's read-only deploy key, so no secret lands in an image layer.

## Collector

### Driver (step-wise)

The driver never loops; the scheduler owns every loop so gates, budget, detectors and stop rules
run between steps.

```python
class Driver(Protocol):
    async def launch(self) -> None: ...  # Chrome on the profile, one about:blank page
    async def navigate(self, url: str) -> Step: ...  # one navigation, capture until settled
    async def scroll_step(self) -> Step: ...  # one human-like wheel gesture, capture
    async def park(self) -> None: ...  # wait for in-flight bodies, then about:blank
    async def close(self) -> None: ...  # graceful context.close(); SIGKILL on timeout or cancel


@dataclass(frozen=True)
class Step:
    final_url: str  # after redirects: detectors
    status: int | None
    document: bytes | None  # SSR HTML (navigate only)
    graphql: list[GraphqlBody]  # every /api/graphql/ body the page received in this step
    dom: DomFacts  # isolated-world facts: html lang, [aria-posinset] count, dialog texts
    lost_bodies: int  # bodies evicted before we could read them
    pagination_fired: bool  # scroll_step only
```

Patchright implementation:

- `launch_persistent_context(root/profile, channel="chrome", headless=False, no_viewport=True,
  chromium_sandbox=True, args=["--no-proxy-server"], ignore_default_args=[…keychain switch on
  macOS…])`. macOS uses the real Keychain; Linux uses `--password-store=basic` on **both** the
  login and the reading launch. At start the driver asserts Chrome's actual command line against
  the recorded login command line and fails closed on any difference.
- Before every launch: delete `Default/Sessions/*`, set `session.restore_on_startup` to "open the
  new tab page" and `exit_type = "Normal"` in Preferences, pass `about:blank` as the only start
  URL; after launch close every page but one. Profile content settings deny notifications and
  background sync for facebook.com; password saving is off.
- **Capture**: a response listener attached before `goto` fetches the body of *every*
  `/api/graphql/` response immediately (size cap 4 MiB) and tags it with
  `fb_api_req_friendly_name` (used for inventory and priority only — the parser walks every
  body). In-flight requests are tracked (`request` → `requestfinished`/`requestfailed`).
  `navigate` waits for load, then U(4, 10) s, polling for stories up to 12 s. `scroll_step`
  waits up to 8 s for a *new* GraphQL request and then up to 20 s for it to finish;
  `pagination_fired=False` is its own outcome, never a gap. `park` waits up to 20 s for in-flight
  bodies first.
- **Input**: once per read, `mouse.move` along a multi-point path to a random point in the feed's
  side margin (never over links or avatars). A scroll step is 3–8 wheel events of 100–120 px,
  16–60 ms apart; step sizes vary; occasionally a small scroll back up. Checked on the local test
  page (coordinates, deltas).
- Images, fonts and CSS are not blocked.
- Chrome version: the major version and Patchright version are recorded; a major not on the tested
  allow-list triggers the local self-test page (webdriver flag, UA, headless markers, response
  capture, isolated-world read, CDP allow-list) before any Facebook navigation; failure pauses
  with an alert. On Linux Chrome is `apt-mark hold` and upgraded only by `fbcollector
  upgrade-chrome` in quiet hours.
- A stale Chrome `SingletonLock` whose PID is not alive is removed; a live one means "another
  Chrome owns the profile" → refuse.

### `fbcollector login` (and the pre-login checklist)

Stops the supervised service, takes the profile lock, launches Chrome **through the same driver
launcher and arguments** on `about:blank`, and does nothing else: the owner types
`facebook.com`, logs in, answers 2FA. The command waits for Chrome to exit, records
`kv.login_at`, restarts the service. The collector starts in `WARMUP` (no automated reads) for
24–72 h after every login; during warm-up the owner browses by hand for a few minutes in
`fbcollector login` sessions. The 14-day cautious ramp is keyed to `login_at`.

Checklist, shown by `fbcollector login` before it launches and repeated in the runbook:
UI language is already English (switched days earlier on a trusted device — the login flow never
changes settings); a passkey or recovery method exists in Accounts Center; a trusted device is at
hand to approve the login; no active lock wave or recent warning; the Mac is at home, on AC.
Every later human action on the listener account (checkpoints, joins, follows, 2FA) happens only
in `fbcollector login` — never on the owner's phone or main browser, which would link the
accounts by device (D2).

### Surfaces and URL allow-list

| Surface | URL | Used for |
|---|---|---|
| Groups feed | `/?filter=groups&sk=h_chr` | new posts from all followed groups |
| Group, chronological | `/groups/<numeric id>/?sorting_setting=CHRONOLOGICAL` | backstop; group metadata (privacy, membership, follow state) |
| Joined groups | `/groups/joins/` | group sync (D8) |

Nothing else is ever opened: not permalinks, profiles or `/marketplace/item/`. A final URL
outside these shapes is a page state for the detectors, never data. Vanity slugs are resolved to
the numeric id once, from the group page's own data. The collector only opens group ids that its
own latest complete sync lists as `member` — or, since no sync can be complete before phase 0
has named the joined-groups JSON, groups the owner confirmed by hand in `monitored.toml`
(`confirmed_member = true`, `fbcollector groups confirm`; owner decision of 2026-09-25). Such a
group's own page must say `viewer_join_state = MEMBER`; anything else, silence included, is
`GROUP_LOST` and withdraws the confirmation, and the next complete sync overrides it. A page
that shows nothing at all (no posts, no empty-group marker) is an empty shell, `UNKNOWN`, as for
any group: silence proves nothing lost on a page that shows nothing.
`CHRONOLOGICAL_LISTINGS` is a per-group switch for buy/sell groups, used only if phase 0 shows
`CHRONOLOGICAL` missing listing posts.

### Parser

Pure functions over bytes, no browser types — fixtures drive it.

1. **Framing** (`frames.py`, done): strip `for (;;);`, split concatenated JSON with
   `JSONDecoder.raw_decode`; SSR: every `script[type="application/json"]` block. Each part keeps
   its `label` and `path`; `page_info` (has_next_page, end_cursor) is read from the `@defer` part
   whose path ends in the connection key (`news_feed`, `group_feed`, the joined-groups connection).
2. **Edge-level story detection.** A story is `…edges[i].node` of a known connection, a stream
   part's `data.node`, or an element of `highlight_units` (tagged `highlight`). The walk never
   descends into a story once found — one post carries `post_id` at four or more nested places
   (verified in the 2026-09-24 capture). Stories are deduped by `post_id`, first position wins.
3. **Scoped fields** (all relative to the story root, never inside `attached_story` unless
   stated):
   - `post_id` — `node.post_id` (digits, kept as a string on the wire).
   - `created_at` — `node.creation_time` → `comet_sections.context_layout.story.comet_sections.
     metadata[*TimestampStrategy].story.creation_time` → `comet_sections.timestamp.story.
     creation_time`. Epoch seconds. A share's own time, never the shared post's.
   - `group_id` — group page: the page's group; feed: `node.to.id` / `node.target_group.id` /
     `context_layout.title.story.to.id`, cross-checked against the permalink; every candidate is
     recorded in phase 0.
   - `url` — `permalink_url` → timestamp-strategy `story.url` → `wwwURL`, canonicalised to
     `https://www.facebook.com/groups/<gid>/posts/<post_id>/` without tracking params.
   - `text` — the story's own message (`content.story.message.text`, else any `message.text`
     under `content.story.comet_sections` — `message` or `message_container`); then, on a new
     line, the `attached_story` text if different; then listing lines: title
     (`title_with_entities.text` or `marketplace_listing_title`), `formatted_price.text`,
     `location_text.text`; then a link card's title, description and source, whether or not
     the story has a message of its own. Cards are read from the story's attachments and its
     shared story's, under an allow-list of keys, and a link, id or token is never text
     (design notes, "Text from attachments"). A card line the text already holds (equal or
     inside it as whole words, compared as the brain's matcher reads text: its cleaning, its
     case-insensitivity, spaces and newlines as they are) is left out (design notes, "Owner
     decisions of 2026-09-30"). The path that produced the text is counted per read (the
     message's when there is one; `attachment.listing`, `attachment.link` for a card alone),
     and so are the stories with a message whose text also got a card's lines.
   - `author` — `actors[0].{id, name, url}`; anonymous participant → `anonymous = true`.
   - `listing` — `{id, title, price, location}` when present (the listing id is not a post id).
   - position, batch index, `is_highlight`, and whether the story is the first of its batch
     (Facebook injects one out-of-order anchor story per pagination batch — fbscrape).
4. **Drop**: `GroupsSectionHeaderUnit`, sponsored/ads, events and reels without a group post id,
   comments. Highlight and pinned stories are **kept** for the novelty check but excluded from
   ordering and stop decisions.
5. **Deferred, not lost.** A story with a text-bearing section but no text, no `created_at`, or an
   unresolved group is stored as *deferred* (retried on the next sightings, raw payload kept),
   not marked seen. `fbcollector replay --since` re-parses raw payloads and emits deferred or
   flagged items still inside the novelty window. Each story is parsed inside its own
   try/except; a poison story is quarantined with its raw bytes and never crashes a read.
6. **Health** per read: stories found / accepted / deferred / dropped by reason, text-path
   histogram, friendly-name histogram, page_info presence, stories whose tree holds a key
   naming a translation (its name only). `PARSER_BROKEN` when accepted/found
   < 50 % over 3 reads, or 0 stories while the DOM shows posts for 3 reads; alert when
   `no_created_at` > 20 %, or when more than 20 % of ≥ 10 posts have empty text nothing
   explains (not a share without words, a photo, a card's text), else when more than half of
   them have no words of their own, whatever explains it (a renamed message section). Each
   post counts once over the 3 reads, however often they read it, and the stories without a
   post id on the same basis (design notes, "The empty-text alert counts posts" and "Review of
   text from attachments and the empty-text alert").

### Seen set, coverage and stop rules

- `seen(group_id, post_id)`, 30-day retention; `sightings(post_id, group_id, read_id, surface,
  position, batch, cursor)` for every story seen.
- **Baseline**: `baseline_at(group)` is set when the group becomes known (sync) or monitored —
  not tied to a read. **Novelty** = unseen AND `created_at ≥ baseline_at` AND
  `created_at ≥ now − 7 d` (configurable). `late = first_seen_at − created_at > 2 h` travels with
  the item so the notification can say "posted 30 h ago, appeared now" (late-approved posts in
  moderated groups keep their submission time — the feed cursor orders by `attached_time`).
  `stale_dropped` is counted per group and shown after downtime.
- **Coverage.** Each read records the newest and oldest `created_at` it reached (using the
  2nd-oldest per batch, ignoring highlights and anchors) and whether it reached its target.
- **Feed stop**: target = start time of the last feed read that reached its target (`T_feed`).
  Stop when ≥ 3 consecutive *distinct* already-seen regular stories AND the 2nd-oldest
  `created_at` of the last batch < `T_feed − 30 min`. Ordinary ticks allow 0–1 steps; every 4th
  tick (jittered) allows up to 3; the day's catch-up allows up to 8 spread over 2–3 reads. A read
  that hits its cap still seeing unseen stories newer than `T_feed` records the uncovered interval
  as a **gap**; gap backstops go to the groups seen in the uncovered part, by posting rate, at most
  3 per gap, each inside the budget and respecting the per-group minimum revisit.
- **Backstop stop**: its own target `B_g` = start of that group's last complete backstop. Scroll
  until the 2nd-oldest `created_at` < `B_g − 30 min`, whatever the feed already saw; cap 2 steps
  (6 in phase 0). A capped read records a backstop gap.
- **feed_miss** counts a post only if its `created_at` lies inside a completed feed read's
  coverage window, its group was followed, and that read did not contain it. Everything else is
  feed latency.
- Watermarks are clamped to `≤ now + 5 min`.

### Scheduler, budget and gates

All caps are counted over sliding UTC windows from `budget_events`. The *planned* load stays at or
below 75 % of every cap, so the scheduler — not the cap — shapes the rhythm; the headroom absorbs
gaps, probes, sync and catch-up.

| | Cautious (first 14 days after every login; 7 days after any warning) | Normal |
|---|---|---|
| Feed | every U(10, 18) min | every U(4.5, 9.5) min |
| Backstop per group | every `max(3 h, the shortest interval that keeps the plan ≤ 75 % of every cautious cap)` ± 30 % — with the defaults `max(3 h, N / 3.21 h)` | every `max(1.5 h, N / 6.4 h)` ± 30 % |
| Hard caps (navigations) | 10 / h, 160 / 24 h | 20 / h, 320 / 24 h |
| Hard caps (scroll steps) | 30 / h, 250 / 24 h | 60 / h, 500 / 24 h |
| Sub-cap | 3 navigations / 15 min | 5 navigations / 15 min |

Normal profile planned load: ≈ 18 h outside quiet hours = ≈ 14.6 full-cadence hours + 2.5 shoulder hours (the first hour after quiet hours and the last 1.5 h before, feed every U(15, 25) min, no backstops) + ≈ 0.9 h of breaks; feed ≈ 8.6/h; ≈ 1.3 scroll steps per feed read and ≈ 1.5 per backstop:

| Monitored groups N | Backstop interval | Navigations/h (mean) | Navigations/24 h | Scroll steps/24 h |
|---|---|---|---|---|
| 10 | 1.6 h | ≈ 15.0 | ≈ 226 | ≈ 312 |
| 15 | 2.3 h | ≈ 15.0 | ≈ 226 | ≈ 312 |
| 20 | 3.1 h | ≈ 15.0 | ≈ 226 | ≈ 312 |
| 25 | 3.9 h | ≈ 15.0 | ≈ 226 | ≈ 312 |

Cautious profile planned load (owner decision of 2026-09-25): the same day shape; feed ≈ 4.3/h
(shoulders U(15, 25) min); backstops get whatever the caps leave at 75 % — the hourly
navigation cap binds at 7.5 − 4.29 = 3.21 backstops an hour — but never come closer than 3 h,
so with a few groups (phase 0 starts with 4) every group is backstopped about every 3 h:

| Monitored groups N | Backstop interval | Navigations/h (mean, p95) | Navigations/24 h (mean, p95) | Scroll steps/24 h (mean, p95) |
|---|---|---|---|---|
| 4 | 3.0 h | ≈ 5.6, 8 | ≈ 89, 96 | ≈ 120, 165 |
| 10 | 3.1 h | ≈ 7.5, 10 | ≈ 117, 130 | ≈ 161, 236 |
| 15 | 4.7 h | ≈ 7.5, 10 | ≈ 117, 126 | ≈ 161, 236 |
| 20 | 6.2 h | ≈ 7.5, 10 | ≈ 117, 127 | ≈ 161, 223 |
| 25 | 7.8 h | ≈ 7.5, 10 | ≈ 117, 127 | ≈ 161, 226 |

Both tables are produced by a unit test from the scheduler's own parameters (mean and p95 under
jitter, `tests/collector/test_budget.py`); the scheduler refuses a configuration that does not
fit under 75 % of every cap.
Latency for posts the feed shows: p50 ≈ 4 min, p95 ≈ 10 min outside breaks. Posts the feed misses
wait for the backstop (N = 15: up to ≈ 2.3 h).

Rhythm:

- Quiet hours ≈ 01:00–07:00 in the pinned timezone, both edges jittered daily by up to 45 min
  (later start and end at weekends); shoulder cadence in the first hour after and the last 1.5 h before them. 2 breaks a day of U(15, 40) min at random times (configurable,
  default on). Reads are grouped into sessions (a feed read plus 0–2 backstops 45–120 s apart,
  then idle), not a uniform stream.
- The day's catch-up happens at a random point in the first 30–90 min of the day.
- At least 45 s between any two navigations (one spacing rule, computed from the last persisted
  event, so it survives restarts); a group is never revisited within 30 min.
- After more than 2 h of downtime, overdue backstops are dropped and the schedule restarts from a
  jittered start — never a burst to "catch up" (D10: no backfill).
- Degradation when the budget is short: stretch backstops of groups with no posts in 7 days →
  skip gap backstops once < 20 % of the day's budget remains → tell users the effective delay per
  group ("up to X h").

**Gates** (all must pass before launching Chrome, before any navigation and before every scroll):

- *Home* — cheap checks before every action: default gateway MAC equals the pinned home router;
  default route not via `utun`/`tun`/`wg`/`ppp`; no system proxy configured (`scutil --proxy` /
  gsettings). The public-network verdict — IPv4 and IPv6 egress addresses (HTTPS endpoints, v4-only
  and v6-only, `trust_env=False`) mapped to ASNs, equal to the pinned ASNs — is valid for at most
  10 min and is invalidated by any change of gateway, interface or route and by detected sleep.
  Chrome runs with `--no-proxy-server`. `fbcollector pin-home` records router MAC, ASNs and the
  IANA timezone, once, at home. Failing → `NOT_HOME`: Chrome closed, no alert unless it lasts
  > 12 h in the daytime.
- *Sleep* — wall-clock vs monotonic divergence > 30 s at any step means the machine slept: abort
  the read, close Chrome, re-run every gate. An OS hook closes Chrome *before* a sleep when it
  can (owner decision of 2026-09-25): on macOS the kernel's power messages
  (`IORegisterForSystemPower`, delivered to every process, background user sessions included;
  DESIGN rev 2 named NSWorkspace's `willSleep`, see `sleephook.py` for why not), on Linux a logind `delay`
  inhibitor and `PrepareForSleep`. On the announcement the read is aborted (nothing a torn-down
  tab returns is judged), Chrome closed, and the sleep let go once Chrome has exited — not when
  its close begins; the sleep gate stays shut until the wake, and the pass after the wake
  re-verifies the network. The divergence stays the backstop;
  `on AC power` is a gate; the runbook says: stop the collector before taking the Mac out.
- *Timezone* — quiet hours use the pinned zone; the collector refuses to run if the OS zone differs
  (Chrome reports the OS zone).
- *Quiet/break*, *budget*, *block*, *warm-up*, *crash loop* (> 3 unplanned process starts or
  Chrome launches per hour or > 8 per day, or > 10 of either per hour or > 40 per day planned or
  not → `CRASH_LOOP`, no Facebook traffic, alert; planned: a start after a stop someone asked
  for, a relaunch after the collector closed Chrome itself), *profile lock*.

### Detectors (fail-closed) and the block ladder

A read step is **OK only if a positive signature holds**: logged-in markers (`c_user` cookie and a
non-zero `USER_ID`) AND the final URL matches the requested surface AND `<html lang="en">` /
`locale en_US` AND no unrecognised `[role=dialog]` AND (≥ 1 story captured OR a known empty
marker). Evaluated in this order, first match wins:

| State | Signals | Action |
|---|---|---|
| `BLOCKED` | the percent-decoded URL (path **and** query, including `next=`) matches `(^|/)checkpoint(/|$)`, `two_step_verification`, `auth_platform`, `/recover`, `/login/device-based`, `/zero/unsupported_browser/`, `captcha`; dialog/body text with "suspect automated behavio(u)r", "temporarily blocked", "going too fast", "confirm your identity", "can't use this feature", "suspended your account", "account has been (disabled|restricted|suspended)", "unusual activity", "ensure that no other users or tools have access", "security check"; a password field `input[name=pass]`; GraphQL error 1675004 in any response; HTTP 429 | block ladder; `checkpoint/advanced_protection` is flagged "no probes until the owner confirms the lock is lifted" |
| `LOGGED_OUT` | `/login` redirect or `USER_ID":"0"` **plus** one of: no `c_user` cookie, `viewer: null` in a query known to select viewer | block ladder (a forced logout is an enforcement outcome, not a nuisance) |
| `UNKNOWN` | the positive signature fails for any other reason: 5xx, navigation error, renderer crash, consent or "pay or consent" modal (`privacy/consent/`), unrecognised dialog, wrong language, empty shell | first: stop the read, close Chrome, back off 5 → 10 → 20 → 40 min; second in a row: block ladder |
| `FEED_NOT_CHRONO` | the feed preloader lacks `MOST_RECENT_GROUPS_FEED` / `orderby MOST_RECENT`, or `sk=h_chr` was lost | backstop-only mode for all groups with honest delays, alert once |
| `GROUP_LOST` | "Only members can see…", `viewer_join_state ≠ MEMBER`, 403/404 on the group | pause that group, alert; two within 24 h → `BLOCKED` |
| `NOT_FOLLOWED` | `subscribe_status ≠ IS_SUBSCRIBED` on the group page; or followed but absent from the feed for > 48 h while the backstop finds posts (possibly snoozed) | alert the owner to fix it by hand; group excluded from `feed_miss`; users see the backstop delay |
| `SORT_IGNORED` | group preloader `sortingSetting ≠ CHRONOLOGICAL` | flag, alert once |
| `PARSER_BROKEN` | see Parser | pause reads, alert, keep raw payloads |

The detector runs after every navigation and scroll step **and once more after the final dwell,
just before `park`** (late dialogs).

Ladder: 6 h → 24 h → 72 h → stop until re-plan. Each rung is a durable `blocks` row (`id`, `until`,
`reason`, redacted excerpt, screenshot path). Strikes expire (owner decision of 2026-09-25): after
30 consecutive days (`FBC_STRIKE_DECAY_DAYS`, never fewer) without a block since the latest one was
lifted, the next block starts again at the first rung (6 h). An open block never decays, and the
stop after the last rung still needs a local re-plan. Resume needs `until` passed **and** a `/resume`
carrying that block's id and a fresh nonce **and** a probe (the Groups feed, waiting ≥ 15 s for
late dialogs) that comes back OK. A failed probe starts the next rung. After resuming: cautious
profile for 7 days (14 after `LOGGED_OUT`).

### Group sync

Once a day at a random daytime minute, and on the owner's "🔄 Refresh groups" (at most 3 a day,
inside the budget), the collector reads `/groups/joins/`, field-walks every Group-shaped dict (id
+ name + `/groups/` url) in the SSR and in every GraphQL body, and scrolls while the list grows.
A sync is **complete** only when `has_next_page` is false and the count equals N in the "All
groups you've joined (N)" header (read in the isolated world). A group is marked `left` only after
two consecutive complete syncs without it **and** a group-page read showing `viewer_join_state ≠
MEMBER`; otherwise it is `unverified` and the owner is told. Privacy, membership and follow state
come from `profile_header_renderer` on backstop reads. The exact queries behind `/groups/joins/`
are a phase-0 inventory item.

### Uplink and control

- `POST /v1/items` — batches of ≤ 512 KiB serialised (`ensure_ascii=False`), per-item text cap
  16 KiB with a `truncated` flag. The brain returns a per-item result: `stored | duplicate |
  not_monitored | rejected(reason)`; the collector deletes every row with a result and keeps only
  those that got a 5xx or a transport error. The collector enqueues items only for groups in the
  last-known monitored set (persisted); others are marked seen and not sent.
- `POST /v1/groups` — the full joined-groups snapshot. `POST /v1/status` every 60 s — state,
  profile, budget, gates, counters, block, versions, `collector_id`. `POST /v1/events` — durable
  one-off events (block, group lost, parser broken, outbox ≥ 80 %) so a transition during an
  outage is not lost.
- `GET /v1/control` every 60 s — monitored ids with tier and listings flag, the lease, one-shot
  commands. Commands are edges, not state: `resume {block_id, nonce}` is applied only to the
  currently open block and only once (used nonces stored); `refresh_groups {nonce}` likewise. The
  brain can only make pacing *more* cautious than the collector's own rules
  (`effective = min(local, remote)`). Facebook actions triggered by a command start after
  U(30, 300) s, so reads never line up with the control poll.
- Auth: `Authorization: Bearer <token>` (constant-time compare), body ≤ 1 MiB, JSON only, client
  timeouts ≤ 10 s (the local tunnel listener accepts even when the remote side cannot).
- When the brain is unreachable the collector keeps collecting; the outbox is capped at 5000
  (oldest dropped with a counter) and ≥ 80 % triggers a direct alert.
- **Lease**: the brain grants one active `collector_id` per listener account; any other collector
  is told to stop. Once the uplink exists, no lease → no reads.
- **Direct alerts** (D19): critical states and events go straight to the owner through the alert
  bot as well, because the brain may be the thing that is down. In phase 0 (no brain) this is
  the only alert path.

### Phase-0 local mode

Without the brain: monitored groups come from `monitored.toml` (default: every group of the
latest complete sync, up to 25), whose entries may carry the owner's membership confirmation
(`confirmed_member = true`, see Surfaces; phase 0 starts with four such groups,
`docs/monitored.example.toml`); items go to a local JSONL sink; `fbcollector status | pause |
resume <block_id> | groups confirm <id|url>…` need a local TTY; alerts go through the alert
bot.

## Brain (ddiver-fb)

ddiver-core composition with a `FacebookSource` instead of the Telegram listener, and its own
≈ 200-line composition root (ddiver's `Application` stays in ddiver).

- **Ingest API**: a small hand-rolled HTTP server like ddiver's `MetricsServer` (bounded reads);
  in the container it binds `0.0.0.0:8781` and compose publishes `127.0.0.1:8781:8781` only. This
  repo's packaging test asserts it.
- **Groups**: `chats` rows with `kind = 'group'`, `id` = the numeric Facebook group id (positive,
  int64), `title`; the slug is **not** stored in `chats.username` (that would build `t.me` links)
  but in `fb_groups(chat_id, slug, url, privacy, state [member|unverified|left|lost], followed,
  last_synced_at, listings, tier, sort_ignored)` on the `fb` migration track. `state` maps to
  `chats.available` in the same transaction, so `monitored_chat_ids()` never lists a group the
  account cannot read; a group reported `LOST` is not re-listed until a sync shows it as member.
- **Monitored set** = available groups with ≥ 1 active subscription → `/v1/control`.
- **Item → notification**: dedupe on `fb_items(group_id, post_id, received_at)` (48 h); for each
  subscriber: `subscribed_since` (exact `created_at`), ignore list, matcher (`evaluate(group_id,
  text, author_id, display_name=author name)`: `from:` rules see the author's id or name, never
  a username, and an anonymous participant matches none), `claim_delivery` (outbox PK
  `(user_id, chat_id, post_id)`), delivery with
  `senders=None` (no Telegram name lookup). An item is acknowledged only when every claim
  succeeded; a busy database leaves it unacknowledged for the collector to resend.
- **Rendering**: `Payload(source="fb", url=…, chat_url=…, sender_url=…, chat_private=…, late=…)`,
  honoured by the notification and the digest. Private groups: "🔒 private group — the link opens
  for members only"; late posts: "posted 30 h ago, appeared now".
- **Strings**: an `fb` i18n overlay (uk/ru/en) replaces Telegram wording and adds Facebook keys;
  its own catalog-sync test. `/export` uses the format tag `ddiver-fb.config` and refuses
  `ddiver.config`.
- **Collector state** in `/status` and owner alerts: `OK`, `WARMUP`, `QUIET`, `BREAK`,
  `NOT_HOME`, `BLOCKED(until, reason)`, `LOGGED_OUT`, `UNKNOWN`, `CRASH_LOOP`, `PARSER_BROKEN`,
  `FEED_NOT_CHRONO`, `OFFLINE` (no status for 20 min in the daytime, suppressed when the last
  status said `NOT_HOME`/`QUIET`/`BREAK`), per group `LOST` / `NOT_FOLLOWED` / `SORT_IGNORED`,
  the effective delay, and a per-group silence alert (last item older than 3× its p90 inter-post
  gap in the daytime, both counted in daytime hours, and no backstop of the group having read its
  page through since). Owner commands: `/resume` (binds to the open block), `/pause`, "🔄 Refresh
  groups".
- **The AI rule** (`ai.py`, `judge.py`; owner only, `/ai`): a wish in plain words per group. Each
  post the ingest stores for a group with a wish is queued before the post is acknowledged, and a
  worker asks a model through OpenRouter (strict JSON schema, temperature 0) how likely, in percent,
  the post fits and why. Failed calls back off and are retried for six hours; a daily cap bounds the
  cost; three failures in a row tell the owner once. In shadow it records verdicts and sends
  nothing, and `/ai` shows what would have been sent. The model is DeepSeek V4.1 Flash, chosen from
  seven on 206 real posts labelled by hand (about $0.24 per thousand posts). Texts and reasons are
  pruned after seven days and never logged above DEBUG (invariant 8).
- **Health**: the container is healthy when the bot is connected and the ingest server is up.
  Collector problems are reported, never "fixed" by restarting the brain.
- Telegram-only settings (count bot messages, anonymous admins) are absent from the menus.

## Wire format (`fbwire`, v1)

```json
{"v": 1, "collector_id": "home-1", "seq": 1843,
 "group_id": "1234567890", "post_id": "9876543210987654",
 "url": "https://www.facebook.com/groups/1234567890/posts/9876543210987654/",
 "group_name": "…", "group_url": "https://www.facebook.com/groups/1234567890/",
 "group_privacy": "private",
 "author": {"id": "100000000000001", "name": "…", "url": "https://www.facebook.com/…", "anonymous": false},
 "created_at": 1790000000, "first_seen_at": 1790000240, "via": "feed", "late": false,
 "text": "…", "truncated": false,
 "listing": {"id": "…", "title": "…", "price": "…", "location": "…"},
 "is_share": false}
```

Ids are digit strings on the wire and integers in the brain. Unknown fields are ignored; `v`
bumps only on incompatible changes, and the brain accepts `v` and `v − 1`.

## Storage

- **Collector**: one root, 0700 — `~/Library/Application Support/fbcollector` (macOS) or
  `~/.local/share/fbcollector` (Linux) — holding `profile/`, `state.sqlite` (WAL), `raw/`,
  `shots/`, `secrets` (0600, or the macOS Keychain). Excluded from Time Machine
  (`tmutil addexclusion`, asserted at start), Spotlight (`.metadata_never_index`) and Linux backup
  tools (`CACHEDIR.TAG`). Disk encryption required (FileVault is on on the Mac; on the laptop
  LUKS with TPM2 auto-unlock, or no unattended reboots). Tables: `seen`, `sightings`, `reads`
  (surface, start/end, coverage newest/oldest, target reached, steps, friendly names, preloader
  variables, page_info, per-step response latency, outcome), `deferred`, `outbox`,
  `budget_events`, `process_events`, `blocks`, `groups` (id, slug, name, privacy, join state,
  follow state, monitored, tier, listings, baseline_at, flags), `kv` (home pin, timezone,
  login_at, command lines, versions, used nonces).
- **Brain** (`/data/fb.sqlite`): ddiver-core's schema + the `fb` track (`fb_groups`, `fb_items`,
  `collector_state`, `collector_events`).

## Phase 0 — measure before trusting

Step 0, before any Facebook load: login → close → collector launch → **cookie gate** (`c_user`
and `xs` present in the profile) passes, command lines match, local self-test passes. Then 24–72 h
of warm-up by hand. Then the collector runs on the Mac in **observe mode**: full reading
behaviour, cautious profile, no brain, no notifications — only the owner's alerts. Phase 0 ends
on data, not dates: at least **48 at-home, awake daytime hours** and at least **300 posts**
(≥ 30 in every group a decision is made for). Output: `docs/phase0-report.md`.

| Question | Measure | Decision rule |
|---|---|---|
| Does the Groups feed miss posts? | `feed_miss` as defined above, per group, listings separately | ≤ 5 %: the 5–10 min target holds. > 5 %: find the cause (not followed, snoozed, listings only, private sort ignored) → faster backstop for ≤ 2–3 groups within the budget, honest per-group delays |
| Is the Groups feed's JSON what we think? | ≥ 95 % field fill (post_id, created_at, text, group, url) | below: fix the parser before building on it |
| Do we read the posts as written, not a Facebook translation? | over the recent distinct posts' authors' words (message and shared texts), and their final text beside it: share Cyrillic only / Latin only / mixed / neither; stories with a key naming a translation (counts only) | Latin-only words in groups that write Cyrillic, or translation keys: find where the original sits before trusting matches (design notes, "Owner decisions of 2026-09-30") |
| Is the text complete without "See more"? | JSON text length vs the DOM's truncated text, trailing ellipses | truncated: owner decides between one allowed "See more" click on matched posts only (a documented exception to invariant 1) and a "text truncated" marker |
| `CHRONOLOGICAL` honoured in private groups? | preloader `sortingSetting`, header unit, order | not honoured: that group relies on the feed |
| Which field names the group of a feed story? | all candidates recorded | pick the authoritative path |
| Listings in buy/sell groups | post ids in feed vs `CHRONOLOGICAL` vs `CHRONOLOGICAL_LISTINGS`, 48 h | enable `CHRONOLOGICAL_LISTINGS` per group only where needed |
| Late approvals | feed cursor `feed_ordering`, `late` rate | tune the 7-day window |
| Volume | posts per group per day, steps per read, stories per pagination response | set tiers, confirm the budget table |
| Pagination in the collector user's background session (screen locked, display asleep, another user in front) | `pagination_fired` rate by display state | if it stalls: `caffeinate -d`, or bring the collector's session to the front |
| Latency | `first_seen_at − created_at`, feed vs backstop | report p50/p95 |
| Account health | any warning, logout, checkpoint | if any: stop, analyse, re-plan |
| Resources | Chrome RSS / CPU | < 1.5 GB; repeat on the laptop |

Fixtures from phase 0 are built by **allow-list**: the builder emits only the JSON paths the
parser reads (ids, times, texts, actors, permalinks, group ids, listing fields, framing, labels,
page_info) with names, ids and text replaced consistently; every other key and script block is
dropped. A CI secret scan over `tests/fixtures` fails on `DTSGInitialData`, `"LSD"`, `fb_dtsg`, a
non-zero `USER_ID`, `c_user`, `xs=`, `oh=`, `encrypted_tracking` and the listener's real id and
name (from a local, uncommitted file).

## Safety envelope (summary)

Home IP and a native manual login are necessary, not sufficient. What the design controls is
volume and rhythm — a planned ≤ 15 loads/h and ≈ 226/day with hard caps above that, jitter
everywhere, daily-jittered quiet hours, breaks, sessions, one tab, no writes, no own requests —
device continuity (one launcher, one profile, one host, one lease, no copied cookies, warm-up and
a cautious ramp after every login), location continuity (home gates, Chrome closed when they
fail), and an immediate, durable, fail-closed stop at the first signal. Membership hygiene: the
listener account stays a member of the target groups only; joins are done by the owner by hand in
the collector profile, ≤ 3 per day, with hand-written answers to admin questions; no joins on a
day with any warning.

Known unknowns: nobody has published a safe ceiling; the Groups feed's completeness is claimed
incomplete by one vendor (~15 %, cause unknown); whether a second listener account may share the
home IP is unknown and is not tested. Terms of Service: automated collection while logged in
breaches Meta's Terms §3.2 regardless of volume, and a secondary account breaches §3.1; the
realistic worst case is the listener account being restricted or disabled.

## Operations

- **VPS**: `/srv/ddiver-fb`, compose project `ddiver-fb`, its own `.env` (`BOT_TOKEN`,
  `API_ID`/`API_HASH`, `OWNER_ID`, `INGEST_TOKEN`), memory limit inside ddiver's host budget,
  deploy-on-push with the same sha-pinned, roll-back-on-unhealthy script as ddiver.
- **Tunnel**: user `fbtunnel`; `/etc/ssh/sshd_config.d/20-fbtunnel.conf` with `Match User
  fbtunnel` → `AllowTcpForwarding local`, `PermitOpen 127.0.0.1:8781`, `PermitListen none`,
  `AllowStreamLocalForwarding no`, `X11Forwarding no`, `AllowAgentForwarding no`, `PermitTTY no`,
  `ForceCommand /bin/false`, `AuthorizedKeysFile /etc/ssh/fbtunnel_keys` (root-owned, each key
  `restrict,port-forwarding,permitopen="127.0.0.1:8781"`), `MaxSessions 0`, `ClientAliveInterval
  30`, `ClientAliveCountMax 4`; `deploy/vps/setup-fbtunnel.sh` installs and checks it. Client: a
  dedicated `-F` config with `ExitOnForwardFailure yes`, `StrictHostKeyChecking yes`, a pinned
  ed25519 host key in its own `UserKnownHostsFile` (`GlobalKnownHostsFile /dev/null`,
  `HostKeyAlgorithms ssh-ed25519`, `UpdateHostKeys no`, `CheckHostIP no`), `BatchMode yes`,
  `IdentitiesOnly yes`, `IdentityAgent none`, `ConnectTimeout 20`, `ServerAliveInterval 15`,
  `ServerAliveCountMax 3`. Its local end is a Unix socket in the collector user's own 0700
  `~/.ssh` (`LocalForward %d/.ssh/fbtunnel.sock 127.0.0.1:8781`, `StreamLocalBindMask 0177`,
  `StreamLocalBindUnlink yes`), which the collector reaches with `FBC_INGEST_SOCKET`: never a
  loopback TCP port, which any local account could bind while ssh is down and so be handed the
  bearer token. macOS: `/usr/bin/ssh -N` under the LaunchAgent `com.ddiver.fbtunnel`
  of the collector's user (Aqua, `KeepAlive`, `ThrottleInterval 200`); its key is made by the
  setup script and the VPS's host key pinned from the owner's own trusted ssh session, both
  through the operator's door. Linux: `autossh.service`, `AUTOSSH_GATETIME=0`. Restarts
  throttled to one per 200 s: at most 4 in 10 minutes, both ends counted, under fail2ban's default sshd jail (5 in
  10 minutes).
- **Collector host**: installed from a checkout with `uv sync --frozen --package fbcollector
  --no-dev` (Patchright pinned exactly), run through the venv's entry point. macOS: a LaunchAgent
  with `LimitLoadToSessionType=Aqua`, `ProcessType=Interactive`, `KeepAlive`, `ThrottleInterval
  ≥ 60`, under `caffeinate -i -s`; `NSAppSleepDisabled` for Chrome. **Phase 0 on the Mac runs
  under a dedicated Standard user** (`fbc`, owner decision of 2026-09-25): the LaunchAgent is
  that user's, loaded in its Aqua session, which keeps running in the background while the
  owner has the screen (fast user switching), so the collector's Chrome never shows on the
  owner's desktop nor takes the focus. The owner's account builds a git bundle of `main`
  (`scripts/make_bundle.sh`, to `/Users/Shared/ddiver-fb`, group `staff` only; refused if any
  commit of `main` ever held a secret-shaped file or secret-shaped content, since the bundle
  carries the history);
  inside `fbc`, `deploy/macos/setup-fbc-user.sh` checks its checksum, installs uv and the
  collector alone, the root, the secrets (asked without echo), the start groups and the
  LaunchAgent, and `--update` repeats it for a newer bundle; it also makes the tunnel's key and,
  once the host key is pinned, the tunnel's LaunchAgent, and `--uplink` / `--local` switch the
  collector between the brain and local mode with the token on stdin. The session must be
  logged in once after every reboot. Chrome Beta is not used. Linux: `fbcollector.service`
  `PartOf=/After=/WantedBy=graphical-session.target`, `Restart=on-failure`, `RestartSec=30`,
  `StartLimitIntervalSec=0`, the session exporting `DISPLAY`/`XAUTHORITY` to systemd; one real
  graphical session (Xorg, or Wayland if phase 0 on the laptop shows it works) used for both login
  and reading; `autossh.service` under lingering; sleep disabled, power-on after AC loss, NTP.
- **Migration to the laptop**: unload the Mac agent and delete the Mac profile *before* the laptop
  login; after warm-up, end the Mac session by hand under "Where you're logged in"; the brain's
  lease makes a forgotten Mac collector harmless.
- **Metrics**: collector `fbc_navigations_total{surface}`, `fbc_scroll_steps_total`,
  `fbc_budget_remaining{window}`, `fbc_gate_closed{gate}`, `fbc_reads_total{surface,outcome}`,
  `fbc_stories_total{surface}`, `fbc_dropped_total{reason}`, `fbc_outbox_depth`,
  `fbc_outbox_dropped_total`, `fbc_state` (sent in `/v1/status`); brain
  `fbb_ingest_items_total{result}`, `fbb_collector_status_age_seconds`,
  `fbb_group_last_item_age_seconds{group}` on its metrics server; the brain's own `PING_URL`.

## Testing

- **Parser**: fixture-driven (phase-0 captures, allow-list anonymised; synthetic until then, built
  from the research captures' shapes), plus hypothesis story trees (typename renames, key order
  with `attached_story` first, missing fields, nested shares, NDJSON framing, anchors, highlights).
- **Scheduler/budget/gates**: the real SQLite store, a virtual clock, and injected crashes between
  every pair of loop steps across restarts; properties: no cap is ever exceeded, nothing happens
  while any gate is closed, overdue work never bursts, DST days in the pinned zone, a random
  unrecognised page never yields another navigation, a stale resume never clears a new block, a
  remote "normal" never beats a local "cautious". The budget table above is generated by a test.
- **Detectors**: saved pages per state (checkpoint incl. `login.php?next=…checkpoint…`, login,
  temporary block, members-only, unsupported browser, consent, unknown dialog, non-English).
- **Driver**: a fake driver for everything above; the Patchright driver against a local test page
  (fingerprint, capture of streamed bodies, in-flight tracking, wheel coordinates/deltas,
  isolated reads, session-restore suppression, cookie survival across a relaunch, CDP method
  allow-list) under the `browser` marker; the invariant-1 tests. The browser tests run the
  installed Chrome **headless by default** (through a test-only wrapper of the driver's
  Playwright entry point), so a test run never opens a window on the owner's screen; the
  headed-fingerprint checks (user agent, `--headless` switch, window metrics) and the full
  self-test run only with `FBC_BROWSER_HEADED=1`, inside the collector's macOS user. The
  production launch cannot be headless: `LaunchSpec` has no such field, the driver passes
  `headless=False` as a literal, and a guard test holds both (owner decision of 2026-09-25).
- **Uplink/brain**: per-item results, idempotency, byte batching, auth, busy-database mid-batch,
  version skew, lease, one-shot commands, state transitions, rendering (notification and digest),
  overlays, export tag.
- **Packaging**: brain compose publishes nothing beyond 127.0.0.1; sshd snippet and client config
  contents; launchd plist and systemd units lint.
- **Live E2E** (`scripts/e2e_live.py`, marker `e2e`, never in CI) goes through `fbcollector`
  itself — same profile, lock, gates and budget — with the owner present.
- ruff (ddiver's rule set), mypy strict, pytest with branch coverage ≥ 85 %, CI on every push.

## Work plan

1. This document, reviewed. ✅ (rev 2)
2. Phase-0 collector: driver, parser, scheduler, gates, detectors, store, local mode, alert bot,
   report; offline tests and local-browser tests.
3. ddiver-core extraction on a branch in a separate worktree (replayable script + additive
   commits), all ddiver tests green; nothing pushed without the owner.
4. ddiver-fb brain on ddiver-core (path source until the tag; pinned to `core-v0.1.0` since). ✅
5. Uplink, control, lease, units, tunnel config; phase-0 decisions applied; fixtures wired in.
6. Owner: pre-login checklist → login → warm-up → phase 0 → report.
7. Live E2E, independent review rounds, README, deploy with the owner.
8. Collector moves to the Linux laptop with a fresh manual login.

## Open (needs the owner)

- The alert bot (D19): a second BotFather bot, owner-only.
- Breaks during the day (default 2 × 15–40 min) trade a little latency for a less regular rhythm.
- ~~When to merge the ddiver-core extraction relative to ddiver's own ongoing work, then tag
  it.~~ Merged and tagged `core-v0.1.0`; this repo is pinned to it.
- VPS changes (the `fbtunnel` user and sshd snippet, `/srv/ddiver-fb`) and a GitHub repo for this
  project with its deploy key.

## Borrowed ideas (attribution)

Code is written here; ideas and, where the license allows, small adapted pieces come from:
nvn01/Recon.id (MIT) — Relay JSON walk, `for (;;);`/NDJSON framing, passive friendly-name
tagging, permalink canonicalisation, commerce records; carousell/sellee PR #79 (Apache-2.0) —
block wall and login detection, durable 6/24/72 h ladder, resume only after a clean probe, ±30 %
jitter; viseshrp/fbn (MIT) — fail-closed page-state classifier; Steby5/stanovanje (MIT) — "0 posts
and 0 errors is a failure"; adbertram/cli-tools (MIT) — logout by `USER_ID "0"`,
`profile_header_renderer` membership/privacy/follow state, `/groups/joins/` listing;
MEOMcGill/fbscrape (MIT snapshot 7fca6ca only) — story paths, timestamp typenames, error 1675004,
members-only marker, the out-of-order anchor story, pagination timing; oricho123/rentifier (MIT) —
account state machine and a well-documented failure; tamnd/facebook-cli (Apache-2.0) — SSR
`data-sjs` stitching; Patchright (Apache-2.0). Unlicensed projects (Groups Watcher extension,
affitti-userbot, fb-group-watcher, sfroomhunter, OooPeople, bgu-housing-bot) — ideas only.
