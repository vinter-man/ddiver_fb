"""The bundle's content scan: every blob a branch reaches, deleted and rewritten ones included.

``scripts/make_bundle.sh`` runs it before it packs ``main``. A git bundle carries the branch's
whole history, so a Telegram token, a cookie export or a raw Facebook capture committed once
under an innocent name (``notes.txt``, ``debug.json``, a fixture) would reach every local account
even after the file was deleted. The bundle script's filename check stays as a second,
independent layer.

What is read: every object ``git rev-list --objects <branch>`` lists. Blobs and commit messages
are scanned as written and once more with JSON escapes (``\\uXXXX``, ``\\"``) and URL quoting
undone. A gzip'd blob is scanned inside (the collector keeps its raw steps gzip'd). A binary
blob, an oversized one and a gzip that does not unpack are findings in themselves, since the
rules cannot read them.

What is reported: the path, the rule, the line and the commit that first had the file at that
path, never the matched text. A finding that is a known-safe fixture (a synthetic cookie in a
test) is listed, after review, in ``scripts/secret_scan_allow.txt`` *as the branch has it*:

    <rule> <path> <fingerprint>  # why it is not a secret

The fingerprint is the first 16 hex digits of the SHA-256 of the matched value, and
``--fingerprints`` prints it, so an entry covers that one value at that one path. The same
value elsewhere, or another value in the same file, is still a finding. The default output
leaves the fingerprints out: a short value such as an account id can be recovered from its
hash.

Exit status: 0 clean, 1 findings, 2 the scan could not run (git, a bad allowlist line).
Standard library only, Python 3.9 or later (macOS's own ``python3`` runs it when the checkout
has no ``.venv``).
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote_to_bytes

ALLOWLIST = "scripts/secret_scan_allow.txt"
MESSAGE = "(commit-message)"
"""The path a commit message's finding is reported (and allow-listed) under."""
MAX_BLOB = 1 << 20
"""A blob larger than this (1 MiB, unpacked) is a finding in itself: captures are large."""
MAX_INFLATED = 64 << 20
BINARY_PROBE = 8000
"""Like git: a NUL in the first 8000 bytes makes a blob binary."""

_Q = r"""(?:\\{0,3}["'])?"""
"""An optional quote, JSON-escaped up to twice (a GraphQL body stored as a JSON string)."""
_SEP = _Q + r"\s*[:=]\s*" + _Q + r"\s*"
_WORD_START = r"(?<![A-Za-z0-9_])"
_SECRET_NAME = r"(?:api[_-]?key|api[_-]?hash|secret|token|passw(?:or)?d)[A-Za-z0-9_]*"  # noqa: S105
"""Anchored on the word itself, not on a name's first letter (``bot_token``): much faster."""
_COOKIE_NAME = r"""["']name["']\s*:\s*["'](?:c_user|xs|fb_dtsg|datr|sb|fr|presence)["']"""
"""A cookie object's name key, for the session cookies, in either quote style."""

