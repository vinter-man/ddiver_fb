"""``ddiver-fb health`` and configuration errors: the parts of the CLI Docker leans on."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from ddiver_fb import __main__ as cli
from ddiver_fb.health import BrainBeat, write_beat
from ddiver_fb.runtime import BrainApp
from tests.brain.helpers import TOKEN

ENV = {
    "BOT_TOKEN": "123456789:" + "C" * 35,
    "API_ID": "7",
    "API_HASH": "abc",
    "OWNER_ID": "42",
    "INGEST_TOKEN": TOKEN,
}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Settings in the environment; main()'s process-wide umask and logging are put back."""
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    umask = os.umask(0o022)
    os.umask(umask)
    yield tmp_path
    os.umask(umask)
    root.handlers[:] = handlers
    root.setLevel(level)


def test_the_parser_knows_three_commands():
    parser = cli.build_parser()
    for command in ("run", "health", "check-config"):
        assert parser.parse_args([command]).command == command
    with pytest.raises(SystemExit):
        parser.parse_args(["login"])  # the brain has no user session to log in


def test_health_without_a_heartbeat_is_unhealthy(env: Path, capsys: pytest.CaptureFixture[str]):
    assert cli.main(["--env-file", str(env / "none.env"), "health"]) == 1
    assert "no heartbeat file" in capsys.readouterr().out


def test_health_with_a_fresh_heartbeat(env: Path, capsys: pytest.CaptureFixture[str]):
    from ddiver_core.timeutil import utcnow

    write_beat(
        env / "health.json", BrainBeat(ts=utcnow().isoformat(), bot_connected=True, ingest_up=True)
    )
    assert cli.main(["--env-file", str(env / "none.env"), "health"]) == 0
    assert capsys.readouterr().out.startswith("ok")


def test_a_config_error_names_the_setting_and_not_its_value(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv("INGEST_TOKEN", "hunter2")
    assert cli.main(["--env-file", str(env / "none.env"), "health"]) == 1
    err = capsys.readouterr().err
    assert "INGEST_TOKEN" in err and "hunter2" not in err


async def test_run_refuses_a_second_process(env: Path, monkeypatch: pytest.MonkeyPatch):
    from ddiver_core.process import InstanceLock
    from ddiver_fb.config import FbSettings

    lock = InstanceLock(env / cli.LOCK_NAME)
    assert lock.acquire()
    try:
        settings = FbSettings.load(None)
        assert await cli.cmd_run(settings) == 4
    finally:
        lock.release()


async def test_run_says_whose_data_directory_it_must_be(
    env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """Round 2 C-F5: an unwritable data directory crash-looped on a traceback."""
    from ddiver_core.process import InstanceLock
    from ddiver_fb.config import FbSettings

    def refuse(self: InstanceLock) -> bool:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(InstanceLock, "acquire", refuse)
    with caplog.at_level(logging.ERROR):
        assert await cli.cmd_run(FbSettings.load(None)) == 1
    assert "install -d -o 10002 -g 10002 -m 700" in caplog.text
    assert "Permission denied" in caplog.text


async def test_run_reports_a_corrupt_database(env: Path, monkeypatch: pytest.MonkeyPatch):
    from ddiver_fb.config import FbSettings

    (env / "fb.sqlite").write_bytes(b"this is not a database" * 100)
    monkeypatch.setattr(BrainApp, "run", _never_called)
    assert await cli.cmd_run(FbSettings.load(None)) in (1, 5)


async def _never_called(self: BrainApp) -> int:  # pragma: no cover - setup must fail first
    raise AssertionError("run() after a failed setup")
