# fbcollector runbook

Short and practical. The why is in [`DESIGN.md`](../DESIGN.md). Commands are written as `fbcollector …`: in the
collector's own account (section 12) it is on `PATH`; elsewhere run it from the checkout as
`.venv/bin/fbcollector …` after the install of section 0, or as
`uv run --frozen --package fbcollector --no-dev fbcollector …`. A plain `uv run` is for the
development checkout only: it syncs the whole workspace with its dev tools — the brain,
ddiver-core from the private ddiver repository, Telethon, pytest — into the checkout's `.venv`,
which in the collector's account is the one its LaunchAgent runs (and which that account,
without a GitHub credential, cannot build).

## 0. Install (once per machine)

**On the Mac, phase 0 runs under a dedicated macOS user: section 12 does this section's install,
the secrets, the monitored groups and the LaunchAgent inside that account.** What follows is
the manual install (the Linux laptop, or a development checkout).

```sh
uv sync --frozen --package fbcollector --no-dev
mkdir -p "$HOME/Library/Application Support/fbcollector"     # macOS; Linux: ~/.local/share/fbcollector
```

Settings are `FBC_*` variables. Secrets go in `<root>/secrets` (dotenv format, `chmod 600`):

```
FBC_TIMEZONE=Europe/Sofia
FBC_ALERT_BOT_TOKEN=123456789:AA…      # the owner-only alert bot, not the user-facing bot
FBC_ALERT_OWNER_ID=…
```

`fbcollector check-config` must say `ok`. On the Mac also run once:
`defaults write com.google.Chrome NSAppSleepDisabled -bool YES` (App Nap must not stall Chrome).

Phase 0 needs nothing else. Once the brain runs, section 9 adds the uplink settings; with them
`run` refuses to start without `FBC_COLLECTOR_ID`.

## 1. Pin home (once, at home)

On the home network, no VPN, no proxy: `fbcollector pin-home`. It records the router's MAC,
the IPv4/IPv6 ASNs and the timezone. Nothing touches Facebook while the machine is anywhere
else (`NOT_HOME`).

## 2. Pre-login checklist

- The account's UI language is already **English** (switched days ago on a trusted device).
- A passkey or recovery method exists in Accounts Center.
- A trusted device is at hand to approve the login.
- No lock wave or recent warning on the account.
- The machine is at home, on AC power.
- From now on every human action on this account (checkpoints, joins, follows, 2FA) happens
  only inside `fbcollector login` — never on your phone or main browser.

## 3. Login

`fbcollector login` stops the service, opens the collector's own Chrome on `about:blank` and
does nothing else. Type `facebook.com`, log in, answer 2FA, **quit Chrome** (Cmd+Q). It records
Chrome's command line and — for a fresh session — `login_at`, then restarts the service.

