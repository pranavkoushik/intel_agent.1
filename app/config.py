"""Typed application settings backed by environment variables / .env file."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # External API credentials
    slack_webhook_url: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash-lite"

    # Google Sheets — service account JSON blob (preferred for Vercel) with
    # local credentials.json as a fallback handled in services.py.
    google_service_account_json: str = ""
    google_sheet_name: str = "Joveo Intel Logs"
    google_worksheet_name: str = "Sheet1"

    news_lookback_days: int = 7

    # Per-language Google News RSS query result cap. The fetch issues one
    # sub-query per supported language (EN/DE/FR/ES/IT/NL/PL), so total items
    # per publisher per run is roughly this × 7 before dedup. Set to 0 to
    # disable Google News RSS entirely.
    google_news_max_results: int = 5

    # Slack delivery (exponential backoff: 2, 4, 8, ... seconds between retries)
    slack_retries: int = 3
    slack_timeout: int = 20

    # Title-similarity dedup threshold (0.0–1.0). Higher = stricter (fewer drops).
    title_similarity_threshold: float = 0.75

    # Optional bearer token guarding /api/cron
    cron_secret: str = ""

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()