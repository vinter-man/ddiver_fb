from __future__ import annotations

import datetime as dt
import os
import zoneinfo
from pathlib import Path

import pytest
from pydantic import ValidationError

from fbcollector.config import (
    CAUTIOUS,
    DAY,
    HOUR,
    NORMAL,
    ConfigError,
    ProfileKnobs,
    Settings,
    check_private_file,
    default_root,
    describe_config_error,
)
from fbwire import BudgetKind

TOKEN = "123456789:" + "A" * 35
INGEST_TOKEN = "t" * 32


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No FBC_* from the developer's shell, and a root that is not the real one."""
    for key in list(os.environ):
        if key.upper().startswith("FBC_"):
            monkeypatch.delenv(key)
    root = tmp_path / "root"
    monkeypatch.setenv("FBC_ROOT", str(root))
    monkeypatch.setenv("FBC_TIMEZONE", "Europe/Sofia")
    return root


def load(**kw: object) -> Settings:
    return Settings.load(**kw)  # type: ignore[arg-type]


def write_secret(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def error_text(exc_info: pytest.ExceptionInfo[BaseException]) -> str:
    return describe_config_error(exc_info.value)  # type: ignore[arg-type]


class TestDefaults:
    def test_design_defaults(self, clean_env: Path) -> None:
        s = load()
        assert s.root == clean_env
        assert s.timezone == "Europe/Sofia"
        assert s.zone == zoneinfo.ZoneInfo("Europe/Sofia")
        assert s.collector_id == "home-1"
        assert s.cautious == CAUTIOUS
        assert s.normal == NORMAL
        assert (s.quiet_start, s.quiet_end) == (dt.time(1, 0), dt.time(7, 0))
        assert s.quiet_jitter_minutes == 45
        assert (s.breaks_per_day, s.break_min_minutes, s.break_max_minutes) == (2, 15, 40)
        assert s.min_navigation_gap_seconds == 45
        assert s.group_revisit_minutes == 30
        assert s.cautious_days_after_login == 14
        assert s.novelty_days == 7
        assert s.outbox_cap == 5000
        assert s.local_mode
        assert s.monitored_file == clean_env / "monitored.toml"
        assert s.raw_retention_seconds == 48 * HOUR
        assert s.missing_for_run() == ["FBC_ALERT_BOT_TOKEN", "FBC_ALERT_OWNER_ID"]

    def test_observe_mode_keeps_raw_payloads_longer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FBC_OBSERVE_MODE", "true")
        assert load().raw_retention_seconds == 21 * DAY

    def test_platform_roots(self, tmp_path: Path) -> None:
        assert default_root("darwin", tmp_path) == (
            tmp_path / "Library" / "Application Support" / "fbcollector"
        )
        assert default_root("linux", tmp_path) == tmp_path / ".local/share/fbcollector"

    def test_default_root_when_nothing_says_otherwise(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("FBC_ROOT")
        s = load(platform="linux", home=tmp_path)
        assert s.root == tmp_path / ".local/share/fbcollector"

    def test_explicit_monitored_path_and_tilde(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FBC_MONITORED_PATH", "~/m.toml")
        assert load().monitored_file == Path("~/m.toml").expanduser()

    def test_settings_are_frozen(self) -> None:
        s = load()
        with pytest.raises(ValidationError):
            s.collector_id = "x"


class TestTimezone:
    @pytest.mark.parametrize("zone", ["Mars/Olympus", "europe/sofia", "", "+02:00", "EET "])
    def test_bad_zones(self, monkeypatch: pytest.MonkeyPatch, zone: str) -> None:
        monkeypatch.setenv("FBC_TIMEZONE", zone)
        if zone == "EET ":  # whitespace around a real zone is forgiven
            assert load().timezone == "EET"
            return
        with pytest.raises(ValidationError) as info:
            load()
        assert "FBC_TIMEZONE" in error_text(info)

    def test_timezone_is_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FBC_TIMEZONE")
        with pytest.raises(ValidationError) as info:
            load()
        assert error_text(info).startswith("FBC_TIMEZONE")


class TestSecrets:
    def test_alert_bot_pair(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FBC_ALERT_BOT_TOKEN", TOKEN)
        monkeypatch.setenv("FBC_ALERT_OWNER_ID", "42")
        s = load()
        assert s.missing_for_run() == []
        assert s.alert_bot_token is not None
        assert s.alert_bot_token.get_secret_value() == TOKEN
        assert TOKEN not in repr(s)
        assert TOKEN not in str(s)

    @pytest.mark.parametrize("token", ["nocolon", "abc:" + "A" * 30, "1:short", "١٢:" + "A" * 30])
    def test_bad_token_is_named_not_quoted(
        self, monkeypatch: pytest.MonkeyPatch, token: str
    ) -> None:
        monkeypatch.setenv("FBC_ALERT_BOT_TOKEN", token)
        monkeypatch.setenv("FBC_ALERT_OWNER_ID", "42")
        with pytest.raises(ValidationError) as info:
            load()
        text = error_text(info)
        assert "FBC_ALERT_BOT_TOKEN" in text
        assert token not in text

    def test_token_needs_an_owner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FBC_ALERT_BOT_TOKEN", TOKEN)
        with pytest.raises(ValidationError) as info:
            load()
        assert "go together" in error_text(info)
        assert TOKEN not in error_text(info)

    def test_secrets_file_is_read_from_the_root(self, clean_env: Path) -> None:
        write_secret(
            clean_env / "secrets",
            f"FBC_ALERT_BOT_TOKEN={TOKEN}\nFBC_ALERT_OWNER_ID=42\nFBC_ROOT=/elsewhere\n",
        )
        s = load()
        assert s.alert_owner_id == 42
        assert s.root == clean_env  # pinned before the file was read

    def test_process_environment_beats_the_secrets_file(
        self, clean_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_secret(clean_env / "secrets", "FBC_COLLECTOR_ID=from-file\n")
        monkeypatch.setenv("FBC_COLLECTOR_ID", "from-env")
        assert load().collector_id == "from-env"

    def test_secrets_file_overrides_the_env_file(self, clean_env: Path, tmp_path: Path) -> None:
        env_file = tmp_path / "dev.env"
        env_file.write_text("FBC_COLLECTOR_ID=from-env-file\nFBC_OBSERVE_MODE=1\n")
        write_secret(clean_env / "secrets", "FBC_COLLECTOR_ID=from-secrets\n")
        s = load(env_file=env_file)
        assert s.collector_id == "from-secrets"
        assert s.observe_mode

    def test_root_can_come_from_the_env_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("FBC_ROOT")
        other = tmp_path / "other-root"
        env_file = tmp_path / "dev.env"
        env_file.write_text(f"fbc_root={other}\n")
        write_secret(other / "secrets", "FBC_COLLECTOR_ID=found\n")
        s = load(env_file=env_file, home=tmp_path)
        assert s.root == other
        assert s.collector_id == "found"

    def test_env_file_without_a_root(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("FBC_ROOT")
        env_file = tmp_path / "dev.env"
        env_file.write_text("FBC_COLLECTOR_ID=x\n")
        s = load(env_file=env_file, platform="darwin", home=tmp_path)
        assert s.root == default_root("darwin", tmp_path)

    def test_explicit_secrets_file(self, tmp_path: Path) -> None:
        path = tmp_path / "elsewhere.secrets"
        write_secret(path, "FBC_COLLECTOR_ID=explicit\n")
        assert load(secrets_file=path).collector_id == "explicit"

    @pytest.mark.parametrize("mode", [0o640, 0o604, 0o660])
    def test_readable_secrets_file_is_refused(self, clean_env: Path, mode: int) -> None:
        write_secret(clean_env / "secrets", f"FBC_ALERT_BOT_TOKEN={TOKEN}\n", mode=mode)
        with pytest.raises(ConfigError, match="chmod 600") as info:
            load()
        assert TOKEN not in str(info.value)

    def test_secrets_path_must_be_a_file(self, clean_env: Path) -> None:
        (clean_env / "secrets").mkdir(parents=True)
        with pytest.raises(ConfigError, match="not a regular file"):
            load()

    def test_secrets_owned_by_someone_else(
        self, clean_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_secret(clean_env / "secrets", "FBC_COLLECTOR_ID=x\n")
        real = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: real + 1)
        with pytest.raises(ConfigError, match="another user"):
            load()

    def test_unreadable_path(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="cannot be read"):
            check_private_file(tmp_path / "nope")


class TestIngest:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("http://127.0.0.1:18781/", "http://127.0.0.1:18781"),
            ("http://localhost:18781", "http://localhost:18781"),
            ("http://[::1]:18781", "http://[::1]:18781"),
            ("https://brain.example", "https://brain.example"),
        ],
    )
    def test_accepted(self, monkeypatch: pytest.MonkeyPatch, url: str, expected: str) -> None:
        monkeypatch.setenv("FBC_INGEST_URL", url)
        monkeypatch.setenv("FBC_INGEST_TOKEN", INGEST_TOKEN)
        s = load()
        assert s.ingest_url == expected
        assert not s.local_mode

    @pytest.mark.parametrize(
        "url", ["http://10.0.0.5:8781", "http://brain.example", "ftp://127.0.0.1", "127.0.0.1:1"]
    )
    def test_rejected(self, monkeypatch: pytest.MonkeyPatch, url: str) -> None:
        monkeypatch.setenv("FBC_INGEST_URL", url)
        monkeypatch.setenv("FBC_INGEST_TOKEN", INGEST_TOKEN)
        with pytest.raises(ValidationError) as info:
            load()
        assert "FBC_INGEST_URL" in error_text(info)

    def test_an_uplink_needs_a_collector_id_of_its_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round 2 F4: every installation defaulted to home-1, the name the lease is granted
        to; a forgotten Mac and its replacement laptop both answered to it."""
        monkeypatch.setenv("FBC_ALERT_BOT_TOKEN", TOKEN)
        monkeypatch.setenv("FBC_ALERT_OWNER_ID", "42")
        assert load().missing_for_run() == []  # phase 0 needs no name
        monkeypatch.setenv("FBC_INGEST_URL", "http://127.0.0.1:18781")
        monkeypatch.setenv("FBC_INGEST_TOKEN", INGEST_TOKEN)
        assert load().missing_for_run() == ["FBC_COLLECTOR_ID"]
        monkeypatch.setenv("FBC_COLLECTOR_ID", "laptop-1")
        s = load()
        assert s.missing_for_run() == [] and s.collector_id == "laptop-1"

    def test_the_tunnels_socket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FBC_INGEST_URL", "http://127.0.0.1:8781")
        monkeypatch.setenv("FBC_INGEST_TOKEN", INGEST_TOKEN)
        assert load().ingest_socket is None
        monkeypatch.setenv("FBC_INGEST_SOCKET", "  ")
        assert load().ingest_socket is None
        monkeypatch.setenv("FBC_INGEST_SOCKET", "/Users/fbc/.ssh/fbtunnel.sock")
        assert load().ingest_socket == Path("/Users/fbc/.ssh/fbtunnel.sock")

    @pytest.mark.parametrize("socket", ["fbtunnel.sock", "~/.ssh/fbtunnel.sock", "/" + "s" * 103])
    def test_a_socket_must_be_an_absolute_path_that_fits(
        self, monkeypatch: pytest.MonkeyPatch, socket: str
    ) -> None:
        monkeypatch.setenv("FBC_INGEST_URL", "http://127.0.0.1:8781")
        monkeypatch.setenv("FBC_INGEST_TOKEN", INGEST_TOKEN)
        monkeypatch.setenv("FBC_INGEST_SOCKET", socket)
        with pytest.raises(ValidationError) as info:
            load()
        assert "FBC_INGEST_SOCKET" in error_text(info)

    def test_a_socket_needs_the_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FBC_INGEST_SOCKET", "/Users/fbc/.ssh/fbtunnel.sock")
        with pytest.raises(ValidationError, match="ingest_socket needs ingest_url"):
            load()

    def test_blank_url_means_local_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FBC_INGEST_URL", "  ")
        assert load().local_mode

    def test_url_and_token_go_together(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FBC_INGEST_URL", "http://127.0.0.1:18781")
        with pytest.raises(ValidationError, match="go together"):
            load()

    def test_short_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FBC_INGEST_URL", "http://127.0.0.1:18781")
        monkeypatch.setenv("FBC_INGEST_TOKEN", "short-secret")
        with pytest.raises(ValidationError) as info:
            load()
        assert "FBC_INGEST_TOKEN" in error_text(info)
        assert "short-secret" not in error_text(info)

    def test_client_timeout_is_at_most_ten_seconds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FBC_INGEST_TIMEOUT", "30")
        with pytest.raises(ValidationError):
            load()


class TestKnobs:
    def test_a_cap_can_be_lowered_and_the_rest_keeps_the_design(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FBC_NORMAL__NAV_PER_HOUR", "15")
        s = load()
        assert s.normal.nav_per_hour == 15
        assert s.normal.nav_per_day == NORMAL.nav_per_day
        assert s.cautious == CAUTIOUS

    @pytest.mark.parametrize(
        ("var", "value", "message"),
        [
            ("FBC_NORMAL__NAV_PER_HOUR", "25", "normal.nav_per_hour may not exceed 20"),
            ("FBC_CAUTIOUS__SCROLL_PER_DAY", "251", "cautious.scroll_per_day may not exceed 250"),
            ("FBC_CAUTIOUS__FEED_MIN_MINUTES", "5", "cautious.feed_min_minutes may not be below"),
            ("FBC_NORMAL__BACKSTOP_GROUPS_PER_HOUR", "10", "backstop_groups_per_hour may not"),
            ("FBC_NORMAL__BACKSTOP_FLOOR_HOURS", "1", "backstop_floor_hours may not be below"),
        ],
    )
    def test_nothing_can_be_made_faster_than_the_design(
        self, monkeypatch: pytest.MonkeyPatch, var: str, value: str, message: str
    ) -> None:
        monkeypatch.setenv(var, value)
        with pytest.raises(ValidationError) as info:
            load()
        assert message in error_text(info)

    @pytest.mark.parametrize(
        ("var", "value"),
        [
            ("FBC_MIN_NAVIGATION_GAP_SECONDS", "30"),
            ("FBC_GROUP_REVISIT_MINUTES", "10"),
            ("FBC_CAUTIOUS_DAYS_AFTER_LOGIN", "3"),
            ("FBC_STRIKE_DECAY_DAYS", "10"),
            ("FBC_WARMUP_MIN_HOURS", "2"),
            ("FBC_SHOULDER_FEED_MIN_MINUTES", "5"),
            ("FBC_QUIET_START", "25:00"),
            ("FBC_QUIET_END", "01:00+02:00"),
            ("FBC_LOG_LEVEL", "TRACE"),
            ("FBC_LOG_FORMAT", "xml"),
            ("FBC_COLLECTOR_ID", "has space"),
            ("FBC_NORMAL__NOT_A_KNOB", "1"),
        ],
    )
    def test_invalid_values(self, monkeypatch: pytest.MonkeyPatch, var: str, value: str) -> None:
        monkeypatch.setenv(var, value)
        with pytest.raises(ValidationError):
            load()

    @pytest.mark.parametrize(
        ("pairs", "message"),
        [
            ({"FBC_BREAK_MIN_MINUTES": "50"}, "break_min_minutes"),
            ({"FBC_SHOULDER_FEED_MIN_MINUTES": "30"}, "shoulder_feed_min_minutes"),
            ({"FBC_WARMUP_MIN_HOURS": "80"}, "warmup_min_hours"),
            ({"FBC_NOVELTY_DAYS": "20", "FBC_SEEN_RETENTION_DAYS": "10"}, "seen_retention_days"),
            ({"FBC_NORMAL__FEED_MIN_MINUTES": "12"}, "feed_min_minutes must not exceed"),
        ],
    )
    def test_cross_field_rules(
        self, monkeypatch: pytest.MonkeyPatch, pairs: dict[str, str], message: str
    ) -> None:
        for key, value in pairs.items():
            monkeypatch.setenv(key, value)
        with pytest.raises(ValidationError, match=message):
            load()

    def test_rhythm_knobs_parse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FBC_QUIET_START", "00:30")
        monkeypatch.setenv("FBC_BREAKS_PER_DAY", "0")
        monkeypatch.setenv("FBC_LOG_LEVEL", "debug")
        monkeypatch.setenv("FBC_LOG_FORMAT", "JSON")
        s = load()
        assert s.quiet_start == dt.time(0, 30)
        assert s.breaks_per_day == 0
        assert (s.log_level, s.log_format) == ("DEBUG", "json")

    def test_backstop_interval(self) -> None:
        assert NORMAL.backstop_interval_hours(5) == 1.5
        assert NORMAL.backstop_interval_hours(10) == pytest.approx(1.5625)
        assert NORMAL.backstop_interval_hours(25) == pytest.approx(3.906, abs=1e-3)
        # No rate of its own: the floor, and the budget decides the rest (planned_load).
        assert CAUTIOUS.backstop_groups_per_hour is None
        assert CAUTIOUS.backstop_interval_hours(25) == 3.0

    def test_a_cautious_rate_may_be_added_but_normal_cannot_drop_its_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FBC_CAUTIOUS__BACKSTOP_GROUPS_PER_HOUR", "2")
        s = load()
        assert s.cautious.backstop_groups_per_hour == 2.0  # a ceiling where there was none
        assert s.cautious.backstop_interval_hours(10) == 5.0
        unlimited = NORMAL.model_copy(update={"backstop_groups_per_hour": None})
        assert unlimited.looser_than(NORMAL) == ["backstop_groups_per_hour may not exceed 6.4"]

    def test_windows_cover_every_cap(self) -> None:
        assert NORMAL.windows() == (
            (BudgetKind.NAVIGATION, 900, 5),
            (BudgetKind.NAVIGATION, 3600, 20),
            (BudgetKind.NAVIGATION, 86400, 320),
            (BudgetKind.SCROLL, 3600, 60),
            (BudgetKind.SCROLL, 86400, 500),
        )

    def test_design_profiles_are_not_looser_than_themselves(self) -> None:
        assert CAUTIOUS.looser_than(CAUTIOUS) == []
        assert NORMAL.looser_than(CAUTIOUS)  # normal is looser than cautious, by design

    def test_knobs_reject_unknown_fields_and_order(self) -> None:
        with pytest.raises(ValidationError):
            ProfileKnobs.model_validate({**NORMAL.model_dump(), "feed_min_minutes": 20})


class TestDescribe:
    def test_plain_exception(self) -> None:
        assert describe_config_error(RuntimeError("boom")) == "boom"

    def test_broken_error_object(self) -> None:
        class WeirdError(Exception):
            def errors(self, **_: object) -> list[object]:
                raise RuntimeError

        assert describe_config_error(WeirdError()) == "WeirdError"

    def test_empty_error_list(self) -> None:
        class EmptyError(Exception):
            def errors(self, **_: object) -> list[object]:
                return []

        assert describe_config_error(EmptyError()) == "EmptyError"
