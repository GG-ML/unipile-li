"""Centralised configuration loaded from environment / .env."""

import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    """Application settings singleton."""

    # --- Unipile ---
    UNIPILE_API_KEY: str = os.getenv("UNIPILE_API_KEY", "")
    UNIPILE_BASE_URL: str = os.getenv("UNIPILE_BASE_URL", "https://api.unipile.com").rstrip("/")
    UNIPILE_WEBHOOK_SECRET: str = os.getenv("UNIPILE_WEBHOOK_SECRET", "")
    # Optional outbound proxy for reaching Unipile custom ports through egress filtering.
    UNIPILE_PROXY_URL: str = os.getenv("UNIPILE_PROXY_URL", "")

    # --- Database ---
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")
    DB_SSLMODE: str = os.getenv("DB_SSLMODE", "require")

    # --- Redis ---
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # --- Scheduler tuning ---
    MAX_CONCURRENT_WORKERS: int = _int("MAX_CONCURRENT_WORKERS", 20)
    EXECUTOR_TICK_SECONDS: int = _int("EXECUTOR_TICK_SECONDS", 30)
    PLANNER_TICK_SECONDS: int = _int("PLANNER_TICK_SECONDS", 900)
    POLLER_TICK_SECONDS: int = _int("POLLER_TICK_SECONDS", 600)
    DEFAULT_DAILY_CONNECT_LIMIT: int = _int("DEFAULT_DAILY_CONNECT_LIMIT", 25)
    DEFAULT_MIN_DELAY_SECONDS: int = _int("DEFAULT_MIN_DELAY_SECONDS", 180)
    DEFAULT_MAX_DELAY_SECONDS: int = _int("DEFAULT_MAX_DELAY_SECONDS", 600)
    ACCEPTED_CHECKS_PER_DAY: int = _int("ACCEPTED_CHECKS_PER_DAY", 2)

    APP_DEBUG: bool = _bool("APP_DEBUG", False)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