RULES: dict[str, re.Pattern[bytes]] = {
    name: re.compile(pattern.encode())
    for name, pattern in (
        # Credentials
        (
            # Also right after "bot": the Bot API carries it so, https://api.telegram.org/bot<token>/
            "telegram-bot-token",
            r"(?:" + _WORD_START + r"|(?<=bot))"
            r"(?P<v>[0-9]{5,16}:[A-Za-z0-9_-]{30,})(?![A-Za-z0-9_-])",
        ),
        ("bearer-token", r"(?i)\bbearer[ \t]+(?P<v>[A-Za-z0-9._~+/-]{16,}=*)"),
        (
            "authorization-header",
            r"(?i)\bauthorization" + _SEP + r"(?:basic|token|bot)[ \t]+"
            r"(?P<v>[A-Za-z0-9._~+/-]{12,}=*)",
        ),
        (
            "api-key-format",
            (
                r"(?P<v>gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}|AKIA[0-9A-Z]{16}"
                r"|xox[abprs]-[A-Za-z0-9-]{10,}|sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"
                r"|AIza[0-9A-Za-z_-]{35})"
            ),
        ),
        (
            "secret-assignment",
            r"(?i)" + _SECRET_NAME + r"""\\{0,3}["']?\s*[:=]\s*\\{0,3}(?P<q>["'])"""
            r"""(?P<v>[^"'\\\s]{20,})\\{0,3}(?P=q)""",
        ),
        (
            # Wherever the name starts a word, with = or : (a shell or env file, a compose map or
            # list, systemd's Environment=, docker run -e). No backslash in the value: an
            # escaped "\n" after an empty one is not a value.
            "secret-env-line",
            (
                _WORD_START + r"[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|API_HASH)"
                r"""[A-Z0-9_]*[ \t]*[=:][ \t]*["']?(?P<v>[^\s"'#$<>{}()\[\]\\]{16,})"""
            ),
        ),
        (
            "private-key",
            (
                r"(?P<v>-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
                r"|PuTTY-User-Key-File-[0-9])"
            ),
        ),
        # Facebook session cookies and the page tokens tied to a login
        ("fb-cookie-c_user", _WORD_START + r"c_user" + _SEP + r"(?P<v>[0-9]{5,})"),
        (
            "fb-cookie-xs",
            _WORD_START + r"xs" + _SEP + r"(?P<v>[0-9]{1,3}(?:%3A|:)[A-Za-z0-9_%.:-]{8,})",
        ),
        ("fb-cookie-datr", _WORD_START + r"_?(?:js_)?datr" + _SEP + r"(?P<v>[A-Za-z0-9_-]{20,})"),
        ("fb-dtsg", _WORD_START + r"fb_dtsg(?:_ag)?" + _SEP + r"(?P<v>[A-Za-z0-9_:%-]{12,})"),
        (
            # A cookie object in JSON or a Python repr, its keys in any order (Playwright's
            # storage state and context.cookies(), Cookie-Editor and EditThisCookie exports):
            # the name before the value, or (the conditional) after it, in the same object.
            "fb-cookie-export",
            (
                r"(?P<name>" + _COOKIE_NAME + r"[^{}]{0,600}?)?"
                r"""["']value["']\s*:\s*["'](?P<v>[^"']{4,})["']"""
                r"(?(name)|(?=[^{}]{0,600}?" + _COOKIE_NAME + r"))"
            ),
        ),
        (
            # A Netscape cookies.txt line (... <tab>c_user<tab>value) or a pasted DevTools table
            # row (c_user<tab>value<tab>.facebook.com ...). A cookie value has no white space.
            "fb-cookie-table",
            r"(?m)(?:^|\t)(?:c_user|xs|datr|fr|sb)\t(?P<v>[^\s]{4,})",
        ),
        # Markers of a raw (not allow-list built) Facebook capture
        (
            "fb-dtsg-initial-data",
            r"DTSGInitialData"
            + _Q
            + r"\s*,\s*\[\s*\]\s*,\s*\{\s*"
            + _Q
            + r"token"
            + _SEP
            + r"""(?P<v>[^"'\\\s,}]{8,})""",
        ),
        (
            "fb-lsd-token",
            _WORD_START
            + r"LSD"
            + _Q
            + r"\s*,\s*\[\s*\]\s*,\s*\{\s*"
            + _Q
            + r"token"
            + _SEP
            + r"(?P<v>[A-Za-z0-9_-]{6,})",
        ),
        (
            "fb-account-id",
            _WORD_START
            + r"(?:USER_ID|ACCOUNT_ID|actorID|actor_id|__user|av)"
            + _SEP
            + r"(?P<v>[1-9][0-9]{4,})",
        ),
        ("fb-encrypted-tracking", r"encrypted_tracking" + _SEP + r"(?P<v>[A-Za-z0-9_-]{20,})"),
        ("fb-cdn-signature", _WORD_START + r"oh=(?P<v>00_[A-Za-z0-9_-]{10,})"),
        (
            # The rest of the start tag too: the html tag with the id alone begins every real
            # page, so its fingerprint would allow-list any of them.
            "fb-page-html",
            r'(?P<v><html[^>]{0,200}\bid=\\{0,3}"facebook\\{0,3}"[^>]{0,200})',
        ),
        (
            "fbcollector-raw-step",
            r'(?P<v>"lost_bodies"\s*:\s*[0-9]+\s*,\s*"pagination_fired"\s*:)',
        ),
    )
}
NEEDLES: dict[str, tuple[bytes, ...]] = {
    "bearer-token": (b"bearer",),
    "authorization-header": (b"authorization",),
    "api-key-format": (b"gh", b"akia", b"xox", b"sk-", b"aiza"),
    "secret-assignment": (b"key", b"hash", b"secret", b"token", b"passw"),
    "secret-env-line": (b"token", b"secret", b"passw", b"api_key", b"api_hash"),
    "private-key": (b"private key", b"putty-user-key-file-"),
    "fb-cookie-c_user": (b"c_user",),
    "fb-cookie-xs": (b"xs",),
    "fb-cookie-datr": (b"datr",),
    "fb-dtsg": (b"fb_dtsg",),
    "fb-cookie-export": (b"value",),
    "fb-cookie-table": (b"c_user\t", b"xs\t", b"datr\t", b"fr\t", b"sb\t"),
    "fb-dtsg-initial-data": (b"dtsginitialdata",),
    "fb-lsd-token": (b"lsd",),
    "fb-encrypted-tracking": (b"encrypted_tracking",),
    "fb-cdn-signature": (b"oh=00_",),
    "fb-page-html": (b"<html",),
    "fbcollector-raw-step": (b"lost_bodies",),
}
"""Lower-case text a rule cannot match without (checked on the lower-cased blob first, which is
far cheaper than the rule); a rule not listed always runs."""
BLOB_RULES = ("binary-blob", "oversized-blob", "unreadable-gzip")
"""Findings about a whole blob; their fingerprint is the blob's own content."""
ALL_RULES = frozenset(RULES) | frozenset(BLOB_RULES)
MARKER_RULES = frozenset({"private-key", "fbcollector-raw-step"})
"""Rules whose match is a fixed marker, not a value: one fingerprint stands for every key or
every raw step, so the allowlist refuses them (a test builds such a sample from pieces)."""

