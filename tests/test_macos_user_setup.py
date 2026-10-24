"""Phase 0 under a dedicated macOS user: the bundle script and the setup script, run for real.

Both scripts run against temporary directories only. The setup script meets stubs for
everything that would reach beyond them — ``id`` (it believes it is the Standard user
``fbc``), ``xcode-select``, ``launchctl``, ``defaults`` and ``curl`` (a tripwire: nothing may be
downloaded) — and a pseudo-terminal for the questions it asks without echo. The uv it installs
is a stub the bundle carries (a fake ``sync``); the tests pin that stub's SHA-256 in their copy
of the setup script, its two digest lines and nothing else. ``git``, ``shasum``, ``stat``,
``sed``, ``install`` and ``plutil`` are the system's own, so the tests are macOS-only;
``shellcheck`` lints the scripts wherever it is installed.

The tunnel to the brain meets ``ssh`` as a tripwire too: only ``ssh -G`` (which prints a
configuration and never connects) reaches the real one; ``ssh-keygen`` is the system's, on keys
made in the temporary home. The operator door's tunnel verbs meet a stub of ``curl``, which
answers only the door's probe through the tunnel's Unix socket.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import platform
import plistlib
import pty
import re
import select
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import termios
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MAKE_BUNDLE = REPO / "scripts" / "make_bundle.sh"
SCAN = REPO / "scripts" / "scan_secrets.py"
SETUP = REPO / "deploy" / "macos" / "setup-fbc-user.sh"
OPERATOR = REPO / "deploy" / "macos" / "fbc-operator.sh"
PLIST = REPO / "deploy" / "macos" / "com.ddiver.fbcollector.plist"
TUNNEL_PLIST = REPO / "deploy" / "macos" / "com.ddiver.fbtunnel.plist"
CLIENT_CONFIG = REPO / "deploy" / "ssh" / "fbtunnel_client.config"
VPS_SETUP = REPO / "deploy" / "vps" / "setup-fbtunnel.sh"
EXAMPLE = REPO / "docs" / "monitored.example.toml"
TOKEN = "123456789:" + "S3cr3t-token_" * 3
OWNER_ID = "4242"
UV_PINS = {
    "arm64": "40a78912c850286ea5756211f1f88c0928fb15b788869d46d4355cd4ac556023",
    "x86_64": "5882e241a33a36d9ad5daca8e6e3d6ce33e22256b0d30f024437b729196ab290",
}
"""The ``uv`` binary in Astral's uv 0.11.7 archives for Apple silicon and Intel Macs."""
PIN_LINE = re.compile(r"^readonly (UV_SHA256_(?:ARM64|X86_64))=([0-9a-f]{64})$", re.MULTILINE)
BUNDLED_UV = "bundled-uv"
MARKER = ".ddiver-fb-bundle"
"""The stub uv ``make_bundle.sh`` bundles, next to the small repository."""

