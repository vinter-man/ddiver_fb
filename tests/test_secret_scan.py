"""``scripts/scan_secrets.py``, the content scan ``make_bundle.sh`` runs over main's history.

Every sample here that looks like a secret is put together at run time from pieces, so this file
never holds one as written; otherwise the scan of the real history would find its own tests.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "scan_secrets.py"
ALLOWLIST = REPO / "scripts" / "secret_scan_allow.txt"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scan_secrets", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["scan_secrets"] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


scan = load()


def j(*parts: str) -> bytes:
    return "".join(parts).encode()


FB_ID = "1000" + "98765432101"
TELEGRAM = j("7012345678", ":", "AAF", "k3y-" * 8)
PEM = "-----BEGIN "
RAW_STEP = {
    "v": 1,
    "final_url": "https://www.facebook.com/",
    "lost_" + "bodies": 0,
    "pagination_" + "fired": False,
}


def rules(data: bytes) -> set[str]:
    return {hit.rule for hit in scan.scan_blob(data)}


HEX32 = "f00d" * 8
COOKIE_EDITOR = j(
    '[{"domain": ".facebook.com", "expirationDate": 1790000000, "hostOnly": false, ',
    '"httpOnly": false, "name": "c_',
    'user", "path": "/", "sameSite": "no_restriction", "secure": true, "session": false, ',
    '"storeId": "0", "value": "',
    FB_ID,
    '", "id": 3}]',
)
"""A Cookie-Editor / EditThisCookie export: its keys in alphabetical order."""

CATCHES = [
    ("telegram-bot-token", TELEGRAM),
    ("telegram-bot-token", b"FBC_ALERT_BOT_TOKEN=" + TELEGRAM),
    # The Bot API's own URL (review F1): the token right after "bot", plain and JSON-escaped.
    ("telegram-bot-token", b"curl https://api.telegram.org/bot" + TELEGRAM + b"/getUpdates"),
    ("telegram-bot-token", b'"https:\\/\\/api.telegram.org\\/bot' + TELEGRAM + b'\\/sendMessage"'),
    ("secret-env-line", b"FBC_ALERT_BOT_TOKEN=" + TELEGRAM),
    # Upper-case secrets wherever they are written (review F4): a compose map and list, a
    # systemd unit, a docker command line.
    ("secret-env-line", j("    FBC_INGEST_", "TOKEN: ", HEX32)),
    ("secret-env-line", j("      - FBC_INGEST_", "TOKEN=", HEX32)),
    ("secret-env-line", j("Environment=FBC_INGEST_", "TOKEN=", HEX32)),
    ("secret-env-line", j("docker run -e FBC_INGEST_", "TOKEN=", HEX32, " image")),
    ("secret-env-line", j("    API_", "HASH: '", "0123456789abcdef" * 2, "'")),
    ("bearer-token", j("Authorization: Bearer ", "eyJhbGciOi", "x" * 20)),
    ("authorization-header", j("authorization: Basic ", "dXNlcjpw", "YXNz" * 4)),
    ("api-key-format", j("gh", "p_", "a1" * 18)),
    ("api-key-format", j("AK", "IA", "ABCDEFGHIJKLMNOP")),
    ("api-key-format", j("sk-", "ant-", "api03-", "z" * 30)),
    ("secret-assignment", j("ingest_", 'token = "', "q7" * 12, '"')),
    ("secret-assignment", j('{"api_', 'hash": "', "0123456789abcdef" * 2, '"}')),
    ("secret-env-line", j("API_", "HASH=", "0123456789abcdef" * 2)),
    ("secret-env-line", j("export FBC_INGEST_", "TOKEN='", "f00d" * 8, "'")),
    ("private-key", j(PEM, "OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAA=")),
    ("private-key", j(PEM, "PRIVATE KEY-----")),
    ("private-key", j(PEM, "RSA PRIVATE KEY-----")),
    ("fb-cookie-c_user", j("cookie: c_", "user=", FB_ID, "; xs=1")),
    ("fb-cookie-c_user", j('{"c_', 'user":"', FB_ID, '"}')),
    ("fb-cookie-c_user", j('{\\"c_', 'user\\":\\"', FB_ID, '\\"}')),
    ("fb-cookie-xs", j("x", "s=", "12%3A", "Ab-cDe_FgH", "%3A2%3A1690000000")),
    ("fb-cookie-datr", j("dat", "r=", "AbCdEfGhIjKlMnOpQrStUvWx")),
    ("fb-cookie-datr", j('{"_js_dat', 'r":"', "AbCdEfGhIjKlMnOpQrStUvWx", '"}')),
    ("fb-dtsg", j("fb_", "dtsg=", "NAcN", "x" * 20, ":12:1690000000")),
    ("fb-cookie-export", j('{"name": "x', 's", "value": "', "12%3Aab", '", "domain": ".f"}')),
    # Every layout of a cookie dump (review F2): a Python repr (print(context.cookies())), an
    # export with the keys in another order, the value before the name, cookies.txt, DevTools.
    ("fb-cookie-export", j("[{'name': 'c_", "user', 'value': '", FB_ID, "', 'path': '/'}]")),
    ("fb-cookie-export", COOKIE_EDITOR),
    ("fb-cookie-export", j('{"value": "', "AbCdEfGhIjKl", '", "path": "/", "name": "dat', 'r"}')),
    ("fb-cookie-table", j(".facebook.com\tTRUE\t/\tTRUE\t1790000000\tc_", "user\t", FB_ID)),
    ("fb-cookie-table", j("c_", "user\t", FB_ID, "\t.facebook.com\t/\t2027-09-25T10:00:00Z\t20")),
    ("fb-cookie-table", j("#HttpOnly_.facebook.com\tTRUE\t/\tTRUE\t0\tx", "s\t", "12%3AabcDEF")),
    ("fb-dtsg-initial-data", j('["DTSG', 'InitialData",[],{"token":"', "NAcM", "q" * 20, '"}')),
    ("fb-lsd-token", j('["LS', 'D",[],{"token":"', "AVq", "z" * 8, '"},323]')),
    ("fb-account-id", j('"USER_', 'ID":"', FB_ID, '"')),
    ("fb-account-id", j('{"actor', 'ID":"', FB_ID, '"}')),
    ("fb-account-id", j("__us", "er=", FB_ID, "&a=1")),
    ("fb-account-id", j("&a", "v=", FB_ID)),
    ("fb-account-id", j("USER_", "ID%22%3A%22", FB_ID)),
    ("fb-account-id", j("\\u0022USER_", "ID\\u0022:\\u0022", FB_ID, "\\u0022")),
    ("fb-encrypted-tracking", j('"encrypted_', 'tracking":"', "AZ", "x" * 30, '"')),
    ("fb-cdn-signature", j("https://scontent.x/v.jpg?_nc_ht=x&o", "h=00_", "AYA", "x" * 20)),
    ("fb-page-html", j('<html id="face', 'book" class="x" lang="en">')),
    ("fb-page-html", j('<html lang=\\"en\\" id=\\"face', 'book\\">')),
    ("fbcollector-raw-step", json.dumps(RAW_STEP).encode()),
]


@pytest.mark.parametrize(
    ("rule", "sample"), CATCHES, ids=[f"{rule}-{n}" for n, (rule, _) in enumerate(CATCHES)]
)
def test_the_scan_catches(rule: str, sample: bytes) -> None:
    assert rule in rules(b"some text before\n" + sample + b"\nand after\n")


@pytest.mark.parametrize(
    "sample",
    [
        # What the code and the docs say about these things, without a value
        "The c_user and xs cookies and fb_dtsg are session secrets; so is datr.",
        "fb_dtsg=AQH and the LSD token are dropped by the fixture builder.",
        "cookie: c_user=100; xs=12%3Aabc",
        '["DTSGInitialData",[],{"token":"x"}]',
        '["LSD",[],{"token":"x"},323]',
        '{"USER_ID":"0","ACCOUNT_ID":"0","actorID":"0"}',
        '"USER_ID": 7',
        "https://scontent.x/v.jpg?_nc_ht=x&oh=00_AQ",
        "the raw step keeps lost_bodies and pagination_fired",
        "token=settings.bot_token.get_secret_value()",
        "self.ingest = IngestServer(token=s.ingest_token.get_secret_value())",
        "Authorization: Bearer {token}",
        'headers={"Authorization": f"Bearer {self._token}"}',
        "BOT_TOKEN=\nAPI_HASH=\nINGEST_TOKEN=",
        "FBC_INGEST_TOKEN=<the value of `openssl rand -hex 32`>",
        "FBC_ALERT_BOT_TOKEN=$TOKEN",
        "    FBC_INGEST_TOKEN: ${FBC_INGEST_TOKEN:?set it in .env}",
        "      - FBC_INGEST_TOKEN=${FBC_INGEST_TOKEN}",
        "`FBC_ALERT_BOT_TOKEN`: the alert bot's token, from @BotFather",
        "KV_TOKEN: Final = 'alert_bot_token'",
        "https://api.telegram.org/bot{token}/sendMessage",
        '[{"name": "locale", "value": "en_US"}, {"name": "c_user"}]',
        "c_user\tis the account id\nxs\tis the session",
        '[[ "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]',
        "-----BEGIN PUBLIC KEY-----",
        "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9G",
        'hash = "sha256:' + "a" * 64 + '"',
        "localhost:8080/abcdefghijklmnopqrstuvwxyz0123456789",
        "<html lang='en'><body>cookie set</body></html>",
    ],
)
def test_the_scan_passes_what_only_names_them(sample: str) -> None:
    assert scan.scan_blob(sample.encode()) == []


def test_a_value_has_one_fingerprint_however_it_is_written() -> None:
    written = scan.scan_text(j('"USER_', 'ID":"', FB_ID, '"'))
    quoted = scan.scan_text(j("line\nUSER_", "ID%22%3A%22", FB_ID))
    assert [h.fingerprint for h in written] == [h.fingerprint for h in quoted]
    assert written[0].line == 1
    assert quoted[0].line is None  # found only once unescaped: no line of the text as written
    assert written[0].fingerprint == scan.fingerprint(FB_ID.encode())
    other = scan.scan_text(j('"USER_', 'ID":"', "1000" + "11111111111", '"'))
    assert other[0].fingerprint != written[0].fingerprint


def test_the_line_is_the_line_as_written() -> None:
    hits = scan.scan_text(b"a\nb\n" + TELEGRAM + b"\n")
    assert [(h.rule, h.line) for h in hits] == [("telegram-bot-token", 3)]


def test_a_gzipped_capture_is_read_inside() -> None:
    packed = gzip.compress(json.dumps(RAW_STEP).encode() + b"\n" + TELEGRAM)
    assert rules(packed) == {"fbcollector-raw-step", "telegram-bot-token"}
    assert {h.line for h in scan.scan_blob(packed)} == {None}
    two = gzip.compress(b"harmless\n") + gzip.compress(TELEGRAM)
    assert rules(two) == {"telegram-bot-token"}  # every member of the stream


@pytest.mark.parametrize(
    ("data", "rule"),
    [
        (b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR", "binary-blob"),
        (gzip.compress(b"\0\x01\x02 packed binary"), "binary-blob"),
        (b"a" * ((1 << 20) + 1), "oversized-blob"),
        (b"\x1f\x8b\x08\x00 not really gzip", "unreadable-gzip"),
    ],
    ids=["png", "gzipped binary", "oversized", "not gzip"],
)
def test_what_the_rules_cannot_read_is_a_finding_itself(data: bytes, rule: str) -> None:
    assert rule in rules(data)


def test_gunzip_stops_at_its_limit() -> None:
    packed = gzip.compress(b"x" * 1000)
    assert scan.gunzip(packed, limit=1000) == b"x" * 1000
    assert scan.gunzip(packed, limit=999) is None
    assert scan.gunzip(packed[:-8]) is None  # cut short


def test_the_page_rule_fingerprints_the_whole_start_tag() -> None:
    """Review F5: the value stopped at id="facebook", which begins every real page, so the
    detector tests' allowlist entry would have passed a real page pasted into that file."""
    synthetic = scan.scan_text(j("f'<html id=\"face", 'book"{lang_attr}><head>{head}</head>'))
    real = scan.scan_text(j('<html id="face', 'book" class="__fb-light-mode" lang="en" dir="ltr">'))
    assert [h.rule for h in synthetic + real] == ["fb-page-html", "fb-page-html"]
    assert synthetic[0].fingerprint != real[0].fingerprint
    entries = [line.split() for line in ALLOWLIST.read_text().splitlines()]
    allowed = {e[2] for e in entries if e and e[0] == "fb-page-html"}
    assert allowed == {synthetic[0].fingerprint}