_UNICODE_ESCAPE = re.compile(rb"\\u([0-9a-fA-F]{4})")
_ALLOW_LINE = re.compile(
    r"^(?P<rule>[a-z0-9_-]+)\s+(?P<path>\S+)\s+(?P<fp>[0-9a-f]{16})\s+#\s*(?P<why>\S.*)$"
)


@dataclass(frozen=True)
class Hit:
    rule: str
    fingerprint: str
    line: int | None
    """The line in the blob as written; ``None`` when only the unescaped text matched."""


@dataclass(frozen=True)
class Finding:
    path: str
    commit: str
    hit: Hit


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    allowed: int = 0
    """Allowlist entries that matched."""
    unused: list[int] = field(default_factory=list)
    """Allowlist lines that matched nothing."""
    objects: int = 0


class ScanError(Exception):
    pass


def fingerprint(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()[:16]


def _unicode(match: re.Match[bytes]) -> bytes:
    return chr(int(match.group(1), 16)).encode("utf-8", "surrogatepass")


def normalise(data: bytes) -> bytes:
    """Undo JSON escaping (nested too) and URL quoting, a few rounds deep."""
    for _ in range(4):
        before = data
        data = _UNICODE_ESCAPE.sub(_unicode, data)
        data = data.replace(b'\\"', b'"').replace(b"\\/", b"/")
        data = unquote_to_bytes(data)
        if data == before:
            break
    return data


def gunzip(data: bytes, limit: int = MAX_INFLATED) -> bytes | None:
    """Every member of a gzip stream, unpacked; ``None`` if it does not unpack or passes
    ``limit``."""
    out = b""
    rest = data
    while rest:
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            out += inflater.decompress(rest, limit + 1 - len(out))
        except zlib.error:
            return None
        if len(out) > limit or not inflater.eof:
            return None
        rest = inflater.unused_data
    return out


def scan_text(data: bytes) -> list[Hit]:
    """Every rule's matches in ``data``, as written and unescaped, one per value."""
    hits: dict[tuple[str, str], Hit] = {}
    unescaped = normalise(data)
    for variant in (data,) if unescaped == data else (data, unescaped):
        written = variant is data
        lowered = variant.lower()
        for name, pattern in RULES.items():
            needles = NEEDLES.get(name)
            if needles is not None and not any(n in lowered for n in needles):
                continue
            for match in pattern.finditer(variant):
                key = (name, fingerprint(match.group("v")))
                if key in hits:
                    continue
                line = data.count(b"\n", 0, match.start()) + 1 if written else None
                hits[key] = Hit(name, key[1], line)
    return list(hits.values())


def scan_blob(data: bytes) -> list[Hit]:
    whole = fingerprint(data)
    if data[:2] == b"\x1f\x8b":
        inflated = gunzip(data)
        if inflated is None:
            return [Hit("unreadable-gzip", whole, None)]
        return [Hit(h.rule, h.fingerprint, None) for h in scan_blob(inflated)]
    hits = scan_text(data)
    if b"\0" in data[:BINARY_PROBE]:
        hits.append(Hit("binary-blob", whole, None))
    if len(data) > MAX_BLOB:
        hits.append(Hit("oversized-blob", whole, None))
    return hits


# -- git ------------------------------------------------------------------------------------------


def git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    done = subprocess.run(  # noqa: S603 - git with our own arguments
        ["git", "-C", str(repo), *args],  # noqa: S607
        input=stdin,
        capture_output=True,
        check=False,
    )
    if done.returncode != 0:
        detail = done.stderr.decode(errors="replace").strip()
        raise ScanError(f"git {args[0]} failed: {detail}")
    return done.stdout


def read_objects(repo: Path, ids: list[str]) -> dict[str, tuple[str, bytes]]:
    """``{id: (type, content)}`` through one ``git cat-file --batch``."""
    out = git(repo, "cat-file", "--batch", stdin="".join(f"{i}\n" for i in ids).encode())
    objects: dict[str, tuple[str, bytes]] = {}
    at = 0
    while at < len(out):
        end = out.index(b"\n", at)
        header = out[at:end].decode().split()
        if len(header) != 3:
            raise ScanError(f"git cat-file: {' '.join(header)}")
        sha, kind, size = header[0], header[1], int(header[2])
        objects[sha] = (kind, out[end + 1 : end + 1 + size])
        at = end + 1 + size + 1
    return objects


def history(repo: Path, branch: str) -> list[str]:
    """The branch's commits, oldest first."""
    return git(repo, "rev-list", "--reverse", "--topo-order", branch).decode().split()


def paths_of(repo: Path, commits: list[str]) -> dict[str, dict[str, str]]:
    """``{blob: {path: the first of ``commits`` that has it there}}``."""
    first: dict[str, dict[str, str]] = {}
    for commit in commits:
        for entry in git(repo, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
            if not entry:
                continue
            meta, _, path = entry.partition(b"\t")
            _mode, kind, sha = meta.decode().split()
            if kind == "blob":
                first.setdefault(sha, {}).setdefault(path.decode(errors="replace"), commit)
    return first


def load_allowlist(text: str) -> dict[tuple[str, str, str], int]:
    """``{(rule, path, fingerprint): line number}``; a line that is not an entry is refused."""
    entries: dict[tuple[str, str, str], int] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ALLOW_LINE.match(line)
        if match is None:
            raise ScanError(
                f"{ALLOWLIST} line {number}: not '<rule> <path> <fingerprint>  # why it is safe'"
            )
        if match.group("rule") not in ALL_RULES:
            raise ScanError(f"{ALLOWLIST} line {number}: no rule {match.group('rule')!r}")
        if match.group("rule") in MARKER_RULES:
            raise ScanError(
                f"{ALLOWLIST} line {number}: {match.group('rule')} matches a fixed marker, so an "
                "entry would pass every such file at that path; build the sample from pieces"
            )
        entries[(match.group("rule"), match.group("path"), match.group("fp"))] = number
    return entries


def branch_allowlist(repo: Path, branch: str) -> str:
    spec = f"{branch}:{ALLOWLIST}"
    try:
        git(repo, "cat-file", "-e", spec)
    except ScanError:
        return ""
    return git(repo, "cat-file", "blob", spec).decode()


def scan_repo(repo: Path, branch: str, allowlist: str) -> Report:
    """Every blob and commit message ``branch`` reaches. One finding per value, path and rule:
    the one of the earliest commit, however many versions of the file carry the same value."""
    entries = load_allowlist(allowlist)
    listed = git(repo, "rev-list", "--objects", branch).decode().splitlines()
    objects = read_objects(repo, [line.split(" ", 1)[0] for line in listed])
    commits = history(repo, branch)
    age = {commit: n for n, commit in enumerate(commits)}
    where = paths_of(repo, commits)
    report = Report(objects=len(objects))
    found: dict[tuple[str, str, str], Finding] = {}
    used: set[tuple[str, str, str]] = set()
    for sha, (kind, content) in objects.items():
        if kind == "blob":
            places = sorted(where.get(sha, {"?": "?"}).items())
            hits = scan_blob(content)
        elif kind == "commit":
            places = [(MESSAGE, sha)]
            hits = scan_text(content)
        else:
            continue
        for hit in hits:
            for path, commit in places:
                key = (hit.rule, path, hit.fingerprint)
                if key in entries:
                    used.add(key)
                    continue
                known = found.get(key)
                if known is None or age.get(commit, -1) < age.get(known.commit, -1):
                    found[key] = Finding(path, commit, hit)
    report.findings = sorted(
        found.values(), key=lambda f: (f.path, f.hit.rule, age.get(f.commit, -1))
    )
    report.allowed = len(used)
    report.unused = sorted(n for key, n in entries.items() if key not in used)
    return report


# -- the command ----------------------------------------------------------------------------------


def describe(finding: Finding, *, fingerprints: bool) -> str:
    hit = finding.hit
    where = f"line {hit.line}" if hit.line is not None else "escaped or packed"
    if finding.path == MESSAGE:
        text = f"  commit {finding.commit[:10]}'s message: {hit.rule} ({where})"
    else:
        text = f"  {finding.path}: {hit.rule} ({where}; since commit {finding.commit[:10]})"
    if fingerprints:
        text += f"\n    allowlist: {hit.rule} {finding.path} {hit.fingerprint}  #"
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("branch", nargs="?", default="main")
    parser.add_argument("--repo", type=Path, default=Path())
    parser.add_argument(
        "--allowlist",
        type=Path,
        help=f"read the allowlist from this file, not from the branch's {ALLOWLIST}",
    )
    parser.add_argument(
        "--fingerprints",
        action="store_true",
        help="also print each finding's allowlist line (review the value first)",
    )
    args = parser.parse_args(argv)
    say = sys.stderr.write
    try:
        allowlist = (
            args.allowlist.read_text()
            if args.allowlist is not None
            else branch_allowlist(args.repo, args.branch)
        )
        report = scan_repo(args.repo, args.branch, allowlist)
    except (ScanError, OSError) as error:
        say(f"scan_secrets: {error}\n")
        return 2
    for number in report.unused:
        say(f"scan_secrets: note: allowlist line {number} matches nothing any more\n")
    if not report.findings:
        say(
            f"scan_secrets: {report.objects} objects of {args.branch}'s history, nothing that "
            f"looks like a secret ({report.allowed} allow-listed values)\n"
        )
        return 0
    say(
        f"scan_secrets: {args.branch}'s history holds what may be secrets (the text itself is "
        "not shown):\n"
    )
    for finding in report.findings:
        say(describe(finding, fingerprints=args.fingerprints) + "\n")
    say(
        "A real secret must leave the history before any bundle is made. A known-safe fixture "
        f"goes into {ALLOWLIST} after review; --fingerprints prints its line.\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
