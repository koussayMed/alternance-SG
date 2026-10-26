"""Settings read from environment variables (12-factor). Secrets have no default value."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # fail fast at startup
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    app_name: str = "meteo-api"
    app_version: str = "dev"
    environment: str = "local"
    log_level: str = "INFO"
    database_url: str | None = None
    # DB connections per replica = gunicorn_workers x db_pool_max_size (see README: connection budget)
    gunicorn_workers: int = 2
    db_pool_min_size: int = 1
    db_pool_max_size: int = 2
    forecast_api_url: str = "https://api.open-meteo.com/v1/forecast"
    geocoding_api_url: str = "https://geocoding-api.open-meteo.com/v1/search"
    http_timeout_s: float = 2.5
    weather_cache_ttl_s: int = 600
    breaker_failure_threshold: int = 3
    breaker_reset_after_s: int = 30
    max_cities: int = 20  # per visitor
    refresh_lease_s: float = 10.0  # longer than one weather API call (timeout + retries)
    refresh_budget_s: float = 4.0  # max time spent refreshing the cache in one /api/weather request
    metrics_pushgateway_url: str | None = None  # e.g. http://pushgateway:9091; empty = no push
    metrics_push_interval_s: float = 15.0

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            app_name=os.environ.get("APP_NAME", cls.app_name),
            # Git SHA set at build time; the smoke tests check it
            app_version=os.environ.get("APP_VERSION", cls.app_version),
            environment=os.environ.get("APP_ENV", cls.environment),
            log_level=os.environ.get("LOG_LEVEL", cls.log_level).upper(),
            database_url=os.environ.get("DATABASE_URL") or None,
            gunicorn_workers=_env_int("GUNICORN_WORKERS", cls.gunicorn_workers),
            db_pool_min_size=_env_int("DB_POOL_MIN_SIZE", cls.db_pool_min_size),
            db_pool_max_size=_env_int("DB_POOL_MAX_SIZE", cls.db_pool_max_size),
            forecast_api_url=os.environ.get("FORECAST_API_URL", cls.forecast_api_url),
            geocoding_api_url=os.environ.get("GEOCODING_API_URL", cls.geocoding_api_url),
            http_timeout_s=_env_float("HTTP_TIMEOUT_S", cls.http_timeout_s),
            weather_cache_ttl_s=_env_int("WEATHER_CACHE_TTL_S", cls.weather_cache_ttl_s),
            breaker_failure_threshold=_env_int("BREAKER_FAILURE_THRESHOLD", cls.breaker_failure_threshold),
            breaker_reset_after_s=_env_int("BREAKER_RESET_AFTER_S", cls.breaker_reset_after_s),
            max_cities=_env_int("MAX_CITIES", cls.max_cities),
            refresh_lease_s=_env_float("REFRESH_LEASE_S", cls.refresh_lease_s),
            refresh_budget_s=_env_float("REFRESH_BUDGET_S", cls.refresh_budget_s),
            metrics_pushgateway_url=os.environ.get("METRICS_PUSHGATEWAY_URL") or None,
            metrics_push_interval_s=_env_float("METRICS_PUSH_INTERVAL_S", cls.metrics_push_interval_s),
        )

    @property
    def db_connections_per_replica(self) -> int:
        return self.gunicorn_workers * self.db_pool_max_size
