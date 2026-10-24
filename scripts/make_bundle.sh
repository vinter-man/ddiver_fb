#!/bin/bash
# Hand the code to the collector's own macOS user (docs/RUNBOOK.md, section 12).
#
# Run by the owner's normal account, from anywhere inside the checkout:
#
#   scripts/make_bundle.sh [OUT_DIR]        # default: /Users/Shared/ddiver-fb ($FBC_SHARED_DIR)
#
# OUT_DIR is made readable by the group `staff` (every local account, the collector's `fbc`
# included) and by nobody else, so it must be this script's own: the default, or a directory it
# creates (the parent must exist) or finds empty, and marks (.ddiver-fb-bundle). It refuses `/`,
# the top-level and system folders, /Users and every home in it, /Users/Shared itself, volumes,
# the temporary folder, your home and its folders, the checkout and everything above those;
# a symbolic link; a directory of someone else's; one inside a folder that another account
# could rename things in (not yours or root's, writable by others without the sticky bit, or
# an access control entry that lets others add or remove); one holding anything it did not
# write (bar the operator key fbc_local.pub of RUNBOOK section 12), and an output name that is
# not a plain file of yours. It changes nothing before all of that is checked. It writes:
#   ddiver_facebook.bundle         a git bundle of the branch `main`, nothing else
#   setup-fbc-user.sh              deploy/macos/setup-fbc-user.sh as `main` has it
#   uv                             this account's uv (or FBC_UV_BINARY), only if it is exactly the
#                                  build whose SHA-256 that setup script pins for this Mac
#   ddiver_facebook.bundle.sha256  the SHA-256 of all three
# Inside the collector's account, `bash /Users/Shared/ddiver-fb/setup-fbc-user.sh` checks the
# checksums before it clones or updates anything, and installs uv from here, never from the
# network.
#
# A bundle carries committed files only: no .env, no secrets, no profile, no state. The script
# still refuses a `main` that tracks anything that looks like one, or ever did: the bundle
# carries main's whole history, so a file committed once and deleted since would travel too.
# Two layers: every path of every commit by its name, then every blob of every commit (and
# every commit message) by its content, whatever its name (scripts/scan_secrets.py; reviewed
# known-safe fixtures are in scripts/secret_scan_allow.txt as main has it). main is looked up
# once; if it moves while the script runs, the bundle is refused.
set -euo pipefail

readonly BRANCH=main
readonly GROUP=staff
readonly NAME=ddiver_facebook.bundle
readonly SETUP=setup-fbc-user.sh
readonly UV_NAME=uv
readonly DEFAULT_OUT="${FBC_SHARED_DIR:-/Users/Shared/ddiver-fb}"
readonly MARKER=.ddiver-fb-bundle
readonly MARKER_TEXT="Written by ddiver_facebook's scripts/make_bundle.sh: the collector's bundle."
readonly OPERATOR_KEY=fbc_local.pub

die() {
  printf 'make_bundle: %s\n' "$*" >&2
  exit 1
}

