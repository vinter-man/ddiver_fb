#!/bin/bash
# Set up the collector inside its own Standard macOS user (docs/RUNBOOK.md, sections 9 and 12).
#
# Run in the collector account's Terminal (`fbc`), never in the owner's own account:
#
#   bash /Users/Shared/ddiver-fb/setup-fbc-user.sh                 # first time; safe to repeat
#   bash /Users/Shared/ddiver-fb/setup-fbc-user.sh --update        # a newer bundle
#   bash /Users/Shared/ddiver-fb/setup-fbc-user.sh --secrets       # ask for the alert bot again
#   bash /Users/Shared/ddiver-fb/setup-fbc-user.sh --tunnel        # the tunnel to the brain alone
#   bash /Users/Shared/ddiver-fb/setup-fbc-user.sh --pin-host-key  # pin the VPS's host key
#   bash /Users/Shared/ddiver-fb/setup-fbc-user.sh --uplink        # switch to the brain
#   bash /Users/Shared/ddiver-fb/setup-fbc-user.sh --local         # back to local mode
#
# What it does, each step only when needed:
#   1. refuses to run as root, as an administrator or as the account that made the bundle;
#   2. checks the bundle's and this script's SHA-256 (scripts/make_bundle.sh wrote them);
#   3. installs uv for this account from the bundle: the uv 0.11.7 binary scripts/make_bundle.sh
#      put next to it (the version uv.lock was written with), into ~/.local/bin, only once its
#      SHA-256 matches the one pinned below; an uv already there is kept only if it is that very
#      build (nothing is downloaded; nothing in the shell profile but the PATH line of step 10);
#   4. clones ~/ddiver_facebook from the bundle, or with --update fast-forwards it;
#   5. installs the collector alone: uv sync --frozen --package fbcollector --no-dev (no brain,
#      no ddiver-core, no development tools);
#   6. creates the collector root, 0700;
#   7. asks, without echo, for the alert bot's token and the owner's Telegram id and writes them
#      into the secrets file 0600, with the timezone pinned to Europe/Sofia and observe mode on
#      (every other line of the file, the uplink settings among them, is kept);
#   8. copies docs/monitored.example.toml to the root when there is no monitored.toml yet;
#   9. installs the LaunchAgent (Aqua sessions only: it runs in this user's own session, also
#      while another user has the screen) without starting it, and keeps Chrome out of App Nap;
#  10. puts the collector's commands on this account's PATH, and prints what comes next;
#  11. the tunnel to the brain (install_tunnel): creates its ssh key once, and once the VPS's
#      host key is pinned writes the ssh config and the tunnel's LaunchAgent and (re)starts it.
#      It runs last and on its own: a failure there is reported (exit 1) after everything else,
#      the collector's restart included, is done.
# With --update, a running collector is stopped before step 4 and started again before step 11.
#
# --tunnel does steps 1, 2 and 11 alone. --pin-host-key reads one line, `<host> ssh-ed25519
# <key>` (or `[<host>]:<port> ...`), from the terminal or stdin, pins it (never over a different
# pin) and goes on as --tunnel. --uplink needs the tunnel's LaunchAgent loaded; it reads the
# brain's ingest token without echo (or one line on stdin), never prints it, merges the four
# uplink settings into the secrets file, checks `fbcollector check-config` says uplink and
# restarts the collector. --local removes those four settings and restarts it. The operator
# door (deploy/macos/fbc-operator.sh) runs these as tunnel-pin, uplink-token and uplink-off.
set -euo pipefail