Use as many tabs as the login takes. Facebook may open a second tab or a popup (2FA, "confirm
it's you"), and you may open tabs yourself; this session closes none of them, and the log says
`a tab opened in the login session; it stays open`. Only the collector's reads keep to one tab
and close any other. The login is read from the profile's cookies, whichever tab it happened
in. They are checked every 10 s while Chrome runs, so leave the feed open for at least ten
seconds before you quit. If the command then says "No Facebook session found" although you are
logged in, run `fbcollector login --record-login` and quit again.

It refuses to open Chrome at all (and says why) when home was never pinned, away from the
pinned network (another router, a VPN route, a proxy, another ASN), in another timezone or on
battery. So does `fbcollector selftest`, which starts Chrome on the same profile. It also
refuses during a block or a back-off, while paused (by you or the brain), without the brain's
lease, and above the crash-loop cap: more than 3 unplanned Chrome launches in the last hour or
8 in the last day (a relaunch after a crash, a failed read or a failed launch), or more than 10
an hour or 40 a day of any kind, counting its own. Its own launch is a planned one. It does run before the login, during the warm-up, in quiet hours and under a
`SELFTEST` halt.

After an upgrade that changed Chrome's command line (`COMMAND_LINE` alert) run `login` again:
it re-records the launch without touching `login_at`.

## 4. Warm-up (24–72 h)

No automated reads (`WARMUP`). Browse a few minutes by hand, a few times, in
`fbcollector login` sessions. The 14-day cautious pacing starts at `login_at`.

## 5. Phase 0

Start (Mac): in the collector's own account (section 12, which installed the LaunchAgent):

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ddiver.fbcollector.plist
```

Observe mode is the phase-0 default: set `FBC_OBSERVE_MODE=true` in the secrets file (or run
`fbcollector run --observe` by hand). Monitored groups: `<root>/monitored.toml`
(`FBC_MONITORED_PATH` overrides the place), or every member group up to 25 when the file is
absent:

```toml
[[group]]
id = "1234567890"
listings = false
confirmed_member = true     # you checked by hand that the account is a member
```

**Phase 0 starts from four groups confirmed by hand**, because the joined-groups sync cannot be
complete until its JSON is known, and without a complete sync or your word no group is read.
Copy them once (the setup script of section 12 does it when no file exists):

```sh
cp docs/monitored.example.toml "$HOME/Library/Application Support/fbcollector/monitored.toml"
chmod 600 "$HOME/Library/Application Support/fbcollector/monitored.toml"
```

To vouch for another group (after checking in `fbcollector login` that the account is a
member), from a terminal: `fbcollector groups confirm <id or group URL> …`. It adds the group
or dates its confirmation (`confirmed_at`); the running collector picks the file up by itself.
Each confirmed group's own page is checked at its first backstop: a page that does not say
`MEMBER` pauses the group, alerts (`GROUP_LOST`) and withdraws the confirmation, which then stays
spent until you run `groups confirm` again. (A page that shows nothing at all — no posts — is an
unrecognised page instead: a back-off, and the confirmation stays.) The first complete sync overrides every older
confirmation (a group it does not list is not read). `fbcollector status` lists the
confirmations and whether each page has verified them.

Watch: `fbcollector status`. Items land in `<root>/items.jsonl`. Report:
`fbcollector report --out docs/phase0-report.md` (ends at ≥ 48 at-home daytime hours and
≥ 300 posts). Its last section, the parser diagnostics, shows structure only: where each
story's text came from, why the stories without a message of their own have the text they have
(the classes the empty-text alert uses), and the shapes of the newest saved stories without
text or with only a link's or listing's text, each shape's attachments drawn apart (key names,
typenames and string lengths, never text, ids, names or links). It ends with the script of the
saved posts' texts (how many are Cyrillic only, Latin only, mixed or neither, counts and shares
only) and how many stories carry a key naming a translation: Latin-only texts from groups that
write Cyrillic would mean Facebook sent a translation. The attachments drawing is
where a live run confirms the paths the parser reads a card's words from (the fixture's are
synthetic until then). The other sections print group ids; `fbcollector report
--diagnostics-only` prints the parser diagnostics alone.

Before any fixture is built from a phase-0 capture (`tools/make_fixture.py`), create
`tests/fixtures/.forbidden` in the checkout — git-ignored, never committed — with the listener
account's numeric id(s) and its display name, one per line (`#` starts a comment). The secret
scan checks every fixture against it; without the file that check is skipped with a warning
(`pytest -rs` shows it), and `FBC_NO_FORBIDDEN=1` marks a run that goes without it on purpose.

Stop (in the collector's account): `launchctl bootout gui/$(id -u)/com.ddiver.fbcollector` —
**always before taking the Mac out of the house**.

## 6. What an alert means

| Alert | Meaning | Do |
|---|---|---|
| ⛔ BLOCKED (block #N) | A checkpoint, a wall, a rate limit, a logout or two unrecognised pages. Chrome is closed. | Look at the screenshot in `<root>/shots/`; for `signature:dialogs_clear`, `fbcollector dialogs` (the operator's door: `dialogs [N]`) prints the texts of the dialogs on the last saved steps, each with what the detectors make of it (Facebook's notifications panel passes unless an unread item in it says the account is in trouble, reason `dialog:panel_account_notice`: read and deal with such a notice in Facebook first; unread login and password notices pass and come as 🔔 FB NOTICE). Deal with the cause first: a resume whose probe meets it again climbs the ladder. After the shown time: `fbcollector resume N`. |
| advanced_protection | Facebook locked the account. | Unlock it by hand in `fbcollector login`, then `fbcollector resume N --lock-lifted`. |
| last rung (no time) | Four warnings, none of them after 30 quiet days (strikes expire after 30 days without a block: the next one starts again at 6 h). The plan itself is wrong. | Stop, analyse, re-plan; `resume N --replanned` only after that. |
| ✅ block lifted | The resume probe was clean. | Nothing: cautious pacing for 7 days (14 after a logout). |
| NEEDS_LOGIN | The profile has no Facebook session. | Pre-login checklist, then `fbcollector login`. |
| COMMAND_LINE | Chrome started with other switches than at the login. | Check the upgrade; `fbcollector login` to re-record. |
| SELFTEST | A new Chrome major failed the local self-test, or Chrome's version could not be read. Nothing touched Facebook. | Investigate (`fbcollector selftest`), then `fbcollector pause --off`. |
| PARSER_BROKEN | Most stories stopped parsing (a Facebook change). Reads paused. | Fix the parser, `fbcollector replay --since 2d`, `fbcollector pause --off`. |
| ⚠ PARSER: N of M distinct posts with empty text nothing explains (or N of M distinct posts without their author's words, or N of M distinct stories without a creation time) | Over the last three reads, each post counted once, more than a fifth of at least 10 posts came out without any text and without a reason the parser knows (a share without words, a photo); or, when that does not fire, over the last ten reads (once there are ten) more than half of at least 10 posts came out without words of their own, whatever the reason (a renamed message section leaves photo and link posts looking explained); or more than a fifth of the stories without a time. Reads go on. At most one alert of each kind a day. | `fbcollector report --diagnostics-only` (the operator's door: `diagnostics`) shows the text paths used, and the shapes of the stories without text, with no ids. The full `report`, like `status` and `log`, shows group ids. Fix the parser, then `fbcollector replay --since 2d`. |
| 🔔 FB NOTICE: unread on the account, 6d ago: «…» | Facebook's notifications panel, open on a page the reading passed, holds an unread notice about a login, a password, or the account's email or phone (a new login, a changed password, a login code, a changed email): a heads-up, not a sign that Facebook acts against the account. Reads go on. Each notice is told once: an old one does not come back, day after day or after a restart. It counts as told only once Telegram took the alert; one that did not get through is told again at the next look. A notice is its words and when it came (the time less its age), so another one in the same words (a new login, Facebook's text is generic) is told on its own. Not always: Facebook's ages are coarse, and a second login first seen a week or more old, within about a week of a told one, may be taken for it, unless the panel has shown the told one read meanwhile. | Nothing, if it was yours. If the login or the change was not yours, `fbcollector pause` and secure the account by hand first. A notice that says the account is in trouble (a restriction, a check, a password Facebook reset) is not this alert: it stops the reading (⛔ BLOCKED, `dialog:panel_account_notice`). |
| ITEMS_INVALID | Posts were read whose item does not fit the wire model (an id, a link, a time or a group name the model refuses). They are not marked seen: each is tried again when a read sees it again. `fbcollector status` lists them (ids and the fields that failed, never text). Once while any wait. | Fix the parser or the model, then `fbcollector replay --since 2d` (it also retries them from older kept payloads). |
| FEED_NOT_CHRONO | The Groups feed is not the newest-first one. Backstop-only mode. | Check by hand; `fbcollector pause --off --feed-check` to re-test the feed. |
| GROUP_LOST | A group page says the account cannot read it (or, for a group you confirmed, does not say `MEMBER`). The group is paused; a confirmation is withdrawn. | Check membership by hand in `fbcollector login`; if it is a member, `fbcollector groups confirm <id>`. |
| NOT_FOLLOWED | The account does not follow a group, or the feed has not shown it for 48 h while backstops find its posts (snoozed?). | Follow or unsnooze it by hand in `fbcollector login`. |
| SORT_IGNORED | A group ignored CHRONOLOGICAL. | Nothing: it relies on the feed. |
| UNVERIFIED / LEFT | A group vanished from the joined-groups list. | Check by hand; it is not read until a sync lists it again. |
| SYNC (incomplete) | The joined-groups list did not match its header; groups it showed for the first time wait, unread, for a complete sync. | Nothing if it passes. If it persists in phase 0, the joins connection needs naming (see `stats.joins_connections` of the sync reads). |
| NOT_HOME (12 h) | Away from the pinned network in daytime. | Expected when travelling. |
| CRASH_LOOP | Too many unplanned restarts or Chrome launches (crashes, failed reads, failed launches; restarts you ask for and relaunches after quiet hours, breaks and sleeps do not count), or too many of any kind. No Facebook traffic. | Read `~/Library/Logs/fbcollector.log`. |
| OUTBOX_HIGH | Items are piling up (sink or brain unreachable). | Check the disk / the tunnel. |
| ⚠️ UPLINK_DOWN: the brain has not been usable for N min (…); M post(s) wait in the outbox | For 5 minutes no cycle of the uplink got through: no answer (`transport: …`: the tunnel or the brain is down), a refused token (`the token was refused`), a wrong URL, or a busy brain. Told once per outage, at most once in 6 h. Reading goes on while the lease is valid (6 h without an answer); posts and events wait on disk. | `ssh fbc-local tunnel` (section 9): the agent, the socket, the log and a probe (401: the brain answers; 000: no answer). `ssh fbc-local uplink` shows the last cycle's reason. A refused token: `uplink-token` again with the brain's `INGEST_TOKEN`. The brain itself: `/status`, `docker compose logs brain` on the VPS. |
| ✅ UPLINK: the brain answers again after N min | The outage you were told of is over. What waited is being sent. | Nothing. |

## 7. Pause and resume

From a local terminal only (TTY): `fbcollector pause`, `fbcollector pause --off`,
`fbcollector resume <block_id>` (and `fbcollector groups confirm`, section 5). A pause acts at
once on a running collector: a read under way stops before its next scroll or final look and
Chrome is closed (so does the brain's pause, or the lease moving away). A resume is accepted
only for the open block, after its time; a probe of the Groups feed follows 30–300 s later and
must come back clean.

## 8. Migration to the Linux laptop

1. Stop and unload the Mac agent; **delete the Mac profile** (`<root>/profile`) before the
   laptop's login. Never copy the profile.
2. Laptop: Chrome `apt-mark hold`; install `deploy/linux/fbcollector.service` (and
   `autossh.service` + `deploy/ssh/fbtunnel_client.config` once the brain exists: the laptop's
   key goes to the VPS like the Mac's in section 9, with the comment `fbtunnel-laptop-1`; watch
   the VPS's auth log for autossh's own retry pace against fail2ban the first time), sleep off,
   power-on after AC loss, NTP. Should it sleep anyway, the collector holds a logind delay lock
   and closes Chrome first, but logind waits for that at most `InhibitDelayMaxSec` (5 s by
   default); `InhibitDelayMaxSec=30` in a `/etc/systemd/logind.conf.d/` file gives it room.
3. `fbcollector pin-home`, pre-login checklist, `fbcollector login` in the real graphical
   session, warm-up.
4. Give the laptop **its own collector id** (`FBC_COLLECTOR_ID=laptop-1`, never the Mac's), with
   the uplink settings of section 9. When it first checks in, the brain tells you that
   `laptop-1` asked to read while the Mac holds the lease: send `/lease laptop-1`. The Mac is
   told to stop at its next check-in and the laptop reads from its own after that; if the Mac
   is already off for good, `/lease laptop-1 now`. (A second machine under the Mac's own id is
   refused as well — the lease is bound to each machine's state database — and
   `/lease <that id>` moves it; but separate names keep `/status` readable.)
5. After warm-up, end the Mac session by hand in Facebook's "Where you're logged in".
6. Chrome upgrades on Linux happen only in quiet hours (`fbcollector upgrade-chrome` prints the
   steps), followed by `fbcollector selftest`.

## 9. The tunnel and the switch to the brain

In phase 0 the collector writes `items.jsonl` and reads `monitored.toml`. With the brain it
talks to the brain's ingest API instead, over an ssh tunnel it opens itself (DESIGN D6): a
local forward from the Unix socket `~fbc/.ssh/fbtunnel.sock` on the Mac to `127.0.0.1:8781` on
the VPS. Four settings in `<root>/secrets` make the switch:

```
FBC_INGEST_URL=http://127.0.0.1:8781             # the brain's address at the tunnel's far end
FBC_INGEST_SOCKET=/Users/fbc/.ssh/fbtunnel.sock  # the tunnel's local end: requests go through it
FBC_INGEST_TOKEN='…'                             # the brain's INGEST_TOKEN, 24+ characters
FBC_COLLECTOR_ID=mac-1                           # one name per machine: the lease is granted to it
```

`fbcollector check-config` names what is missing. `monitored.toml` is no longer read: the brain
decides which groups are read (the groups its users watch). The first collector to check in
holds the lease; any other one is told to stop, and the owner moves the lease with
`/lease <id>` (two steps: the holder hears it must stop first). A holder that cannot reach the
brain for 6 h stops reading until it can (`lease lapsed` in `fbcollector status`).

**On the Mac nobody types anything in `fbc`.** The tunnel is `/usr/bin/ssh` under a LaunchAgent
of `fbc` (`com.ddiver.fbtunnel`, no autossh), and the setup script installs everything through
the operator's door (section 12):

- **The key** `~fbc/.ssh/fbtunnel_ed25519` is made once by every setup run (`update` included)
  and never replaced. It has no passphrase, since nobody is there to type one; the account,
  FileVault and the VPS's restrictions protect it. `ssh fbc-local tunnel-key` prints its public
  line.
- **The VPS's host key** is pinned from your own ssh session to the VPS, which your known_hosts
  has trusted for long (`tunnel-pin`, step 4 below). It goes into
  `~fbc/.ssh/fbtunnel_known_hosts` and gives the tunnel's config its host and port, so the
  VPS's address is in neither git nor the shared bundle directory. A *different* host key is
  refused: a changed host key is a security event. Compare it on the VPS's console; if it
  really changed, delete that file by hand in `fbc` and pin again.
- **Until the pin exists** setup says `tunnel: key ready, waiting for the host key` and loads
  nothing. With the pin it writes `~fbc/.ssh/fbtunnel_client.config` (checked offline with
  `ssh -G`) and the LaunchAgent, loads it, and restarts it when either changed. The log is
  `~fbc/Library/Logs/fbtunnel.log`, cut to its last 1000 lines beyond 1 MiB at each run. The
  tunnel is the last step of a setup run: if it fails, the run ends with `WARNING: the tunnel
  step failed` and exit 1, after everything else (the collector's restart in `update`, the
  door) is done.
- **Restarts:** launchd restarts a session that dropped (sleep, a new Wi-Fi) at once; one that
  fails again within 200 s waits 200 s (`ThrottleInterval`). A refused key or host key is thus
  tried at most 4 times in 10 minutes, both ends of the window counted, under fail2ban's
  default sshd jail (5 failures in 10 minutes); `setup-fbtunnel.sh` prints the VPS's own values. If the log shows a refusal over
  and over, `ssh fbc-local tunnel-stop` stops the retries until the cause is fixed.
- **The tunnel's local end** is the Unix socket `~fbc/.ssh/fbtunnel.sock`, owner-only in a
  0700 directory (ssh replaces a stale one when it reconnects), not a TCP port: no other local
  account can connect to it, nor create it while ssh is down and so receive the token the
  collector sends with every request (a loopback port, any account could bind).

**The switch, in this order.** Everything that talks to the VPS waits for the VPS's side.

1. Your account: the code with the tunnel is pushed and `scripts/make_bundle.sh` has made a
   bundle of it. `ssh fbc-local update` installs it in `fbc`: the new door, the key, and
   `waiting for the host key`. The collector stays in local mode.
2. The VPS, one sudo session: redeploy the brain at the same `main` (section 10); `/status` in
   the bot must answer. The brain must be at least as new as the collector: an older one
   refuses the newer event kinds (422), and the collector then drops them one by one.
3. The key to the VPS, and its user and sshd (from your account):

   ```sh
   ssh fbc-local tunnel-key | ssh vps 'cat > /tmp/fbtunnel-mac-1.pub'
   ssh -t vps 'sudo bash /srv/ddiver-fb/deploy/vps/setup-fbtunnel.sh --key /tmp/fbtunnel-mac-1.pub'
   ```

   The script (safe to repeat) makes the system user `fbtunnel`, installs
   `deploy/vps/20-fbtunnel.conf`, writes the key as
   `restrict,port-forwarding,permitopen="127.0.0.1:8781" ssh-ed25519 … fbtunnel-mac-1` into
   `/etc/ssh/fbtunnel_keys` (replacing that collector's older line), checks `sshd -t` and
   `sshd -T` for `fbtunnel` and for you, reloads sshd, and prints the host's ed25519 key with
   its fingerprint and the fail2ban arithmetic. For the first hour, optionally:
   `sudo fail2ban-client set sshd addignoreip <your home IP>` (and `delignoreip` after).
4. Pin the VPS's host key in `fbc` (from your account; the host and port are the ones your
   own `ssh vps` uses):

   ```sh
   ssh vps cat /etc/ssh/ssh_host_ed25519_key.pub \
     | awk -v h="$(ssh -G vps | awk '/^hostname /{h=$2} /^port /{p=$2} END{print (p == 22 ? h : "[" h "]:" p)}')" '{print h, $1, $2}' \
     | ssh fbc-local tunnel-pin
   ```

   The line must be exactly `<host> ssh-ed25519 <key>` (or `[<host>]:<port> …`), with a
   lower-case host name or IPv4 address. Setup then starts the tunnel.
5. Check it: `ssh fbc-local tunnel` must show the agent `state = running`, the socket
   `/Users/fbc/.ssh/fbtunnel.sock` as `srw------- fbc`, and `probe of /v1/control without the
   token: HTTP 401` (the brain answered through the tunnel; `000` is no answer: `ssh fbc-local
   tunnel-log 50`). Its `host key` fingerprint must be the one step 3 printed and the one `ssh-keygen -lF <host>`
   shows in your own known_hosts. Each probe adds one to the brain's count of unauthorized
   requests.
6. In the bot: `/pause`. Until the bot has subscriptions every post read would be marked
   seen and never delivered.
7. `ssh fbc-local status` must show `outbox: 0 items` (rows left from phase 0 would reach the
   brain under the old id `home-1`). Then the switch, with the token from your development
   checkout's `.env` (stdin only: never on a command line, never in a file in
   `/Users/Shared`):

   ```sh
   sed -n 's/^\(FBC_\)\{0,1\}INGEST_TOKEN=//p' <your checkout>/.env | ssh fbc-local uplink-token
   ```

   It refuses while the tunnel's LaunchAgent is not loaded, and a token that is not 24 to 512
   characters of `A-Z a-z 0-9 . _ ~ + / = -` (no quotes around it). It merges the four
   settings into the secrets file (every other line is kept; `--secrets` later keeps them
   too), checks that `fbcollector check-config` says `mode: uplink` and `ok` (else it puts
   the file back), and restarts the collector. The token is never printed.
8. `ssh fbc-local uplink` must show `mode: uplink`, `lease: held by mac-1`, a recent `last
   control answer` and `last cycle: … ok`. `/lease` in the bot shows `mac-1`; if it names
   another holder, `/lease mac-1 now`.
9. In the bot: subscribe in the groups you want (for example two rideshare groups and a
   buy/sell group), `/listings <id> on` for a buy/sell group whose listing posts phase 0 found
   missing, then `/resume`.

**Back to local mode:** `ssh fbc-local uplink-off` removes the four settings and restarts the
collector; the tunnel stays (`tunnel-stop` stops it).

**What the switch does to phase 0's state:** `items.jsonl` and `monitored.toml` are no longer
read (keep or archive them); posts already seen stay seen for 30 days and are never sent; the
collector introduces its known groups to the brain once, and the brain's first answers turn
every group off until subscriptions turn them back on, each with a baseline of "now", so
nothing older is sent. Observe mode (`FBC_OBSERVE_MODE=true`) stays as it is: raw payloads
kept 21 days, deeper backstops.

**The laptop (Linux)** runs the same config under `deploy/linux/autossh.service` (section 8):
there `VPS_HOST` is replaced by hand and the pin line goes into `~/.ssh/fbtunnel_known_hosts`
by hand, from the same trusted source; its secrets name its own socket,
`FBC_INGEST_SOCKET=/home/<user>/.ssh/fbtunnel.sock`, and `FBC_COLLECTOR_ID=laptop-1`.

## 10. The brain on the VPS (first deploy)

In `/srv/ddiver-fb`, with the checkout and its `.env` (`BOT_TOKEN`, `API_ID`, `API_HASH`,
`OWNER_ID`, `INGEST_TOKEN`; see `.env.example`). The container runs as uid 10002 and keeps
its database in the bind-mounted `./data`; Docker would create a missing directory as root,
and the brain could not open its lock file there. Create it first, once:

```sh
sudo install -d -o 10002 -g 10002 -m 700 /srv/ddiver-fb/data
```

The image pins ddiver-core to the tag `core-v0.1.0` of the **private** ddiver repository and
fetches it over ssh during the build (`RUN --mount=type=ssh`; github.com is checked against
`deploy/brain/github_known_hosts`). The build takes its key from an ssh-agent, and the key never
reaches a layer. Use a read-only deploy key of the ddiver repository: the one the VPS already
has for ddiver if GitHub lists it as read-only (ddiver → Settings → Deploy keys), otherwise a
new read-only one. Put **only that key** in the agent. GitHub accepts the first deploy key
offered and then refuses every repository that key does not belong to.

```sh
cd /srv/ddiver-fb
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/<ddiver deploy key>        # the file ddiver's own checkout pulls with
GIT_SHA=$(git rev-parse HEAD) docker compose up -d --build
ssh-agent -k
docker compose logs -f brain     # "ingest on http://0.0.0.0:8781/v1/"
```

The checkout of this repository is a separate matter from the build. This is another GitHub
repository, and GitHub refuses to register one deploy key on two repositories. So either give
it a read-only deploy key of its own (a second key file, picked through a `Host` alias in
`~/.ssh/config`, never loaded into the build's agent), or bring the code over without GitHub,
as a `git bundle` of `main` made on the Mac, like section 12 does for the collector's account.

Without compose: `docker build --ssh default --build-arg GIT_SHA=$(git rev-parse HEAD) -t
ddiver-fb:latest .` with the same agent. A build that fails with `Permission denied
(publickey)` or `Repository not found` had no key or the wrong key in the agent. Restarting
the brain needs no key; only a build does.

If the log says `cannot write the data directory`, the directory exists with another owner:
`sudo chown -R 10002:10002 /srv/ddiver-fb/data`.

## 11. Tests and the headed checks

`uv run pytest` never opens a window: the browser tests (`tests/collector/browser`) start the
installed Google Chrome **headless** on a page served on 127.0.0.1, never Facebook. The few
checks that are about the headed fingerprint itself — no `HeadlessChrome` in the user agent, no
`--headless` switch, a screen that holds the window, and the full self-test — are skipped there
and say why (`uv run pytest -rs`). `FBC_SKIP_BROWSER=1` skips every browser test.

The headed checks run where a window disturbs nobody: **inside the collector's own macOS user**
(its Chrome window lives in that user's session, not on your screen). Logged into that user,
after `pin-home`:

```sh
fbcollector selftest          # the production self-test: headed, on the account's profile
```

Every check must say `ok` (it refuses to start Chrome away from home, in another timezone, on
battery, during a block, while paused or above the crash-loop cap: section 3). The headed
browser tests need test tools the collector's install leaves out; `uv run --with` lends them
for the one run, without ddiver-core (checked in a clone that cannot read it). In that user's
checkout:

```sh
cd ~/ddiver_facebook
FBC_BROWSER_HEADED=1 uv run --frozen --package fbcollector --no-dev \
  --with pytest --with pytest-asyncio --with pytest-timeout --with hypothesis \
  pytest tests/collector/browser -rs
```

(`FBC_BROWSER_HEADED=1` runs every browser test headed, plus the headed-only ones. They never
leave 127.0.0.1 and use throwaway profiles, not the account's.)

## 12. Phase 0 on the Mac under a dedicated user

Owner decision of 2026-09-25: the collector runs in a Standard macOS account of its own (`fbc`
below), in a background fast-user-switching session. Its Chrome window lives in that session:
it never appears on your screen and never takes your keyboard focus, and its headed checks
(`selftest`) run there too. Chrome is the ordinary Google Chrome in `/Applications` — Chrome
Beta is not used. Your own account keeps its development checkout and never runs the
collector (do not install the LaunchAgent in it).

**Once, as an administrator (your own account):**

1. The user: System Settings → Users & Groups → Add User…, **Standard**, account name `fbc`, a
   strong password kept in your password manager; no Apple Account. If `fbc` already exists,
   check it is not an administrator: `dscl . -read /Groups/admin GroupMembership` must not list
   it.
2. Fast user switching: System Settings → Control Center → Fast User Switching → Show in Menu
   Bar. You switch with that menu; you never log `fbc` out.
3. Check the Mac: time zone Europe/Sofia (General → Date & Time), Google Chrome in
   `/Applications`, the Command Line Tools (`xcode-select -p`; else `xcode-select --install`),
   FileVault on, and Battery/Energy → Options → "Prevent automatic sleeping on power adapter
   when the display is off" on.
4. Recommended: your home folder is readable by the group `staff`, which every local account —
   `fbc` too — belongs to. `chmod go-rwx ~/Code` (or `chmod 700 ~`) keeps the collector's
   account out of your files.

**The code:** in your account, from the checkout, `scripts/make_bundle.sh`. It writes a git
bundle of `main`, the setup script, your own uv and their SHA-256 to `/Users/Shared/ddiver-fb/`,
readable by local accounts only (group `staff`), no secrets. The uv must be exactly the uv
0.11.7 build whose SHA-256 the setup script pins (the official installer's, which your account
has); the script refuses any other (`FBC_UV_BINARY=/path/to/uv` names another copy). The
bundle carries `main`'s whole history, so the script refuses if any commit of it ever held a
file named like a secret (`.env`, `secrets`, `monitored.toml`, keys, databases, sessions, raw
steps), or any file at all whose content looks like one: a bot or API token (a Bot API URL
too), an upper-case `…TOKEN`/`…SECRET` in an env file, a compose file or a unit, a Facebook
cookie (as a header, JSON, a Python print-out, a cookie-extension export, `cookies.txt`, a
DevTools table), a private key, a raw Facebook capture. That covers files deleted since, too. It
names the path, the rule and the commit, never the text. A real secret has to leave the history
before a bundle can be made. A made-up value in a test, once you have looked at it, goes into
`scripts/secret_scan_allow.txt` (`python3 scripts/scan_secrets.py --fingerprints` prints the
line; commit it). It reads `main` once; if a commit lands on `main` while it runs, it refuses
the bundle: run it again. It writes only to `/Users/Shared/ddiver-fb` or to a new or empty
directory given as its argument, which it marks as its own (`.ddiver-fb-bundle`). It refuses your
home and its folders, the checkout, shared and system folders, symbolic links, a directory
inside a folder another account could rename things in, and a directory that holds anything it
did not write (the operator key `fbc_local.pub` below is allowed). It changes nothing until all
of that is checked.

**In `fbc`** (menu bar → `fbc`, its password, then Terminal):

1. `bash /Users/Shared/ddiver-fb/setup-fbc-user.sh` — refuses to run as an administrator or as
   the account that made the bundle; checks the checksums; installs the bundle's uv (its
   SHA-256 checked against the pinned one first; nothing is downloaded), the checkout
   `~/ddiver_facebook` and the collector alone (`uv sync --frozen --package fbcollector
   --no-dev`); creates the 0700 root; asks for the alert bot's token (not shown) and your
   Telegram id and writes `secrets` 0600 with `FBC_TIMEZONE=Europe/Sofia` and observe mode;
   copies the four start groups (`docs/monitored.example.toml`) to `monitored.toml`; installs
   the LaunchAgent without starting it. Open a new Terminal window afterwards (PATH).
2. `fbcollector pin-home` — at home, on the home network, no VPN.
3. `fbcollector selftest` — Chrome opens headed in `fbc`'s session, invisible in yours; every
   line must say `ok` (section 11: these are the headed checks).
4. The pre-login checklist (section 2), then `fbcollector login`: Chrome opens on
   `about:blank` in `fbc`'s session; **type `facebook.com` yourself**, log in, answer 2FA,
   quit Chrome (Cmd+Q).
5. Warm-up, 24–72 h (section 4): a few minutes of browsing by hand, always in `fbc`'s
   `fbcollector login`.
6. Start: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ddiver.fbcollector.plist`,
   check `fbcollector status`, then switch back to your own account. From then on it reads in
   the background; alerts reach you through the alert bot.

**Updating the code:** `scripts/make_bundle.sh` in your account, then in `fbc`
`bash /Users/Shared/ddiver-fb/setup-fbc-user.sh --update` (stops a running collector,
fast-forwards the checkout from the bundle, reinstalls, starts it again; the secrets stay,
`--secrets` asks again). It refuses a checkout edited by hand.

**After a reboot** nothing runs until `fbc`'s session exists: unlock FileVault and log in to your
account, switch to `fbc` once and type its password (the LaunchAgent starts by itself), switch
back. Automatic login is not possible with FileVault, and not wanted. Until then the collector is
off — after a long outage the schedule restarts calmly, it never catches up in a burst.

**Sleep:** closing the lid or Apple menu → Sleep: the collector closes its Chrome first (the
sleep hook) and reads again after the wake, once the home network is verified. Taking the Mac
out of the house still starts with the stop command of section 5, in `fbc`.

**Operator access over SSH (the door; nothing here is enabled by the setup):** to watch the
collector, ship an update and run the tunnel from your own account without switching users.
One key that can run nothing but `deploy/macos/fbc-operator.sh`, from this Mac only.

1. As an administrator: System Settings → General → Sharing → Remote Login on, "Allow access
   for: Only these users" with `fbc` (and your own account only if you already use SSH into it).
2. Key-only for `fbc`, no forwarding: as an administrator copy
   `deploy/macos/sshd-fbc-operator.conf` to `/etc/ssh/sshd_config.d/200-fbc-operator.conf` and
   check it with `sudo sshd -t`.
3. In your account: `ssh-keygen -t ed25519 -f ~/.ssh/fbc_local -C fbc-operator` (with a
   passphrase), and copy `~/.ssh/fbc_local.pub` to `/Users/Shared/ddiver-fb/`. Add to
   `~/.ssh/config`:

   ```
   Host fbc-local
       HostName localhost
       User fbc
       IdentityFile ~/.ssh/fbc_local
       IdentitiesOnly yes
       BatchMode yes
   ```

4. In `fbc`: `mkdir -p -m 700 ~/.ssh ~/bin`, `install -m 755
   ~/ddiver_facebook/deploy/macos/fbc-operator.sh ~/bin/`, then append the key, pinned to this
   Mac and to the door:
   `printf 'restrict,from="127.0.0.1,::1",command="%s/bin/fbc-operator.sh" %s\n' "$HOME" "$(cat /Users/Shared/ddiver-fb/fbc_local.pub)" >> ~/.ssh/authorized_keys`
   and `chmod 600 ~/.ssh/authorized_keys`. From then on every setup run (`update` too) keeps
   `~/bin/fbc-operator.sh` in step with the checkout.
5. From your account, `ssh fbc-local <verb>` (the same as
   `ssh -i ~/.ssh/fbc_local fbc@localhost <verb>`). The verbs:

   | Verb | What it does |
   |---|---|
   | `status`, `report`, `diagnostics`, `dialogs [N]`, `log [N]` | `fbcollector status`, `report`, `report --diagnostics-only`, `dialogs --last N` (1–20, default 4), the last N lines (up to 999) of `fbcollector.log` |
   | `agent` | launchd's state of the collector and of the tunnel |
   | `restart`, `update` | restart (or load) the collector; `setup-fbc-user.sh --update` |
   | `tunnel` | the tunnel: its agent, its socket `~/.ssh/fbtunnel.sock` (mode, owner, since when), the fingerprints of its key and of the pinned host key, the log's last 5 lines, and a probe of `/v1/control` through the socket without the token that prints only the HTTP code |
   | `tunnel-log [N]` | the last N lines (up to 999) of `fbtunnel.log` |
   | `tunnel-key` | the tunnel's public key line, for the VPS |
   | `tunnel-pin` | `setup-fbc-user.sh --pin-host-key`: the host key line on stdin (section 9) |
   | `tunnel-restart`, `tunnel-stop` | restart (or load) the tunnel; unload it |
   | `uplink` | `fbcollector uplink`: mode, brain host:port, token set or not, collector id, lease, last answer, last cycle, queues, refusal counters (no ids, no text, no token) |
   | `uplink-token`, `uplink-off` | `setup-fbc-user.sh --uplink` (the token on stdin) and `--local` (section 9) |

   `status` and the full `report` show group ids (`status` also group names and quarantined
   post ids); `report --diagnostics-only` shows structure and counts only; `dialogs` prints
   the texts of the dialogs on the last saved steps, which may quote a post or a name (the
   owner allowed it for the operator's door on 2026-09-30). `login`, `resume`, `pause`,
   `groups`, `pin-home` and `selftest` are refused: they act on the Facebook account or
   override a safety decision, and stay with the owner at the keyboard in `fbc`.