[[ $# -le 1 ]] || die "usage: scripts/make_bundle.sh [OUT_DIR]"
out_dir="${1:-$DEFAULT_OUT}"
case "$out_dir" in
  /*) ;;
  *) die "OUT_DIR must be an absolute path" ;;
esac

repo="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)" \
  || die "not inside a git checkout"
# The commit of main, looked up once: both scans, the pins and the setup script read this very
# commit, and the bundle is refused unless main still points at it (a commit landing on main
# meanwhile would travel unscanned).
tip="$(git -C "$repo" rev-parse --verify --quiet "refs/heads/$BRANCH^{commit}")" \
  || die "no branch $BRANCH"

# -- where it goes: checked before anything else, changed only after every check -------------

physical() { # physical <existing directory>: its path with every symbolic link resolved
  (cd -P "$1" 2>/dev/null && pwd -P)
}

me="$(id -un)"
open_to_others() { # open_to_others <physical directory>: how an account other than yours and
  # root's could rename what is in it, or in a folder above it, or nothing. Such an account
  # could swap OUT_DIR for a symbolic link between its check and the chgrp/chmod that follow it.
  local d="$1" owner mode acl
  while :; do
    owner="$(stat -f %Su "$d")" || { echo "cannot read $d" && return; }
    if [[ "$owner" != "$me" && "$owner" != root ]]; then
      echo "$d belongs to $owner" && return
    fi
    mode="$(stat -f %Lp "$d")" || { echo "cannot read $d" && return; }
    if (((8#$mode & 8#022) != 0)) && [[ ! -k "$d" ]]; then
      echo "$d is writable by others and not sticky" && return
    fi
    acl="$(ls -lde "$d")" || { echo "cannot read $d" && return; }
    if [[ "$acl" == *$'\n'* ]]; then acl="${acl#*$'\n'}"; else acl=""; fi # its entries only
    if grep -Eq ' allow .*(add_file|add_subdirectory|delete_child)' <<<"$acl"; then
      echo "$d lets others add or remove what is in it (an access control entry)" && return
    fi
    [[ "$d" != / ]] || return 0
    d="${d%/*}"
    d="${d:-/}"
  done
}

why_not_here() { # why_not_here <absolute path>: why no bundle may take it over, or nothing
  local p="$1" h t r
  case "$p" in
    /) echo "the root of the disk" && return ;;
    /*/*) ;;
    *) echo "a top-level folder of the disk" && return ;;
  esac
  case "$p" in
    /System/* | /Library/* | /Applications/* | /usr/* | /bin/* | /sbin/* | /etc/* | /dev/* \
      | /opt/* | /cores/* | /private/etc/* | /private/var/db/* | /private/var/root/*)
      echo "inside a system folder" && return ;;
  esac
  case "$p" in
    /Users/*/* | /Volumes/*/* | /private/*/*) ;;
    /Users/*) echo "a home folder, or /Users/Shared itself" && return ;;
    /Volumes/*) echo "a whole volume" && return ;;
    /private/*) echo "a system folder" && return ;;
  esac
  for h in "${HOME%/}" "$(physical "$HOME" || true)"; do
    [[ -n "$h" ]] || continue
    if [[ "$p" == "$h" ]]; then echo "your home folder" && return; fi
    case "$h/" in "$p"/*) echo "a folder above your home" && return ;; esac
    if [[ "${p%/*}" == "$h" && -e "$p" ]]; then echo "a folder of your home" && return; fi
  done
  t="${TMPDIR:-}"
  for t in "${t%/}" "$(physical "${t:-/nonexistent}" || true)"; do
    [[ -n "$t" ]] || continue
    if [[ "$p" == "$t" ]]; then echo "the temporary folder" && return; fi
    case "$t/" in "$p"/*) echo "a folder above the temporary folder" && return ;; esac
  done
  for r in "$repo" "$(physical "$repo" || true)"; do
    [[ -n "$r" ]] || continue
    if [[ "$p" == "$r" ]]; then echo "the checkout itself" && return; fi
    case "$r/" in "$p"/*) echo "a folder above the checkout" && return ;; esac
    case "$p/" in "$r"/*) echo "inside the checkout" && return ;; esac
  done
  return 0
}

is_ours() { # is_ours <name>: a name this script writes (or a run that was killed left behind)
  case "$1" in
    "$NAME" | "$NAME.sha256" | "$SETUP" | "$UV_NAME" | "$MARKER" | "$OPERATOR_KEY") return 0 ;;
    .bundle.?????? | .bundle.??????.setup | .bundle.??????.uv | .bundle.??????.sha256) return 0 ;;
    .bundle.??????.marker) return 0 ;;
  esac
  return 1
}

while [[ "$out_dir" == ?*/ ]]; do out_dir="${out_dir%/}"; done
reason="$(why_not_here "$out_dir")"
[[ -z "$reason" ]] || die "refusing OUT_DIR $out_dir: $reason"
case "$out_dir/" in
  */./* | */../* | *//*) die "refusing OUT_DIR $out_dir: not a plain path (., .. or //)" ;;
esac
parent="${out_dir%/*}"
[[ -d "$parent" ]] || die "refusing OUT_DIR $out_dir: $parent does not exist"
parent="$(physical "$parent")" || die "refusing OUT_DIR $out_dir: cannot enter its parent"
reason="$(open_to_others "$parent")"
[[ -z "$reason" ]] || die "refusing OUT_DIR $out_dir: $reason"
out_real="${parent%/}/${out_dir##*/}"
reason="$(why_not_here "$out_real")"
[[ -z "$reason" ]] || die "refusing OUT_DIR $out_dir ($out_real): $reason"
default_real="$DEFAULT_OUT"
if [[ -d "${DEFAULT_OUT%/*}" ]]; then
  default_real="$(physical "${DEFAULT_OUT%/*}" || true)/${DEFAULT_OUT##*/}"
