#!/bin/bash
# The VPS's side of the collector's tunnel (DESIGN D6 and Operations; docs/RUNBOOK.md, section 9).
#
# Run as root on the VPS, from the checkout in /srv/ddiver-fb; safe to repeat:
#
#   sudo bash deploy/vps/setup-fbtunnel.sh                              # user and sshd only
#   sudo bash deploy/vps/setup-fbtunnel.sh --key /tmp/fbtunnel-mac-1.pub   # and the Mac's key
#
# What it does, each step only when needed:
#   1. creates the system user fbtunnel: no home, /usr/sbin/nologin, password `*` (no password
#      opens it; useradd's own `!` would lock it out of ssh keys too);
#   2. installs deploy/vps/20-fbtunnel.conf as /etc/ssh/sshd_config.d/20-fbtunnel.conf (0644),
#      after checking that sshd_config includes sshd_config.d/*.conf;
#   3. creates /etc/ssh/fbtunnel_keys, root's, 0644 (fbtunnel can read it, never change it);
#   4. with --key FILE: one ed25519 public key line whose comment is fbtunnel-<collector id>
#      (the operator door's `tunnel-key`), written as
#        restrict,port-forwarding,permitopen="127.0.0.1:8781" ssh-ed25519 AAAA... fbtunnel-mac-1
#      in place of any line with the same comment or the same key: the options are this
#      script's, never the collector's;
#   5. checks the configuration: `sshd -t`, then `sshd -T` for fbtunnel must show the Match
#      block (one local forward to 127.0.0.1:8781, no sessions, the keys file, the keepalives)
#      and for another user (the one behind sudo) must not;
#   6. refuses an AllowUsers or AllowGroups that leaves fbtunnel out (or a DenyUsers or
#      DenyGroups that names it): sshd would refuse the key without saying why to the Mac;
#   7. reloads sshd (the unit ssh, or sshd);
#   8. prints the host's ed25519 key and fingerprint, to compare with the Mac's pin, and the
#      sshd jail of fail2ban against the Mac's retry pace.
#
# --root DIR reads and writes every file under DIR instead of / (the tests); the commands it
# runs are the system's either way.
set -euo pipefail

readonly TUNNEL_USER=fbtunnel
readonly PERMIT='restrict,port-forwarding,permitopen="127.0.0.1:8781"'
readonly KEY_SHAPE='^ssh-ed25519 (AAAAC3NzaC1lZDI1NTE5AAAAI[A-Za-z0-9+/]{43}) (fbtunnel-[A-Za-z0-9._-]{1,64})$'
readonly THROTTLE=200 # the Mac's ThrottleInterval (deploy/macos/com.ddiver.fbtunnel.plist)
HERE="$(cd "$(dirname "$0")" && pwd)"
readonly HERE

prefix=""
key_file=""

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die() {
  printf 'setup-fbtunnel: %s\n' "$*" >&2
  exit 1
}