def test_every_rule_has_a_name_the_allowlist_accepts() -> None:
    for name in scan.ALL_RULES - scan.MARKER_RULES:
        assert scan.load_allowlist(f"{name} a/b.py 0123456789abcdef  # why\n")
    assert set(scan.NEEDLES) <= set(scan.RULES)


@pytest.mark.parametrize("rule", sorted(scan.MARKER_RULES))
def test_a_rule_that_matches_a_fixed_marker_cannot_be_allow_listed(rule: str) -> None:
    """Review F5: its fingerprint stands for every private key, or every raw step."""
    samples = {
        "private-key": (j(PEM, "PRIVATE KEY-----\nMIIEvQIBAD"), j(PEM, "PRIVATE KEY-----\nMIIEvg")),
        "fbcollector-raw-step": (
            json.dumps(RAW_STEP).encode(),
            json.dumps({**RAW_STEP, "final_url": "https://www.facebook.com/groups/1/"}).encode(),
        ),
    }[rule]
    one, two = (scan.scan_text(sample) for sample in samples)
    assert [h.fingerprint for h in one] == [h.fingerprint for h in two]  # one for all of them
    with pytest.raises(scan.ScanError, match="fixed marker"):
        scan.load_allowlist(f"{rule} tests/x.py 0123456789abcdef  # a made-up one\n")


