"""How Chrome is started on the account's profile, and how we know it was started the same way.

One launcher serves ``fbcollector login`` and every reading launch (DESIGN D15): the same
executable, flags and profile, so Facebook sees one device. Three things live here, all
without a browser so they can be tested offline:

- **The launch spec.** Installed Google Chrome, headed, the persistent profile, real window
  size, Chrome's own sandbox, ``--no-proxy-server`` (the home gate checked the direct route;
  a proxy would make that check meaningless). Headed is not a setting: ``LaunchSpec`` has no
  ``headless`` field and the driver passes ``headless=False`` as a literal (DESIGN, non-goals:
  no headless reading; ``test_headed_only`` holds both). The browser tests run Chrome headless
  through the driver's test-only ``factory`` seam, never through the spec. On macOS
  Patchright's ``--use-mock-keychain`` default is dropped, so the profile's cookies are
  encrypted with the real Keychain as in a hand-started Chrome; on Linux
  ``--password-store=basic`` is passed explicitly, not left to Patchright's defaults, because
  the login and the reads must agree on it.
- **Profile hygiene before every launch.** Session files are deleted and the Preferences say
  "open the New Tab page" and "exited normally", so Chrome never restores a Facebook tab on its
  own (a navigation we did not gate) and never shows a "Restore pages?" bubble. Notifications
  and background sync are denied for facebook.com (a push or a sync event would wake the
  profile while every gate is closed); password saving is off.
- **The command line.** After launch the browser process's command line is read from ``ps``
  (Chrome does not reveal it to CDP without ``--enable-automation``, which we must not pass),
  normalised (the profile path becomes ``<profile>``) and compared with the one recorded at the
  login. Any difference fails closed: a Patchright upgrade that changed a default switch is a
  new device fingerprint until a human logs in again.

What a launch is *for* — the collector's read or the owner's login — is its ``SessionMode``,
passed to every ``Driver.launch``. It decides what the driver does with the running Chrome,
never how Chrome is started: both modes launch from the same ``LaunchSpec``.
"""

from __future__ import annotations

import contextlib
import enum
import json
import logging
import os
import plistlib
import re
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from fbcollector.paths import Runner, run_command

log = logging.getLogger(__name__)

CHANNEL: Final = "chrome"
NO_PROXY: Final = "--no-proxy-server"
PASSWORD_STORE_BASIC: Final = "--password-store=basic"  # noqa: S105 - a Chrome switch
MOCK_KEYCHAIN: Final = "--use-mock-keychain"
START_URL: Final = "about:blank"
PROFILE_TOKEN: Final = "<profile>"  # noqa: S105 - a placeholder, not a secret

MAC_CHROME_APP: Final = Path("/Applications/Google Chrome.app")
MAC_CHROME_BINARY: Final = MAC_CHROME_APP / "Contents/MacOS/Google Chrome"
LINUX_CHROME_BINARY: Final = Path("/opt/google/chrome/chrome")

TESTED_MAJORS: Final = frozenset({153})
"""Chrome majors the local self-test has passed on (DESIGN: a major outside this list runs the
self-test before any Facebook navigation)."""

RESTORE_NEW_TAB: Final = 5
"""``session.restore_on_startup``: 1 = continue where you left off, 4 = URLs, 5 = New Tab page."""
CONTENT_BLOCK: Final = 2
FACEBOOK_PATTERNS: Final = ("https://www.facebook.com:443,*", "[*.]facebook.com,*")
SESSION_FILES: Final = ("Current Session", "Current Tabs", "Last Session", "Last Tabs")

_VERSION: Final = re.compile(r"(\d+)\.(\d+)\.(\d+)\.(\d+)")
_USER_DATA_DIR: Final = "--user-data-dir="


class SessionMode(enum.StrEnum):
    """Who drives the Chrome a launch starts. Never part of the command line (D15).

    ``READ``: the collector, one step at a time, in one tab. A tab or popup anything else opens
    is closed unread (and logged).

    ``LOGIN``: the owner, by hand (``fbcollector login``). The driver only waits for Chrome to
    exit and reads cookie names. It closes no tab or popup the owner or the page opens (2FA,
    "confirm it's you", a link opened in a new tab), takes no step and captures nothing.
    """

    READ = "read"
    LOGIN = "login"


class LaunchError(Exception):
    """Chrome cannot be launched safely on this profile (nothing was navigated)."""