macos = pytest.mark.skipif(sys.platform != "darwin", reason="macOS tools (stat -f, plutil)")


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
@pytest.mark.parametrize("script", [MAKE_BUNDLE, SETUP, OPERATOR, VPS_SETUP])
def test_the_scripts_are_shellcheck_clean(script: Path) -> None:
    done = subprocess.run(  # noqa: S603 - a fixed tool on our own file
        ["shellcheck", "--shell=bash", str(script)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert script.read_text().startswith("#!/bin/bash\n")
    assert os.access(script, os.X_OK)


def operator(home: Path, command: str) -> subprocess.CompletedProcess[str]:
    """The operator door (``fbc-operator.sh``) as the forced command of ``command``, with a
    stub ``fbcollector`` that prints how it was called."""
    stub = home / "ddiver_facebook" / ".venv" / "bin" / "fbcollector"
    if not stub.exists():
        stub.parent.mkdir(parents=True)
        stub.write_text('#!/bin/bash\necho "fbcollector $*"\n')
        stub.chmod(0o755)
    return subprocess.run(  # noqa: S603 - our own script
        ["/bin/bash", str(OPERATOR)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "SSH_ORIGINAL_COMMAND": command},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("command", "runs"),
    [
        ("diagnostics", "report --diagnostics-only"),
        ("report", "report"),
        ("status", "status"),
        ("diagnostics --anything", "report --diagnostics-only"),
        ("dialogs", "dialogs --last 4"),
        ("dialogs 20", "dialogs --last 20"),
        ("dialogs 08", "dialogs --last 8"),
    ],
)
def test_the_operator_door_runs_its_verbs(tmp_path: Path, command: str, runs: str) -> None:
    done = operator(tmp_path, command)
    assert (done.returncode, done.stdout, done.stderr) == (0, f"fbcollector {runs}\n", "")


def test_the_operator_door_lists_and_refuses(tmp_path: Path) -> None:
    listed = operator(tmp_path, "")
    assert listed.returncode == 2
    assert "diagnostics" in listed.stderr
    assert operator(tmp_path, "login").returncode == 3
    unknown = operator(tmp_path, "rm")
    assert (unknown.returncode, unknown.stdout) == (3, "")
    assert "diagnostics" in unknown.stderr
    assert "dialogs [N]" in listed.stderr
    for bad in ("dialogs 0", "dialogs 21", "dialogs 100", "dialogs x", "dialogs -1"):
        done = operator(tmp_path, bad)
        assert (done.returncode, done.stdout) == (2, ""), bad


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


def write_uv(path: Path, body: str | None = None) -> str:
    """An executable stub uv at ``path``; returns its SHA-256."""
    data = (STUB_UV if body is None else body).encode()
    path.write_bytes(data)
    path.chmod(0o755)
    return hashlib.sha256(data).hexdigest()


def pin_uv(setup: Path, digest: str) -> None:
    """Pin ``digest`` in a copy of the setup script: its two digest lines, nothing else."""
    text, count = PIN_LINE.subn(lambda m: f"readonly {m.group(1)}={digest}", setup.read_text())
    assert count == 2
    setup.write_text(text)


def small_repo(root: Path) -> Path:
    """A repository with what the scripts use from the real one, on a branch ``main``, and the
    stub uv its setup script pins next to it."""
    root.mkdir(parents=True)
    git("init", "--quiet", "--initial-branch=main", cwd=root)
    for source, target in (
        (MAKE_BUNDLE, "scripts/make_bundle.sh"),
        (SCAN, "scripts/scan_secrets.py"),
        (SETUP, "deploy/macos/setup-fbc-user.sh"),
        (PLIST, "deploy/macos/com.ddiver.fbcollector.plist"),
        (TUNNEL_PLIST, "deploy/macos/com.ddiver.fbtunnel.plist"),
        (CLIENT_CONFIG, "deploy/ssh/fbtunnel_client.config"),
        (OPERATOR, "deploy/macos/fbc-operator.sh"),
        (EXAMPLE, "docs/monitored.example.toml"),
    ):
        path = root / target
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, path)
    pin_uv(root / "deploy" / "macos" / "setup-fbc-user.sh", write_uv(root.parent / BUNDLED_UV))
    git("add", "-A", cwd=root)
    git("commit", "--quiet", "-m", "one", cwd=root)
    return root


def make_bundle(
    repo: Path,
    *out: Path | str,
    uv: Path | None = None,
    path_first: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """The owner's run of the bundle script, with ``out`` as its arguments. It bundles ``uv``
    (default: the stub next to ``repo``) or, with ``path_first``, the ``uv`` that directory
    puts first on PATH; ``env`` adds to the environment."""
    full = {k: v for k, v in os.environ.items() if k not in {"FBC_UV_BINARY", "FBC_SHARED_DIR"}}
    if path_first is None:
        full["FBC_UV_BINARY"] = str(uv or repo.parent / BUNDLED_UV)
    else:
        full["PATH"] = f"{path_first}:{full['PATH']}"
    full.update(env or {})
    return subprocess.run(  # noqa: S603 - our own script
        ["/bin/bash", str(repo / "scripts" / "make_bundle.sh"), *map(str, out)],
        capture_output=True,
        text=True,
        check=False,
        env=full,
    )


@macos
def test_make_bundle_writes_a_checked_group_readable_bundle(tmp_path: Path) -> None:
    repo = small_repo(tmp_path / "repo")
    out = tmp_path / "shared" / "ddiver-fb"
    out.parent.mkdir()
    done = make_bundle(repo, out)
    assert done.returncode == 0, done.stderr
    assert "Wrote" in done.stdout
    assert stat.S_IMODE(out.stat().st_mode) == 0o750
    names = {p.name for p in out.iterdir()}
    assert names == {
        "ddiver_facebook.bundle",
        "ddiver_facebook.bundle.sha256",
        "setup-fbc-user.sh",
        "uv",
        MARKER,
    }
    assert (out / MARKER).read_text().startswith("Written by ddiver_facebook's")
    for path in out.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o640, path.name
        assert path.stat().st_gid == 20  # staff: every local account, nobody else
    check = subprocess.run(
        ["/usr/bin/shasum", "-a", "256", "-c", "ddiver_facebook.bundle.sha256"],
        cwd=out,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stdout
    assert "setup-fbc-user.sh: OK" in check.stdout
    assert "uv: OK" in check.stdout
    assert (out / "uv").read_bytes() == (tmp_path / BUNDLED_UV).read_bytes()
    clone = tmp_path / "clone"
    git(
        "clone",
        "--quiet",
        "--branch",
        "main",
        str(out / "ddiver_facebook.bundle"),
        str(clone),
        cwd=tmp_path,
    )
    assert git("rev-parse", "HEAD", cwd=clone) == git("rev-parse", "main", cwd=repo)
    published = (out / "setup-fbc-user.sh").read_text()
    assert PIN_LINE.sub("", published) == PIN_LINE.sub("", SETUP.read_text())


@macos
def test_make_bundle_carries_only_the_pinned_uv(tmp_path: Path) -> None:
    repo = small_repo(tmp_path / "repo")
    other = tmp_path / "other-uv"
    write_uv(other, STUB_UV.replace("uv 0.11.7", "uv 0.11.8"))
    done = make_bundle(repo, tmp_path / "out", uv=other)
    assert done.returncode != 0
    assert "is not the uv 0.11.7 build setup-fbc-user.sh pins" in done.stderr
    assert not (tmp_path / "out").exists()
    done = make_bundle(repo, tmp_path / "out", uv=tmp_path / "nowhere")
    assert done.returncode != 0
    assert "no uv to bundle" in done.stderr
    assert not (tmp_path / "out").exists()
    # Without FBC_UV_BINARY it is this account's own uv, the first on PATH.
    own = tmp_path / "bin"
    own.mkdir()
    write_uv(own / "uv", STUB_UV + "# another build\n")
    done = make_bundle(repo, tmp_path / "out", path_first=own)
    assert done.returncode != 0
    assert f"{own / 'uv'} is not the uv 0.11.7 build" in done.stderr
    assert not (tmp_path / "out").exists()
    write_uv(own / "uv")
    done = make_bundle(repo, tmp_path / "out", path_first=own)
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "out" / "uv").read_bytes() == (own / "uv").read_bytes()


def test_the_setup_script_pins_uv_0_11_7_as_astral_released_it() -> None:
    text = SETUP.read_text()
    assert "\nreadonly UV_VERSION=0.11.7\n" in text
    assert dict(PIN_LINE.findall(text)) == {
        "UV_SHA256_ARM64": UV_PINS["arm64"],
        "UV_SHA256_X86_64": UV_PINS["x86_64"],
    }
    assert "curl" not in text  # nothing is downloaded, let alone piped into a shell


@macos
def test_this_macs_own_uv_0_11_7_is_the_pinned_build() -> None:
    """The owner's uv 0.11.7 (from the official installer) is the build the bundle carries."""
    own = Path.home() / ".local" / "bin" / "uv"
    if platform.machine() not in UV_PINS or not own.is_file():
        pytest.skip("no ~/.local/bin/uv on this Mac")
    version = subprocess.run(  # noqa: S603 - this account's own uv
        [str(own), "--version"], capture_output=True, text=True, check=False
    ).stdout
    if not version.startswith("uv 0.11.7 "):
        pytest.skip(f"this Mac's uv is {version.strip()!r}")
    assert hashlib.sha256(own.read_bytes()).hexdigest() == UV_PINS[platform.machine()]


@macos
@pytest.mark.parametrize(
    "name",
    [
        ".env",
        "secrets",
        "config/monitored.toml",
        "state.sqlite",
        "config/bot.session",
        "config/Cookies",
        "config/1727000000-r00000001-s00.step.json.gz",
    ],
)
def test_make_bundle_refuses_a_main_that_tracks_secrets(tmp_path: Path, name: str) -> None:
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    git("init", "--quiet", "--initial-branch=main", cwd=repo)
    (repo / "scripts").mkdir()
    shutil.copy2(MAKE_BUNDLE, repo / "scripts" / "make_bundle.sh")
    (repo / name).write_text("x")
    git("add", "-A", cwd=repo)
    git("commit", "--quiet", "-m", "oops", cwd=repo)
    done = make_bundle(repo, tmp_path / "out")
    assert done.returncode != 0
    assert "may hold secrets" in done.stderr
    assert not (tmp_path / "out").exists()


@macos
@pytest.mark.parametrize("how", ["deleted", "renamed", "on a merged branch"])
def test_make_bundle_refuses_a_secret_anywhere_in_mains_history(tmp_path: Path, how: str) -> None:
    # The bundle carries main's whole history: a secret committed once and gone from the tip
    # would still reach every local account.
    repo = small_repo(tmp_path / "repo")
    if how == "on a merged branch":
        git("checkout", "--quiet", "-b", "side", cwd=repo)
    (repo / ".env").write_text("BOT_TOKEN=x\n")
    git("add", ".env", cwd=repo)
    git("commit", "--quiet", "-m", "oops", cwd=repo)
    if how == "renamed":
        git("mv", ".env", "env.txt", cwd=repo)
    else:
        git("rm", "--quiet", ".env", cwd=repo)
    git("commit", "--quiet", "-m", "tidy", cwd=repo)
    if how == "on a merged branch":
        git("checkout", "--quiet", "main", cwd=repo)
        git("merge", "--quiet", "--no-ff", "-m", "merge", "side", cwd=repo)
    assert ".env" not in git("ls-tree", "-r", "--name-only", "main", cwd=repo).split()
    done = make_bundle(repo, tmp_path / "out")
    assert done.returncode != 0
    assert "may hold secrets" in done.stderr
    assert ".env" in done.stderr
    assert not (tmp_path / "out").exists()


CAPTURE = json.dumps(
    {"final_url": "https://www.facebook.com/", "lost_" + "bodies": 0, "pagination_" + "fired": 0}
).encode()
"""A raw step as the collector saves it (keys in pieces, or this file would be one)."""


@macos
@pytest.mark.parametrize(
    ("name", "data", "rule"),
    [
        ("notes.txt", f"alert bot: {TOKEN}\n".encode(), "telegram-bot-token"),
        ("tests/fixtures/debug.json", CAPTURE, "fbcollector-raw-step"),
        ("docs/feed.json.gz", gzip.compress(CAPTURE), "fbcollector-raw-step"),
    ],
    ids=["token in notes", "capture as a fixture", "gzipped capture"],
)
def test_make_bundle_refuses_a_secret_by_its_content_whatever_its_name(
    tmp_path: Path, name: str, data: bytes, rule: str
) -> None:
    repo = small_repo(tmp_path / "repo")
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_bytes(data)
    git("add", name, cwd=repo)
    git("commit", "--quiet", "-m", "oops", cwd=repo)
    added = git("rev-parse", "--short=10", "HEAD", cwd=repo)
    git("rm", "--quiet", name, cwd=repo)
    git("commit", "--quiet", "-m", "tidy", cwd=repo)
    done = make_bundle(repo, tmp_path / "out")
    assert done.returncode != 0
    assert f"{name}: {rule} (" in done.stderr
    assert f"since commit {added}" in done.stderr
    assert "holds what may be secrets" in done.stderr
    assert TOKEN not in done.stdout + done.stderr  # never the text itself
    assert not (tmp_path / "out").exists()


@macos
def test_make_bundle_takes_a_reviewed_fixture_and_refuses_a_scan_that_cannot_run(
    tmp_path: Path,
) -> None:
    repo = small_repo(tmp_path / "repo")
    (repo / "tests").mkdir()
    (repo / "tests" / "cookie.txt").write_text(f"a made-up bot token: {TOKEN}\n")
    git("add", "tests", cwd=repo)
    git("commit", "--quiet", "-m", "fixture", cwd=repo)
    assert make_bundle(repo, tmp_path / "out").returncode != 0
    digest = hashlib.sha256(TOKEN.encode()).hexdigest()[:16]
    allow = repo / "scripts" / "secret_scan_allow.txt"
    allow.write_text(f"telegram-bot-token tests/cookie.txt {digest}  # made up for a test\n")
    git("add", str(allow), cwd=repo)
    git("commit", "--quiet", "-m", "reviewed", cwd=repo)
    done = make_bundle(repo, tmp_path / "out")
    assert done.returncode == 0, done.stderr
    assert "(1 allow-listed values)" in done.stderr
    allow.write_text("telegram-bot-token tests/cookie.txt\n")
    git("commit", "--quiet", "-am", "broken", cwd=repo)
    done = make_bundle(repo, tmp_path / "out2")
    assert done.returncode != 0
    assert "the secret scan could not run" in done.stderr
    assert not (tmp_path / "out2").exists()


@macos
def test_make_bundle_refuses_a_main_that_moved_while_it_was_scanned(tmp_path: Path) -> None:
    """Review F3: main was looked up again at every step, so a commit that landed on it during
    the scan (a token, here) was bundled without either scan seeing it."""
    repo = small_repo(tmp_path / "repo")
    scanned = git("rev-parse", "main", cwd=repo)
    wrapper = repo / ".venv" / "bin" / "python"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        "#!/bin/bash\n"
        f'"{sys.executable}" "$@"; status=$?\n'
        f"printf 'alert bot: %s\\n' \"$LATE_TOKEN\" >'{repo}/notes.txt'\n"
        f"git -C '{repo}' add notes.txt\n"
        f"git -C '{repo}' -c user.name=t -c user.email=t@example.invalid commit -qm late\n"
        "exit $status\n"
    )
    wrapper.chmod(0o755)
    out = tmp_path / "out"
    done = make_bundle(repo, out, env={"LATE_TOKEN": TOKEN})
    assert done.returncode != 0
    assert "nothing that looks like a secret" in done.stderr  # the scan saw only `scanned`
    assert f"main moved while it was scanned (from {scanned[:10]})" in done.stderr
    assert git("log", "-1", "--format=%s", "main", cwd=repo) == "late"
    assert sorted(p.name for p in out.iterdir()) == [MARKER]  # nothing published


@macos
def test_make_bundle_wants_an_absolute_place(tmp_path: Path) -> None:
    repo = small_repo(tmp_path / "repo")
    done = make_bundle(repo, Path("relative"))
    assert done.returncode != 0
    assert "absolute" in done.stderr
    done = make_bundle(repo, tmp_path / "a", tmp_path / "b")
    assert done.returncode != 0
    assert "usage" in done.stderr
    assert not (tmp_path / "a").exists()
    assert not (tmp_path / "b").exists()


# -- where the bundle may go ------------------------------------------------------------------


@dataclass
class Places:
    """A home, a temporary folder and a checkout of their own, so that no case below could ever
    touch the real ones, even with the checks broken. The system paths among the cases are all
    root's, which an owner's run cannot change in any case."""

    home: Path
    tmp: Path
    repo: Path
    env: dict[str, str]

    def resolve(self, where: str) -> str:
        links = self.repo.parent
        return (
            where.replace("ALIAS", str(links / "alias"))  # -> the checkout
            .replace("ROOTLINK", str(links / "rootlink"))  # -> /
            .replace("HOMEPARENT", str(self.home.parent))
            .replace("HOME", str(self.home))
            .replace("TMPDIR", str(self.tmp))
            .replace("REPOPARENT", str(self.repo.parent))
            .replace("REPO", str(self.repo))
        )


@pytest.fixture
def places(tmp_path: Path) -> Places:
    home = tmp_path / "home" / "owner"
    (home / "Desktop").mkdir(parents=True)
    tmp = tmp_path / "var" / "T"
    tmp.mkdir(parents=True)
    repo = small_repo(tmp_path / "work" / "repo")
    (repo.parent / "alias").symlink_to(repo)
    (repo.parent / "rootlink").symlink_to("/")
    return Places(home, tmp, repo, {"HOME": str(home), "TMPDIR": f"{tmp}/"})


@macos
@pytest.mark.parametrize(
    ("where", "why"),
    [
        ("/", "the root of the disk"),
        ("//", "the root of the disk"),
        ("/tmp", "a top-level folder of the disk"),  # noqa: S108 - refused, never written
        ("/Users", "a top-level folder of the disk"),
        ("/Users/Shared", "a home folder, or /Users/Shared itself"),
        ("/Users/Shared/", "a home folder, or /Users/Shared itself"),
        ("/private/tmp", "a system folder"),
        ("/Library/ddiver-fb", "inside a system folder"),
        ("/System/Library/ddiver-fb", "inside a system folder"),
        ("/Volumes/ddiver-fb", "a whole volume"),
        ("HOME", "your home folder"),
        ("HOME/", "your home folder"),
        ("HOME/Desktop", "a folder of your home"),
        ("HOMEPARENT", "a folder above your home"),
        ("TMPDIR", "the temporary folder"),
        ("REPO", "the checkout itself"),
        ("REPO/dist", "inside the checkout"),
        ("REPOPARENT", "a folder above the checkout"),
        ("REPOPARENT/./out", "not a plain path"),
        ("REPOPARENT/x/../out", "not a plain path"),
        ("REPOPARENT//out", "not a plain path"),
        ("REPOPARENT/missing/out", "missing does not exist"),
        # Refused where the links lead, though not by the path as given
        ("ALIAS/dist", "inside the checkout"),
        ("ROOTLINK/Users", "a top-level folder of the disk"),
    ],
)
def test_make_bundle_refuses_a_place_it_must_not_take_over(
    places: Places, where: str, why: str
) -> None:
    before = {p: stat.S_IMODE(p.stat().st_mode) for p in (places.home, places.tmp, places.repo)}
    done = make_bundle(places.repo, places.resolve(where), env=places.env)
    assert done.returncode != 0
    assert f"refusing OUT_DIR {places.resolve(where).rstrip('/') or '/'}" in done.stderr
    assert why in done.stderr
    assert before == {p: stat.S_IMODE(p.stat().st_mode) for p in before}
    assert not (places.repo / "dist").exists()
    assert not (places.repo.parent / "out").exists()
    assert list(places.tmp.iterdir()) == []


@macos
def test_make_bundle_takes_a_new_empty_or_marked_directory_and_nothing_else(
    tmp_path: Path,
) -> None:
    repo = small_repo(tmp_path / "repo")
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    (out / "notes.txt").write_text("mine\n")
    done = make_bundle(repo, out)
    assert done.returncode != 0
    assert "it holds what this script did not write: notes.txt" in done.stderr
    assert stat.S_IMODE(out.stat().st_mode) == 0o700  # judged before it was changed
    assert [p.name for p in out.iterdir()] == ["notes.txt"]
    (out / "notes.txt").unlink()
    done = make_bundle(repo, out)  # empty: taken, and marked as this script's
    assert done.returncode == 0, done.stderr
    assert stat.S_IMODE(out.stat().st_mode) == 0o750
    assert (out / MARKER).is_file()
    assert make_bundle(repo, out).returncode == 0  # marked: taken again
    (out / ".hidden").write_text("x\n")
    done = make_bundle(repo, out)
    assert done.returncode != 0
    assert "did not write: .hidden" in done.stderr
    (out / ".hidden").unlink()
    (out / MARKER).unlink()  # a bundle without the marker: only the default may be one
    done = make_bundle(repo, out)
    assert done.returncode != 0
    assert "neither empty nor marked" in done.stderr
    (out / MARKER).write_text("my own notes\n")
    done = make_bundle(repo, out)
    assert done.returncode != 0
    assert f"its {MARKER} is not this script's" in done.stderr


@macos
def test_make_bundle_refuses_links_and_outputs_that_are_not_plain_files(tmp_path: Path) -> None:
    repo = small_repo(tmp_path / "repo")
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / "link").symlink_to(target)
    done = make_bundle(repo, tmp_path / "link")
    assert done.returncode != 0
    assert "it is a symbolic link" in done.stderr
    assert list(target.iterdir()) == []
    (tmp_path / "file").write_text("x\n")
    done = make_bundle(repo, tmp_path / "file")
    assert done.returncode != 0
    assert "it is not a directory" in done.stderr
    out = tmp_path / "out"
    out.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    (out / "uv").symlink_to(victim)
    done = make_bundle(repo, out)
    assert done.returncode != 0
    assert "uv is not a plain file of yours" in done.stderr
    assert victim.read_text() == "keep\n"
    assert [p.name for p in out.iterdir()] == ["uv"]
    (out / "uv").unlink()
    (out / "uv").mkdir()
    done = make_bundle(repo, out)
    assert done.returncode != 0
    assert "uv is not a plain file of yours" in done.stderr


