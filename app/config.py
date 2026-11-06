"""Application configuration.

All configuration is read from environment variables (optionally via a local
``.env`` file that is never committed). Secrets use ``SecretStr`` so they are
not accidentally rendered in logs or reprs.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
ExperienceLevel = Literal["beginner", "intermediate", "advanced", "elite"]


def _validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown IANA timezone: {value!r}") from exc
    return value


class Settings(BaseSettings):
    """Runtime settings for the service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application -------------------------------------------------------
    app_name: str = "ai-cycling-coach"
    app_env: Literal["development", "test", "production"] = "development"
    app_timezone: str = "Asia/Kolkata"

    # --- Logging -----------------------------------------------------------
    log_level: LogLevel = "INFO"
    log_format: Literal["json", "console"] = "json"

    # --- API ---------------------------------------------------------------
    api_host: str = "0.0.0.0"  # noqa: S104 - container bind; host exposure is set in compose
    api_port: int = Field(default=8010, ge=1, le=65535)

    # --- Web login (app.auth) ----------------------------------------------------
    # Signs session cookies; at least 32 characters. Unset: a random key per
    # process (sessions end on every restart; a warning is logged).
    # Generate: python -c "import secrets; print(secrets.token_urlsafe(48))"
    auth_secret_key: SecretStr | None = None
    auth_session_hours: int = Field(default=168, ge=1, le=24 * 90)
    # Cookie "Secure" flag. None: set when the request arrived over HTTPS
    # (X-Forwarded-Proto from a trusted proxy counts).
    auth_cookie_secure: bool | None = None

    # --- PostgreSQL ----------------------------------------------------------
    postgres_host: str = "localhost"
    postgres_port: int = Field(default=5434, ge=1, le=65535)
    postgres_db: str = "cycling_coach"
    postgres_user: str = "coach"
    postgres_password: SecretStr  # required, no default
    database_url: SecretStr | None = Field(
        default=None,
        description="Optional full SQLAlchemy URL; overrides the POSTGRES_* components.",
    )
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=5, ge=0)
    db_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    db_connect_timeout_seconds: int = Field(default=5, ge=1)
    db_echo: bool = False
    health_db_timeout_seconds: float = Field(default=3.0, gt=0)

    # --- Intervals.icu -------------------------------------------------------
    # Auth: HTTP Basic, username literally "API_KEY", password = personal API key
    # (Intervals.icu /settings -> Developer Settings). See docs/INTERVALS_ICU.md.
    intervals_api_key: SecretStr | None = None
    intervals_athlete_id: str | None = Field(
        default=None, description="Intervals.icu athlete id, e.g. 'i12345' (from /settings)."
    )
    intervals_base_url: str = "https://intervals.icu"
    intervals_timeout_seconds: float = Field(default=30.0, gt=0)
    intervals_max_retries: int = Field(default=4, ge=0, le=10)
    intervals_backoff_base_seconds: float = Field(default=1.0, ge=0)
    intervals_backoff_max_seconds: float = Field(default=60.0, gt=0)
    # First/--full sync fetches this many days of history (spec: at least 30).
    # 180 because CTL needs 96 covered days to be authoritative (Phase 3).
    intervals_history_days: int = Field(default=180, ge=30, le=3650)
    # Incremental syncs re-fetch this many days before the previous window end so
    # late uploads (wearable sync delays) and edits (RPE, feel) are picked up.
    intervals_sync_overlap_days: int = Field(default=7, ge=1, le=60)
    # Calendar events are fetched up to this many days into the future.
    intervals_calendar_future_days: int = Field(default=28, ge=0, le=365)
    # Phase 5: publish validated workouts to the Intervals.icu calendar
    # automatically after planning. False = plan only (publish manually).
    intervals_publish_enabled: bool = True

    # --- Multi-user (Phase 10) ------------------------------------------------
    # Fernet key that encrypts each user's Intervals.icu API key in the
    # database. Losing it means every user has to reconnect Intervals.icu.
    # Generate: python -c "from cryptography.fernet import Fernet;
    #   print(Fernet.generate_key().decode())"
    credentials_encryption_key: SecretStr | None = None

    # --- Adaptive coaching / scheduler (Phase 7) --------------------------------
    # Poll Intervals.icu for new activities every N minutes (sync + evaluation).
    scheduler_poll_minutes: int = Field(default=30, ge=5, le=1440)
    # Local time (athlete timezone) to plan and publish the next day.
    scheduler_plan_time: str = Field(default="20:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    # Evaluate rides of the last N days that have no evaluation yet.
    activity_lookback_days: int = Field(default=14, ge=1, le=90)
    # Extend an open-ended plan so it always covers this many days ahead.
    plan_horizon_days: int = Field(default=28, ge=7, le=180)
    # Athletes processed at the same time by one scheduler job (each run may
    # call the LLM and Intervals.icu).
    scheduler_max_parallel: int = Field(default=2, ge=1, le=16)

    # --- OpenAI / LangGraph (Phase 4) ---------------------------------------------
    openai_api_key: SecretStr | None = None
    # Model id is configuration, never hardcoded elsewhere. Changing it is safe:
    # every decision records the model the API actually reports.
    openai_model: str = "gpt-6.1-sol"
    openai_base_url: str | None = None
    openai_timeout_seconds: float = Field(default=300.0, gt=0)
    # Attempts per LLM call for transient errors (timeouts, 429, 5xx, network).
    openai_max_attempts: int = Field(default=3, ge=1, le=10)
    openai_backoff_base_seconds: float = Field(default=2.0, ge=0)
    openai_backoff_max_seconds: float = Field(default=60.0, gt=0)
    # Times a node may ask the model to correct an output that failed
    # schema or deterministic validation before the safe fallback is used.
    llm_max_corrections: int = Field(default=2, ge=0, le=5)

    # --- Notifications (Phase 9) ----------------------------------------------------
    # Email alerts from the scheduler (failures, blocked publishing, fallback
    # plans) and a daily summary after planning. See docs/OPERATIONS.md.
    notify_email_enabled: bool = False
    # Comma-separated recipients.
    notify_email_to: str | None = None
    # Sender; defaults to SMTP_USERNAME.
    notify_email_from: str | None = None
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    # starttls (port 587) | ssl (port 465) | none (local relay only)
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_timeout_seconds: float = Field(default=20.0, gt=0)
    # One email after each daily plan saying what was published. Without it,
    # only problems are sent.
    notify_daily_summary: bool = True
    # An unresolved problem is emailed again after this many hours.
    notify_repeat_hours: int = Field(default=12, ge=1, le=168)
    # Poll problems (Intervals.icu down, sync errors) are emailed only after
    # this many failed polls in a row, so a single network blip stays quiet.
    notify_poll_failure_threshold: int = Field(default=2, ge=1, le=48)

    @field_validator("auth_secret_key", mode="before")
    @classmethod
    def _check_auth_secret(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, str) and len(value.strip()) < 32:
            raise ValueError("AUTH_SECRET_KEY must be at least 32 characters")
        return value.strip() if isinstance(value, str) else value

    @field_validator("openai_api_key", mode="before")
    @classmethod
    def _blank_openai_key_is_none(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("credentials_encryption_key", mode="before")
    @classmethod
    def _blank_credentials_key_is_none(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("openai_model")
    @classmethod
    def _check_model(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("OPENAI_MODEL must not be empty")
        return value

    def langgraph_conninfo(self) -> str:
        """libpq URL for LangGraph's checkpointer, confined to schema ``langgraph``."""
        base = self.sqlalchemy_url().replace("postgresql+psycopg://", "postgresql://", 1)
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}options=-c%20search_path%3Dlanggraph"

    @field_validator("intervals_api_key", mode="before")
    @classmethod
    def _blank_key_is_none(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("intervals_athlete_id")
    @classmethod
    def _check_athlete_id(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not value.replace("_", "").isalnum():
            raise ValueError("INTERVALS_ATHLETE_ID must be alphanumeric, e.g. 'i12345'")
        return value

    @field_validator("intervals_base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        value = value.rstrip("/")
        if not value.startswith(("https://", "http://")):
            raise ValueError("INTERVALS_BASE_URL must be an http(s) URL")
        return value

    @property
    def intervals_configured(self) -> bool:
        return self.intervals_api_key is not None and self.intervals_athlete_id is not None

    @property
    def notify_recipients(self) -> list[str]:
        return [a.strip() for a in (self.notify_email_to or "").split(",") if a.strip()]

    def notify_email_problems(self) -> list[str]:
        """Missing email settings; empty when email alerts can be sent."""
        if not self.notify_email_enabled:
            return ["NOTIFY_EMAIL_ENABLED is false"]
        missing = []
        if not self.smtp_host:
            missing.append("SMTP_HOST is not set")
        if not self.notify_recipients:
            missing.append("NOTIFY_EMAIL_TO is not set")
        if not (self.notify_email_from or self.smtp_username):
            missing.append("NOTIFY_EMAIL_FROM (or SMTP_USERNAME) is not set")
        if self.smtp_username and self.smtp_password is None:
            missing.append("SMTP_PASSWORD is not set")
        return missing

    @field_validator("app_timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        return _validate_timezone(value)

    def sqlalchemy_url(self, database: str | None = None) -> str:
        """Return the SQLAlchemy URL (psycopg 3 driver, usable sync and async)."""
        if self.database_url is not None and database is None:
            return self.database_url.get_secret_value()
        password = quote_plus(self.postgres_password.get_secret_value())
        user = quote_plus(self.postgres_user)
        db = database or self.postgres_db
        return (
            f"postgresql+psycopg://{user}:{password}@{self.postgres_host}:{self.postgres_port}/{db}"
        )


class AthleteSeedSettings(BaseSettings):
    """Initial athlete profile values used only by ``scripts/seed_athlete_profile.py``.

    These are seed values, not truths: once stored, the athlete profile in the
    database is authoritative and may be updated by the athlete.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_prefix="ATHLETE_"
    )

    slug: str = "primary"
    display_name: str = "Athlete"
    ftp_watts: int = Field(default=175, gt=0, le=600)
    weight_kg: float = Field(default=86.0, gt=20, le=250)
    height_cm: float = Field(default=172.0, gt=100, le=250)
    age_years: int = Field(default=46, gt=10, le=100)
    experience_level: ExperienceLevel = "intermediate"
    primary_objective: str = "1200 km brevet / ultra-endurance cycling"
    secondary_objective: str = "FTP improvement"
    timezone: str = "Asia/Kolkata"
    indoor_platform: str = "icTrainer"
    wearable: str = "COROS"

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        return _validate_timezone(value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings (validated once per process)."""
    return Settings()  # values (incl. required POSTGRES_PASSWORD) come from the environment