class CommandLineMismatchError(LaunchError):
    """Chrome's actual command line differs from the one recorded at the login."""

    def __init__(self, added: Sequence[str], removed: Sequence[str]) -> None:
        self.added = tuple(added)
        self.removed = tuple(removed)
        parts = []
        if added:
            parts.append(f"new: {' '.join(added)}")
        if removed:
            parts.append(f"missing: {' '.join(removed)}")
        super().__init__("Chrome's command line changed since the login (" + "; ".join(parts) + ")")


@dataclass(frozen=True, slots=True)
class LaunchInfo:
    """What a launch found; the caller compares the command line with the login's."""

    command_line: str
    """Normalised (the profile path is ``<profile>``)."""
    pid: int
    chrome_version: ChromeVersion | None
    patchright_version: str | None


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """Everything passed to ``launch_persistent_context``; identical for login and reads.

    There is deliberately no ``headless`` field: Chrome is always headed (see the module
    docstring), so no setting, caller or future default can turn a reading launch headless.
    """

    profile: Path
    args: tuple[str, ...]
    ignore_default_args: tuple[str, ...]
    channel: str = CHANNEL
    no_viewport: bool = True
    chromium_sandbox: bool = True


def launch_spec(profile: Path, *, platform: str | None = None) -> LaunchSpec:
    system = platform or sys.platform
    if system == "darwin":
        return LaunchSpec(profile, args=(NO_PROXY,), ignore_default_args=(MOCK_KEYCHAIN,))
    if system.startswith("linux"):
        return LaunchSpec(profile, args=(NO_PROXY, PASSWORD_STORE_BASIC), ignore_default_args=())
    msg = f"platform {system!r} is not supported"
    raise LaunchError(msg)


# -- profile hygiene --------------------------------------------------------------------------