@pytest.mark.parametrize(
    ("line", "error"),
    [
        ("fb-account-id tests/x.py 0123456789abcdef", "not '<rule>"),
        ("fb-account-id tests/x.py 0123456789abcdef  #", "not '<rule>"),
        ("fb-account-id tests/x.py 0123456789ABCDEF  # why", "not '<rule>"),
        ("fb-account-id tests/x.py 0123456789abcde  # why", "not '<rule>"),
        ("no-such-rule tests/x.py 0123456789abcdef  # why", "no rule 'no-such-rule'"),
    ],
)
def test_a_bad_allowlist_line_is_refused(line: str, error: str) -> None:
    with pytest.raises(scan.ScanError, match=error):
        scan.load_allowlist(f"# a comment\n\n{line}\n")


# -- a repository -------------------------------------------------------------------------------


def git(*args: str, cwd: Path) -> str:
    done = subprocess.run(  # noqa: S603 - git with fixed arguments
        ["git", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )
    return done.stdout.strip()


def commit(repo: Path, files: dict[str, bytes | None], message: str = "c") -> str:
    for name, data in files.items():
        path = repo / name
        if data is None:
            git("rm", "--quiet", name, cwd=repo)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        git("add", name, cwd=repo)
    git("commit", "--quiet", "--allow-empty", "-m", message, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git("init", "--quiet", "--initial-branch=main", cwd=root)
    commit(root, {"README.md": b"# a repository\n"})
    return root


def run(repo: Path, *args: str, python: str = sys.executable) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - our own script
        [python, str(SCRIPT), "--repo", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_clean_history_passes(repo: Path) -> None:
    done = run(repo)
    assert done.returncode == 0, done.stderr
    assert "nothing that looks like a secret" in done.stderr


@pytest.mark.parametrize("how", ["deleted", "renamed", "on a merged branch", "gzipped"])
def test_a_secret_anywhere_in_the_history_is_reported_without_its_text(
    repo: Path, how: str
) -> None:
    if how == "on a merged branch":
        git("checkout", "--quiet", "-b", "side", cwd=repo)
    data = gzip.compress(TELEGRAM) if how == "gzipped" else b"notes\n\n" + TELEGRAM + b"\n"
    added = commit(repo, {"docs/notes.txt": data}, "notes")
    if how == "renamed":
        git("mv", "docs/notes.txt", "docs/old.txt", cwd=repo)
        commit(repo, {}, "rename")
    else:
        commit(repo, {"docs/notes.txt": None}, "tidy")
    if how == "on a merged branch":
        git("checkout", "--quiet", "main", cwd=repo)
        git("merge", "--quiet", "--no-ff", "-m", "merge", "side", cwd=repo)
    done = run(repo)
    assert done.returncode == 1
    output = done.stdout + done.stderr
    assert TELEGRAM.decode() not in output
    assert TELEGRAM.decode().split(":")[1] not in output
    line = "escaped or packed" if how == "gzipped" else "line 3"
    assert f"docs/notes.txt: telegram-bot-token ({line}; since commit {added[:10]})" in output
    if how == "renamed":  # the same blob under its second name, since the rename
        assert "docs/old.txt: telegram-bot-token" in output


def test_a_commit_message_is_scanned(repo: Path) -> None:
    sha = commit(repo, {}, "debugging with " + TELEGRAM.decode())
    done = run(repo)
    assert done.returncode == 1
    assert f"commit {sha[:10]}'s message: telegram-bot-token" in done.stderr
    assert TELEGRAM.decode() not in done.stderr


def test_a_reviewed_fixture_passes_and_only_that_value_at_that_path(repo: Path) -> None:
    fake = j('{"USER_', 'ID":"', FB_ID, '"}\n')
    commit(repo, {"tests/fixture.json": fake})
    done = run(repo, "--fingerprints")
    assert done.returncode == 1
    listed = [
        line.strip().removeprefix("allowlist: ")
        for line in done.stderr.splitlines()
        if line.strip().startswith("allowlist: ")
    ]
    assert listed == [f"fb-account-id tests/fixture.json {scan.fingerprint(FB_ID.encode())}  #"]
    entry = listed[0] + " made-up id in a test\n"
    # Only as the branch has it: an allowlist in the working tree alone is not reviewed.
    (repo / "scripts").mkdir()
    (repo / "scripts" / "secret_scan_allow.txt").write_text(entry)
    assert run(repo).returncode == 1
    commit(repo, {"scripts/secret_scan_allow.txt": entry.encode()})
    done = run(repo)
    assert done.returncode == 0, done.stderr
    assert "(1 allow-listed values)" in done.stderr
    # The same value elsewhere, and another value in the same file, are still findings.
    commit(repo, {"tests/other.json": fake})
    commit(repo, {"tests/fixture.json": fake + j('{"actor', 'ID":"', "1000" + "22222222222", '"}')})
    done = run(repo)
    assert done.returncode == 1
    assert "tests/other.json: fb-account-id" in done.stderr
    assert "tests/fixture.json: fb-account-id (line 2;" in done.stderr
    assert "tests/fixture.json: fb-account-id (line 1;" not in done.stderr


def test_an_allowlist_entry_that_matches_nothing_is_noted(repo: Path, tmp_path: Path) -> None:
    allow = tmp_path / "allow.txt"
    allow.write_text("fb-account-id tests/gone.json 0123456789abcdef  # was a fixture\n")
    done = run(repo, "--allowlist", str(allow))
    assert done.returncode == 0
    assert "allowlist line 1 matches nothing any more" in done.stderr


def test_the_scan_that_cannot_run_says_so(repo: Path, tmp_path: Path) -> None:
    done = run(repo, "no-such-branch")
    assert done.returncode == 2
    assert "git rev-list failed" in done.stderr
    bad = tmp_path / "allow.txt"
    bad.write_text("fb-account-id tests/x.json 0123456789abcdef\n")
    done = run(repo, "--allowlist", str(bad))
    assert done.returncode == 2
    assert "line 1" in done.stderr
    done = run(repo, "--allowlist", str(tmp_path / "missing.txt"))
    assert done.returncode == 2


def test_read_objects_refuses_what_git_does_not_have(repo: Path) -> None:
    with pytest.raises(scan.ScanError, match="missing"):
        scan.read_objects(repo, ["0" * 40])


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="no system python3")
def test_the_systems_own_python3_runs_it(repo: Path) -> None:
    """make_bundle.sh falls back to python3 on a checkout without .venv (macOS: 3.9)."""
    commit(repo, {"notes.txt": TELEGRAM})
    done = run(repo, python="/usr/bin/python3")
    assert done.returncode == 1, done.stderr
    assert "notes.txt: telegram-bot-token" in done.stderr


@pytest.mark.skipif(shutil.which("git") is None or not (REPO / ".git").exists(), reason="no git")
def test_this_repositorys_history_passes_with_its_allowlist() -> None:
    """What make_bundle.sh will find: nothing, every reviewed entry still in use."""
    done = run(REPO, "--allowlist", str(ALLOWLIST), "HEAD")
    assert done.returncode == 0, done.stderr
    assert "matches nothing" not in done.stderr
