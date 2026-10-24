"""The brain's settings: the core's, the ingest endpoint, and the collector's day."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ddiver_core.config import Settings
from ddiver_core.process import describe_config_error
from ddiver_fb.config import FbSettings
from tests.brain.helpers import TOKEN, settings_for


class TestDefaults:
    def test_a_bare_metal_run_listens_on_loopback_only(self, tmp_path: Path):
        settings = settings_for(tmp_path, ingest_port=8781)
        assert settings.ingest_host == "127.0.0.1" and settings.ingest_port == 8781

    def test_paths(self, tmp_path: Path):
        settings = settings_for(tmp_path)
        assert settings.db_path == tmp_path / "fb.sqlite"
        assert settings.health_path == tmp_path / "health.json"

    def test_the_day(self, tmp_path: Path):
        settings = settings_for(tmp_path)
        assert (settings.day_start_hour, settings.day_end_hour) == (8, 23)
        assert settings.collector_tz is None and settings.offline_after_minutes == 20


class TestToken:
    @pytest.mark.parametrize(
        "token",
        ["short", "k" * 31, "k" * 20 + " " + "k" * 20, "ключ" * 10, "k" * 39 + "\x07"],
    )
    def test_a_weak_or_unsendable_token_is_refused_without_echoing_it(
        self, tmp_path: Path, token: str
    ):
        with pytest.raises(ValidationError) as caught:
            settings_for(tmp_path, ingest_token=token)
        message = describe_config_error(caught.value)
        assert "INGEST_TOKEN" in message
        assert token not in message

    def test_a_generated_token_is_fine(self, tmp_path: Path):
        assert settings_for(tmp_path).ingest_token.get_secret_value() == TOKEN


class TestZoneAndHours:
    def test_a_zone(self, tmp_path: Path):
        assert settings_for(tmp_path, collector_tz="Europe/Sofia").collector_tz == "Europe/Sofia"
        assert settings_for(tmp_path, collector_tz="  ").collector_tz is None

    def test_a_bad_zone(self, tmp_path: Path):
        with pytest.raises(ValidationError):
            settings_for(tmp_path, collector_tz="Mars/Olympus")

    def test_the_day_must_start_before_it_ends(self, tmp_path: Path):
        with pytest.raises(ValidationError):
            settings_for(tmp_path, day_start_hour=23, day_end_hour=8)


REQUIRED = ("BOT_TOKEN", "API_ID", "API_HASH", "OWNER_ID", "INGEST_TOKEN", "DATA_DIR")


def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str = "") -> Path:
    """A complete .env in ``tmp_path``, with nothing of it left in the process's environment."""
    for name in REQUIRED:
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "BOT_TOKEN=123456789:" + "B" * 35 + "\nAPI_ID=5\nAPI_HASH=abc\nOWNER_ID=42\n"
        f"INGEST_TOKEN={TOKEN}\nDATA_DIR={tmp_path}\n{extra}"
    )
    return env


class TestLoad:
    def test_from_an_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        env = env_file(tmp_path, monkeypatch, "COLLECTOR_TZ=Europe/Kyiv\n")
        settings = FbSettings.load(env)
        assert isinstance(settings, FbSettings)
        assert (settings.owner_id, settings.collector_tz) == (42, "Europe/Kyiv")


BRAIN_FIELDS = {
    "ingest_host",
    "ingest_port",
    "ingest_token",
    "collector_tz",
    "day_start_hour",
    "day_end_hour",
    "offline_after_minutes",
    "items_retention_days",
    "silence_factor",
    "silence_floor_hours",
    "openrouter_api_key",
    "ai_model",
    "ai_daily_calls",
}


class TestOnTheCoresBase:
    """ddiver-core's ``Settings`` is the base every app shares; ddiver's listener settings live
    in ddiver's own subclass since core-v0.1.0, and must not come back here."""

    def test_the_brain_adds_only_its_own_fields_to_the_base(self):
        assert issubclass(FbSettings, Settings)
        assert set(FbSettings.model_fields) - set(Settings.model_fields) == BRAIN_FIELDS

    def test_the_bases_fields_keep_their_meaning(self):
        for name, base in Settings.model_fields.items():
            mine = FbSettings.model_fields[name]
            assert (mine.annotation, mine.default) == (base.annotation, base.default), name

    def test_there_is_no_user_session_and_no_catch_up(self, tmp_path: Path):
        settings = settings_for(tmp_path)
        for name in ("catchup_hours", "catchup_max_per_chat", "ignore_own_messages"):
            assert name not in FbSettings.model_fields
        assert not hasattr(settings, "user_session")
        assert settings.bot_session == tmp_path / "bot.session"

    def test_a_listener_setting_left_in_the_env_file_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """An operator who copies ddiver's .env must not get a brain that refuses to start
        over a value it never reads, however wrong the value. Only a file can show it: an
        unknown name in the process's environment is never read at all, whatever the model's
        ``extra`` says, while an unknown key in the file is an extra input."""
        stray = "CATCHUP_HOURS=not a number\nIGNORE_OWN_MESSAGES=maybe\nUSER_SESSION=x\n"
        settings = FbSettings.load(env_file(tmp_path, monkeypatch, stray))
        assert settings.owner_id == 42
        for name in ("catchup_hours", "ignore_own_messages", "user_session"):
            assert not hasattr(settings, name), name
            assert name not in (settings.model_extra or {}), name

    def test_a_listener_setting_left_in_the_environment_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The same names exported by a shell that still has ddiver's settings."""
        monkeypatch.setenv("CATCHUP_HOURS", "not a number")
        monkeypatch.setenv("IGNORE_OWN_MESSAGES", "maybe")
        settings = settings_for(tmp_path)
        assert not hasattr(settings, "catchup_hours")

    def test_load_builds_the_brains_class_through_the_base(self):
        assert "load" not in vars(FbSettings)  # the base's ``load`` returns ``Self``