readonly SHARED_DIR="${FBC_SHARED_DIR:-/Users/Shared/ddiver-fb}"
readonly BUNDLE="$SHARED_DIR/ddiver_facebook.bundle"
readonly SUMS="$BUNDLE.sha256"
readonly CHECKOUT="${FBC_CHECKOUT:-$HOME/ddiver_facebook}"
readonly ROOT="$HOME/Library/Application Support/fbcollector"
readonly LABEL=com.ddiver.fbcollector
readonly AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
readonly ZONE=Europe/Sofia
readonly COLLECTOR_ID=mac-1
# The brain's address at the far end of the tunnel; the requests go through the tunnel's Unix
# socket (TUNNEL_SOCKET), never a loopback TCP port another local account could bind first.
readonly INGEST_URL=http://127.0.0.1:8781
readonly UPLINK_KEYS="FBC_INGEST_URL FBC_INGEST_SOCKET FBC_INGEST_TOKEN FBC_COLLECTOR_ID"
readonly TUNNEL_LABEL=com.ddiver.fbtunnel
readonly TUNNEL_AGENT="$HOME/Library/LaunchAgents/$TUNNEL_LABEL.plist"
readonly SSH_DIR="$HOME/.ssh"
readonly TUNNEL_KEY="$SSH_DIR/fbtunnel_ed25519"
readonly TUNNEL_PIN="$SSH_DIR/fbtunnel_known_hosts"
readonly TUNNEL_CONFIG="$SSH_DIR/fbtunnel_client.config"
readonly TUNNEL_SOCKET="$SSH_DIR/fbtunnel.sock"
readonly TUNNEL_LOG="$HOME/Library/Logs/fbtunnel.log"
readonly TUNNEL_LOG_MAX=1048576
readonly UV_VERSION=0.11.7
# SHA-256 of the `uv` binary inside Astral's release archives uv-aarch64-apple-darwin.tar.gz and
# uv-x86_64-apple-darwin.tar.gz of uv 0.11.7 (each archive checked against the release's own
# .sha256). scripts/make_bundle.sh reads
# these two lines and refuses to bundle any other uv.
readonly UV_SHA256_ARM64=40a78912c850286ea5756211f1f88c0928fb15b788869d46d4355cd4ac556023
readonly UV_SHA256_X86_64=5882e241a33a36d9ad5daca8e6e3d6ce33e22256b0d30f024437b729196ab290
readonly UV="$HOME/.local/bin/uv"
readonly BUNDLED_UV="$SHARED_DIR/uv"
readonly CHROME_APP="${FBC_CHROME_APP:-/Applications/Google Chrome.app}"
readonly PROFILE_LINE="export PATH=\"$CHECKOUT/.venv/bin:\$HOME/.local/bin:\$PATH\"  # fbcollector"

mode=setup
ask_secrets=0

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die() {
  printf 'setup-fbc-user: %s\n' "$*" >&2
  exit 1
}

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

set_mode() { # one mode per run; --secrets goes with a setup or an update only
  [[ "$mode" == setup || "$mode" == "$1" ]] || usage 2 >&2
  mode="$1"
}

for arg in "$@"; do
  case "$arg" in
    --update) set_mode update ;;
    --secrets) ask_secrets=1 ;;
    --tunnel) set_mode tunnel ;;
    --pin-host-key) set_mode pin ;;
    --uplink) set_mode uplink ;;
    --local) set_mode local ;;
    -h | --help) usage 0 ;;
    *) usage 2 >&2 ;;
  esac
done
if [[ "$ask_secrets" -eq 1 && "$mode" != setup && "$mode" != update ]]; then
  usage 2 >&2
fi

# -- 1. who is running this ---------------------------------------------------------------------

check_account() {
  [[ "$(uname -s)" == Darwin ]] || die "this is for the Mac (the laptop has its own runbook)"
  [[ "$(id -u)" -ne 0 ]] || die "not as root: log in as the collector's own user"
  local me groups maker
  me="$(id -un)"
  groups=" $(id -Gn) "
  if [[ "$groups" == *" admin "* ]]; then
    die "$me is an administrator: run this in the collector's Standard account (e.g. fbc), not your own"
  fi
  [[ -r "$BUNDLE" && -r "$SUMS" ]] \
    || die "no readable bundle in $SHARED_DIR: run scripts/make_bundle.sh in your own account first"
  maker="$(stat -f %Su "$BUNDLE")"
  if [[ "$maker" == "$me" ]]; then
    die "$me made the bundle, so this is the owner's account: log in as the collector's user"
  fi
  say "account: $me (Standard); bundle made by $maker"
}

# -- 2. the bundle ------------------------------------------------------------------------------

check_sums() {
  (cd "$SHARED_DIR" && shasum -a 256 -c "$(basename "$SUMS")" >/dev/null) \
    || die "the checksums in $SUMS do not match: make the bundle again"
  say "bundle and setup script match their SHA-256"
}

# -- 3. tools -----------------------------------------------------------------------------------

check_tools() {
  xcode-select -p >/dev/null 2>&1 \
    || die "git needs the Command Line Tools: an administrator runs \`xcode-select --install\`"
  [[ -d "$CHROME_APP" ]] || die "Google Chrome is not installed in /Applications (Beta is not used)"
}

sha256() { # sha256 <file>: its SHA-256, hex
  local sum
  sum="$(shasum -a 256 <"$1")" || return 1
  printf '%s\n' "${sum%% *}"
}

is_pinned_uv() { # is_pinned_uv <file> <sha256>: the pinned build, by its digest before anything
  # of it runs, then by its own word (exactly uv $UV_VERSION)
  local version
  [[ -f "$1" && ! -L "$1" ]] || return 1
  [[ "$(sha256 "$1")" == "$2" ]] || return 1
  version="$("$1" --version 2>/dev/null)" || return 1
  [[ "$version" == "uv $UV_VERSION" || "$version" == "uv $UV_VERSION "* ]]
}