fi

existing=0
if [[ -L "$out_real" ]]; then
  die "refusing OUT_DIR $out_dir: it is a symbolic link"
elif [[ -e "$out_real" ]]; then
  [[ -d "$out_real" ]] || die "refusing OUT_DIR $out_dir: it is not a directory"
  [[ -O "$out_real" ]] \
    || die "refusing OUT_DIR $out_dir: it belongs to $(stat -f %Su "$out_real"), not to you"
  marked=0
  entries=0
  strangers=""
  for entry in "$out_real"/* "$out_real"/.[!.]* "$out_real"/..?*; do
    [[ -e "$entry" || -L "$entry" ]] || continue # a pattern that matched nothing
    entries=$((entries + 1))
    name="${entry##*/}"
    if ! is_ours "$name"; then
      strangers="$strangers $name"
      continue
    fi
    [[ -f "$entry" && ! -L "$entry" && -O "$entry" ]] \
      || die "refusing OUT_DIR $out_dir: $name is not a plain file of yours"
    if [[ "$name" == "$MARKER" ]]; then
      [[ "$(cat "$entry")" == "$MARKER_TEXT" ]] \
        || die "refusing OUT_DIR $out_dir: its $MARKER is not this script's"
      marked=1
    fi
  done
  [[ -z "$strangers" ]] \
    || die "refusing OUT_DIR $out_dir: it holds what this script did not write:$strangers"
  # Only the default may hold a bundle without the marker: the ones made before it existed.
  if [[ "$marked" -eq 0 && "$entries" -gt 0 && "$out_real" != "$default_real" ]]; then
    die "refusing OUT_DIR $out_dir: it is neither empty nor marked as this script's ($MARKER)"
  fi
  existing=1
fi

# Nothing that could be a secret may travel, even committed by mistake. The bundle packs every
# commit of main, so every path of every commit is checked, not only the tip's: a file added
# and deleted since, renamed away or brought in by a merge is still inside the bundle.
readonly SECRET_NAMES='(^|/)(\.env|secrets|monitored\.toml|\.forbidden|id_[a-z0-9]+|[Cc]ookies|storage_state\.json|quarantine-[^/]*\.json\.gz)$|\.(sqlite|sqlite3|pem|key|p12|session|har)$|\.step\.json\.gz$'
history_paths="$(git -C "$repo" rev-list "$tip" \
  | while read -r commit; do git -C "$repo" ls-tree -r -z --name-only "$commit" || exit 1; done \
  | tr '\0' '\n' | sort -u)" \
  || die "cannot list the files in $BRANCH's history"
suspicious="$(grep -E "$SECRET_NAMES" <<<"$history_paths" || true)"
if [[ -n "$suspicious" ]]; then
  die "refusing: $BRANCH has (or had, in its history) files that may hold secrets:" \
    "$(tr '\n' ' ' <<<"$suspicious")"
fi
# The content: the checkout's own interpreter, else python3 (the scan needs 3.9 and nothing
# else). It reports path, rule, line and commit on stderr, never the text it matched.
python="$repo/.venv/bin/python"
[[ -x "$python" ]] || python="$(command -v python3 || true)"
[[ -n "$python" ]] || die "no python3 for the secret scan"
[[ -f "$repo/scripts/scan_secrets.py" ]] || die "no scripts/scan_secrets.py in $repo"
scan=0
"$python" "$repo/scripts/scan_secrets.py" --repo "$repo" "$tip" || scan=$?
case "$scan" in
  0) ;;
  1) die "refusing: $BRANCH's history holds what may be secrets (listed above)" ;;
  *) die "the secret scan could not run (above)" ;;
esac

sha256() { # sha256 <file>: its SHA-256, hex
  local sum
  sum="$(shasum -a 256 <"$1")" || return 1
  printf '%s\n' "${sum%% *}"
}

# The collector's account installs the uv that travels here, nothing downloaded: exactly the
# build the setup script (as main has it) pins for this Mac's architecture.
setup_text="$(git -C "$repo" show "$tip:deploy/macos/$SETUP")" \
  || die "$BRANCH has no deploy/macos/$SETUP"
