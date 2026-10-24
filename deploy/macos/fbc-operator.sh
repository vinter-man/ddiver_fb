#!/bin/bash
# Forced command for the operator's ssh key into the collector's macOS account (RUNBOOK §12).
#
# The key is installed in ~fbc/.ssh/authorized_keys as
#   restrict,from="127.0.0.1,::1",command="/Users/fbc/bin/fbc-operator.sh" ssh-ed25519 ... operator
# so it works only from this Mac itself and can run nothing but the verbs below. The verbs are
# the ones an operator needs to watch the collector, ship an update and keep the tunnel to the
# brain. Everything that acts on the Facebook account or overrides a safety decision stays with
# the owner at the keyboard, by design (DESIGN, invariants 3 and 7): login, resume, pause, group
# confirmation, pin-home. None of the tunnel's verbs reach Facebook.
#
# `status`, `report` and `log` show group ids (status also group names and quarantined post
# ids). `diagnostics` is the report's parser-diagnostics section alone: structure and counts,
# no ids. `dialogs` prints the texts
# of the dialogs on the last saved steps, which may quote a post or a name: the owner allowed it
# on 2026-09-30, to learn why a page was unrecognised without a trip to the fbc account.
#
# The tunnel and the switch to the brain (RUNBOOK §9): `tunnel`, `tunnel-log [N]`, `tunnel-key`
# (the tunnel's public key, for the VPS) and `uplink` (`fbcollector uplink`: no ids, no text, no
# token) only read; `tunnel-restart` and `tunnel-stop` act on the tunnel's LaunchAgent alone;
# `tunnel-pin`, `uplink-token` and `uplink-off` run the setup script, which takes the host key
# line or the ingest token on stdin, checks its shape and never prints the token.
set -euo pipefail

PATH="${PATH:-/usr/bin:/bin}:/usr/sbin:/sbin"
FBC="$HOME/ddiver_facebook/.venv/bin/fbcollector"
LOG="$HOME/Library/Logs/fbcollector.log"
LABEL="com.ddiver.fbcollector"
TUNNEL_LABEL="com.ddiver.fbtunnel"
TUNNEL_LOG="$HOME/Library/Logs/fbtunnel.log"
TUNNEL_KEY="$HOME/.ssh/fbtunnel_ed25519.pub"
TUNNEL_PIN="$HOME/.ssh/fbtunnel_known_hosts"
TUNNEL_SOCKET="$HOME/.ssh/fbtunnel.sock"
PROBE="http://127.0.0.1:8781/v1/control"
SETUP="${FBC_SHARED_DIR:-/Users/Shared/ddiver-fb}/setup-fbc-user.sh"
VERBS="status | report | diagnostics | dialogs [N] | log [N] | agent | restart | update"
VERBS="$VERBS | tunnel | tunnel-log [N] | tunnel-key | tunnel-restart | tunnel-stop | tunnel-pin"
VERBS="$VERBS | uplink | uplink-token | uplink-off"

read -r -a argv <<<"${SSH_ORIGINAL_COMMAND:-}"
verb="${argv[0]:-}"

agent_state() { # agent_state <label>: launchd's state, pid, last exit code and runs of it
	local label="$1" printed
	if printed="$(launchctl print "gui/$(id -u)/$label" 2>/dev/null)"; then
		printf '%s\n' "$printed" | grep -E '^[[:space:]]*(state|pid|last exit code|runs) =' || true
	else
		echo "	not loaded"
	fi
}

line_count() { # line_count <verb> <word>: a line count up to 999 (100 when not given)
	local lines="${2:-100}"
	[[ $lines =~ ^[0-9]{1,3}$ ]] || {
		echo "$1 takes a line count up to 999" >&2
		exit 2
	}
	echo "$lines"
}

fingerprint() { # fingerprint <file>
	if [[ -f "$1" ]]; then
		ssh-keygen -l -f "$1" </dev/null 2>/dev/null || echo "unreadable"
	else
		echo "none"
	fi
}

