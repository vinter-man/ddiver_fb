"""The collector's root directory: create it private, keep it out of backups, refuse it otherwise.

The Chrome profile under this root *is* the listener account (invariant 7): its cookies log
anyone in. So the root is 0700, the files in it 0600, it is excluded from Time Machine, Spotlight
and the Linux backup tools that honour ``CACHEDIR.TAG``, and ``check()`` runs at every start and
fails closed — a root that is readable by others, owned by someone else, a symlink, or included in
a backup is refused before Chrome is launched on it.

``tmutil addexclusion`` without ``-p`` sets a sticky exclusion (an extended attribute on the
folder), which needs no root and follows the folder if it moves; ``tmutil isexcluded`` verifies
it. Every external command goes through an injected runner so tests never call the real one.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

log = logging.getLogger(__name__)

TMUTIL: Final = "/usr/bin/tmutil"
NEVER_INDEX: Final = ".metadata_never_index"
CACHEDIR_TAG: Final = "CACHEDIR.TAG"
CACHEDIR_SIGNATURE: Final = "Signature: 8a477f597d28d172789f06886806bc55"
"""The fixed header from https://bford.info/cachedir/ that backup tools look for."""

DIR_MODE: Final = 0o700
FILE_MODE: Final = 0o600
COMMAND_TIMEOUT: Final = 15.0


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str]], CommandResult]


def run_command(args: Sequence[str]) -> CommandResult:
    """Run a system tool without a shell; a missing or hung tool is a failed result, not a crash."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed absolute executables, no shell
            list(args), capture_output=True, text=True, timeout=COMMAND_TIMEOUT, check=False
        )
    except FileNotFoundError:
        return CommandResult(127, stderr=f"{args[0]} not found")
    except subprocess.TimeoutExpired:
        return CommandResult(124, stderr=f"{args[0]} timed out")
    return CommandResult(done.returncode, done.stdout, done.stderr)


class UnsafeLayoutError(Exception):
    """The root is not private or not excluded from backups; nothing may run on it."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True, slots=True)
class Layout:
    root: Path

    @property
    def profile(self) -> Path:
        return self.root / "profile"

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def shots(self) -> Path:
        return self.root / "shots"

    @property
    def db(self) -> Path:
        return self.root / "state.sqlite"

    @property
    def secrets(self) -> Path:
        return self.root / "secrets"

    @property
    def lock(self) -> Path:
        return self.root / "collector.lock"

    @property
    def subdirs(self) -> tuple[Path, ...]:
        return (self.profile, self.raw, self.shots)

    def state_files(self) -> tuple[Path, ...]:
        """Files of ours that must be 0600 when they exist."""
        db = self.db
        return (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm"), self.secrets)


def prepare(root: Path, *, platform: str | None = None, runner: Runner = run_command) -> Layout:
    """Create the root and its subdirectories private, exclude it from backups, then ``check``.

    Idempotent: safe to run on every start, and it repairs modes a restore or a stray ``cp``
    loosened before verifying them.
    """
    layout = Layout(root)
    system = platform or sys.platform
    for directory in (root, *layout.subdirs):
        # Whatever cannot be created or tightened here, check() reports below.
        with contextlib.suppress(FileExistsError):
            directory.mkdir(mode=DIR_MODE, parents=directory is root, exist_ok=True)
        with contextlib.suppress(OSError):
            if not directory.is_symlink():
                directory.chmod(DIR_MODE)
    for path in _own_files(layout):
        with contextlib.suppress(OSError):
            path.chmod(FILE_MODE)
    if system == "darwin":
        _write_private(root / NEVER_INDEX, "")
        result = runner([TMUTIL, "addexclusion", str(root)])
        if result.returncode != 0:
            log.warning("tmutil addexclusion failed (%s): %s", result.returncode, result.stderr)
    elif system.startswith("linux"):
        tag = root / CACHEDIR_TAG
        if not _has_cachedir_signature(tag):
            _write_private(
                tag,
                f"{CACHEDIR_SIGNATURE}\n"
                "# This directory holds fbcollector's browser profile and state.\n"
                "# It must never be backed up or copied: the profile is the account.\n",
            )
    return check(root, platform=system, runner=runner)


def check(root: Path, *, platform: str | None = None, runner: Runner = run_command) -> Layout:
    """Verify the root is private and excluded from backups; raise ``UnsafeLayoutError`` if not."""
    layout = Layout(root)
    system = platform or sys.platform
    problems: list[str] = []
    for directory in (root, *layout.subdirs):
        problems += _check_path(directory, want_dir=True)
    if root.is_dir() and not root.is_symlink():
        for path in _own_files(layout):
            problems += _check_path(path, want_dir=False)
    if system == "darwin":
        if not (root / NEVER_INDEX).is_file():
            problems.append(f"{root / NEVER_INDEX} is missing (Spotlight would index the profile)")
        result = runner([TMUTIL, "isexcluded", str(root)])
        if result.returncode != 0 or "[Excluded]" not in result.stdout:
            problems.append(f"{root} is not excluded from Time Machine (tmutil addexclusion)")
    elif system.startswith("linux"):
        if not _has_cachedir_signature(root / CACHEDIR_TAG):
            problems.append(f"{root / CACHEDIR_TAG} is missing or has no signature")
    else:
        problems.append(f"platform {system!r} is not supported")
    if problems:
        raise UnsafeLayoutError(problems)
    return layout


def _check_path(path: Path, *, want_dir: bool) -> list[str]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return [f"{path} is missing"] if want_dir else []
    except OSError as exc:
        return [f"{path} cannot be inspected ({exc.strerror})"]
    if stat.S_ISLNK(info.st_mode):
        return [f"{path} is a symlink; the profile must live in the root itself"]
    kind_ok = stat.S_ISDIR(info.st_mode) if want_dir else stat.S_ISREG(info.st_mode)
    if not kind_ok:
        return [f"{path} is not a {'directory' if want_dir else 'regular file'}"]
    problems = []
    if info.st_uid != os.getuid():
        problems.append(f"{path} is owned by another user")
    if info.st_mode & 0o077:
        mode = "700" if want_dir else "600"
        problems.append(f"{path} is accessible to other users; run `chmod {mode} {path}`")
    return problems


def _own_files(layout: Layout) -> Iterator[Path]:
    """Our state files and every file in raw/ and shots/ (not the profile: Chrome owns it)."""
    for path in layout.state_files():
        if path.exists() or path.is_symlink():
            yield path
    for directory in (layout.raw, layout.shots):
        if directory.is_dir() and not directory.is_symlink():
            for dirpath, _dirnames, filenames in os.walk(directory):
                for name in filenames:
                    yield Path(dirpath) / name


def _has_cachedir_signature(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(CACHEDIR_SIGNATURE)) == CACHEDIR_SIGNATURE.encode()
    except OSError:
        return False


def _write_private(path: Path, content: str) -> None:
    # O_NOFOLLOW: a planted symlink must not make us write (or chmod) somewhere else.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, FILE_MODE)
    except OSError as exc:
        log.warning("cannot write %s: %s", path, exc.strerror)
        return
    # The creation mode applies only to a new file; an existing one keeps its old mode.
    os.fchmod(fd, FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
