"""Collector settings: ``FBC_*`` environment variables, an optional env file, and the secrets file.

Secrets (the alert bot token, the ingest token) live in ``<root>/secrets``, a dotenv-format file
that must be 0600 and owned by the collector's user — it is refused otherwise, the way ssh
refuses a readable private key, because the root it sits in is the account (invariant 7).

The pacing knobs exist so the owner can make the collector *slower*, never faster: every cap may
only go down from the design's value and every interval only up. A typo that doubles a cap is
the kind of mistake that loses the account, so it fails at start instead of taking effect.

Errors are described by field name and reason only (``describe_config_error``): pydantic's own
message quotes the rejected value, which for a token means printing the token.
"""

from __future__ import annotations

import datetime as dt
import os
import stat
import sys
import zoneinfo
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fbwire.models import BudgetKind

ENV_PREFIX: Final = "FBC_"
SECRETS_NAME: Final = "secrets"
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})
MAX_SOCKET_PATH: Final = 103
"""A Unix socket's path in bytes: macOS's ``sun_path`` holds 104 with the closing NUL."""
DEFAULT_COLLECTOR_ID: Final = "home-1"
"""Phase 0 only: with an uplink the id must be set, one per machine (``missing_for_run``)."""

MINUTE: Final = 60
HOUR: Final = 3600
DAY: Final = 86400


class ConfigError(Exception):
    """A setting cannot be used; the message names the setting and never its value."""


def default_root(platform: str | None = None, home: Path | None = None) -> Path:
    """Where the collector keeps its profile and state (DESIGN, Storage)."""
    base = home if home is not None else Path.home()
    if (platform or sys.platform) == "darwin":
        return base / "Library" / "Application Support" / "fbcollector"
    return base / ".local" / "share" / "fbcollector"


@cache
def _zones() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones())


class ProfileKnobs(BaseModel):
    """The pacing of one profile (DESIGN, Scheduler, budget and gates)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    feed_min_minutes: float = Field(gt=0)
    feed_max_minutes: float = Field(gt=0)
    backstop_floor_hours: float = Field(gt=0)
    backstop_groups_per_hour: float | None = Field(gt=0)
    """Backstop interval is ``max(floor, N / this)`` hours for N monitored groups.

    ``None``: no rate of its own — the planner gives backstops whatever room the caps leave at
    75 % (``budget.backstop_rate_limit``), never less than ``backstop_floor_hours`` apart. The
    cautious profile is set so (owner decision of 2026-09-25)."""
    nav_per_15min: int = Field(ge=0)
    nav_per_hour: int = Field(ge=0)
    nav_per_day: int = Field(ge=0)
    scroll_per_hour: int = Field(ge=0)
    scroll_per_day: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> ProfileKnobs:
        if self.feed_min_minutes > self.feed_max_minutes:
            msg = "feed_min_minutes must not exceed feed_max_minutes"
            raise ValueError(msg)
        return self

    def backstop_interval_hours(self, groups: int) -> float:
        """The configured interval before the budget is asked (the floor without a rate)."""
        rate = self.backstop_groups_per_hour
        if rate is None:
            return self.backstop_floor_hours
        return max(self.backstop_floor_hours, groups / rate)

    def windows(self) -> tuple[tuple[BudgetKind, int, int], ...]:
        """Every hard cap as ``(kind, window seconds, cap)``."""
        return (
            (BudgetKind.NAVIGATION, 15 * MINUTE, self.nav_per_15min),
            (BudgetKind.NAVIGATION, HOUR, self.nav_per_hour),
            (BudgetKind.NAVIGATION, DAY, self.nav_per_day),
            (BudgetKind.SCROLL, HOUR, self.scroll_per_hour),
            (BudgetKind.SCROLL, DAY, self.scroll_per_day),
        )

    def looser_than(self, design: ProfileKnobs) -> list[str]:
        """Every knob that is less cautious than ``design``'s."""
        # Higher is looser for caps and rates, lower is looser for intervals.
        ceilings = (
            "nav_per_15min",
            "nav_per_hour",
            "nav_per_day",
            "scroll_per_hour",
            "scroll_per_day",
            "backstop_groups_per_hour",
        )
        floors = ("feed_min_minutes", "feed_max_minutes", "backstop_floor_hours")
        problems = [
            f"{name} may not exceed {getattr(design, name)}"
            for name in ceilings
            if _exceeds(getattr(self, name), getattr(design, name))
        ]
        problems += [
            f"{name} may not be below {getattr(design, name)}"
            for name in floors
            if getattr(self, name) < getattr(design, name)
        ]
        return problems