@macos
@pytest.mark.skipif(
    not Path("/var/empty").is_dir() or Path("/var/empty").stat().st_uid == os.getuid(),
    reason="no /var/empty of root's",
)
def test_make_bundle_refuses_a_directory_of_someone_elses(tmp_path: Path) -> None:
    repo = small_repo(tmp_path / "repo")
    done = make_bundle(repo, "/var/empty")
    assert done.returncode != 0
    assert "it belongs to root, not to you" in done.stderr


@macos
@pytest.mark.parametrize(
    ("where", "setup", "why"),
    [
        ("parent", "chmod 777", "is writable by others and not sticky"),
        ("parent", "chmod 775", "is writable by others and not sticky"),
        ("above", "chmod 777", "is writable by others and not sticky"),
        (
            "parent",
            "chmod +a|everyone allow add_file,delete_child",
            "lets others add or remove what is in it (an access control entry)",
        ),
        ("parent", "owned by fbc", "belongs to fbc"),
        ("parent", "chmod 1777", None),
        ("parent", "chmod 1775", None),
        ("parent", "chmod +a|everyone deny delete", None),  # as on every home folder
    ],
)
def test_make_bundle_refuses_a_directory_another_account_could_swap_for_a_link(
    tmp_path: Path, where: str, setup: str, why: str | None
) -> None:
    """Review F6: chgrp and chmod follow a symbolic link, and the directory is judged seconds
    before them. An account that can rename entries in a folder above it (`fbc`, in one it
    made under /Users/Shared) could swap it for a link to ~/.ssh in between."""
    repo = small_repo(tmp_path / "repo")
    above = tmp_path / "above"
    parent = above / "shared"
    parent.mkdir(parents=True)
    target = parent if where == "parent" else above
    env: dict[str, str] = {}
    if setup == "owned by fbc":  # no other account to make one with: `stat` says so
        stub = tmp_path / "stub"
        stub.mkdir()
        (stub / "stat").write_text(
            '#!/bin/bash\nif [ "$1 $2 $3" = "-f %Su $OTHERS" ]; then echo fbc; exit 0; fi\n'
            'exec /usr/bin/stat "$@"\n'
        )
        (stub / "stat").chmod(0o755)
        env = {"PATH": f"{stub}:{os.environ['PATH']}", "OTHERS": str(target.resolve())}
    else:
        mode = setup.removeprefix("chmod ").split("|")
        subprocess.run(["/bin/chmod", *mode, str(target)], check=True)  # noqa: S603
    out = parent / "ddiver-fb"
    try:
        done = make_bundle(repo, out, env=env)
    finally:
        subprocess.run(["/bin/chmod", "-N", str(target)], check=True)  # noqa: S603
        target.chmod(0o755)
    if why is None:
        assert done.returncode == 0, done.stderr
        return
    assert done.returncode != 0
    assert f"refusing OUT_DIR {out}: {target.resolve()} {why}" in done.stderr
    assert not out.exists()


@macos
def test_make_bundle_takes_over_the_default_directory_an_older_run_left(tmp_path: Path) -> None:
    """The default directory may hold a bundle without the marker: it was made before the
    marker existed. Nothing else is taken without it."""
    repo = small_repo(tmp_path / "repo")
    shared = tmp_path / "Shared" / "ddiver-fb"
    shared.mkdir(parents=True, mode=0o750)
    for name in ("ddiver_facebook.bundle", "ddiver_facebook.bundle.sha256", "setup-fbc-user.sh"):
        (shared / name).write_text("old\n")
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKeyOfTheOperator fbc-operator\n"
    (shared / "fbc_local.pub").write_text(key)  # RUNBOOK section 12 puts it there
    default = {"FBC_SHARED_DIR": str(shared)}
    done = make_bundle(repo, env=default)
    assert done.returncode == 0, done.stderr
    assert f"Wrote {shared}/ddiver_facebook.bundle" in done.stdout
    assert (shared / MARKER).is_file()
    assert (shared / "fbc_local.pub").read_text() == key
    assert (shared / "ddiver_facebook.bundle").read_bytes() != b"old\n"
    (shared / MARKER).unlink()
    (shared / "notes.txt").write_text("x\n")
    done = make_bundle(repo, env=default)
    assert done.returncode != 0
    assert "did not write: notes.txt" in done.stderr


# -- the setup script -------------------------------------------------------------------------