usage() {
  sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --key)
      [[ $# -ge 2 ]] || usage 2 >&2
      key_file="$2"
      shift 2
      ;;
    --root)
      [[ $# -ge 2 ]] || usage 2 >&2
      prefix="${2%/}"
      shift 2
      ;;
    -h | --help) usage 0 ;;
    *) usage 2 >&2 ;;
  esac
done

readonly SSH_ETC="$prefix/etc/ssh"
readonly SNIPPET_DIR="$SSH_ETC/sshd_config.d"
readonly SNIPPET="$SNIPPET_DIR/20-fbtunnel.conf"
readonly KEYS="$SSH_ETC/fbtunnel_keys"
readonly HOST_KEY="$SSH_ETC/ssh_host_ed25519_key.pub"

root_file() { # root_file <path> <mode>: root's, with that mode
  chown root:root "$1"
  chmod "$2" "$1"
}

has_line() { # has_line <text> <line>: the text holds that whole line
  [[ $'\n'"$1"$'\n' == *$'\n'"$2"$'\n'* ]]
}

# -- 1. the user ----------------------------------------------------------------------------------

make_user() {
  if id -u "$TUNNEL_USER" >/dev/null 2>&1; then
    say "user: $TUNNEL_USER exists"
  else
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin \
      "$TUNNEL_USER"
    say "user: $TUNNEL_USER created"
  fi
  usermod --shell /usr/sbin/nologin --password '*' "$TUNNEL_USER"
}

# -- 2. and 3. sshd's files -----------------------------------------------------------------------

install_snippet() {
  local tmp
  [[ -f "$HERE/20-fbtunnel.conf" ]] || die "no 20-fbtunnel.conf next to this script"
  grep -Eiq '^[[:space:]]*Include[[:space:]]+([^[:space:]]*/)?sshd_config\.d/\*\.conf([[:space:]]|$)' \
    "$SSH_ETC/sshd_config" \
    || die "$SSH_ETC/sshd_config does not include sshd_config.d/*.conf: add" \
      "'Include /etc/ssh/sshd_config.d/*.conf' at its top, then run this again"
  [[ ! -L "$SNIPPET_DIR" && ! -L "$SNIPPET" ]] || die "$SNIPPET or its directory is a link"
  mkdir -p "$SNIPPET_DIR"
  if cmp -s "$HERE/20-fbtunnel.conf" "$SNIPPET"; then
    say "sshd: $SNIPPET unchanged"
    return
  fi
  tmp="$(mktemp "$SNIPPET_DIR/.20-fbtunnel.XXXXXX")" # not *.conf: sshd never reads it
  cat "$HERE/20-fbtunnel.conf" >"$tmp"
  root_file "$tmp" 644
  mv -f "$tmp" "$SNIPPET"
  say "sshd: $SNIPPET installed"
}

keys_file() {
  [[ ! -L "$KEYS" ]] || die "$KEYS is a symbolic link"
  if [[ ! -e "$KEYS" ]]; then
    : >"$KEYS"
    say "keys: $KEYS created"
  fi
  [[ -f "$KEYS" ]] || die "$KEYS is not a plain file"
  root_file "$KEYS" 644
}

# -- 4. the collector's key -----------------------------------------------------------------------

add_key() {
  local text blob comment line tmp
  [[ -f "$key_file" && ! -L "$key_file" ]] || die "--key $key_file: not a plain file"
  text="$(cat "$key_file")"
  [[ "$text" =~ $KEY_SHAPE ]] \
    || die "--key $key_file: not one 'ssh-ed25519 AAAA... fbtunnel-<id>' line (the door's tunnel-key)"
  blob="${BASH_REMATCH[1]}"
  comment="${BASH_REMATCH[2]}"
  tmp="$(mktemp "$SSH_ETC/.fbtunnel_keys.XXXXXX")"
  while IFS= read -r line || [[ -n "$line" ]]; do
    # The same collector (comment) or the same key under any name: replaced, never doubled.
    if [[ " $line " == *" $blob "* || "$line" == *" $comment" ]]; then
      continue
    fi
    printf '%s\n' "$line"
  done <"$KEYS" >"$tmp"
  printf '%s ssh-ed25519 %s %s\n' "$PERMIT" "$blob" "$comment" >>"$tmp"
  if cmp -s "$tmp" "$KEYS"; then
    rm -f "$tmp"
    say "keys: $comment already allowed, unchanged"
    return
  fi
  root_file "$tmp" 644
  mv -f "$tmp" "$KEYS"
  say "keys: $comment allowed one forward to 127.0.0.1:8781 ($PERMIT)"
}

# -- 5. and 6. what sshd makes of it --------------------------------------------------------------

# matches <word> <pattern>...: sshd's user and group patterns (* and ?; a negation never
# admits; user@host takes the user part).
matches() {
  local word="$1" pattern
  shift
  for pattern in "$@"; do
    pattern="${pattern%%@*}"
    [[ "$pattern" == '!'* ]] && continue
    # shellcheck disable=SC2053 # a glob on purpose: sshd's patterns
    [[ "$word" == $pattern ]] && return 0
  done
  return 1
}

values() { # values <sshd -T output> <keyword>: that keyword's values, one per line
  printf '%s\n' "$1" | sed -n "s/^$2 //p"
}

check_sshd() {
  local tunnel other seen want groups group allowed
  local -a list
  sshd -t || die "sshd -t refuses the configuration; nothing was reloaded"
  tunnel="$(sshd -T -C "user=$TUNNEL_USER,host=fbtunnel-check,addr=192.0.2.1")" \
    || die "sshd -T for $TUNNEL_USER failed"
  for want in "allowtcpforwarding local" "permitopen 127.0.0.1:8781" "permitlisten none" \
    "allowstreamlocalforwarding no" "x11forwarding no" "allowagentforwarding no" \
    "permittty no" "forcecommand /bin/false" "authorizedkeysfile /etc/ssh/fbtunnel_keys" \
    "passwordauthentication no" "maxsessions 0" "clientaliveinterval 30" \
    "clientalivecountmax 4"; do
    has_line "$tunnel" "$want" || die "sshd -T for $TUNNEL_USER does not show '$want'" \
      "(another file in sshd_config.d set it first?)"
  done
  other="${SUDO_USER:-root}"
  [[ "$other" != "$TUNNEL_USER" ]] || other=root
  seen="$(sshd -T -C "user=$other,host=fbtunnel-check,addr=192.0.2.1")" \
    || die "sshd -T for $other failed"
  for want in "forcecommand /bin/false" "authorizedkeysfile /etc/ssh/fbtunnel_keys" \
    "permitopen 127.0.0.1:8781" "maxsessions 0"; do
    if has_line "$seen" "$want"; then
      die "the Match block leaks: sshd -T for $other shows '$want' as well"
    fi
  done
  say "sshd: $TUNNEL_USER may forward 127.0.0.1:8781 and nothing else; $other is not affected"

  list=()
  while IFS= read -r allowed; do
    [[ -n "$allowed" ]] && list+=("$allowed")
  done < <(values "$tunnel" allowusers)
  if [[ ${#list[@]} -gt 0 ]] && ! matches "$TUNNEL_USER" "${list[@]}"; then
    die "sshd's AllowUsers leaves $TUNNEL_USER out: add it there (or a Match of its own)"
  fi
  list=()
  while IFS= read -r allowed; do
    [[ -n "$allowed" ]] && list+=("$allowed")
  done < <(values "$tunnel" denyusers)
  if [[ ${#list[@]} -gt 0 ]] && matches "$TUNNEL_USER" "${list[@]}"; then
    die "sshd's DenyUsers names $TUNNEL_USER"
  fi
  groups="$(id -Gn "$TUNNEL_USER")" || die "the groups of $TUNNEL_USER cannot be read"
  list=()
  while IFS= read -r allowed; do
    [[ -n "$allowed" ]] && list+=("$allowed")
  done < <(values "$tunnel" allowgroups)
  if [[ ${#list[@]} -gt 0 ]]; then
    seen=no
    for group in $groups; do
      if matches "$group" "${list[@]}"; then
        seen=yes
      fi
    done
    [[ "$seen" == yes ]] || die "sshd's AllowGroups leaves $TUNNEL_USER (groups: $groups) out"
  fi
  list=()
  while IFS= read -r allowed; do
    [[ -n "$allowed" ]] && list+=("$allowed")
  done < <(values "$tunnel" denygroups)
  if [[ ${#list[@]} -gt 0 ]]; then
    for group in $groups; do
      if matches "$group" "${list[@]}"; then
        die "sshd's DenyGroups names $group, a group of $TUNNEL_USER"
      fi
    done
  fi
}

# -- 7. reload ------------------------------------------------------------------------------------

reload_sshd() {
  local unit=""
  for unit in ssh sshd ""; do
    [[ -n "$unit" ]] || die "neither ssh.service nor sshd.service is known to systemctl"
    systemctl cat "$unit.service" >/dev/null 2>&1 && break
  done
  if systemctl is-active --quiet "$unit.service"; then
    systemctl reload "$unit.service"
    say "sshd: $unit.service reloaded (open sessions keep running)"
  else
    say "sshd: $unit.service is not running (socket-activated?): the next connection reads the" \
      "new configuration"
  fi
}

# -- 8. for the Mac -------------------------------------------------------------------------------

report() {
  local type blob maxretry findtime attempts
  step "for the Mac"
  if [[ -f "$HOST_KEY" ]]; then
    read -r type blob _ <"$HOST_KEY"
    say "this host's key, for the Mac's pin (RUNBOOK section 9): $type $blob"
    say "fingerprint: $(ssh-keygen -l -f "$HOST_KEY" </dev/null)"
  else
    say "WARNING: no $HOST_KEY: the Mac's tunnel accepts an ed25519 host key only"
  fi
  if command -v fail2ban-client >/dev/null 2>&1 \
    && maxretry="$(fail2ban-client get sshd maxretry 2>/dev/null)" \
    && findtime="$(fail2ban-client get sshd findtime 2>/dev/null)"; then
    if [[ "$maxretry" =~ ^[0-9]+$ && "$findtime" =~ ^[0-9]+$ ]]; then
      attempts=$((findtime / THROTTLE + 1)) # a failure at each end of the window counts
      say "fail2ban sshd jail: maxretry $maxretry within findtime $findtime s."
      say "  The Mac retries a refused key or host key at most once every $THROTTLE s: at most"
      if ((attempts < maxretry)); then
        say "  $attempts attempts within findtime, under maxretry: a broken key cannot ban the home IP."
      else
        say "  $attempts attempts within findtime: NOT under maxretry $maxretry. Raise maxretry or"
        say "  findtime's ratio, or add the home IP to ignoreip, before the Mac's key goes in."
      fi
    else
      say "fail2ban sshd jail: maxretry '$maxretry', findtime '$findtime': check the Mac's"
      say "  $THROTTLE s retry pace against them by hand."
    fi
  else
    say "fail2ban: no sshd jail here (nothing to check against the Mac's retry pace)"
  fi
}

# -- main -----------------------------------------------------------------------------------------

[[ "$(id -u)" -eq 0 ]] || die "run it as root: sudo bash $0 ..."
step "the tunnel user and sshd"
make_user
install_snippet
keys_file
if [[ -n "$key_file" ]]; then
  add_key
fi
check_sshd
reload_sshd
report
say "done."