def _exceeds(value: float | None, ceiling: float | None) -> bool:
    """A ceiling knob looser than the design's; ``None`` is "no limit of its own" (loosest)."""
    if ceiling is None:
        return False
    return value is None or value > ceiling


CAUTIOUS: Final = ProfileKnobs(
    # Owner decision of 2026-09-25: feed every U(10, 18) min; backstops as often as 75 % of the
    # caps allow, never more often than every 3 h (DESIGN rev 2 had U(8, 14) and N / 3.2 h).
    feed_min_minutes=10,
    feed_max_minutes=18,
    backstop_floor_hours=3,
    backstop_groups_per_hour=None,
    nav_per_15min=3,
    nav_per_hour=10,
    nav_per_day=160,
    scroll_per_hour=30,
    scroll_per_day=250,
)
NORMAL: Final = ProfileKnobs(
    feed_min_minutes=4.5,
    feed_max_minutes=9.5,
    backstop_floor_hours=1.5,
    backstop_groups_per_hour=6.4,
    nav_per_15min=5,
    nav_per_hour=20,
    nav_per_day=320,
    scroll_per_hour=60,
    scroll_per_day=500,
)


def _merge_knobs(design: ProfileKnobs, value: Any) -> Any:
    # FBC_NORMAL__NAV_PER_HOUR=15 arrives as {"nav_per_hour": "15"}: the rest keeps the design.
    if isinstance(value, Mapping):
        return {**design.model_dump(), **value}
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        frozen=True,
    )

    root: Path = Field(default_factory=default_root, description="profile, state, raw, shots")
    timezone: str = Field(description="IANA zone the quiet hours are pinned to, e.g. Europe/Sofia")
    collector_id: str = Field(
        default=DEFAULT_COLLECTOR_ID,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
        description="this machine's name for the brain's lease; required with an uplink",
    )

    alert_bot_token: SecretStr | None = Field(default=None, description="owner-only alert bot")
    alert_owner_id: int | None = Field(default=None, gt=0)

    ingest_url: str | None = Field(default=None, description="unset: phase-0 local mode")
    ingest_token: SecretStr | None = None
    ingest_socket: Path | None = Field(
        default=None,
        description="the tunnel's Unix socket; requests go through it to the brain the URL names",
    )
    ingest_timeout: float = Field(default=10.0, gt=0, le=10)

    observe_mode: bool = False
    monitored_path: Path | None = Field(default=None, description="default: <root>/monitored.toml")

    log_level: str = "INFO"
    log_format: str = "text"

    cautious: ProfileKnobs = CAUTIOUS
    normal: ProfileKnobs = NORMAL

    quiet_start: dt.time = dt.time(1, 0)
    quiet_end: dt.time = dt.time(7, 0)
    quiet_jitter_minutes: int = Field(default=45, ge=0, le=120)
    quiet_weekend_shift_minutes: int = Field(default=60, ge=0, le=180)
    shoulder_after_minutes: int = Field(default=60, ge=0, le=240)
    shoulder_before_minutes: int = Field(default=90, ge=0, le=240)
    shoulder_feed_min_minutes: float = Field(default=15, ge=15)
    shoulder_feed_max_minutes: float = Field(default=25, ge=15)

    breaks_per_day: int = Field(default=2, ge=0, le=6)
    break_min_minutes: int = Field(default=15, ge=1)
    break_max_minutes: int = Field(default=40, ge=1, le=240)

    min_navigation_gap_seconds: float = Field(default=45, ge=45)
    group_revisit_minutes: float = Field(default=30, ge=30)
    downtime_reset_hours: float = Field(default=2, gt=0, le=24)

    cautious_days_after_login: int = Field(default=14, ge=14)
    cautious_days_after_warning: int = Field(default=7, ge=7)
    cautious_days_after_logout: int = Field(default=14, ge=14)
    strike_decay_days: int = Field(default=30, ge=30, le=3650)
    """Block strikes expire after this many days without a block (owner, 2026-09-25)."""
    warmup_min_hours: float = Field(default=24, ge=24)
    warmup_max_hours: float = Field(default=72, ge=24)

    novelty_days: int = Field(default=7, ge=1, le=30)
    late_after_hours: float = Field(default=2, gt=0)
    raw_retention_hours: int = Field(default=48, ge=1)
    observe_raw_retention_days: int = Field(default=21, ge=7)
    seen_retention_days: int = Field(default=30, ge=7)
    outbox_cap: int = Field(default=5000, ge=100)

    # -- field checks --------------------------------------------------------------------------

    @field_validator("root", "monitored_path", mode="after")
    @classmethod
    def _expand(cls, value: Path | None) -> Path | None:
        return None if value is None else value.expanduser()

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        # Exact, case-sensitive: macOS resolves "europe/sofia" from its case-insensitive disk,
        # Linux does not, and the laptop is where this runs for good.
        name = value.strip()
        if name not in _zones():
            msg = "must be an IANA zone name such as Europe/Sofia"
            raise ValueError(msg)
        return name

    @field_validator("alert_bot_token")
    @classmethod
    def _bot_token_shape(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        digits, sep, rest = value.get_secret_value().partition(":")
        if not sep or not digits.isascii() or not digits.isdigit() or len(rest) < 20:
            msg = "must look like 123456789:AA... (from @BotFather)"
            raise ValueError(msg)
        return value

    @field_validator("ingest_url")
    @classmethod
    def _ingest_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        parts = urlsplit(value.strip())
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            msg = "must be an http(s) URL"
            raise ValueError(msg)
        # Plain HTTP carries the bearer token in clear: only over the ssh tunnel's local end.
        if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
            msg = "plain http is allowed only to the tunnel on 127.0.0.1"
            raise ValueError(msg)
        return value.strip().rstrip("/")

    @field_validator("ingest_socket", mode="before")
    @classmethod
    def _ingest_socket(cls, value: object) -> object:
        # The tunnel's local end as a socket in the collector's own 0700 ~/.ssh: no other local
        # account can create it, so none can stand in for ssh and be handed the bearer token (a
        # loopback TCP port, any account can bind while ssh is down).
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        path = Path(str(value).strip()) if isinstance(value, str) else value
        if not isinstance(path, Path) or not path.is_absolute():
            msg = "must be an absolute path"
            raise ValueError(msg)
        if len(os.fsencode(path)) > MAX_SOCKET_PATH:
            msg = f"must be at most {MAX_SOCKET_PATH} bytes long (a Unix socket's limit)"
            raise ValueError(msg)
        return path

    @field_validator("ingest_token")
    @classmethod
    def _ingest_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < 24:
            msg = "must be at least 24 characters"
            raise ValueError(msg)
        return value

    @field_validator("log_format")
    @classmethod
    def _log_format(cls, value: str) -> str:
        lower = value.lower()
        if lower not in {"text", "json"}:
            msg = "must be text or json"
            raise ValueError(msg)
        return lower

    @field_validator("log_level")
    @classmethod
    def _log_level(cls, value: str) -> str:
        upper = value.upper()
        if upper not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            msg = "must be DEBUG, INFO, WARNING or ERROR"
            raise ValueError(msg)
        return upper

    @field_validator("cautious", mode="before")
    @classmethod
    def _cautious_defaults(cls, value: Any) -> Any:
        return _merge_knobs(CAUTIOUS, value)

    @field_validator("normal", mode="before")
    @classmethod
    def _normal_defaults(cls, value: Any) -> Any:
        return _merge_knobs(NORMAL, value)

    @model_validator(mode="after")
    def _consistent(self) -> Settings:
        problems = [f"cautious.{p}" for p in self.cautious.looser_than(CAUTIOUS)]
        problems += [f"normal.{p}" for p in self.normal.looser_than(NORMAL)]
        if (self.alert_bot_token is None) != (self.alert_owner_id is None):
            problems.append("alert_bot_token and alert_owner_id go together")
        if (self.ingest_url is None) != (self.ingest_token is None):
            problems.append("ingest_url and ingest_token go together")
        if self.ingest_socket is not None and self.ingest_url is None:
            problems.append("ingest_socket needs ingest_url")
        if self.break_min_minutes > self.break_max_minutes:
            problems.append("break_min_minutes must not exceed break_max_minutes")
        if self.shoulder_feed_min_minutes > self.shoulder_feed_max_minutes:
            problems.append("shoulder_feed_min_minutes must not exceed shoulder_feed_max_minutes")
        if self.warmup_min_hours > self.warmup_max_hours:
            problems.append("warmup_min_hours must not exceed warmup_max_hours")
        if self.quiet_start.tzinfo is not None or self.quiet_end.tzinfo is not None:
            problems.append("quiet hours are local times in the pinned zone, without an offset")
        if self.seen_retention_days < self.novelty_days:
            problems.append("seen_retention_days must cover novelty_days")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    # -- loading -------------------------------------------------------------------------------

    @classmethod
    def load(
        cls,
        env_file: Path | str | None = None,
        *,
        secrets_file: Path | None = None,
        platform: str | None = None,
        home: Path | None = None,
    ) -> Settings:
        """Read the environment, then ``env_file`` (if any), then the root's secrets file.

        The root has to be known before the secrets file can be found, so it is resolved first
        (``FBC_ROOT`` from the process, then from ``env_file``, then the platform default) and
        pinned, so a ``FBC_ROOT`` inside the secrets file cannot point somewhere else.
        """
        root = _root_hint(env_file, platform=platform, home=home)
        secrets = secrets_file if secrets_file is not None else root / SECRETS_NAME
        files: list[Path] = [Path(env_file)] if env_file is not None else []
        if secrets.exists():
            check_private_file(secrets)
            files.append(secrets)
        return cls(_env_file=tuple(files) or None, root=root)  # type: ignore[call-arg]

    # -- derived -------------------------------------------------------------------------------

    @property
    def zone(self) -> zoneinfo.ZoneInfo:
        return zoneinfo.ZoneInfo(self.timezone)

    @property
    def local_mode(self) -> bool:
        """Phase 0: no brain; monitored groups from monitored.toml, items to a local sink."""
        return self.ingest_url is None

    @property
    def monitored_file(self) -> Path:
        return self.monitored_path or self.root / "monitored.toml"

    @property
    def raw_retention_seconds(self) -> int:
        """Raw payloads and screenshots: 48 h, or all of phase 0 and a week in observe mode."""
        if self.observe_mode:
            return self.observe_raw_retention_days * DAY
        return self.raw_retention_hours * HOUR

    def missing_for_run(self) -> list[str]:
        """Settings ``run`` needs that tests and ``check-config`` can do without.

        With an uplink, ``FBC_COLLECTOR_ID`` is one of them. It is what the brain's lease is
        granted to, and a default shared by every installation let a forgotten Mac and the
        laptop that replaced it both answer to ``home-1``; the migration gives the laptop a
        name of its own and moves the lease to it (RUNBOOK, section 8).
        """
        missing = []
        if self.alert_bot_token is None:
            missing.append(f"{ENV_PREFIX}ALERT_BOT_TOKEN")
        if self.alert_owner_id is None:
            missing.append(f"{ENV_PREFIX}ALERT_OWNER_ID")
        if not self.local_mode and "collector_id" not in self.model_fields_set:
            missing.append(f"{ENV_PREFIX}COLLECTOR_ID")
        return missing


def _root_hint(env_file: Path | str | None, *, platform: str | None, home: Path | None) -> Path:
    name = f"{ENV_PREFIX}ROOT"
    raw = next((v for k, v in os.environ.items() if k.upper() == name and v), None)
    if raw is None and env_file is not None and Path(env_file).is_file():
        values = {k.upper(): v for k, v in dotenv_values(env_file).items()}
        raw = values.get(name) or None
    if raw is not None:
        return Path(raw).expanduser()
    return default_root(platform, home)


def check_private_file(path: Path) -> None:
    """Refuse a secrets file anyone else could read or that someone else owns."""
    try:
        info = path.stat()
    except OSError as exc:
        msg = f"{path} cannot be read ({exc.strerror})"
        raise ConfigError(msg) from exc
    if not stat.S_ISREG(info.st_mode):
        msg = f"{path} is not a regular file"
        raise ConfigError(msg)
    if info.st_uid != os.getuid():
        msg = f"{path} is owned by another user"
        raise ConfigError(msg)
    if info.st_mode & 0o077:
        msg = f"{path} is readable by other users; run `chmod 600 {path}`"
        raise ConfigError(msg)


def describe_config_error(exc: Exception) -> str:
    """Say which setting is wrong without quoting what was in it.

    pydantic's ValidationError renders ``input_value=...`` with the raw value, so printing the
    exception puts a malformed token straight into the log. The variable name and the reason
    are what a person needs anyway.
    """
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    try:
        problems = errors(include_input=False, include_url=False)
    except Exception:  # noqa: BLE001 - a broken error object must not replace the real error
        return type(exc).__name__
    lines = []
    for problem in problems:
        loc = "__".join(str(part) for part in problem.get("loc", ()))
        name = f"{ENV_PREFIX}{loc.upper()}" if loc else "settings"
        reason = str(problem.get("msg", "is invalid")).removeprefix("Value error, ")
        lines.append(f"{name}: {reason}")
    return "; ".join(lines) or type(exc).__name__