STUB_ID = """#!/bin/bash
case "$1" in
  -un) cat "$STUB_STATE/user" ;;
  -Gn) cat "$STUB_STATE/groups" ;;
  -u) echo 501 ;;
  *) exec /usr/bin/id "$@" ;;
esac
"""
STUB_LOG = """#!/bin/bash
echo "$(basename "$0") $*" >>"$STUB_STATE/calls"
if [ "$(basename "$0")" = launchctl ]; then
  # The job: the last part of a gui/<uid>/<label> target, or the name of a bootstrapped plist.
  # The collector's state is in `loaded` and `stopping`, the tunnel's in `loaded.tunnel` and
  # `stopping.tunnel`.
  case "$1" in
    bootstrap) label="$(basename "$3" .plist)" ;;
    kickstart) label="${3##*/}" ;;
    *) label="${2##*/}" ;;
  esac
  suffix=""
  [ "$label" = com.ddiver.fbtunnel ] && suffix=.tunnel
  loaded="$STUB_STATE/loaded$suffix"
  stopping="$STUB_STATE/stopping$suffix"
  case "$1" in
    print)
      # After a bootout the agent stays loaded for `stopping` more looks, as a collector that
      # is still closing Chrome does, then it is gone.
      if [ -e "$stopping" ]; then
        left=$(cat "$stopping")
        if [ "$left" -gt 0 ]; then echo $((left - 1)) >"$stopping"; exit 0; fi
        rm -f "$stopping" "$loaded"
      fi
      [ -e "$loaded" ] || exit 113
      printf '%s = {\n\tstate = running\n\tpid = 4242\n\truns = 1\n' "gui/501/$label"
      printf '\tlast exit code = (never exited)\n\tprogram = /usr/bin/true\n}\n'
      ;;
    bootout)
      [ -e "$stopping" ] || echo 0 >"$stopping"
      ;;
    bootstrap)
      [ -e "$loaded" ] && exit 5
      # `bootstrap-fails` (or `.tunnel`): launchd refuses to load the job.
      if [ -e "$STUB_STATE/bootstrap-fails$suffix" ]; then
        echo "Bootstrap failed: 5: Input/output error" >&2
        exit 5
      fi
      touch "$loaded"
      ;;
  esac
fi
exit 0
"""
STUB_SLEEP = """#!/bin/bash
exit 0
"""
STUB_CURL = """#!/bin/bash
echo "curl $*" >>"$STUB_STATE/calls"
# The operator door's probe through the tunnel's socket (it must be there), answered with the
# code in `probe`; anything else is a download the scripts must never make.
sock=""
prev=""
for arg in "$@"; do
  if [ "$prev" = --unix-socket ]; then sock="$arg"; fi
  prev="$arg"
done
if [ "${@: -1}" = http://127.0.0.1:8781/v1/control ] && [ "$sock" = "$HOME/.ssh/fbtunnel.sock" ] \
  && [ -S "$sock" ] && [ -e "$STUB_STATE/probe" ]; then
  printf '%s' "$(cat "$STUB_STATE/probe")"
  exit 0
fi
printf '000'
exit 7
"""
STUB_SSH = """#!/bin/bash
echo "ssh $*" >>"$STUB_STATE/calls"
# `ssh -G` prints the configuration it would use and never connects: the real one does that.
# Anything else would reach a network host.
for arg in "$@"; do
  if [ "$arg" = -G ]; then exec /usr/bin/ssh "$@"; fi
done
exit 255
"""
STUB_UV = """#!/bin/bash
echo "uv $*" >>"$STUB_STATE/calls"
if [ "$1" = --version ]; then echo "uv 0.11.7 (stub 2026-04-15 aarch64-apple-darwin)"; exit 0; fi
mkdir -p .venv/bin
cat >.venv/bin/fbcollector <<'EOF'
#!/bin/bash
# check-config's mode follows the secrets file, as the real one's does; `config-refused` in the
# stub state makes it refuse the settings.
echo "fbcollector $*" >>"$STUB_STATE/calls"
echo "root: stub"
if grep -q '^FBC_INGEST_URL=' "$HOME/Library/Application Support/fbcollector/secrets" 2>/dev/null
then echo "mode: uplink"
else echo "mode: local (phase 0)"
fi
if [ -e "$STUB_STATE/config-refused" ]; then echo "missing for run: FBC_COLLECTOR_ID"; exit 6; fi
echo "ok"
EOF
chmod 755 .venv/bin/fbcollector
"""


@dataclass
class World:
    home: Path
    shared: Path
    state: Path
    repo: Path
    env: dict[str, str]

    def calls(self) -> list[str]:
        path = self.state / "calls"
        return path.read_text().splitlines() if path.exists() else []

    @property
    def root(self) -> Path:
        return self.home / "Library" / "Application Support" / "fbcollector"

    @property
    def checkout(self) -> Path:
        return self.home / "ddiver_facebook"

    @property
    def uv(self) -> Path:
        return self.home / ".local" / "bin" / "uv"

    def publish(self) -> None:
        """What make_bundle.sh does, from the small repository."""
        done = make_bundle(self.repo, self.shared)
        assert done.returncode == 0, done.stderr


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    # The home is a short temporary directory of its own: the tunnel's socket path in it must
    # fit a Unix socket's 104 bytes, and the real `ssh -G` refuses a config whose path does not
    # ("Bad forwarding specification"). pytest's own directory for a test is longer than that.
    short = Path(tempfile.mkdtemp(prefix="fbc")).resolve()
    home = short / "fbc"
    home.mkdir()
    assert len(str(home / ".ssh" / "fbtunnel.sock")) <= 103
    state = tmp_path / "state"
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    state.mkdir()
    (state / "user").write_text("fbc\n")
    (state / "groups").write_text("staff everyone localaccounts\n")
    for name, body in (
        ("id", STUB_ID),
        ("launchctl", STUB_LOG),
        ("sleep", STUB_SLEEP),
        ("defaults", STUB_LOG),
        ("xcode-select", STUB_LOG),
        ("curl", STUB_CURL),
        ("ssh", STUB_SSH),
    ):
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    chrome = tmp_path / "Google Chrome.app"
    chrome.mkdir()
    repo = small_repo(tmp_path / "repo")
    shared = tmp_path / "shared" / "ddiver-fb"
    shared.parent.mkdir()
    env = {
        "HOME": str(home),
        "PATH": f"{stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
        "STUB_STATE": str(state),
        "FBC_SHARED_DIR": str(shared),
        "FBC_CHROME_APP": str(chrome),
        "LANG": "C",
        "TERM": "dumb",
    }
    made = World(home, shared, state, repo, env)
    made.publish()
    try:
        yield made
    finally:
        shutil.rmtree(short, ignore_errors=True)


