"""The brain's settings: ddiver-core's base, plus the ingest endpoint and the collector's day.

Everything the core reads (``BOT_TOKEN``, ``API_ID``/``API_HASH`` for the bot's MTProto
connection, ``OWNER_ID``, ``PING_URL``, logging, metrics, the digest and regex limits) keeps its
name and meaning, so an operator who knows ddiver's ``.env`` knows most of this one. ddiver's
own listener settings (the user session, catch-up, its own messages) are ddiver's subclass, not
the base, so they do not exist here: a stray ``CATCHUP_HOURS`` is ignored like any unknown name.
What is new is the ingest server the collector talks to through the tunnel, the hours the
brain calls "daytime" when it decides whether a silent collector or a silent group is worth
waking the owner for, and the AI rule's model (no ``OPENROUTER_API_KEY``: no AI rule).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator

from ddiver_core.config import Settings
from ddiver_core.timeutil import validate_tz
from ddiver_fb.judge import DEFAULT_MODEL

MIN_TOKEN_LENGTH = 32
"""A bearer token shorter than this is a password somebody typed, not a generated secret."""


class FbSettings(Settings):
    ingest_host: str = Field(
        default="127.0.0.1",
        description="0.0.0.0 inside the container; compose publishes 127.0.0.1:8781 only",
    )
    ingest_port: int = Field(default=8781, ge=0, le=65535)
    ingest_token: SecretStr = Field(description="the collector's bearer token (openssl rand)")

    collector_tz: str | None = Field(
        default=None,
        description="IANA zone of the collector's day; unset: the owner's own timezone",
    )
    day_start_hour: int = Field(default=8, ge=0, le=23)
    day_end_hour: int = Field(default=23, ge=1, le=24)
    offline_after_minutes: int = Field(default=20, ge=5, le=240)
    items_retention_days: int = Field(
        default=14,
        ge=2,
        le=60,
        description="post ids and times kept for dedupe (48 h at least) and the silence alert",
    )
    silence_factor: float = Field(default=3.0, ge=1.5, le=20)
    silence_floor_hours: float = Field(default=2.0, ge=0.5, le=48)

    openrouter_api_key: SecretStr | None = Field(
        default=None, description="the AI rule's OpenRouter key; unset: no AI rule"
    )
    ai_model: str = Field(default=DEFAULT_MODEL, min_length=3, max_length=128)
    ai_daily_calls: int = Field(
        default=1000, ge=0, le=100_000, description="model calls a day at most (a cost cap)"
    )

    @field_validator("ingest_token")
    @classmethod
    def _token_shape(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if len(raw) < MIN_TOKEN_LENGTH or any(ch.isspace() for ch in raw):
            msg = f"INGEST_TOKEN must be at least {MIN_TOKEN_LENGTH} characters with no spaces"
            raise ValueError(msg)
        if not raw.isascii() or not raw.isprintable():
            # It travels in an HTTP header, which is latin-1 at best.
            msg = "INGEST_TOKEN must be printable ASCII"
            raise ValueError(msg)
        return value

    @field_validator("collector_tz")
    @classmethod
    def _zone(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        zone = validate_tz(value)
        if zone is None:
            msg = "COLLECTOR_TZ must be an IANA zone name such as Europe/Sofia"
            raise ValueError(msg)
        return zone

    @field_validator("openrouter_api_key")
    @classmethod
    def _key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value().strip():
            return None
        raw = value.get_secret_value().strip()
        if not raw.isascii() or not raw.isprintable() or any(ch.isspace() for ch in raw):
            msg = "OPENROUTER_API_KEY must be printable ASCII with no spaces"
            raise ValueError(msg)
        return SecretStr(raw)

    @model_validator(mode="after")
    def _day(self) -> FbSettings:
        if self.day_start_hour >= self.day_end_hour:
            msg = "DAY_START_HOUR must be earlier than DAY_END_HOUR"
            raise ValueError(msg)
        return self

    @property
    def db_path(self) -> Path:
        return self.data_dir / "fb.sqlite"

    @property
    def health_path(self) -> Path:
        return self.data_dir / "health.json"
