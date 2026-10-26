from __future__ import annotations

import logging
import re
import time
import uuid

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from . import metrics
from .config import Settings
from .errors import ApiError
from .instance import instance_info
from .logging_setup import configure_logging
from .routes import bp
from .weather_client import CircuitBreaker, OpenMeteoClient
from .weather_service import PlaceSearch, WeatherService

log = logging.getLogger("app")

# Incoming X-Request-ID (set by nginx) is reused only if short and safe
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_QUIET_PATHS = {"/healthz", "/readyz", "/metrics"}  # polled every few seconds


def create_app(settings: Settings | None = None, repository=None, weather_client=None) -> Flask:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)

    if repository is None:
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL is required")
        from .repository import PostgresRepository  # lazy import: unit tests do not need psycopg

        repository = PostgresRepository(
            settings.database_url, settings.db_pool_min_size, settings.db_pool_max_size
        )
        # max_replicas x this value must stay below the database connection limit
        log.info(
            "database pool configured",
            extra={
                "pool_max_per_worker": settings.db_pool_max_size,
                "gunicorn_workers": settings.gunicorn_workers,
                "max_connections_per_replica": settings.db_connections_per_replica,
            },
        )

    if weather_client is None:
        weather_client = OpenMeteoClient(
            forecast_url=settings.forecast_api_url,
            geocoding_url=settings.geocoding_api_url,
            timeout_s=settings.http_timeout_s,
            breaker=CircuitBreaker(
                settings.breaker_failure_threshold, settings.breaker_reset_after_s, name="open-meteo-forecast"
            ),
            geocoding_breaker=CircuitBreaker(
                settings.breaker_failure_threshold,
                settings.breaker_reset_after_s,
                name="open-meteo-geocoding",
            ),
        )

    app = Flask(__name__, static_folder=None)  # API only: nginx serves the static files
    app.config["SETTINGS"] = settings
    app.json.sort_keys = False
    app.json.ensure_ascii = False
    app.extensions["repository"] = repository
    app.extensions["weather_client"] = weather_client
    app.extensions["weather_service"] = WeatherService(
        repository,
        weather_client,
        ttl_s=settings.weather_cache_ttl_s,
        lease_s=settings.refresh_lease_s,
    )
    app.extensions["place_search"] = PlaceSearch(weather_client, repository)

    # Prometheus metrics, pushed to the Pushgateway when configured
    metrics.bind_runtime(weather_client, repository, settings.app_version, settings.environment)
    if settings.metrics_pushgateway_url:
        pusher = metrics.MetricsPusher(
            settings.metrics_pushgateway_url, instance_info()["replica"], settings.metrics_push_interval_s
        )
        pusher.start()
        app.extensions["metrics_pusher"] = pusher

    # Behind nginx and the Container Apps ingress: trust one proxy hop for scheme and client IP
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    app.register_blueprint(bp)
    _register_request_hooks(app)
    _register_error_handlers(app)

    log.info(
        "application started",
        extra={"version": settings.app_version, "environment": settings.environment},
    )
    return app


def _register_request_hooks(app: Flask) -> None:
    @app.before_request
    def start_request():
        incoming = request.headers.get("X-Request-ID", "")
        g.request_id = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        g.started = time.perf_counter()

    @app.after_request
    def finish_request(response):
        response.headers["X-Request-ID"] = g.get("request_id", "")
        response.headers.setdefault("Cache-Control", "no-store")

        duration_s = time.perf_counter() - g.get("started", time.perf_counter())
        duration_ms = round(duration_s * 1000, 2)
        metrics.observe_request(
            request.method,
            request.url_rule.rule if request.url_rule else None,
            response.status_code,
            duration_s,
        )
        log.log(
            logging.DEBUG if request.path in _QUIET_PATHS else logging.INFO,
            "request completed",
            extra={
                "method": request.method,
                "path": request.path,
                "route": request.url_rule.rule if request.url_rule else None,  # aggregate per route
                "status": response.status_code,
                "duration_ms": duration_ms,
                "client_ip": request.remote_addr,
            },
        )
        return response


def _error_body(code: str, message: str) -> dict:
    return {"error": code, "message": message, "request_id": g.get("request_id")}


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def api_error(err: ApiError):
        if err.status >= 500:
            log.warning("dependency error", extra={"error": err.code, "detail": err.message})
        return jsonify(_error_body(err.code, err.message)), err.status

    @app.errorhandler(HTTPException)
    def http_error(err: HTTPException):
        code = err.name.lower().replace(" ", "_")
        return jsonify(_error_body(code, err.description or err.name)), err.code

    @app.errorhandler(Exception)
    def unhandled(err: Exception):
        log.exception("unhandled error")
        return jsonify(_error_body("internal_error", "Erreur interne")), 500