install_uv() {
  local pin tmp
  case "$(uname -m)" in
    arm64) pin="$UV_SHA256_ARM64" ;;
    x86_64) pin="$UV_SHA256_X86_64" ;;
    *) die "no uv build is pinned for $(uname -m)" ;;
  esac
  if is_pinned_uv "$UV" "$pin"; then
    say "uv: $UV_VERSION, the pinned build"
    return
  fi
  if [[ -e "$UV" || -L "$UV" ]]; then
    say "uv: $UV is not the pinned uv $UV_VERSION build; replacing it"
  fi
  [[ ! -d "$UV" || -L "$UV" ]] || die "$UV is a directory"
  step "installing uv $UV_VERSION for $(id -un) (the bundle's copy, SHA-256 pinned)"
  [[ -f "$BUNDLED_UV" && ! -L "$BUNDLED_UV" && -r "$BUNDLED_UV" ]] \
    || die "the bundle carries no uv: make the bundle again (scripts/make_bundle.sh)"
  mkdir -p "${UV%/*}"
  tmp="$(mktemp "${UV%/*}/.uv.XXXXXX")"
  cat "$BUNDLED_UV" >"$tmp"
  chmod 755 "$tmp"
  if ! is_pinned_uv "$tmp" "$pin"; then
    rm -f "$tmp"
    die "the bundle's uv is not uv $UV_VERSION with SHA-256 $pin: make the bundle again"
  fi
  mv -f "$tmp" "$UV"
  say "uv: $UV_VERSION installed into $UV"
}

# -- 4. the checkout ----------------------------------------------------------------------------

checkout() {
  if [[ ! -d "$CHECKOUT/.git" ]]; then
    step "cloning $CHECKOUT from the bundle"
    git clone --quiet --branch main "$BUNDLE" "$CHECKOUT"
  elif [[ "$mode" == update ]]; then
    step "updating $CHECKOUT from the bundle"
    [[ -z "$(git -C "$CHECKOUT" status --porcelain --untracked-files=no)" ]] \
      || die "$CHECKOUT has local changes; the collector's checkout is never edited by hand"
    git -C "$CHECKOUT" fetch --quiet "$BUNDLE" main
    git -C "$CHECKOUT" merge --quiet --ff-only FETCH_HEAD \
      || die "the bundle's main does not fast-forward $CHECKOUT"
  fi
  say "checkout: $CHECKOUT at $(git -C "$CHECKOUT" rev-parse --short HEAD)"
}

# -- 5. the collector ---------------------------------------------------------------------------

install_collector() {
  step "installing the collector (fbcollector only, no development tools)"
  (cd "$CHECKOUT" && "$UV" sync --frozen --package fbcollector --no-dev)
  [[ -x "$CHECKOUT/.venv/bin/fbcollector" ]] || die "the install left no .venv/bin/fbcollector"
}

# -- 6.-8. the collector's root -----------------------------------------------------------------

make_root() {
  mkdir -p "$ROOT"
  chmod 700 "$ROOT"
  say "collector root: $ROOT (0700)"
}

check_zone() {
  local zone
  zone="$(readlink /etc/localtime 2>/dev/null || true)"
  zone="${zone#*zoneinfo/}"
  if [[ "$zone" != "$ZONE" ]]; then
    say "WARNING: this Mac's time zone is ${zone:-unknown}, not $ZONE: the collector refuses to"
    say "  read in another zone. An administrator sets it in System Settings > General > Date & Time."
  fi
}

# sets_key <line> <name>...: whether a dotenv line sets one of the names (in any case, as the
# collector reads them). Builtins only: the line may hold a token, which must never reach the
# argument list of another process.
sets_key() {
  local line="$1" key name found=1
  shift
  [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*= ]] \
    || return 1
  key="${BASH_REMATCH[2]}"
  shopt -s nocasematch
  for name in "$@"; do
    if [[ "$key" == "$name" ]]; then
      found=0
      break
    fi
  done
  shopt -u nocasematch
  return "$found"
}

# merge_secrets <name>... -- <line>...: rewrite the secrets file (0600, atomically) with every
# line that sets none of the names kept as it is, then the given lines. Nothing else of the file
# is touched: --secrets keeps the uplink settings, --uplink keeps the alert bot.
merge_secrets() {
  local secrets="$ROOT/secrets" tmp line
  local -a names=() lines=()
  while [[ $# -gt 0 && "$1" != -- ]]; do
    names+=("$1")
    shift
  done
  if [[ $# -gt 0 ]]; then
    shift
  fi
  lines=("$@")
  [[ ! -L "$secrets" ]] || die "$secrets is a symbolic link; refusing to write through it"
  umask 077
  tmp="$(mktemp "$ROOT/.secrets.XXXXXX")"
  {
    if [[ -f "$secrets" ]]; then
      while IFS= read -r line || [[ -n "$line" ]]; do
        sets_key "$line" ${names[@]+"${names[@]}"} || printf '%s\n' "$line"
      done <"$secrets"
    fi
    for line in ${lines[@]+"${lines[@]}"}; do
      printf '%s\n' "$line"
    done
  } >"$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$secrets"
}

write_secrets() {
  local secrets="$ROOT/secrets" token owner
  if [[ -e "$secrets" && "$ask_secrets" -eq 0 ]]; then
    say "secrets: $secrets kept (--secrets asks again)"
    return
  fi
  [[ -t 0 ]] || die "the alert bot's token is asked for on a terminal; run this in Terminal"
  step "the owner-only alert bot (DESIGN D19)"
  while :; do
    read -r -s -p "Alert bot token (from @BotFather, not shown): " token
    printf '\n'
    [[ "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]] && break
    say "That does not look like 123456789:AA... ; try again."
  done
  while :; do
    read -r -p "Your own Telegram user id (digits): " owner
    [[ "$owner" =~ ^[1-9][0-9]*$ ]] && break
    say "Digits only; try again."
  done
  merge_secrets FBC_TIMEZONE FBC_OBSERVE_MODE FBC_ALERT_BOT_TOKEN FBC_ALERT_OWNER_ID -- \
    "FBC_TIMEZONE=$ZONE" "FBC_OBSERVE_MODE=true" \
    "FBC_ALERT_BOT_TOKEN=$token" "FBC_ALERT_OWNER_ID=$owner"
  token=""
  say "secrets: written to $secrets (0600; timezone $ZONE, observe mode on; other lines kept)"
}

copy_monitored() {
  local target="$ROOT/monitored.toml"
  if [[ -e "$target" ]]; then
    say "monitored groups: $target kept"
    return
  fi
  install -m 600 "$CHECKOUT/docs/monitored.example.toml" "$target"
  say "monitored groups: the four start groups copied to $target"
}

# -- 9. the LaunchAgent -------------------------------------------------------------------------

agent_loaded() { # agent_loaded [label]: the collector's LaunchAgent by default
  launchctl print "gui/$(id -u)/${1:-$LABEL}" >/dev/null 2>&1
}

install_agent() {
  local template="$CHECKOUT/deploy/macos/$LABEL.plist" tmp
  case "$CHECKOUT$HOME" in
    *'|'* | *'&'* | *\\*) die "the checkout or home path holds a character sed cannot take" ;;
  esac
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  tmp="$(mktemp "$HOME/Library/LaunchAgents/.$LABEL.XXXXXX")"
  sed -e "s|__REPO__|$CHECKOUT|g" -e "s|__HOME__|$HOME|g" "$template" >"$tmp"
  plutil -lint "$tmp" >/dev/null || die "the LaunchAgent does not lint"
  chmod 644 "$tmp"
  mv -f "$tmp" "$AGENT"
  defaults write com.google.Chrome NSAppSleepDisabled -bool YES
  say "LaunchAgent: $AGENT (not started; loads by itself at this user's next login)"
}

add_to_path() {
  local profile="$HOME/.zprofile"
  if [[ -f "$profile" ]] && grep -qF "# fbcollector" "$profile"; then
    return
  fi
  printf '%s\n' "$PROFILE_LINE" >>"$profile"
  say "PATH: $profile now puts fbcollector and uv on it (open a new Terminal window)"
}

next_steps() {
  local bin="$CHECKOUT/.venv/bin/fbcollector"
  step "configuration"
  "$bin" check-config || true
  if [[ "$mode" == update ]]; then
    return
  fi
  cat <<EOF

== next, in this account, in this order (docs/RUNBOOK.md, section 12)
  1. fbcollector pin-home     at home, on the home network, no VPN
  2. fbcollector selftest     Chrome opens in this session only; every line must say ok
  3. fbcollector login        type facebook.com yourself, log in, answer 2FA, quit Chrome
  4. warm-up, 24-72 h         a few minutes of browsing by hand in \`fbcollector login\`
  5. start the collector:     launchctl bootstrap gui/\$(id -u) $AGENT
Then switch back to your own account (fast user switching); do not log this one out.
The tunnel to the brain waits for the VPS's host key: docs/RUNBOOK.md, section 9.
EOF
}

# The operator's ssh door (RUNBOOK section 12) runs ~/bin/fbc-operator.sh as its forced command.
# Once the owner has set the door up, every run keeps that script in step with the checkout, so a
# new verb or fix arrives with the bundle instead of by hand. Where there is no door, nothing.
refresh_operator_door() {
  local door="$HOME/bin/fbc-operator.sh"
  local shipped="$CHECKOUT/deploy/macos/fbc-operator.sh"
  [[ -f "$door" && -f "$shipped" ]] || return 0
  install -m 755 "$shipped" "$door"
  say "operator door: $door updated"
}

# Until the agent is gone from the GUI domain (up to 150 s: the collector's 90 s ExitTimeOut plus
# launchd's own teardown).
wait_unloaded() { # wait_unloaded [label]
  local _
  for _ in $(seq 1 75); do
    agent_loaded "${1:-$LABEL}" || return 0
    sleep 2
  done
  return 1
}

# -- 10. the tunnel to the brain (RUNBOOK section 9) ---------------------------------------------

# The pinned host key, one known_hosts line: `<host> ssh-ed25519 <key>`, or `[<host>]:<port> ...`
# for another port than 22. A lower-case host name or IPv4 address; the key an ed25519 one.
readonly PIN_SHAPE='^(\[([a-z0-9][a-z0-9.-]{0,252})\]:([0-9]{1,5})|([a-z0-9][a-z0-9.-]{0,252})) ssh-ed25519 (AAAAC3NzaC1lZDI1NTE5AAAAI[A-Za-z0-9+/]{43})$'
pin_line="" pin_host="" pin_port=""

# parse_pin <line>: sets pin_line (the line as it is written: a port 22 without brackets),
# pin_host and pin_port; fails on anything else than the shape above.
parse_pin() {
  local key
  [[ "$1" =~ $PIN_SHAPE ]] || return 1
  if [[ -n "${BASH_REMATCH[2]}" ]]; then
    pin_host="${BASH_REMATCH[2]}"
    pin_port="${BASH_REMATCH[3]}"
  else
    pin_host="${BASH_REMATCH[4]}"
    pin_port=22
  fi
  key="${BASH_REMATCH[5]}"
  [[ "$pin_port" =~ ^[1-9][0-9]*$ ]] && ((pin_port <= 65535)) || return 1
  if [[ "$pin_port" == 22 ]]; then
    pin_line="$pin_host ssh-ed25519 $key"
  else
    pin_line="[$pin_host]:$pin_port ssh-ed25519 $key"
  fi
}

read_pin_file() { # parse_pin over the pin file, which must hold exactly that one line
  local text
  [[ -f "$TUNNEL_PIN" && ! -L "$TUNNEL_PIN" ]] || return 1
  text="$(cat "$TUNNEL_PIN")"
  [[ "$text" != *$'\n'* ]] || return 1
  parse_pin "$text" && [[ "$pin_line" == "$text" ]]
}

fingerprint() { # fingerprint <file>: ssh-keygen's line for it, or why there is none
  if [[ -f "$1" ]]; then
    ssh-keygen -l -f "$1" </dev/null 2>/dev/null || say "unreadable"
  else
    say "none"
  fi
}

ssh_dir() {
  [[ ! -L "$SSH_DIR" ]] || die "$SSH_DIR is a symbolic link; refusing to use it"
  mkdir -p "$SSH_DIR"
  chmod 700 "$SSH_DIR" # ssh and sshd both refuse a directory others can write to
}

# The tunnel's own key, made once and never replaced: its public half is what the VPS lets
# forward one port. No passphrase, since nobody is there to type it: the account, FileVault
# and the VPS's restrictions (restrict, port-forwarding, one permitopen) protect it.
tunnel_key() {
  local key="$TUNNEL_KEY" pub="$TUNNEL_KEY.pub" tmp
  [[ ! -L "$key" && ! -L "$pub" ]] || die "$key or $pub is a symbolic link; refusing to use it"
  [[ ! -e "$key" || -f "$key" ]] || die "$key is not a plain file"
  [[ ! -e "$pub" || -f "$pub" ]] || die "$pub is not a plain file"
  if [[ ! -e "$key" ]]; then
    if [[ -e "$pub" ]]; then
      rm -f "$pub" # a public half without its key opens nothing
    fi
    ssh-keygen -q -t ed25519 -N '' -C "fbtunnel-$COLLECTOR_ID" -f "$key" </dev/null >/dev/null
    say "tunnel: new key $key (its public half goes to the VPS: tunnel-key)"
  fi
  chmod 600 "$key"
  if [[ ! -e "$pub" ]]; then
    tmp="$(mktemp "$SSH_DIR/.fbtunnel_pub.XXXXXX")"
    ssh-keygen -y -f "$key" </dev/null >"$tmp" || {
      rm -f "$tmp"
      die "$key cannot be read by ssh-keygen; nothing replaced it (move it away by hand)"
    }
    mv -f "$tmp" "$pub"
    say "tunnel: $pub made again from the key"
  fi
  chmod 644 "$pub"
}

# Cut the tunnel's log to its last 1000 lines once it passes 1 MiB, in place: the running ssh
# keeps writing to the same file (launchd opened it for appending).
trim_tunnel_log() {
  local size tmp
  [[ -f "$TUNNEL_LOG" && ! -L "$TUNNEL_LOG" ]] || return 0
  size="$(stat -f %z "$TUNNEL_LOG")"
  ((size > TUNNEL_LOG_MAX)) || return 0
  tmp="$(mktemp "$HOME/Library/Logs/.fbtunnel.XXXXXX")"
  tail -n 1000 "$TUNNEL_LOG" >"$tmp"
  cat "$tmp" >"$TUNNEL_LOG"
  rm -f "$tmp"
  say "tunnel log: cut to its last 1000 lines ($size bytes before)"
}

install_tunnel() {
  local config_template="$CHECKOUT/deploy/ssh/fbtunnel_client.config"
  local plist_template="$CHECKOUT/deploy/macos/$TUNNEL_LABEL.plist"
  local tmp resolved domain changed_config=0 changed_plist=0
  step "the tunnel to the brain"
  [[ -f "$config_template" && -f "$plist_template" ]] \
    || die "the checkout has no tunnel files yet: run this script with --update first"
  case "$HOME" in
    *'|'* | *'&'* | *\\*) die "the home path holds a character sed cannot take" ;;
  esac
  ssh_dir
  tunnel_key
  if [[ ! -e "$TUNNEL_PIN" && ! -L "$TUNNEL_PIN" ]]; then
    say "tunnel: key ready, waiting for the host key (RUNBOOK section 9: tunnel-pin)"
    say "tunnel key: $(fingerprint "$TUNNEL_KEY.pub")"
    return 0
  fi
  if ! read_pin_file; then
    say "WARNING: $TUNNEL_PIN is not one ed25519 host key line; the tunnel is left as it is."
    say "  Check it, delete it by hand in this account, and pin the host key again (tunnel-pin)."
    return 0
  fi

  # The ssh config, from the pin: rendered, checked offline (ssh -G never connects), replaced
  # only when it changed.
  tmp="$(mktemp "$SSH_DIR/.fbtunnel_client.XXXXXX")"
  sed -e "s|VPS_HOST|$pin_host|" \
    -e "s|^\([[:space:]]*Port[[:space:]][[:space:]]*\)22\$|\1$pin_port|" \
    -e "s|~/\.ssh/|$SSH_DIR/|g" -e "s|%d/\.ssh/|$SSH_DIR/|g" "$config_template" >"$tmp"
  chmod 600 "$tmp"
  resolved="$(ssh -G -F "$tmp" fbtunnel </dev/null 2>/dev/null)" || resolved=""
  resolved=$'\n'"$resolved"$'\n'
  if [[ "$resolved" != *$'\n'"host fbtunnel"$'\n'* ]] \
    || [[ "$resolved" != *$'\n'"hostname $pin_host"$'\n'* ]] \
    || [[ "$resolved" != *$'\n'"port $pin_port"$'\n'* ]] \
    || [[ "$resolved" != *$'\n'"localforward $TUNNEL_SOCKET [127.0.0.1]:8781"$'\n'* ]] \
    || [[ "$resolved" != *$'\n'"streamlocalbindmask 0177"$'\n'* ]] \
    || [[ "$resolved" != *$'\n'"streamlocalbindunlink yes"$'\n'* ]] \
    || [[ "$resolved" != *$'\n'"userknownhostsfile $TUNNEL_PIN"$'\n'* ]]; then
    rm -f "$tmp"
    die "the tunnel's ssh config does not check out (ssh -G -F <config> fbtunnel)"
  fi
  if cmp -s "$tmp" "$TUNNEL_CONFIG"; then
    rm -f "$tmp"
  else
    mv -f "$tmp" "$TUNNEL_CONFIG"
    changed_config=1
    say "tunnel: $TUNNEL_CONFIG written (to $pin_host, port $pin_port)"
  fi

  # The LaunchAgent.
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  tmp="$(mktemp "$HOME/Library/LaunchAgents/.$TUNNEL_LABEL.XXXXXX")"
  sed -e "s|__HOME__|$HOME|g" "$plist_template" >"$tmp"
  plutil -lint "$tmp" >/dev/null || {
    rm -f "$tmp"
    die "the tunnel's LaunchAgent does not lint"
  }
  chmod 644 "$tmp"
  if cmp -s "$tmp" "$TUNNEL_AGENT"; then
    rm -f "$tmp"
  else
    mv -f "$tmp" "$TUNNEL_AGENT"
    changed_plist=1
    say "tunnel: $TUNNEL_AGENT written"
  fi

  trim_tunnel_log
  domain="gui/$(id -u)"
  if ! agent_loaded "$TUNNEL_LABEL"; then
    launchctl bootstrap "$domain" "$TUNNEL_AGENT"
    say "tunnel: started"
  elif [[ "$changed_plist" -eq 1 ]]; then
    launchctl bootout "$domain/$TUNNEL_LABEL" || true
    wait_unloaded "$TUNNEL_LABEL" || die "the old tunnel did not stop; run this again"
    launchctl bootstrap "$domain" "$TUNNEL_AGENT"
    say "tunnel: started again with the new LaunchAgent"
  elif [[ "$changed_config" -eq 1 ]]; then
    launchctl kickstart -k "$domain/$TUNNEL_LABEL"
    say "tunnel: restarted with the new config"
  else
    say "tunnel: running, nothing changed"
  fi
  say "tunnel key: $(fingerprint "$TUNNEL_KEY.pub")"
  say "host key:   $(fingerprint "$TUNNEL_PIN")"
}

# --pin-host-key: the VPS's ed25519 host key, from the owner's own long-trusted ssh session to
# it (RUNBOOK section 9). A first pin, or the very same line again; never a different one.
pin_host_key() {
  local line="" extra="" tmp
  if [[ -t 0 ]]; then
    read -r -p "The VPS's host key line (<host> ssh-ed25519 AAAA...): " line || true
  else
    IFS= read -r line || [[ -n "$line" ]] || die "no host key line on stdin"
    if IFS= read -r extra || [[ -n "$extra" ]]; then
      die "more than one line on stdin: give exactly one host key line"
    fi
  fi
  parse_pin "$line" || die "not a host key line: expected '<host> ssh-ed25519 <key>' or" \
    "'[<host>]:<port> ssh-ed25519 <key>' (a lower-case host name or IPv4 address, an ed25519 key)"
  ssh_dir
  if [[ -e "$TUNNEL_PIN" || -L "$TUNNEL_PIN" ]]; then
    local wanted="$pin_line"
    if read_pin_file && [[ "$pin_line" == "$wanted" ]]; then
      say "host key: already pinned, the same line"
      return 0
    fi
    die "another host key is pinned already ($(fingerprint "$TUNNEL_PIN")); nothing changed." \
      "A changed host key is a security event: compare it on the VPS's console. If it really" \
      "changed, delete $TUNNEL_PIN by hand in this account and pin again."
  fi
  tmp="$(mktemp "$SSH_DIR/.fbtunnel_known_hosts.XXXXXX")"
  printf '%s\n' "$pin_line" >"$tmp"
  chmod 600 "$tmp"
  ssh-keygen -l -f "$tmp" </dev/null >/dev/null 2>&1 || {
    rm -f "$tmp"
    die "ssh-keygen does not read that host key"
  }
  mv -f "$tmp" "$TUNNEL_PIN"
  say "host key: pinned for $pin_host, port $pin_port ($(fingerprint "$TUNNEL_PIN"))"
}

# -- the switch between local mode and the brain (RUNBOOK section 9) ------------------------------

need_installed() {
  [[ -x "$CHECKOUT/.venv/bin/fbcollector" && -f "$ROOT/secrets" && ! -L "$ROOT/secrets" ]] \
    || die "the collector is not set up in this account yet: run this script without options first"
}

# check_mode <uplink|local>: `fbcollector check-config` must say that mode and ok. Its output
# names settings, never their values.
check_mode() {
  local out
  out="$("$CHECKOUT/.venv/bin/fbcollector" check-config 2>&1)" || {
    printf '%s\n' "$out" | sed 's/^/  /'
    return 1
  }
  printf '%s\n' "$out" | sed 's/^/  /'
  [[ $'\n'"$out"$'\n' == *$'\n'"mode: $1"* && $'\n'"$out"$'\n' == *$'\n'ok$'\n'* ]]
}

# switch_secrets <mode> -- <line>...: the uplink settings replaced by the lines, checked by
# check-config; on refusal the file is put back as it was.
switch_secrets() {
  local want="$1" backup
  shift 2
  backup="$(mktemp "$ROOT/.secrets.before.XXXXXX")"
  cat "$ROOT/secrets" >"$backup"
  # shellcheck disable=SC2086 # UPLINK_KEYS is a fixed list of names
  merge_secrets $UPLINK_KEYS -- ${1+"$@"}
  say "fbcollector check-config:"
  if ! check_mode "$want"; then
    mv -f "$backup" "$ROOT/secrets"
    die "check-config does not say mode: $want and ok; the secrets file is as it was"
  fi
  rm -f "$backup"
}

restart_collector() { # restart_collector <mode>
  if agent_loaded; then
    launchctl kickstart -k "gui/$(id -u)/$LABEL"
    say "collector: restarted, in $1 mode"
  else
    say "collector: not running; it starts in $1 mode when it is started"
  fi
}

# --uplink: the brain's ingest token from the terminal (not shown) or one line on stdin. It is
# never printed, never in a command's arguments (builtins only), and only its shape is checked.
switch_uplink() {
  local token="" extra=""
  agent_loaded "$TUNNEL_LABEL" || die "the tunnel's LaunchAgent is not loaded: pin the VPS's" \
    "host key first (tunnel-pin) and check that \`tunnel\` probes 401; nothing changed"
  need_installed
  if [[ -t 0 ]]; then
    read -r -s -p "The brain's ingest token (INGEST_TOKEN, not shown): " token || true
    printf '\n'
  else
    IFS= read -r token || [[ -n "$token" ]] || die "no token on stdin; nothing changed"
    if IFS= read -r extra || [[ -n "$extra" ]]; then
      token="" extra=""
      die "more than one line on stdin: give the token alone; nothing changed"
    fi
  fi
  if ! [[ "$token" =~ ^[A-Za-z0-9._~+/=-]+$ ]] || ((${#token} < 24 || ${#token} > 512)); then
    token=""
    die "that is not an ingest token (24 to 512 of A-Z a-z 0-9 . _ ~ + / = -); nothing changed"
  fi
  step "switching the collector to the brain"
  switch_secrets uplink -- "FBC_INGEST_URL=$INGEST_URL" "FBC_INGEST_SOCKET=$TUNNEL_SOCKET" \
    "FBC_INGEST_TOKEN='$token'" "FBC_COLLECTOR_ID=$COLLECTOR_ID"
  token=""
  say "secrets: FBC_INGEST_URL=$INGEST_URL, FBC_INGEST_SOCKET=$TUNNEL_SOCKET," \
    "FBC_INGEST_TOKEN set, FBC_COLLECTOR_ID=$COLLECTOR_ID"
  restart_collector uplink
}

# --local: back to phase 0's local mode, the uplink settings removed.
switch_local() {
  need_installed
  step "switching the collector back to local mode"
  switch_secrets local --
  say "secrets: FBC_INGEST_URL, FBC_INGEST_SOCKET, FBC_INGEST_TOKEN and FBC_COLLECTOR_ID removed"
  restart_collector local
}

# -- main ---------------------------------------------------------------------------------------

need_checkout() {
  [[ -d "$CHECKOUT/.git" ]] \
    || die "no checkout at $CHECKOUT yet: run this script without options first"
}

case "$mode" in
  tunnel | pin)
    check_account
    check_sums
    need_checkout
    if [[ "$mode" == pin ]]; then
      pin_host_key
    fi
    install_tunnel
    say "done."
    exit 0
    ;;
  uplink | local)
    check_account
    check_sums
    if [[ "$mode" == uplink ]]; then
      switch_uplink
    else
      switch_local
    fi
    say "done."
    exit 0
    ;;
esac

check_account
check_sums
check_tools
was_running=0
if [[ "$mode" == update ]] && agent_loaded; then
  step "stopping the running collector for the update"
  launchctl bootout "gui/$(id -u)/$LABEL" || true
  was_running=1
  # bootout returns while the collector is still shutting down (closing Chrome may take up to
  # its 90 s ExitTimeOut); bootstrapping before it is gone fails with "5: Input/output error".
  wait_unloaded || true
fi
install_uv
checkout
install_collector
make_root
check_zone
write_secrets
copy_monitored
install_agent
refresh_operator_door
add_to_path
next_steps
if [[ "$was_running" -eq 1 ]]; then
  if wait_unloaded; then
    launchctl bootstrap "gui/$(id -u)" "$AGENT"
    say "collector started again"
  else
    say "the old collector did not stop in time; start it with: launchctl bootstrap gui/\$(id -u) $AGENT"
  fi
fi
# The tunnel last, in a subshell of its own (errexit on inside it, so any failing command ends
# it): its failure must not leave the collector this update stopped unstarted, nor the old door.
set +e
(
  set -e
  install_tunnel
)
tunnel_status=$?
set -e
if [[ "$tunnel_status" -ne 0 ]]; then
  say "WARNING: the tunnel step failed (above); everything else is done. Fix the cause and run" \
    "this again (the door: update; tunnel and tunnel-log show the tunnel)."
  exit 1
fi
say "done."
