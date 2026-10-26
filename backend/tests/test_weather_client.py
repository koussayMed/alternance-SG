import pytest
import requests

from app.config import Settings
from app.errors import WeatherApiUnavailable
from app.models import describe_weather
from app.weather_client import CircuitBreaker, OpenMeteoClient


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def make_client(*responses, breaker=None):
    session = FakeSession(*responses)
    client = OpenMeteoClient(
        "https://forecast",
        "https://geo",
        timeout_s=2.5,
        session=session,
        breaker=breaker or CircuitBreaker(failure_threshold=2, reset_after_s=30),
    )
    return client, session


FORECAST = {
    "current": {
        "time": "2026-10-21T12:45",
        "temperature_2m": 13.4,
        "wind_speed_10m": 21.0,
        "weather_code": 61,
    }
}
GEOCODING = {
    "results": [
        {"id": 2998324, "name": "Lille", "country": "France", "latitude": 50.63297, "longitude": 3.05858}
    ]
}


def test_current_weather_parses_payload():
    client, session = make_client(FakeResponse(FORECAST))
    weather = client.current_weather(50.6, 3.06)
    assert weather.temperature_c == 13.4
    assert weather.weather_code == 61
    assert weather.observed_at.isoformat() == "2026-10-21T12:45:00+00:00"
    assert session.calls[0]["timeout"] == (1.5, 2.5)
    assert session.calls[0]["params"]["current"] == "temperature_2m,wind_speed_10m,weather_code"


def test_geocode_found_and_not_found():
    client, _ = make_client(FakeResponse(GEOCODING), FakeResponse({"generationtime_ms": 0.5}))
    geo = client.geocode("Lille")
    assert (geo.external_id, geo.name, geo.country) == (2998324, "Lille", "France")
    assert client.geocode("Atlantide") is None


@pytest.mark.parametrize(
    "response",
    [
        requests.ConnectTimeout("timeout"),
        requests.ConnectionError("dns"),
        FakeResponse({}, status=503),
        FakeResponse(ValueError("not json")),
        FakeResponse({"current": {"time": "pas une date"}}),
    ],
)
def test_failures_become_weather_api_unavailable(response):
    client, _ = make_client(response)
    with pytest.raises(WeatherApiUnavailable):
        client.current_weather(0, 0)


def test_circuit_opens_after_threshold_and_skips_calls():
    client, session = make_client(requests.ConnectTimeout("t"), requests.ConnectTimeout("t"))
    for _ in range(2):
        with pytest.raises(WeatherApiUnavailable):
            client.current_weather(0, 0)
    assert client.breaker.state == "open"

    with pytest.raises(WeatherApiUnavailable, match="circuit ouvert"):
        client.current_weather(0, 0)
    assert len(session.calls) == 2  # the 3rd call never reached the network


def test_circuit_half_open_then_closes_on_success():
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=1, reset_after_s=30, clock=lambda: now[0])
    client, _ = make_client(requests.ConnectTimeout("t"), FakeResponse(FORECAST), breaker=breaker)

    with pytest.raises(WeatherApiUnavailable):
        client.current_weather(0, 0)
    assert breaker.state == "open"

    now[0] = 31.0
    assert breaker.state == "half_open"
    client.current_weather(0, 0)
    assert breaker.state == "closed"


def test_circuit_reopens_when_half_open_trial_fails():
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=1, reset_after_s=30, clock=lambda: now[0])
    breaker.record_failure()
    now[0] = 31.0
    assert breaker.state == "half_open"
    breaker.record_failure()
    assert breaker.state == "open"


def test_describe_weather():
    assert describe_weather(0) == "Ciel dégagé"
    assert describe_weather(95) == "Orage"
    assert describe_weather(None) == "Inconnu"
    assert describe_weather(1234) == "Code météo 1234"


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "abc1234")
    monkeypatch.setenv("WEATHER_CACHE_TTL_S", "60")
    monkeypatch.setenv("GUNICORN_WORKERS", "3")
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "4")
    s = Settings.from_env()
    assert (s.app_version, s.weather_cache_ttl_s, s.db_pool_max_size) == ("abc1234", 60, 4)
    assert s.db_connections_per_replica == 12


def test_default_connection_budget_fits_small_azure_database():
    # Azure PostgreSQL B1ms: 35 user connections. Defaults: 2 workers x 2 = 4 per replica,
    # so up to 8 replicas (32 connections), with headroom for migrations and admin.
    assert Settings().db_connections_per_replica == 4
    assert 8 * Settings().db_connections_per_replica <= 35 - 2


def test_retry_policy_skips_read_timeouts_and_429():
    from app.weather_client import build_session

    retry = build_session().get_adapter("https://api.open-meteo.com").max_retries
    assert retry.read == 0
    assert 429 not in retry.status_forcelist
    assert retry.connect == 2


def test_settings_rejects_bad_number(monkeypatch):
    monkeypatch.setenv("HTTP_TIMEOUT_S", "rapide")
    with pytest.raises(ValueError, match="HTTP_TIMEOUT_S"):
        Settings.from_env()