pinned() { sed -n "s/^readonly $1=\([0-9a-f]\{64\}\)\$/\1/p" <<<"$setup_text"; }
case "$(uname -m)" in
  arm64) uv_pin="$(pinned UV_SHA256_ARM64)" ;;
  x86_64) uv_pin="$(pinned UV_SHA256_X86_64)" ;;
  *) die "no uv build is pinned for $(uname -m)" ;;
esac
uv_version="$(sed -n 's/^readonly UV_VERSION=\([0-9][0-9.]*\)$/\1/p' <<<"$setup_text")"
[[ -n "$uv_pin" && -n "$uv_version" ]] || die "$BRANCH's $SETUP pins no uv build for $(uname -m)"
uv_source="${FBC_UV_BINARY:-$(command -v uv || true)}"
[[ -n "$uv_source" && -f "$uv_source" && -r "$uv_source" ]] \
  || die "no uv to bundle: install uv $uv_version in this account, or set FBC_UV_BINARY"
uv_sum="$(sha256 "$uv_source")"
[[ "$uv_sum" == "$uv_pin" ]] \
  || die "$uv_source is not the uv $uv_version build $SETUP pins for $(uname -m) (SHA-256" \
    "$uv_sum, pinned $uv_pin): set FBC_UV_BINARY to that build"

umask 027
if [[ "$existing" -eq 0 ]]; then
  mkdir "$out_real" || die "cannot create $out_real"
fi
out_dir="$out_real"
# chgrp and chmod follow a symbolic link: once more, right before them, that it is still the
# directory judged above (no other account can rename it: open_to_others).
[[ -d "$out_dir" && ! -L "$out_dir" && -O "$out_dir" ]] \
  || die "refusing OUT_DIR $out_dir: it changed while it was checked"
chgrp "$GROUP" "$out_dir"
chmod 750 "$out_dir"

publish() { # publish <temporary file> <final path>: never over what is not a file of ours
  if [[ -e "$2" || -L "$2" ]]; then
    [[ -f "$2" && ! -L "$2" && -O "$2" ]] || die "refusing to replace $2: not a plain file of yours"
  fi
  chgrp "$GROUP" "$1"
  chmod 640 "$1"
  mv -f "$1" "$2"
}

bundle="$out_dir/$NAME"
tmp="$(mktemp "$out_dir/.bundle.XXXXXX")"
trap 'rm -f "$tmp" "$tmp.setup" "$tmp.uv" "$tmp.sha256" "$tmp.marker"' EXIT
if [[ ! -e "$out_dir/$MARKER" ]]; then
  printf '%s\n' "$MARKER_TEXT" >"$tmp.marker"
  publish "$tmp.marker" "$out_dir/$MARKER"
fi
git -C "$repo" bundle create --quiet "$tmp" "$BRANCH"
heads="$(git -C "$repo" bundle list-heads "$tmp")" || die "cannot read the bundle's heads"
[[ "$heads" == "$tip refs/heads/$BRANCH" ]] \
  || die "$BRANCH moved while it was scanned (from ${tip:0:10}): run the script again"
git -C "$repo" show "$tip:deploy/macos/$SETUP" >"$tmp.setup" \
  || die "$BRANCH has no deploy/macos/$SETUP"
cat "$uv_source" >"$tmp.uv"
[[ "$(sha256 "$tmp.uv")" == "$uv_pin" ]] || die "$uv_source changed while it was copied"

publish "$tmp" "$bundle"
publish "$tmp.setup" "$out_dir/$SETUP"
publish "$tmp.uv" "$out_dir/$UV_NAME"
(cd "$out_dir" && shasum -a 256 "$NAME" "$SETUP" "$UV_NAME") >"$tmp.sha256"
publish "$tmp.sha256" "$bundle.sha256"

commit="$(git -C "$repo" rev-parse --short "$tip")"
printf 'Wrote %s (%s at %s), %s, uv %s and their .sha256.\n' "$bundle" "$BRANCH" "$commit" \
  "$SETUP" "$uv_version"
if [[ -n "$(git -C "$repo" status --porcelain --untracked-files=no)" ]]; then
  printf 'Note: the checkout has uncommitted changes; the bundle has %s as committed.\n' \
    "$BRANCH"
fi
printf 'Next, inside the collector account: bash %s/%s (or --update).\n' "$out_dir" "$SETUP"
