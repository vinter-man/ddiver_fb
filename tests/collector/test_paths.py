from __future__ import annotations

import logging
import os
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from fbcollector import paths
from fbcollector.paths import (
    CACHEDIR_SIGNATURE,
    CACHEDIR_TAG,
    NEVER_INDEX,
    TMUTIL,
    CommandResult,
    Layout,
    UnsafeLayoutError,
    check,
    prepare,
    run_command,
)


class FakeTmutil:
    """Stands in for /usr/bin/tmutil: remembers exclusions, answers isexcluded."""

    def __init__(self, *, add_fails: bool = False, missing: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.excluded: set[str] = set()
        self.add_fails = add_fails
        self.missing = missing

    def __call__(self, args: Sequence[str]) -> CommandResult:
        self.calls.append(list(args))
        if self.missing:
            return CommandResult(127, stderr="not found")
        assert args[0] == TMUTIL
        verb, target = args[1], args[2]
        if verb == "addexclusion":
            if self.add_fails:
                return CommandResult(1, stderr="denied")
            self.excluded.add(target)
            return CommandResult(0)
        if verb == "isexcluded":
            tag = "[Excluded]" if target in self.excluded else "[Included]"
            return CommandResult(0, stdout=f"{tag}    {target}\n")
        raise AssertionError(args)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "Application Support" / "fbcollector"


class TestPrepareMac:
    def test_creates_a_private_excluded_root(self, root: Path) -> None:
        tm = FakeTmutil()
        layout = prepare(root, platform="darwin", runner=tm)
        assert layout == Layout(root)
        for directory in (root, layout.profile, layout.raw, layout.shots):
            assert directory.is_dir()
            assert mode(directory) == 0o700
        assert mode(root / NEVER_INDEX) == 0o600
        assert tm.calls == [
            [TMUTIL, "addexclusion", str(root)],
            [TMUTIL, "isexcluded", str(root)],
        ]

    def test_is_idempotent_and_repairs_loose_modes(self, root: Path) -> None:
        tm = FakeTmutil()
        layout = prepare(root, platform="darwin", runner=tm)
        root.chmod(0o755)
        layout.raw.chmod(0o777)
        layout.db.write_bytes(b"")
        layout.db.chmod(0o644)
        (layout.raw / "day").mkdir()
        dump = layout.raw / "day" / "x.json.gz"
        dump.write_bytes(b"")
        dump.chmod(0o644)
        (root / NEVER_INDEX).chmod(0o644)
        prepare(root, platform="darwin", runner=tm)
        assert (mode(root), mode(layout.raw), mode(layout.db), mode(dump)) == (
            0o700,
            0o700,
            0o600,
            0o600,
        )
        assert mode(root / NEVER_INDEX) == 0o600

    def test_failed_exclusion_fails_closed(
        self, root: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            caplog.at_level(logging.WARNING, logger="fbcollector.paths"),
            pytest.raises(UnsafeLayoutError, match="Time Machine"),
        ):
            prepare(root, platform="darwin", runner=FakeTmutil(add_fails=True))
        assert "addexclusion failed" in caplog.text

    def test_missing_tmutil_fails_closed(self, root: Path) -> None:
        with pytest.raises(UnsafeLayoutError, match="Time Machine"):
            prepare(root, platform="darwin", runner=FakeTmutil(missing=True))

    def test_never_index_is_not_written_through_a_symlink(
        self, root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        root.mkdir(parents=True)
        target = tmp_path / "elsewhere"
        target.write_text("untouched")
        (root / NEVER_INDEX).symlink_to(target)
        with caplog.at_level(logging.WARNING, logger="fbcollector.paths"):
            prepare(root, platform="darwin", runner=FakeTmutil())
        assert target.read_text() == "untouched"
        assert "cannot write" in caplog.text


class TestPrepareLinux:
    def test_cachedir_tag_and_no_tmutil(self, root: Path) -> None:
        tm = FakeTmutil()
        prepare(root, platform="linux", runner=tm)
        assert tm.calls == []
        tag = (root / CACHEDIR_TAG).read_text()
        assert tag.startswith(CACHEDIR_SIGNATURE)
        assert not (root / NEVER_INDEX).exists()

    def test_existing_tag_is_kept(self, root: Path) -> None:
        root.mkdir(parents=True)
        (root / CACHEDIR_TAG).write_text(CACHEDIR_SIGNATURE + "\n# mine\n")
        prepare(root, platform="linux", runner=FakeTmutil())
        assert (root / CACHEDIR_TAG).read_text().endswith("# mine\n")

    def test_bad_tag_is_rewritten(self, root: Path) -> None:
        root.mkdir(parents=True)
        (root / CACHEDIR_TAG).write_text("Signature: wrong\n")
        prepare(root, platform="linux2", runner=FakeTmutil())
        assert (root / CACHEDIR_TAG).read_text().startswith(CACHEDIR_SIGNATURE)


class TestCheck:
    def ready(self, root: Path) -> Layout:
        return prepare(root, platform="linux", runner=FakeTmutil())

    def problems(self, root: Path, platform: str = "linux") -> tuple[str, ...]:
        with pytest.raises(UnsafeLayoutError) as info:
            check(root, platform=platform, runner=FakeTmutil())
        return info.value.problems

    def test_a_prepared_root_passes(self, root: Path) -> None:
        self.ready(root)
        assert check(root, platform="linux", runner=FakeTmutil()) == Layout(root)

    def test_missing_root(self, root: Path) -> None:
        problems = self.problems(root)
        assert any(f"{root} is missing" in p for p in problems)

    def test_group_readable_root(self, root: Path) -> None:
        self.ready(root)
        root.chmod(0o750)
        assert any("chmod 700" in p for p in self.problems(root))

    def test_world_readable_state_file(self, root: Path) -> None:
        layout = self.ready(root)
        layout.secrets.write_text("x")
        layout.secrets.chmod(0o644)
        assert any("chmod 600" in p and "secrets" in p for p in self.problems(root))

    def test_readable_raw_dump(self, root: Path) -> None:
        layout = self.ready(root)
        (layout.shots / "block-1.png").write_bytes(b"")
        (layout.shots / "block-1.png").chmod(0o604)
        assert any("block-1.png" in p for p in self.problems(root))

    def test_symlinked_subdir(self, root: Path, tmp_path: Path) -> None:
        layout = self.ready(root)
        layout.raw.rmdir()
        shared = tmp_path / "shared"
        shared.mkdir(mode=0o700)
        layout.raw.symlink_to(shared)
        assert any("symlink" in p for p in self.problems(root))

    def test_symlinked_root(self, root: Path, tmp_path: Path) -> None:
        real = tmp_path / "real"
        self.ready(real)
        root.parent.mkdir(parents=True)
        root.symlink_to(real)
        assert any("symlink" in p for p in self.problems(root))

    def test_subdir_that_is_a_file(self, root: Path) -> None:
        layout = self.ready(root)
        layout.shots.rmdir()
        layout.shots.write_bytes(b"")
        assert any("not a directory" in p for p in self.problems(root))

    def test_state_path_that_is_a_directory(self, root: Path) -> None:
        layout = self.ready(root)
        layout.db.mkdir(mode=0o700)
        assert any("not a regular file" in p for p in self.problems(root))

    def test_missing_cachedir_tag(self, root: Path) -> None:
        self.ready(root)
        (root / CACHEDIR_TAG).unlink()
        assert any(CACHEDIR_TAG in p for p in self.problems(root))

    def test_mac_without_spotlight_marker(self, root: Path) -> None:
        tm = FakeTmutil()
        prepare(root, platform="darwin", runner=tm)
        (root / NEVER_INDEX).unlink()
        with pytest.raises(UnsafeLayoutError, match="Spotlight"):
            check(root, platform="darwin", runner=tm)

    def test_mac_root_included_in_backups(self, root: Path) -> None:
        tm = FakeTmutil()
        prepare(root, platform="darwin", runner=tm)
        tm.excluded.clear()  # e.g. the folder was recreated and lost its xattr
        with pytest.raises(UnsafeLayoutError, match="Time Machine"):
            check(root, platform="darwin", runner=tm)

    def test_unsupported_platform(self, root: Path) -> None:
        self.ready(root)
        assert any("not supported" in p for p in self.problems(root, platform="win32"))

    def test_error_lists_every_problem(self, root: Path) -> None:
        self.ready(root)
        root.chmod(0o755)
        (root / CACHEDIR_TAG).unlink()
        with pytest.raises(UnsafeLayoutError) as info:
            check(root, platform="linux", runner=FakeTmutil())
        assert len(info.value.problems) == 2
        assert str(info.value) == "; ".join(info.value.problems)

    def test_owned_by_someone_else(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.ready(root)
        real = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: real + 1)
        assert any("owned by another user" in p for p in self.problems(root))

    def test_prepare_on_an_unsupported_platform_refuses(self, root: Path) -> None:
        with pytest.raises(UnsafeLayoutError, match="not supported"):
            prepare(root, platform="win32", runner=FakeTmutil())

    def test_uninspectable_path(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.ready(root)
        real_lstat = Path.lstat

        def lstat(self: Path) -> object:
            if self.name == "profile":
                raise PermissionError(13, "Permission denied")
            return real_lstat(self)

        monkeypatch.setattr(Path, "lstat", lstat)
        assert any("cannot be inspected" in p for p in self.problems(root))


class TestLayout:
    def test_paths(self, tmp_path: Path) -> None:
        layout = Layout(tmp_path)
        assert layout.profile == tmp_path / "profile"
        assert layout.db == tmp_path / "state.sqlite"
        assert layout.lock == tmp_path / "collector.lock"
        assert layout.state_files() == (
            tmp_path / "state.sqlite",
            tmp_path / "state.sqlite-wal",
            tmp_path / "state.sqlite-shm",
            tmp_path / "secrets",
        )


class TestRunCommand:
    def test_success(self) -> None:
        result = run_command(["/bin/echo", "hi"])
        assert result == CommandResult(0, "hi\n", "")

    def test_missing_executable(self, tmp_path: Path) -> None:
        result = run_command([str(tmp_path / "no-such-tool")])
        assert result.returncode == 127

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def hang(*_a: object, **_k: object) -> object:
            raise subprocess.TimeoutExpired(cmd="tmutil", timeout=paths.COMMAND_TIMEOUT)

        monkeypatch.setattr(subprocess, "run", hang)
        assert run_command([TMUTIL, "isexcluded", "/"]).returncode == 124
