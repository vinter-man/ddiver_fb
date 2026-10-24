"""``scripts/local_e2e.py`` refuses to start unless told twice: ``--i-know`` and the environment.

Its real path sends Telegram messages through the owner's bot, so no test ever reaches it:
``run`` is replaced before ``main`` is called, and a refusal must come before anything opens.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "local_e2e.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("local_e2e", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = load()
    started: list[Any] = []

    async def must_not_run(args: argparse.Namespace, root: Path) -> int:
        started.append(args)
        return 0

    monkeypatch.setattr(module, "run", must_not_run)
    module.started = started  # type: ignore[attr-defined]
    return module


@pytest.fixture
def token_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("BOT_TOKEN", "API_ID", "API_HASH", "OWNER_ID"):
        monkeypatch.setenv(name, "dummy")


def test_without_i_know_nothing_starts(
    script: ModuleType, token_in_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert script.main(["--rule", "flat"]) == 2
    assert "--i-know" in capsys.readouterr().err
    assert script.started == []


def test_the_token_must_be_in_the_environment(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("BOT_TOKEN", "API_ID", "API_HASH", "OWNER_ID"):
        monkeypatch.delenv(name, raising=False)
    assert script.main(["--i-know"]) == 2
    err = capsys.readouterr().err
    assert "BOT_TOKEN" in err and "OWNER_ID" in err
    assert script.started == []


@pytest.mark.parametrize("argv", [["--every", "0"], ["--posts", "0"], ["--posts", "51"]])
def test_silly_numbers_are_refused(script: ModuleType, token_in_env: None, argv: list[str]) -> None:
    assert script.main(["--i-know", *argv]) == 2
    assert script.started == []


def test_told_twice_it_runs(script: ModuleType, token_in_env: None) -> None:
    assert script.main(["--i-know", "--rule", "room", "--posts", "2"]) == 0
    (args,) = script.started
    assert (args.rule, args.posts) == ("room", 2)


def test_the_brain_reads_no_env_file_and_binds_loopback_only() -> None:
    source = SCRIPT.read_text()
    assert "_env_file=None" in source
    assert 'ingest_host="127.0.0.1"' in source
    assert "facebook.com" not in source.replace("https://www.facebook.com/groups/", "")


async def test_the_whole_run_works_offline_with_telegram_faked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Everything but the bot's connection is real; the bot is ddiver-core's recording fake."""
    from ddiver_core import i18n
    from ddiver_core_testing import FakeBotClient
    from tests.brain.helpers import FakeTelethon

    module = load()
    sent = FakeBotClient()
    real = module.BrainApp

    def offline(settings: Any) -> Any:
        return real(settings, bot_client=FakeTelethon(), bot=sent)

    monkeypatch.setattr(module, "BrainApp", offline)
    monkeypatch.setenv("BOT_TOKEN", "123456789:" + "A" * 35)
    monkeypatch.setenv("API_ID", "1")
    monkeypatch.setenv("API_HASH", "0" * 32)
    monkeypatch.setenv("OWNER_ID", "111")
    args = module.parse(["--i-know", "--rule", "flat", "--every", "1", "--posts", "1"])
    try:
        assert await module.run(args, tmp_path) == 0
    finally:
        i18n.clear_overlays()
    notes = [m for m in sent.sent if any(getattr(e, "url", None) for e in m.entities)]
    assert len(notes) == 1
    assert notes[0].user_id == 111 and "flat" in notes[0].text