def run_setup(
    world: World,
    *args: str,
    answers: tuple[str, ...] = (),
    tty: bool = True,
    stdin: str | None = None,
) -> tuple[int, str]:
    """The published setup script, as the collector's user runs it in Terminal (or, with
    ``stdin``, without a terminal, as the operator door runs it with what its caller piped)."""
    command = ["/bin/bash", str(world.shared / "setup-fbc-user.sh"), *args]
    if stdin is not None:
        done = subprocess.run(  # noqa: S603 - our own script
            command,
            env=world.env,
            capture_output=True,
            text=True,
            check=False,
            input=stdin,
            timeout=60,
        )
        return done.returncode, done.stdout + done.stderr
    if not tty:
        done = subprocess.run(  # noqa: S603 - our own script
            command,
            env=world.env,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        return done.returncode, done.stdout + done.stderr
    master, slave = pty.openpty()
    process = subprocess.Popen(  # noqa: S603 - our own script
        command, env=world.env, stdin=slave, stdout=slave, stderr=slave, close_fds=True
    )
    os.close(slave)
    output = b""
    pending = list(answers)
    answered = 0  # every prompt up to this offset of the output has had its answer
    deadline = time.monotonic() + 60
    while True:
        assert time.monotonic() < deadline, output.decode(errors="replace")
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            try:
                chunk = os.read(master, 4096)
            except OSError:  # the other side closed: the script is done
                chunk = b""
            if not chunk and process.poll() is not None:
                break
            output += chunk
            while pending:
                found = {p: output.find(p, answered) for p in PROMPTS}
                hits = {p: at for p, at in found.items() if at >= 0}
                if not hits:
                    break
                prompt = min(hits, key=hits.__getitem__)
                answered = hits[prompt] + 1
                if prompt in SILENT:
                    wait_for_no_echo(master)
                os.write(master, (pending.pop(0) + "\n").encode())
        elif process.poll() is not None:
            break
    os.close(master)
    return process.wait(), output.decode(errors="replace").replace("\r\n", "\n")


PROMPTS = (b"Alert bot token", b"Telegram user id", b"ingest token", b"host key line")
SILENT = frozenset({b"Alert bot token", b"ingest token"})
"""The prompts the script asks with ``read -s`` (no echo)."""


def wait_for_no_echo(master: int) -> None:
    """Until the terminal has stopped echoing: bash prints a ``read -s`` prompt *before* it
    turns echo off, so an answer typed as soon as the prompt shows could still be echoed.

    Typing only then makes "read without echo" a real check rather than a timing accident.
    """
    deadline = time.monotonic() + 5
    while termios.tcgetattr(master)[3] & termios.ECHO:
        assert time.monotonic() < deadline, "the terminal never stopped echoing"
        time.sleep(0.001)


@macos
def test_setup_installs_everything_once(world: World) -> None:
    code, out = run_setup(world, answers=(TOKEN, OWNER_ID))
    assert code == 0, out
    assert TOKEN not in out  # read without echo
    calls = world.calls()
    assert not any(c.startswith("curl") for c in calls)  # nothing downloaded
    assert "installing uv 0.11.7" in out
    assert world.uv.read_bytes() == (world.shared / "uv").read_bytes()
    assert stat.S_IMODE(world.uv.stat().st_mode) == 0o755
    assert [p.name for p in world.uv.parent.iterdir()] == ["uv"]
    assert "uv sync --frozen --package fbcollector --no-dev" in calls
    assert git("rev-parse", "HEAD", cwd=world.checkout) == git("rev-parse", "main", cwd=world.repo)

    assert stat.S_IMODE(world.root.stat().st_mode) == 0o700
    secrets = world.root / "secrets"
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o600
    assert secrets.read_text() == (
        "FBC_TIMEZONE=Europe/Sofia\nFBC_OBSERVE_MODE=true\n"
        f"FBC_ALERT_BOT_TOKEN={TOKEN}\nFBC_ALERT_OWNER_ID={OWNER_ID}\n"
    )
    monitored = world.root / "monitored.toml"
    assert monitored.read_bytes() == EXAMPLE.read_bytes()
    assert stat.S_IMODE(monitored.stat().st_mode) == 0o600

    agent = world.home / "Library" / "LaunchAgents" / "com.ddiver.fbcollector.plist"
    plist = plistlib.loads(agent.read_bytes())
    assert plist["LimitLoadToSessionType"] == "Aqua"
    assert plist["ProgramArguments"][3] == f"{world.checkout}/.venv/bin/fbcollector"
    assert plist["WorkingDirectory"] == str(world.checkout)
    assert plist["StandardOutPath"] == f"{world.home}/Library/Logs/fbcollector.log"
    assert "__" not in agent.read_text()
    assert not any(c.startswith("launchctl bootstrap") for c in calls)  # installed, not started
    assert "defaults write com.google.Chrome NSAppSleepDisabled -bool YES" in calls
    assert "fbcollector check-config" in calls
    profile = (world.home / ".zprofile").read_text()
    assert profile.count("# fbcollector") == 1
    assert f"{world.checkout}/.venv/bin" in profile
    for step in ("fbcollector pin-home", "fbcollector selftest", "fbcollector login"):
        assert step in out

    # Again: nothing asked, nothing duplicated, nothing cloned twice.
    code, again = run_setup(world, tty=False)
    assert code == 0, again
    assert "secrets: " in again and "kept" in again
    assert (world.home / ".zprofile").read_text().count("# fbcollector") == 1
    assert "cloning" not in again
    assert "uv: 0.11.7, the pinned build" in again
    assert "installing uv" not in again
    assert secrets.read_text().endswith(f"FBC_ALERT_OWNER_ID={OWNER_ID}\n")


@macos
def test_update_waits_for_a_collector_still_closing_chrome_before_starting_it(
    world: World,
) -> None:
    # Seen live: bootout returned while the collector was still closing Chrome, the bootstrap
    # right after it failed with "5: Input/output error" and the collector stayed stopped.
    assert run_setup(world, answers=(TOKEN, OWNER_ID))[0] == 0
    (world.state / "loaded").write_text("")
    (world.state / "stopping").write_text("3")
    before = len(world.calls())
    code, out = run_setup(world, "--update", tty=False)
    assert code == 0, out
    calls = [c for c in world.calls()[before:] if c.startswith("launchctl")]
    assert calls[-1].startswith("launchctl bootstrap")
    assert "collector started again" in out


def test_an_existing_operator_door_is_kept_in_step_with_the_checkout(world: World) -> None:
    assert run_setup(world, answers=(TOKEN, OWNER_ID))[0] == 0
    door = world.home / "bin" / "fbc-operator.sh"
    assert not door.exists()  # no door set up: nothing is created
    door.parent.mkdir(parents=True, exist_ok=True)
    door.write_text("#!/bin/bash\necho old\n")
    code, out = run_setup(world, "--update", tty=False)
    assert code == 0, out
    shipped = world.checkout / "deploy" / "macos" / "fbc-operator.sh"
    assert door.read_text() == shipped.read_text()
    assert door.stat().st_mode & 0o777 == 0o755
    assert "operator door" in out


def test_update_that_cannot_see_the_collector_stop_says_how_to_start_it(world: World) -> None:
    assert run_setup(world, answers=(TOKEN, OWNER_ID))[0] == 0
    (world.state / "loaded").write_text("")
    (world.state / "stopping").write_text("100000")
    before = len(world.calls())
    code, out = run_setup(world, "--update", tty=False)
    assert code == 0, out
    assert not any(c.startswith("launchctl bootstrap") for c in world.calls()[before:])
    assert "did not stop in time" in out
    assert "launchctl bootstrap gui/$(id -u)" in out


def test_update_stops_fast_forwards_and_starts_again(world: World) -> None:
    assert run_setup(world, answers=(TOKEN, OWNER_ID))[0] == 0
    (world.repo / "NEWS").write_text("two\n")
    git("add", "NEWS", cwd=world.repo)
    git("commit", "--quiet", "-m", "two", cwd=world.repo)
    world.publish()
    (world.state / "loaded").write_text("")
    before = len(world.calls())
    code, out = run_setup(world, "--update", tty=False)
    assert code == 0, out
    assert git("rev-parse", "HEAD", cwd=world.checkout) == git("rev-parse", "main", cwd=world.repo)
    calls = world.calls()[before:]
    order = [c.split()[0:2] for c in calls if c.startswith(("launchctl", "uv sync"))]
    assert order[:2] == [["launchctl", "print"], ["launchctl", "bootout"]]
    assert order.index(["uv", "sync"]) > 1
    assert order[-1] == ["launchctl", "bootstrap"]
    assert order.count(["launchctl", "bootstrap"]) == 1
    assert "collector started again" in out
    # An edited checkout is never overwritten.
    (world.checkout / "NEWS").write_text("edited by hand\n")
    code, out = run_setup(world, "--update", tty=False)
    assert code != 0
    assert "local changes" in out


@macos
def test_a_failing_tunnel_step_does_not_leave_the_update_half_done(
    world: World, host_line: str
) -> None:
    """The tunnel step runs last and on its own: when it fails, the collector the update
    stopped is started again and the operator door is refreshed; the update then fails."""
    assert run_setup(world, answers=(TOKEN, OWNER_ID))[0] == 0
    assert run_setup(world, "--pin-host-key", stdin=host_line + "\n")[0] == 0
    door = world.home / "bin" / "fbc-operator.sh"
    door.parent.mkdir(parents=True, exist_ok=True)
    door.write_text("#!/bin/bash\necho old\n")
    (world.state / "loaded").write_text("")  # the collector runs
    (world.state / "loaded.tunnel").unlink()  # the tunnel is not loaded...
    (world.state / "bootstrap-fails.tunnel").write_text("")  # ...and launchd refuses it
    before = len(world.calls())
    code, out = run_setup(world, "--update", tty=False)
    assert code != 0
    assert "WARNING: the tunnel step failed" in out
    assert "collector started again" in out
    assert (world.state / "loaded").exists()
    collector = world.home / "Library" / "LaunchAgents" / "com.ddiver.fbcollector.plist"
    assert f"launchctl bootstrap gui/501 {collector}" in calls_since(world, before, "launchctl")
    shipped = world.checkout / "deploy" / "macos" / "fbc-operator.sh"
    assert door.read_text() == shipped.read_text()
    # Once launchd takes it, the next update loads the tunnel.
    (world.state / "bootstrap-fails.tunnel").unlink()
    assert run_setup(world, "--update", tty=False)[0] == 0
    assert (world.state / "loaded.tunnel").exists()


@macos
def test_setup_refuses_the_wrong_account_and_a_bad_bundle(world: World) -> None:
    (world.state / "groups").write_text("staff admin everyone\n")
    code, out = run_setup(world, tty=False)
    assert code != 0
    assert "administrator" in out
    (world.state / "groups").write_text("staff everyone\n")
    maker = subprocess.run(  # noqa: S603
        ["/usr/bin/stat", "-f", "%Su", str(world.shared / "ddiver_facebook.bundle")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (world.state / "user").write_text(maker + "\n")
    code, out = run_setup(world, tty=False)
    assert code != 0
    assert "owner's account" in out
    (world.state / "user").write_text("fbc\n")
    bundle = world.shared / "ddiver_facebook.bundle"
    bundle.chmod(0o600)
    bundle.write_bytes(bundle.read_bytes() + b"tampered")
    code, out = run_setup(world, tty=False)
    assert code != 0
    assert "checksums" in out
    assert not world.checkout.exists()


@macos
def test_setup_replaces_an_uv_that_is_not_the_pinned_build_without_running_it(
    world: World,
) -> None:
    world.uv.parent.mkdir(parents=True)
    write_uv(world.uv, '#!/bin/bash\ntouch "$STUB_STATE/impostor-ran"\necho "uv 0.11.7"\n')
    code, out = run_setup(world, answers=(TOKEN, OWNER_ID))
    assert code == 0, out
    assert "is not the pinned uv 0.11.7 build; replacing it" in out
    assert not (world.state / "impostor-ran").exists()  # judged by its digest alone
    assert world.uv.read_bytes() == (world.shared / "uv").read_bytes()


def resum(shared: Path, *names: str) -> None:
    """Rewrite the checksum file over ``names``, as a bundle with those contents would have it."""
    sums = subprocess.run(  # noqa: S603
        ["/usr/bin/shasum", "-a", "256", *names], cwd=shared, capture_output=True, check=True
    ).stdout
    (shared / "ddiver_facebook.bundle.sha256").write_bytes(sums)


@macos
@pytest.mark.parametrize("how", ["changed", "another version", "missing"])
def test_setup_refuses_a_bundled_uv_that_is_not_the_pinned_build(world: World, how: str) -> None:
    bundled = world.shared / "uv"
    if how == "changed":  # the checksums agree: only the pin tells
        bundled.chmod(0o600)
        write_uv(bundled, STUB_UV + "# changed\n")
        resum(world.shared, "ddiver_facebook.bundle", "setup-fbc-user.sh", "uv")
        expected = "the bundle's uv is not uv 0.11.7 with SHA-256"
    elif how == "another version":  # pinned by its digest, but it says 0.11.70
        setup = world.repo / "deploy" / "macos" / "setup-fbc-user.sh"
        body = STUB_UV.replace("uv 0.11.7 (", "uv 0.11.70 (")
        pin_uv(setup, write_uv(world.repo.parent / BUNDLED_UV, body))
        git("commit", "--quiet", "-am", "another uv", cwd=world.repo)
        world.publish()
        expected = "the bundle's uv is not uv 0.11.7 with SHA-256"
    else:  # a bundle made before uv travelled with it
        bundled.unlink()
        resum(world.shared, "ddiver_facebook.bundle", "setup-fbc-user.sh")
        expected = "the bundle carries no uv"
    code, out = run_setup(world, tty=False)
    assert code != 0
    assert expected in out
    assert not world.uv.exists()
    if world.uv.parent.exists():
        assert list(world.uv.parent.iterdir()) == []  # no temporary copy left behind
    assert not world.checkout.exists()
    runs = [c for c in world.calls() if c.startswith("uv ")]
    assert runs == (["uv --version"] if how == "another version" else [])


@macos
def test_setup_asks_for_secrets_only_on_a_terminal(world: World) -> None:
    code, out = run_setup(world, tty=False)
    assert code != 0
    assert "asked for on a terminal" in out
    assert not (world.root / "secrets").exists()
    code, out = run_setup(world, answers=("not-a-token", TOKEN, "abc", OWNER_ID))
    assert code == 0, out
    assert TOKEN not in out
    assert "not-a-token" not in out  # a wrong answer is not echoed either
    assert "does not look like" in out
    assert "Digits only" in out
    assert (world.root / "secrets").read_text().count(TOKEN) == 1


@macos
def test_setup_help_and_bad_arguments(world: World) -> None:
    code, out = run_setup(world, "--help", tty=False)
    assert code == 0
    assert "--update" in out
    code, out = run_setup(world, "--frobnicate", tty=False)
    assert code == 2


def test_the_runbook_walks_the_owner_through_the_dedicated_user() -> None:
    runbook = (REPO / "docs" / "RUNBOOK.md").read_text()
    section = runbook.split("## 12. Phase 0 on the Mac under a dedicated user", 1)[1]
    order = [
        "Standard",
        "Fast User Switching",
        "scripts/make_bundle.sh",
        "bash /Users/Shared/ddiver-fb/setup-fbc-user.sh",
        "fbcollector pin-home",
        "fbcollector selftest",
        "fbcollector login",
        "Warm-up",
        "launchctl bootstrap",
        "--update",
        "After a reboot",
        "ssh -i ~/.ssh/fbc_local fbc@localhost",
    ]
    positions = [section.find(text) for text in order]
    assert all(p >= 0 for p in positions), dict(zip(order, positions, strict=True))
    assert positions == sorted(positions)
    assert "Chrome Beta is not used" in " ".join(section.split())
    design = " ".join((REPO / "DESIGN.md").read_text().split())
    assert "dedicated Standard macOS user" in design
    assert "not Chrome Beta" in design


# -- the tunnel to the brain and the switch (RUNBOOK section 9) ------------------------------


INGEST_TOKEN = "Ing3st-t0ken." + "x" * 20 + "/+=~_"
"""A made-up brain ingest token of the allowed alphabet, 38 characters."""
VPS = "vps.example.net"
TUNNEL = "com.ddiver.fbtunnel"


def ed25519_blob(where: Path, name: str) -> str:
    """The base64 part of a fresh ed25519 public key (a throwaway one, made with ssh-keygen)."""
    key = where / name
    subprocess.run(  # noqa: S603 - the system's ssh-keygen, a key in a temporary directory
        ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        stdin=subprocess.DEVNULL,
    )
    return key.with_suffix(".pub").read_text().split()[1]


@pytest.fixture
def host_line(tmp_path: Path) -> str:
    """The VPS's host key as the owner pins it: ``<host> ssh-ed25519 <key>``."""
    (tmp_path / "vps").mkdir()
    return f"{VPS} ssh-ed25519 {ed25519_blob(tmp_path / 'vps', 'host')}"


def tunnel_paths(world: World) -> dict[str, Path]:
    ssh = world.home / ".ssh"
    return {
        "key": ssh / "fbtunnel_ed25519",
        "pub": ssh / "fbtunnel_ed25519.pub",
        "pin": ssh / "fbtunnel_known_hosts",
        "config": ssh / "fbtunnel_client.config",
        "agent": world.home / "Library" / "LaunchAgents" / f"{TUNNEL}.plist",
        "log": world.home / "Library" / "Logs" / "fbtunnel.log",
        "socket": ssh / "fbtunnel.sock",
    }


def unix_socket(path: Path) -> None:
    """A Unix socket file at ``path``, as ssh leaves one; bound by its name from inside its
    directory, since a temporary home's full path may pass a socket's 104-byte limit."""
    here = Path.cwd()
    os.chdir(path.parent)
    try:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(path.name)
    finally:
        os.chdir(here)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def door(world: World, command: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    """The operator door the setup keeps in step with the checkout, as sshd runs it for the
    operator's key: ``command`` as SSH_ORIGINAL_COMMAND, ``stdin`` what the caller piped in."""
    return subprocess.run(  # noqa: S603 - our own script
        ["/bin/bash", str(world.checkout / "deploy" / "macos" / "fbc-operator.sh")],
        env={**world.env, "SSH_ORIGINAL_COMMAND": command},
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def set_up(world: World) -> None:
    code, out = run_setup(world, answers=(TOKEN, OWNER_ID))
    assert code == 0, out


def calls_since(world: World, before: int, prefix: str = "") -> list[str]:
    return [c for c in world.calls()[before:] if c.startswith(prefix)]


@macos
def test_the_tunnel_key_is_made_once_and_the_tunnel_waits_for_the_host_key(world: World) -> None:
    set_up(world)
    paths = tunnel_paths(world)
    assert mode(world.home / ".ssh") == 0o700
    assert mode(paths["key"]) == 0o600
    assert mode(paths["pub"]) == 0o644
    kind, _blob, comment = paths["pub"].read_text().split()
    assert (kind, comment) == ("ssh-ed25519", "fbtunnel-mac-1")
    # No pin: no config, no LaunchAgent, nothing loaded or asked of launchd for the tunnel.
    assert not paths["config"].exists()
    assert not paths["agent"].exists()
    assert not any(TUNNEL in c for c in world.calls())
    assert not any(c.startswith("ssh ") for c in world.calls())
    key, pub = paths["key"].read_bytes(), paths["pub"].read_bytes()
    code, out = run_setup(world, "--tunnel", tty=False)
    assert code == 0, out
    assert "waiting for the host key" in out
    assert (paths["key"].read_bytes(), paths["pub"].read_bytes()) == (key, pub)  # never made twice
    paths["pub"].unlink()  # a lost public half is made again from the key, not a new key
    code, out = run_setup(world, "--update", tty=False)
    assert code == 0, out
    assert (paths["key"].read_bytes(), paths["pub"].read_bytes()) == (key, pub)
    # A key that is a link is refused, never followed or replaced.
    paths["key"].unlink()
    victim = world.home / "victim"
    victim.write_text("keep\n")
    paths["key"].symlink_to(victim)
    code, out = run_setup(world, "--tunnel", tty=False)
    assert code != 0
    assert "symbolic link" in out
    assert victim.read_text() == "keep\n"


@macos
def test_a_pinned_host_key_writes_the_config_and_starts_the_tunnel(
    world: World, host_line: str
) -> None:
    set_up(world)
    paths = tunnel_paths(world)
    before = len(world.calls())
    code, out = run_setup(world, "--pin-host-key", stdin=host_line + "\n")
    assert code == 0, out
    assert paths["pin"].read_text() == host_line + "\n"
    assert mode(paths["pin"]) == 0o600
    config = paths["config"].read_text()
    assert mode(paths["config"]) == 0o600
    assert f"    HostName {VPS}\n" in config and "    Port 22\n" in config
    assert "VPS_HOST" not in config and "~" not in config
    assert f"    IdentityFile {paths['key']}\n" in config
    assert f"    UserKnownHostsFile {paths['pin']}\n" in config
    # The local end is a socket in fbc's own 0700 ~/.ssh, not a TCP port another account
    # could bind while ssh is down (and then be handed the bearer token).
    assert f"    LocalForward {paths['socket']} 127.0.0.1:8781\n" in config
    assert "    StreamLocalBindUnlink yes\n" in config
    assert "    StreamLocalBindMask 0177\n" in config
    settings = [line for line in config.splitlines() if not line.startswith("#")]
    assert not any("%d" in line or "18781" in line for line in settings)
    agent = plistlib.loads(paths["agent"].read_bytes())
    assert agent["ProgramArguments"] == [
        "/usr/bin/ssh",
        "-N",
        "-F",
        str(paths["config"]),
        "fbtunnel",
    ]
    assert agent["StandardOutPath"] == str(paths["log"])
    assert "__" not in paths["agent"].read_text()
    launchd = calls_since(world, before, "launchctl")
    assert f"launchctl bootstrap gui/501 {paths['agent']}" in launchd
    assert any(c.startswith("ssh -G -F ") for c in world.calls()[before:])
    assert not any(c.startswith("ssh ") and " -G " not in c for c in world.calls())
    assert "SHA256:" in out  # both fingerprints, to compare by hand
    assert not any("com.ddiver.fbcollector" in c for c in launchd)  # the collector untouched

    # Again: nothing changed, nothing restarted.
    before = len(world.calls())
    code, out = run_setup(world, "--tunnel", tty=False)
    assert code == 0, out
    assert "tunnel: running, nothing changed" in out
    assert calls_since(world, before, "launchctl") == [f"launchctl print gui/501/{TUNNEL}"]

    # A config edited by hand is put back, and the tunnel restarted on it.
    paths["config"].write_text(config.replace("ServerAliveInterval 15", "ServerAliveInterval 99"))
    before = len(world.calls())
    code, out = run_setup(world, "--tunnel", tty=False)
    assert code == 0, out
    assert paths["config"].read_text() == config
    assert calls_since(world, before, "launchctl")[-1] == f"launchctl kickstart -k gui/501/{TUNNEL}"

    # A LaunchAgent that changed: out, gone, in again.
    paths["agent"].write_text(paths["agent"].read_text().replace("200", "60"))
    before = len(world.calls())
    code, out = run_setup(world, "--update", tty=False)
    assert code == 0, out
    order = [c.split()[1] for c in calls_since(world, before, "launchctl") if TUNNEL in c]
    assert order[-3:] == ["bootout", "print", "bootstrap"], order
    assert plistlib.loads(paths["agent"].read_bytes())["ThrottleInterval"] == 200


@macos
def test_the_pin_takes_another_port(world: World, host_line: str) -> None:
    set_up(world)
    blob = host_line.split()[2]
    code, out = run_setup(world, "--pin-host-key", stdin=f"[{VPS}]:2222 ssh-ed25519 {blob}\n")
    assert code == 0, out
    paths = tunnel_paths(world)
    assert paths["pin"].read_text() == f"[{VPS}]:2222 ssh-ed25519 {blob}\n"
    assert "    Port 2222\n" in paths["config"].read_text()


@macos
def test_a_bracketed_port_22_is_pinned_the_way_ssh_looks_it_up(
    world: World, host_line: str
) -> None:
    set_up(world)
    blob = host_line.split()[2]
    code, out = run_setup(world, "--pin-host-key", stdin=f"[{VPS}]:22 ssh-ed25519 {blob}\n")
    assert code == 0, out
    assert tunnel_paths(world)["pin"].read_text() == host_line + "\n"


@macos
@pytest.mark.parametrize(
    "line",
    [
        "",
        "{host} ssh-ed25519 {blob} comment",
        "{host} ssh-rsa {blob}",
        "{host}  ssh-ed25519 {blob}",
        " {host} ssh-ed25519 {blob}",
        "{host} ssh-ed25519 {blob} ",
        "VPS.example.net ssh-ed25519 {blob}",
        "@cert-authority {host} ssh-ed25519 {blob}",
        "{host},10.0.0.1 ssh-ed25519 {blob}",
        "|1|c2FsdA==|aGFzaA== ssh-ed25519 {blob}",
        "{host} ssh-ed25519 {blob}x",
        "{host} ssh-ed25519 AAAAB3NzaC1yc2EAAAADAQABAAABAQ{rest}",
        "[{host}]:0 ssh-ed25519 {blob}",
        "[{host}]:65536 ssh-ed25519 {blob}",
        "[{host}]:022 ssh-ed25519 {blob}",
        "{host} ssh-ed25519 {blob}\n{host} ssh-ed25519 {blob}",
    ],
)
def test_the_pin_is_one_strict_line(world: World, host_line: str, line: str) -> None:
    set_up(world)
    blob = host_line.split()[2]
    text = line.format(host=VPS, blob=blob, rest=blob[30:])
    before = len(world.calls())
    code, out = run_setup(world, "--pin-host-key", stdin=text + "\n")
    assert code != 0, out
    assert not tunnel_paths(world)["pin"].exists()
    assert not tunnel_paths(world)["config"].exists()
    assert not calls_since(world, before, "launchctl")


@macos
def test_a_different_pin_is_refused_and_the_same_one_is_taken(
    world: World, host_line: str, tmp_path: Path
) -> None:
    set_up(world)
    assert run_setup(world, "--pin-host-key", stdin=host_line + "\n")[0] == 0
    pin = tunnel_paths(world)["pin"]
    other = f"{VPS} ssh-ed25519 {ed25519_blob(tmp_path / 'vps', 'other')}"
    for changed in (other, host_line.replace(VPS, "vps2.example.net")):
        code, out = run_setup(world, "--pin-host-key", stdin=changed + "\n")
        assert code != 0
        assert "another host key is pinned already" in out
        assert "delete" in out
        assert pin.read_text() == host_line + "\n"
    code, out = run_setup(world, "--pin-host-key", stdin=host_line + "\n")
    assert code == 0, out
    assert "already pinned, the same line" in out
    # A pin file that is not one line of the shape is never replaced either.
    pin.write_text(host_line + "\n" + other + "\n")
    code, out = run_setup(world, "--pin-host-key", stdin=host_line + "\n")
    assert code != 0
    assert "another host key is pinned already" in out
    code, out = run_setup(world, "--tunnel", tty=False)
    assert code == 0, out
    assert "is not one ed25519 host key line" in out


@macos
def test_the_pin_can_be_typed_on_a_terminal(world: World, host_line: str) -> None:
    set_up(world)
    code, out = run_setup(world, "--pin-host-key", answers=(host_line,))
    assert code == 0, out
    assert tunnel_paths(world)["pin"].read_text() == host_line + "\n"


@macos
def test_the_tunnel_log_is_cut_beyond_one_mib(world: World, host_line: str) -> None:
    set_up(world)
    log = tunnel_paths(world)["log"]
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(f"line {n} {'x' * 100}\n" for n in range(20000)))
    assert run_setup(world, "--pin-host-key", stdin=host_line + "\n")[0] == 0
    lines = log.read_text().splitlines()
    assert len(lines) == 1000
    assert lines[-1].startswith("line 19999 ")
    small = "a short log\n"
    log.write_text(small)
    assert run_setup(world, "--tunnel", tty=False)[0] == 0
    assert log.read_text() == small


# -- the switch ------------------------------------------------------------------------------

SECRETS_BEFORE = (
    f"FBC_TIMEZONE=Europe/Sofia\nFBC_OBSERVE_MODE=true\n"
    f"FBC_ALERT_BOT_TOKEN={TOKEN}\nFBC_ALERT_OWNER_ID={OWNER_ID}\n"
)


def uplink_lines(world: World) -> str:
    """The four settings of the switch: the brain's address at the far end of the tunnel, and
    the tunnel's socket in fbc's own ~/.ssh that the requests go through."""
    return (
        "FBC_INGEST_URL=http://127.0.0.1:8781\n"
        f"FBC_INGEST_SOCKET={world.home}/.ssh/fbtunnel.sock\n"
        f"FBC_INGEST_TOKEN='{INGEST_TOKEN}'\n"
        "FBC_COLLECTOR_ID=mac-1\n"
    )


def argv_tripwire(world: World) -> None:
    """Every external command the setup script may name, wrapped so that its arguments are
    recorded in ``argv``: a token passed to one of them would show there."""
    stubs = Path(world.env["PATH"].split(":")[0])
    for name in [
        "cat",
        "cp",
        "mv",
        "rm",
        "chmod",
        "mkdir",
        "mktemp",
        "sed",
        "grep",
        "awk",
        "tr",
        "head",
        "tail",
        "cmp",
        "stat",
        "shasum",
        "basename",
        "dirname",
        "seq",
        "printf",
        "echo",
        "env",
        "uname",
        "install",
        "plutil",
        "ssh-keygen",
        "git",
        "defaults",
    ]:
        real = shutil.which(name, path="/usr/bin:/bin:/usr/sbin:/sbin")
        if real is None or (stubs / name).exists():
            continue
        (stubs / name).write_text(
            f'#!/bin/bash\necho "{name} $*" >>"$STUB_STATE/argv"\nexec {real} "$@"\n'
        )
        (stubs / name).chmod(0o755)


def secrets_of(world: World) -> str:
    return (world.root / "secrets").read_text()


def every_argv(world: World) -> str:
    """The arguments of every command run so far, stubbed or wrapped."""
    argv = world.state / "argv"
    return "\n".join(world.calls()) + "\n" + (argv.read_text() if argv.exists() else "")


@macos
def test_the_uplink_needs_the_tunnel(world: World) -> None:
    set_up(world)
    code, out = run_setup(world, "--uplink", stdin=INGEST_TOKEN + "\n")
    assert code != 0
    assert "the tunnel's LaunchAgent is not loaded" in out
    assert secrets_of(world) == SECRETS_BEFORE
    assert INGEST_TOKEN not in out


@macos
def test_the_switch_to_the_brain_and_back(world: World) -> None:
    set_up(world)
    (world.state / "loaded.tunnel").write_text("")  # the tunnel runs
    (world.state / "loaded").write_text("")  # and so does the collector
    argv_tripwire(world)
    before = len(world.calls())
    code, out = run_setup(world, "--uplink", stdin=INGEST_TOKEN + "\n")
    assert code == 0, out
    assert INGEST_TOKEN not in out
    assert INGEST_TOKEN not in every_argv(world)  # in no command's arguments
    assert "\nmv " in every_argv(world)  # the tripwire saw them
    assert secrets_of(world) == SECRETS_BEFORE + uplink_lines(world)
    assert mode(world.root / "secrets") == 0o600
    assert [p.name for p in world.root.iterdir() if p.name.startswith(".")] == []
    calls = world.calls()[before:]
    assert "fbcollector check-config" in calls
    assert calls[-1] == "launchctl kickstart -k gui/501/com.ddiver.fbcollector"
    assert "mode: uplink" in out
    assert "FBC_INGEST_TOKEN set" in out

    # --secrets asks for the alert bot again and keeps the uplink (it rewrote the file whole).
    other = "987654321:" + "N3w-alert_" * 3
    code, out = run_setup(world, "--secrets", answers=(other, "77"))
    assert code == 0, out
    assert secrets_of(world) == uplink_lines(world) + (
        f"FBC_TIMEZONE=Europe/Sofia\nFBC_OBSERVE_MODE=true\n"
        f"FBC_ALERT_BOT_TOKEN={other}\nFBC_ALERT_OWNER_ID=77\n"
    )

    # Again with another token: replaced, not doubled.
    second = "Z" * 30
    assert run_setup(world, "--uplink", stdin=second + "\n")[0] == 0
    text = secrets_of(world)
    assert text.count("FBC_INGEST_TOKEN") == 1 and f"FBC_INGEST_TOKEN='{second}'\n" in text
    assert INGEST_TOKEN not in text

    # Back to local mode: the three settings go, the rest stays.
    before = len(world.calls())
    code, out = run_setup(world, "--local", tty=False)
    assert code == 0, out
    assert secrets_of(world) == (
        f"FBC_TIMEZONE=Europe/Sofia\nFBC_OBSERVE_MODE=true\n"
        f"FBC_ALERT_BOT_TOKEN={other}\nFBC_ALERT_OWNER_ID=77\n"
    )
    assert "mode: local" in out
    assert world.calls()[-1] == "launchctl kickstart -k gui/501/com.ddiver.fbcollector"
    assert second not in every_argv(world) + out


@macos
def test_the_switch_keeps_lines_it_does_not_own_in_any_case(world: World) -> None:
    set_up(world)
    (world.state / "loaded.tunnel").write_text("")
    secrets = world.root / "secrets"
    secrets.write_text(
        SECRETS_BEFORE
        + "# a note\nFBC_LOG_LEVEL=DEBUG\nexport fbc_ingest_url=http://127.0.0.1:1\n"
        + "FBC_INGEST_TIMEOUT=5\n"
    )
    assert run_setup(world, "--uplink", stdin=INGEST_TOKEN + "\n")[0] == 0
    assert secrets_of(world) == (
        SECRETS_BEFORE
        + "# a note\nFBC_LOG_LEVEL=DEBUG\nFBC_INGEST_TIMEOUT=5\n"
        + uplink_lines(world)
    )
    assert "collector: not running" in run_setup(world, "--local", tty=False)[1]
    assert secrets_of(world) == (
        SECRETS_BEFORE + "# a note\nFBC_LOG_LEVEL=DEBUG\nFBC_INGEST_TIMEOUT=5\n"
    )


@macos
@pytest.mark.parametrize(
    "given",
    [
        "",
        "short-token-0123456789",
        "x" * 513,
        INGEST_TOKEN + " ",
        "with space" + "x" * 20,
        "quote'" + "x" * 30,
        'dollar$HOME"' + "x" * 30,
        INGEST_TOKEN + "\n" + INGEST_TOKEN,
    ],
    ids=["empty", "short", "long", "trailing", "space", "quote", "dollar", "two lines"],
)
def test_a_token_that_is_not_one_is_refused_unechoed(world: World, given: str) -> None:
    set_up(world)
    (world.state / "loaded.tunnel").write_text("")
    before = len(world.calls())
    code, out = run_setup(world, "--uplink", stdin=given + "\n")
    assert code != 0
    assert secrets_of(world) == SECRETS_BEFORE
    for part in given.split("\n"):
        if len(part) >= 8:
            assert part.strip() not in out
    assert not calls_since(world, before, "launchctl kickstart")


@macos
def test_a_token_check_config_refuses_leaves_the_secrets_as_they_were(world: World) -> None:
    set_up(world)
    (world.state / "loaded.tunnel").write_text("")
    (world.state / "config-refused").write_text("")
    before = len(world.calls())
    code, out = run_setup(world, "--uplink", stdin=INGEST_TOKEN + "\n")
    assert code != 0
    assert "the secrets file is as it was" in out
    assert secrets_of(world) == SECRETS_BEFORE
    assert [p.name for p in world.root.iterdir() if p.name.startswith(".")] == []
    assert not calls_since(world, before, "launchctl kickstart")


@macos
def test_the_token_can_be_typed_without_echo(world: World) -> None:
    set_up(world)
    (world.state / "loaded.tunnel").write_text("")
    code, out = run_setup(world, "--uplink", answers=(INGEST_TOKEN,))
    assert code == 0, out
    assert INGEST_TOKEN not in out
    assert secrets_of(world) == SECRETS_BEFORE + uplink_lines(world)


@macos
def test_the_modes_need_a_set_up_account_and_go_alone(world: World) -> None:
    (world.state / "loaded.tunnel").write_text("")
    for args in (("--uplink",), ("--local",)):
        code, out = run_setup(world, *args, stdin=INGEST_TOKEN + "\n")
        assert code != 0
        assert "not set up in this account yet" in out
    code, out = run_setup(world, "--tunnel", tty=False)
    assert code != 0
    assert "no checkout" in out
    for pair in (("--uplink", "--local"), ("--tunnel", "--update"), ("--secrets", "--uplink")):
        assert run_setup(world, *pair, tty=False)[0] == 2


# -- the operator door's tunnel verbs --------------------------------------------------------


@macos
def test_the_door_pins_reads_and_restarts_the_tunnel(world: World, host_line: str) -> None:
    set_up(world)
    paths = tunnel_paths(world)
    key = door(world, "tunnel-key")
    assert (key.returncode, key.stdout) == (0, paths["pub"].read_text())
    waiting = door(world, "tunnel")
    assert waiting.returncode == 0, waiting.stderr
    assert "not loaded" in waiting.stdout
    assert "host key:   none" in waiting.stdout
    assert "\tnone (ssh is not connected)" in waiting.stdout
    assert "HTTP 000" in waiting.stdout

    pinned = door(world, "tunnel-pin", stdin=host_line + "\n")
    assert pinned.returncode == 0, pinned.stderr
    assert paths["pin"].read_text() == host_line + "\n"
    assert (world.state / "loaded.tunnel").exists()
    refused = door(world, "tunnel-pin", stdin=host_line.replace(VPS, "other.example") + "\n")
    assert refused.returncode != 0
    assert paths["pin"].read_text() == host_line + "\n"

    paths["log"].write_text("".join(f"ssh says {n}\n" for n in range(9)))
    unix_socket(paths["socket"])
    (world.state / "probe").write_text("401")
    shown = door(world, "tunnel")
    assert shown.returncode == 0, shown.stderr
    out = shown.stdout
    assert "state = running" in out
    assert re.search(rf"socket {re.escape(str(paths['socket']))}:\n\ts[rwx-]{{9}} ", out)
    assert out.count("SHA256:") == 2
    assert "ssh says 3" not in out and "ssh says 4" in out and "ssh says 8" in out
    assert "probe of /v1/control without the token: HTTP 401" in out
    probe = [c for c in world.calls() if c.startswith("curl")][-1]
    assert probe.endswith(f" --unix-socket {paths['socket']} http://127.0.0.1:8781/v1/control")
    assert "-H" not in probe.split() and "Authorization" not in probe

    assert door(world, "tunnel-log 2").stdout == "ssh says 7\nssh says 8\n"
    for bad in ("tunnel-log x", "tunnel-log 1000", "tunnel-log -1"):
        assert door(world, bad).returncode == 2, bad
    agent = door(world, "agent").stdout
    assert "com.ddiver.fbcollector:\n\tnot loaded" in agent
    assert f"{TUNNEL}:\n\tstate = running" in agent

    before = len(world.calls())
    assert door(world, "tunnel-restart").returncode == 0
    assert door(world, "tunnel-stop").returncode == 0
    assert calls_since(world, before, "launchctl") == [
        f"launchctl print gui/501/{TUNNEL}",
        f"launchctl kickstart -k gui/501/{TUNNEL}",
        f"launchctl bootout gui/501/{TUNNEL}",
    ]
    (world.state / "loaded.tunnel").unlink()
    (world.state / "stopping.tunnel").unlink()
    assert door(world, "tunnel-restart").returncode == 0
    assert world.calls()[-1] == f"launchctl bootstrap gui/501 {paths['agent']}"
    paths["agent"].unlink()
    (world.state / "loaded.tunnel").unlink()
    gone = door(world, "tunnel-restart")
    assert gone.returncode != 0
    assert "tunnel-pin" in gone.stderr


@macos
def test_the_door_switches_the_uplink_with_the_token_on_stdin(world: World, host_line: str) -> None:
    set_up(world)
    early = door(world, "uplink-token", stdin=INGEST_TOKEN + "\n")
    assert early.returncode != 0  # no tunnel yet
    assert secrets_of(world) == SECRETS_BEFORE
    assert door(world, "tunnel-pin", stdin=host_line + "\n").returncode == 0
    argv_tripwire(world)
    switched = door(world, "uplink-token", stdin=INGEST_TOKEN + "\n")
    assert switched.returncode == 0, switched.stderr
    assert INGEST_TOKEN not in switched.stdout + switched.stderr
    assert INGEST_TOKEN not in every_argv(world)
    assert secrets_of(world) == SECRETS_BEFORE + uplink_lines(world)
    assert door(world, "uplink").returncode == 0
    assert world.calls()[-1] == "fbcollector uplink"
    off = door(world, "uplink-off")
    assert off.returncode == 0, off.stderr
    assert secrets_of(world) == SECRETS_BEFORE


@macos
def test_the_door_refuses_a_tunnel_key_of_another_shape(world: World) -> None:
    set_up(world)
    pub = tunnel_paths(world)["pub"]
    good = pub.read_text()
    for bad in (
        good + good,
        good.replace("fbtunnel-mac-1", "owner@laptop"),
        'restrict,command="x" ' + good,
        "-----BEGIN OPENSSH " + "PRIVATE KEY-----\n",  # split: the secret scan reads the tests
    ):
        pub.write_text(bad)
        done = door(world, "tunnel-key")
        assert (done.returncode, done.stdout) == (1, ""), bad
        assert "refused" in done.stderr
    pub.unlink()
    assert door(world, "tunnel-key").returncode == 1


def test_the_door_lists_the_tunnel_verbs_and_still_refuses_the_owners(tmp_path: Path) -> None:
    listed = operator(tmp_path, "").stderr
    for verb in (
        "tunnel",
        "tunnel-log [N]",
        "tunnel-key",
        "tunnel-restart",
        "tunnel-stop",
        "tunnel-pin",
        "uplink",
        "uplink-token",
        "uplink-off",
    ):
        assert f"| {verb}" in listed, verb
    for verb in ("login", "resume", "pause", "groups", "pin-home", "selftest"):
        done = operator(tmp_path, verb)
        assert (done.returncode, done.stdout) == (3, ""), verb
        assert "the owner's to run at the keyboard" in done.stderr


def test_the_runbook_walks_the_owner_through_the_switch_and_lists_the_doors_verbs() -> None:
    runbook = (REPO / "docs" / "RUNBOOK.md").read_text()
    switch = runbook.split("## 9. The tunnel and the switch to the brain", 1)[1].split("## 10.")[0]
    order = [
        "ssh fbc-local update",
        "ssh fbc-local tunnel-key | ssh vps 'cat > /tmp/fbtunnel-mac-1.pub'",
        "setup-fbtunnel.sh --key /tmp/fbtunnel-mac-1.pub",
        "| ssh fbc-local tunnel-pin",
        "HTTP 401",
        "`/pause`",
        "outbox: 0 items",
        "| ssh fbc-local uplink-token",
        "ssh fbc-local uplink` must show",
        "`/resume`",
        "ssh fbc-local uplink-off",
    ]
    positions = [switch.find(text) for text in order]
    assert all(p >= 0 for p in positions), dict(zip(order, positions, strict=True))
    assert positions == sorted(positions)
    door_section = runbook.split("**Operator access over SSH", 1)[1]
    verbs = re.findall(r'^VERBS="(?:\$VERBS \| )?(.*)"$', OPERATOR.read_text(), re.MULTILINE)
    for verb in " | ".join(verbs).split(" | "):
        assert f"`{verb.removesuffix(' [N]')}" in door_section, verb
    assert "restrict,pty" not in runbook
    hint = (REPO / "packages" / "fbcollector" / "fbcollector" / "uplink.py").read_text()
    assert "ssh fbc-local tunnel" in hint and "Check autossh" not in hint