def prepare_profile(profile: Path) -> list[str]:
    """Make the next launch open exactly one blank tab. Returns what was changed, for the log.

    Runs only while Chrome is not running on the profile (the caller holds the profile lock and
    has checked ``SingletonLock``). Preferences are rewritten atomically; a Preferences file that
    is not JSON is left alone and reported, because Chrome would rebuild it and a rewrite by us
    could lose the account's settings.
    """
    changes: list[str] = []
    default = profile / "Default"
    default.mkdir(parents=True, exist_ok=True, mode=0o700)
    sessions = default / "Sessions"
    if sessions.is_dir() and not sessions.is_symlink():
        for entry in sorted(sessions.iterdir()):
            with contextlib.suppress(FileNotFoundError):
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                changes.append(f"deleted Sessions/{entry.name}")
    for name in SESSION_FILES:
        path = default / name
        if path.is_file() or path.is_symlink():
            path.unlink()
            changes.append(f"deleted {name}")
    prefs_path = default / "Preferences"
    prefs: dict[str, Any] = {}
    if prefs_path.exists():
        try:
            loaded = json.loads(prefs_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            msg = f"Chrome's Preferences cannot be read ({type(exc).__name__})"
            raise LaunchError(msg) from exc
        if not isinstance(loaded, dict):
            msg = "Chrome's Preferences are not a JSON object"
            raise LaunchError(msg)
        prefs = loaded
    if apply_preferences(prefs):
        _write_json_atomic(prefs_path, prefs)
        changes.append("rewrote Preferences")
    return changes


def apply_preferences(prefs: dict[str, Any]) -> bool:
    """Set the keys hygiene needs; ``True`` when anything changed."""
    before = json.dumps(prefs, sort_keys=True)
    session = _branch(prefs, "session")
    session["restore_on_startup"] = RESTORE_NEW_TAB
    profile = _branch(prefs, "profile")
    profile["exit_type"] = "Normal"
    profile["exited_cleanly"] = True
    profile["password_manager_enabled"] = False
    prefs["credentials_enable_service"] = False
    exceptions = _branch(_branch(profile, "content_settings"), "exceptions")
    for setting in ("notifications", "background_sync"):
        table = _branch(exceptions, setting)
        for pattern in FACEBOOK_PATTERNS:
            entry = table.get(pattern)
            entry = entry if isinstance(entry, dict) else {}
            entry["setting"] = CONTENT_BLOCK
            table[pattern] = entry
    return json.dumps(prefs, sort_keys=True) != before


def _branch(tree: dict[str, Any], key: str) -> dict[str, Any]:
    value = tree.get(key)
    if not isinstance(value, dict):
        value = {}
        tree[key] = value
    return value


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".fbc-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


# -- the running Chrome -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChromeProcess:
    pid: int
    command: str


def find_browser_process(profile: Path, *, runner: Runner = run_command) -> ChromeProcess | None:
    """The Chrome *browser* process (not a renderer or helper) running on ``profile``."""
    result = runner(["/bin/ps", "-axww", "-o", "pid=,command="])
    if result.returncode != 0:
        return None
    marker = f"{_USER_DATA_DIR}{profile}"
    for line in result.stdout.splitlines():
        pid_text, _, command = line.strip().partition(" ")
        if not pid_text.isdigit() or " --type=" in command:
            continue
        # The profile flag is followed by a space (the next switch) or the end of the line.
        at = command.find(marker)
        end = at + len(marker)
        if at >= 0 and (end == len(command) or command[end] == " "):
            return ChromeProcess(int(pid_text), command.strip())
    return None


def chrome_resources(profile: Path, *, runner: Runner = run_command) -> dict[str, float]:
    """RSS (MiB) and CPU (%) of every Chrome process on ``profile`` (phase-0 resources measure).

    Helpers and renderers carry the profile's ``--user-data-dir`` too; processes that do not are
    missed, so this is a lower bound.
    """
    result = runner(["/bin/ps", "-axww", "-o", "rss=,pcpu=,command="])
    if result.returncode != 0:
        return {}
    marker = f"{_USER_DATA_DIR}{profile}"
    rss = cpu = 0.0
    count = 0
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3 or marker not in parts[2]:
            continue
        try:
            rss += float(parts[0]) / 1024
            cpu += float(parts[1])
        except ValueError:
            continue
        count += 1
    return {"chrome_rss_mb": round(rss, 1), "chrome_cpu_pct": round(cpu, 1), "processes": count}


def normalise_command(command: str, profile: Path) -> str:
    """The command line with the profile path replaced, so it compares across runs."""
    return command.replace(f"{_USER_DATA_DIR}{profile}", f"{_USER_DATA_DIR}{PROFILE_TOKEN}")


def switches(command: str) -> list[str]:
    """Split a ``ps`` command line into its switches (the executable path may hold spaces)."""
    head, sep, rest = command.partition(" --")
    if not sep:
        return [head.strip()]
    parts = [head.strip()]
    for chunk in ("--" + rest).split(" --"):
        piece = chunk if chunk.startswith("--") else "--" + chunk
        parts.append(piece.strip())
    return parts


def compare_command(recorded: str, actual: str) -> None:
    """Raise ``CommandLineMismatchError`` unless both command lines are the same."""
    if recorded == actual:
        return
    old, new = switches(recorded), switches(actual)
    added = [s for s in new if s not in old]
    removed = [s for s in old if s not in new]
    if not added and not removed:
        added, removed = ["(order changed)"], []
    raise CommandLineMismatchError(added, removed)


def check_start_url(command: str) -> None:
    """Chrome must have been handed ``about:blank`` as its only start URL."""
    last = switches(command)[-1]
    if not last.endswith(f" {START_URL}"):
        msg = "Chrome was not started on about:blank"
        raise LaunchError(msg)


# -- versions ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChromeVersion:
    full: str

    @property
    def major(self) -> int:
        return int(self.full.split(".", 1)[0])

    @property
    def tested(self) -> bool:
        return self.major in TESTED_MAJORS


def parse_version(text: str) -> ChromeVersion | None:
    match = _VERSION.search(text)
    return ChromeVersion(match.group(0)) if match else None


def installed_chrome_version(
    *,
    platform: str | None = None,
    runner: Runner = run_command,
    app: Path = MAC_CHROME_APP,
    binary: Path = LINUX_CHROME_BINARY,
) -> ChromeVersion | None:
    """The installed Chrome's version, without starting it on any profile.

    macOS reads the app bundle's Info.plist; Linux asks the binary for ``--version`` (it prints
    and exits, touching no profile).
    """
    system = platform or sys.platform
    if system == "darwin":
        try:
            with (app / "Contents" / "Info.plist").open("rb") as handle:
                info = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException):
            return None
        value = info.get("CFBundleShortVersionString")
        return parse_version(value) if isinstance(value, str) else None
    result = runner([str(binary), "--version"])
    return parse_version(result.stdout) if result.returncode == 0 else None


def patchright_version() -> str | None:
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415 - cold path

    try:
        return version("patchright")
    except PackageNotFoundError:  # pragma: no cover - it is a hard dependency
        return None
