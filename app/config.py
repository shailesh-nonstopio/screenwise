"""Application configuration, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_DEFAULT_SECRET = "dev-insecure-secret-change-me"
DEFAULT_SQLITE = "sqlite:///./screenwise.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Core -----------------------------------------------------------------
    app_env: Literal["development", "test", "production"] = "development"
    secret_key: str = INSECURE_DEFAULT_SECRET
    base_url: str = ""  # defaults to the Vercel production URL, else http://localhost:8000
    database_url: str = DEFAULT_SQLITE
    auto_migrate: bool | None = None  # default: on when running on Vercel with PostgreSQL
    log_level: str = "INFO"
    display_timezone: str = "Asia/Kolkata"

    # --- Branding / candidate-facing text --------------------------------------
    product_name: str = "Screenwise"
    company_name: str = "Example Company"
    support_email: str = "recruiting@example.com"
    accessibility_email: str = "accessibility@example.com"
    privacy_email: str = "privacy@example.com"
    retention_days: int = 180
    response_days: str = "5 business days"

    # --- AI provider ----------------------------------------------------------
    llm_provider: Literal["mock", "openai", "anthropic"] = "mock"
    llm_timeout_seconds: float = 45.0
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5"
    ai_provider_public_name: str = ""  # shown to candidates in privacy text

    # --- Email ----------------------------------------------------------------
    email_backend: Literal["dryrun", "smtp", "resend"] = "dryrun"
    email_from: str = "Recruiting <no-reply@example.com>"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    resend_api_key: str = ""

    # --- Uploads / storage ----------------------------------------------------
    max_upload_mb: float = 4.0  # Vercel caps request bodies at 4.5 MB
    storage_backend: Literal["database", "local"] = "database"
    local_storage_dir: str = "./var/uploads"

    # --- Interviews -----------------------------------------------------------
    invite_valid_days: int = 5
    max_answer_chars: int = 4000
    max_clarifications_per_question: int = 3
    report_generation: Literal["background", "inline", "deferred"] = "background"

    # --- Security -------------------------------------------------------------
    session_max_age_hours: int = 12
    cron_secret: str = ""
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""
    rate_limit_per_minute: int = 30

    @field_validator("database_url")
    @classmethod
    def _normalise_db_url(cls, v: str) -> str:
        # Hosted Postgres providers often hand out postgres:// URLs; SQLAlchemy wants a driver.
        if v.startswith("postgres://"):
            v = "postgresql+psycopg://" + v[len("postgres://"):]
        elif v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v[len("postgresql://"):]
        return v

    @model_validator(mode="after")
    def _platform_defaults(self) -> "Settings":
        # BASE_URL: fall back to the URL Vercel assigns to the project.
        if not self.base_url:
            host = os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or os.environ.get("VERCEL_URL")
            self.base_url = f"https://{host}" if host else "http://localhost:8000"
        self.base_url = self.base_url.rstrip("/")
        # DATABASE_URL: the Neon / Vercel Postgres integration may only set POSTGRES_URL.
        if self.database_url == DEFAULT_SQLITE:
            for key in ("POSTGRES_URL", "NEON_DATABASE_URL"):
                if os.environ.get(key):
                    self.database_url = self._normalise_db_url(os.environ[key])
                    break
        return self

    @property
    def should_auto_migrate(self) -> bool:
        if self.auto_migrate is not None:
            return self.auto_migrate
        return self.on_vercel and not self.is_sqlite

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def on_vercel(self) -> bool:
        return bool(os.environ.get("VERCEL"))

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_mb * 1024 * 1024)

    @property
    def cookie_secure(self) -> bool:
        return self.is_production or self.base_url.startswith("https://")

    @property
    def llm_configured(self) -> bool:
        if self.llm_provider == "openai":
            return bool(self.openai_api_key)
        if self.llm_provider == "anthropic":
            return bool(self.anthropic_api_key)
        return True

    @property
    def ai_processor_name(self) -> str:
        if self.ai_provider_public_name:
            return self.ai_provider_public_name
        return {"openai": "OpenAI", "anthropic": "Anthropic", "mock": "development mock; nothing is sent to an external AI service"}[
            self.llm_provider
        ]

    def mode_badges(self) -> list[str]:
        badges: list[str] = []
        if self.email_backend == "dryrun":
            badges.append("Email dry-run")
        if self.llm_provider == "mock":
            badges.append("Mock AI")
        elif not self.llm_configured:
            badges.append("AI key missing")
        if not self.is_production:
            badges.insert(0, "Dev mode")
        return badges

    def startup_problems(self) -> list[str]:
        """Configuration errors that must block a production start."""
        problems: list[str] = []
        if self.is_production or self.on_vercel:
            if self.secret_key == INSECURE_DEFAULT_SECRET or len(self.secret_key) < 32:
                problems.append("SECRET_KEY is missing or shorter than 32 characters. Set it to a long random value.")
            if self.is_sqlite and self.on_vercel:
                problems.append("DATABASE_URL is not set. Add your Neon PostgreSQL connection string (the filesystem on Vercel is not persistent).")
            if self.storage_backend == "local" and self.on_vercel:
                problems.append("STORAGE_BACKEND=local is not persistent on Vercel; use 'database'.")
            if not self.base_url.startswith("https://"):
                problems.append(f"BASE_URL must start with https:// (currently “{self.base_url}”).")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings_override: Settings | None = None


def settings() -> Settings:
    """Return active settings. Tests can replace them via ``app.config.settings_override``."""
    return settings_override or get_settings()
