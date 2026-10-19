"""Load configuration without requiring LLM credentials for database setup."""

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Literal

from pydantic import Field, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Database settings; environment variables override the project .env file."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: PostgresDsn


class OpenFDASettings(BaseSettings):
    """Source configuration, usable independently of database credentials."""

    model_config = Settings.model_config.copy()

    openfda_base_url: Literal["https://api.fda.gov/drug/enforcement.json"] = (
        "https://api.fda.gov/drug/enforcement.json"
    )
    openfda_api_key: SecretStr = SecretStr("")
    openfda_timeout_seconds: float = Field(default=15.0, gt=0)
    openfda_max_retries: int = Field(default=3, ge=0, le=8)
    openfda_page_size: int = Field(default=100, ge=1, le=1000)


class GroqSettings(BaseSettings):
    """Optional extraction settings; missing credentials disable extraction."""

    model_config = Settings.model_config.copy()

    groq_extraction_enabled: bool = False
    groq_api_key: SecretStr = SecretStr("")
    groq_model: str = ""
    groq_timeout_seconds: float = Field(default=15.0, gt=0, allow_inf_nan=False)
    groq_max_retries: int = Field(default=2, ge=0, le=5)
    groq_min_confidence: float = Field(default=0.9, gt=0, le=1, allow_inf_nan=False)


class SchedulerSettings(BaseSettings):
    """Daily local-time scheduling; destinations remain subject to human approval."""

    model_config = Settings.model_config.copy()
    scheduler_enabled: bool = True
    scheduler_hour: int = Field(default=9, ge=0, le=23)
    scheduler_minute: int = Field(default=0, ge=0, le=59)
    scheduler_timezone: str = "Asia/Kolkata"
    scheduler_destination: str = Field(default="mock://synthetic-household", min_length=1, max_length=320)

    @field_validator("scheduler_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value


@lru_cache
def get_settings() -> Settings:
    """Read settings lazily so importing models needs no configured database."""
    return Settings()