case "$verb" in
	status)
		exec "$FBC" status
		;;
	report)
		exec "$FBC" report
		;;
	diagnostics)
		exec "$FBC" report --diagnostics-only
		;;
	dialogs)
		last="${argv[1]:-4}"
		if ! [[ $last =~ ^[0-9]{1,2}$ ]] || ((10#$last < 1 || 10#$last > 20)); then
			echo "dialogs takes a step count from 1 to 20" >&2
			exit 2
		fi
		exec "$FBC" dialogs --last "$((10#$last))"
		;;
	log)
		lines="$(line_count log "${argv[1]:-}")"
		exec tail -n "$lines" "$LOG"
		;;
	agent)
		for label in "$LABEL" "$TUNNEL_LABEL"; do
			echo "$label:"
			agent_state "$label"
		done
		;;
	restart)
		# Restart a loaded collector; load it if it is not (after an update that found it gone).
		if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
			exec launchctl kickstart -k "gui/$(id -u)/$LABEL"
		fi
		exec launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$LABEL.plist"
		;;
	update)
		exec bash "$SETUP" --update
		;;
	tunnel)
		echo "agent $TUNNEL_LABEL:"
		agent_state "$TUNNEL_LABEL"
		# The tunnel's local end: ssh's Unix socket in this account's own 0700 ~/.ssh.
		echo "socket $TUNNEL_SOCKET:"
		if [[ -S $TUNNEL_SOCKET ]]; then
			stat -f '	%Sp %Su, since %Sm' "$TUNNEL_SOCKET"
		else
			echo "	none (ssh is not connected)"
		fi
		echo "tunnel key: $(fingerprint "$TUNNEL_KEY")"
		echo "host key:   $(fingerprint "$TUNNEL_PIN")"
		echo "log, last 5 lines:"
		tail -n 5 "$TUNNEL_LOG" 2>/dev/null || echo "	no log yet"
		# Without the token: 401 means the brain itself answered through the tunnel; 000 means
		# no answer. Each probe adds one to the brain's count of unauthorized requests.
		code="$(curl -q -s --noproxy '*' -o /dev/null -m 5 -w '%{http_code}' \
			--unix-socket "$TUNNEL_SOCKET" "$PROBE" || true)"
		[[ $code =~ ^[0-9]{3}$ ]] || code=000
		echo "probe of /v1/control without the token: HTTP $code"
		;;
	tunnel-log)
		lines="$(line_count tunnel-log "${argv[1]:-}")"
		exec tail -n "$lines" "$TUNNEL_LOG"
		;;
	tunnel-key)
		# A public key; its shape is checked so that nothing else of this account leaves by it.
		if ! key="$(cat "$TUNNEL_KEY" 2>/dev/null)"; then
			echo "no tunnel key yet: run update" >&2
			exit 1
		fi
		if ! [[ $key =~ ^ssh-ed25519\ AAAAC3NzaC1lZDI1NTE5AAAAI[A-Za-z0-9+/]{43}\ fbtunnel-[A-Za-z0-9._-]{1,64}$ ]]; then
			echo "refused: $TUNNEL_KEY is not one ed25519 public key line of the tunnel" >&2
			exit 1
		fi
		printf '%s\n' "$key"
		;;
	tunnel-restart)
		# Restart a loaded tunnel; load it if it is not (after tunnel-stop).
		if launchctl print "gui/$(id -u)/$TUNNEL_LABEL" >/dev/null 2>&1; then
			exec launchctl kickstart -k "gui/$(id -u)/$TUNNEL_LABEL"
		fi
		if [[ ! -f "$HOME/Library/LaunchAgents/$TUNNEL_LABEL.plist" ]]; then
			echo "no tunnel LaunchAgent yet: pin the VPS's host key first (tunnel-pin)" >&2
			exit 1
		fi
		exec launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$TUNNEL_LABEL.plist"
		;;
	tunnel-stop)
		# Stops a retry loop before the VPS's fail2ban bans the home IP. It stays stopped until
		# tunnel-restart, an update, or the account's next login.
		exec launchctl bootout "gui/$(id -u)/$TUNNEL_LABEL"
		;;
	tunnel-pin)
		exec bash "$SETUP" --pin-host-key
		;;
	uplink)
		exec "$FBC" uplink
		;;
	uplink-token)
		exec bash "$SETUP" --uplink
		;;
	uplink-off)
		exec bash "$SETUP" --local
		;;
	"")
		echo "verbs: $VERBS" >&2
		exit 2
		;;
	login | resume | pause | groups | pin-home | selftest)
		echo "refused: '$verb' is the owner's to run at the keyboard in the fbc account" >&2
		exit 3
		;;
	*)
		echo "refused: unknown verb '$verb' ($VERBS)" >&2
		exit 3
		;;
esac
