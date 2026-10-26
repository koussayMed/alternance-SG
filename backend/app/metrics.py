"""Collects backend metrics like request times, errors, and API calls to monitor app performance.
Metrics are sent to Prometheus Pushgateway for monitoring and debugging.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
from collections.abc import Callable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    ProcessCollector,
    delete_from_gateway,
    disable_created_metrics,
    generate_latest,
    push_to_gateway,
)

log = logging.getLogger(__name__)

JOB = "meteo-backend"

disable_created_metrics()

REGISTRY = CollectorRegistry(auto_describe=True)
ProcessCollector(registry=REGISTRY)  # process CPU, memory, file descriptors

HTTP_REQUESTS = Counter(
    "meteo_http_requests",
    "HTTP requests handled by the backend",
    ["method", "route", "status"],
    registry=REGISTRY,
)
HTTP_DURATION = Histogram(
    "meteo_http_request_duration_seconds",
    "Backend HTTP request duration",
    ["route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)
WEATHER_SOURCE = Counter(
    "meteo_weather_source",
    "Source of each weather served: cache, live, refreshing, stale, unavailable",
    ["source"],
    registry=REGISTRY,
)
SUGGESTIONS = Counter(
    "meteo_suggestions",
    "Suggestions served per cache level: memory, shared (database), api, stale, unavailable",
    ["level"],
    registry=REGISTRY,
)
DEPENDENCY_CALLS = Counter(
    "meteo_dependency_calls",
    "Open-Meteo calls per API (forecast, geocoding) and outcome (success, circuit_open, error type)",
    ["api", "outcome"],
    registry=REGISTRY,
)
DEPENDENCY_DURATION = Histogram(
    "meteo_dependency_duration_seconds",
    "Duration of the Open-Meteo calls actually sent",
    ["api"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=REGISTRY,
)
CIRCUIT_OPEN = Gauge(
    "meteo_circuit_open",
    "1 if the circuit breaker of this API is open (calls refused without trying)",
    ["api"],
    registry=REGISTRY,
)
DB_POOL = Gauge(
    "meteo_db_pool",
    "PostgreSQL connection pool of the process: size, available, waiting",
    ["state"],
    registry=REGISTRY,
)
APP_INFO = Gauge(
    "meteo_app_info",
    "Deployed version (value is always 1)",
    ["version", "environment"],
    registry=REGISTRY,
)


def observe_request(method: str, route: str | None, status: int, duration_s: float) -> None:
    # Route pattern (/api/cities/<int:city_id>), never the raw path: bounded series count
    label = route or "inconnue"
    HTTP_REQUESTS.labels(method, label, str(status)).inc()
    HTTP_DURATION.labels(label).observe(duration_s)


def exposition() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def _safe(read: Callable[[], float]) -> Callable[[], float]:
    def wrapped() -> float:
        try:
            return float(read())
        except Exception:  # noqa: BLE001 - a metric must never break the export
            return float("nan")

    return wrapped


def bind_runtime(weather_client, repository, version: str, environment: str) -> None:
    """Gauges computed at export time from the live state of the app objects."""
    for api, attr in (("forecast", "breaker"), ("geocoding", "geocoding_breaker")):
        breaker = getattr(weather_client, attr, None)
        if breaker is not None:
            CIRCUIT_OPEN.labels(api).set_function(_safe(lambda b=breaker: b.state == "open"))
    for state, key in (
        ("size", "pool_size"),
        ("available", "pool_available"),
        ("waiting", "requests_waiting"),
    ):
        DB_POOL.labels(state).set_function(_safe(lambda k=key: repository.pool_stats()[k]))
    APP_INFO.labels(version, environment).set(1)


class MetricsPusher:
    """Pushes the registry to the Pushgateway at a fixed interval, in a group owned by this process."""

    def __init__(
        self,
        gateway_url: str,
        replica: str,
        interval_s: float = 15.0,
        push: Callable = push_to_gateway,
        delete: Callable = delete_from_gateway,
    ):
        self.gateway_url = gateway_url
        self.interval_s = interval_s
        self.grouping_key = {"replica": replica, "worker": str(os.getpid())}
        self._push = push
        self._delete = delete
        self._stop = threading.Event()
        self._lock = threading.Lock()  # the final delete waits for any push in progress
        self._failing = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="metrics-pusher", daemon=True)
        self._thread.start()
        atexit.register(self.stop)  # clean gunicorn shutdown removes the group

    def _run(self) -> None:
        self.push_once()
        while not self._stop.wait(self.interval_s):
            self.push_once()

    def push_once(self) -> bool:
        with self._lock:
            if self._stop.is_set():  # no push after stop, so the group does not come back
                return False
            try:
                self._push(
                    self.gateway_url, job=JOB, registry=REGISTRY, grouping_key=self.grouping_key, timeout=5
                )
            except Exception as exc:  # noqa: BLE001 - observability must never break the app
                if not self._failing:  # one log per outage, not one every 15 s
                    log.warning("metrics push failed", extra={"error": str(exc), "gateway": self.gateway_url})
                self._failing = True
                return False
            if self._failing:
                log.info("metrics push recovered", extra={"gateway": self.gateway_url})
            self._failing = False
            return True

    def stop(self) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            self._stop.set()
            try:
                self._delete(self.gateway_url, job=JOB, grouping_key=self.grouping_key, timeout=3)
            except Exception:  # noqa: BLE001, S110 - worst case the group stays until a Pushgateway restart
                pass
