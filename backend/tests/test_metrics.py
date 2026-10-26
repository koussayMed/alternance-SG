import pytest
import requests

from app import metrics
from app.errors import WeatherApiUnavailable
from app.weather_client import CircuitBreaker, OpenMeteoClient


def value(name, **labels):
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


def test_metrics_endpoint_exposes_request_counters(client):
    client.get("/api/cities")
    res = client.get("/metrics")
    assert res.status_code == 200
    assert res.mimetype == "text/plain"
    body = res.get_data(as_text=True)
    assert 'meteo_http_requests_total{method="GET",route="/api/cities",status="200"}' in body
    assert "meteo_http_request_duration_seconds_bucket" in body
    assert 'meteo_app_info{environment="test",version="test-sha"} 1.0' in body


def test_unknown_paths_share_one_label(client):
    # Unknown paths do not create one series per URL (bounded cardinality)
    before = value("meteo_http_requests_total", method="GET", route="inconnue", status="404")
    client.get("/api/nexiste/pas-1")
    client.get("/api/nexiste/pas-2")
    assert value("meteo_http_requests_total", method="GET", route="inconnue", status="404") == before + 2


def test_weather_sources_are_counted(client, lille):
    live = value("meteo_weather_source_total", source="live")
    cache = value("meteo_weather_source_total", source="cache")
    client.get(f"/api/cities/{lille['id']}/weather")  # first time: API call
    client.get(f"/api/cities/{lille['id']}/weather")  # then: cache
    assert value("meteo_weather_source_total", source="live") == live + 1
    assert value("meteo_weather_source_total", source="cache") == cache + 1


def test_suggestion_levels_are_counted(client):
    api = value("meteo_suggestions_total", level="api")
    memory = value("meteo_suggestions_total", level="memory")
    client.get("/api/geocode?q=lil")
    client.get("/api/geocode?q=lil")
    assert value("meteo_suggestions_total", level="api") == api + 1
    assert value("meteo_suggestions_total", level="memory") == memory + 1


def test_circuit_gauge_follows_breaker_state(client, weather_client):
    for _ in range(weather_client.breaker.failure_threshold):
        weather_client.breaker.record_failure()
    assert value("meteo_circuit_open", api="forecast") == 1.0
    assert value("meteo_circuit_open", api="geocoding") == 0.0
    weather_client.breaker.record_success()
    assert value("meteo_circuit_open", api="forecast") == 0.0


def test_db_pool_gauges(client):
    assert value("meteo_db_pool", state="waiting") == 0.0
    assert value("meteo_db_pool", state="size") == 1.0


def test_dependency_calls_by_outcome():
    class Session:
        def get(self, url, params=None, timeout=None):
            raise requests.ConnectTimeout("lent")

    client = OpenMeteoClient(
        "https://f", "https://g", session=Session(), breaker=CircuitBreaker(failure_threshold=1)
    )
    timeout = value("meteo_dependency_calls_total", api="forecast", outcome="ConnectTimeout")
    skipped = value("meteo_dependency_calls_total", api="forecast", outcome="circuit_open")
    for _ in range(2):  # 1st call times out, then the open circuit refuses without trying
        with pytest.raises(WeatherApiUnavailable):
            client.current_weather(48.8, 2.3)
    assert value("meteo_dependency_calls_total", api="forecast", outcome="ConnectTimeout") == timeout + 1
    assert value("meteo_dependency_calls_total", api="forecast", outcome="circuit_open") == skipped + 1


def test_pusher_pushes_its_own_group_and_deletes_it_on_stop():
    calls = []
    pusher = metrics.MetricsPusher(
        "http://pushgateway:9091",
        "replica-a",
        push=lambda url, **kw: calls.append(("push", url, kw)),
        delete=lambda url, **kw: calls.append(("delete", url, kw)),
    )
    assert pusher.push_once() is True
    pusher.stop()
    pusher.stop()  # idempotent (atexit may call stop again)
    assert pusher.push_once() is False  # no push after stop: the group does not come back
    assert [c[0] for c in calls] == ["push", "delete"]
    kind, url, kw = calls[0]
    assert url == "http://pushgateway:9091" and kw["job"] == "meteo-backend"
    assert kw["grouping_key"]["replica"] == "replica-a" and kw["grouping_key"]["worker"].isdigit()
    assert kw["registry"] is metrics.REGISTRY


def test_pusher_survives_gateway_outage():
    attempts = []

    def failing(url, **kw):
        attempts.append(url)
        raise OSError("connexion refusée")

    pusher = metrics.MetricsPusher("http://pushgateway:9091", "replica-a", push=failing, delete=failing)
    assert pusher.push_once() is False
    assert pusher.push_once() is False  # the app keeps going, with a single warning log
    pusher.stop()  # the delete fails too, without raising
    assert len(attempts) == 3


def test_no_pusher_without_gateway(app):
    assert "metrics_pusher" not in app.extensions
