from dataclasses import replace
from datetime import timedelta

from fakes import VISITOR

from app.models import GeoResult
from app.weather_service import WeatherService


def _add(repo, n):
    for i in range(n):
        repo.add_city(GeoResult(1000 + i, f"Ville{i}", "France", 45.0 + i, 2.0), VISITOR)


def _expire_all(repo, seconds=10_000):
    for city_id, w in list(repo.cache.items()):
        repo.cache[city_id] = replace(w, fetched_at=w.fetched_at - timedelta(seconds=seconds))


# --- Aggregated endpoint ---------------------------------------------------------------------
def test_all_weather_empty(client):
    res = client.get("/api/weather")
    assert res.status_code == 200
    assert res.get_json() == []


def test_all_weather_returns_cities_with_weather(client, repo, weather_client):
    _add(repo, 3)
    first = client.get("/api/weather").get_json()
    assert [c["name"] for c in first] == ["Ville0", "Ville1", "Ville2"]
    assert {c["weather"]["source"] for c in first} == {"live"}
    assert first[0]["weather"]["description"] == "Couvert"

    second = client.get("/api/weather").get_json()
    assert {c["weather"]["source"] for c in second} == {"cache"}
    assert weather_client.weather_calls == 3


def test_all_weather_uses_one_query_when_cache_is_fresh(client, repo):
    _add(repo, 5)
    client.get("/api/weather")  # fills the cache
    repo.queries = 0
    client.get("/api/weather")
    assert repo.queries == 1  # one SQL query, whatever the number of cities


def test_all_weather_degrades_per_city_when_api_down(client, repo, weather_client):
    _add(repo, 2)
    client.get("/api/weather")
    _expire_all(repo)
    repo.add_city(GeoResult(9999, "Nouvelle", "France", 1.0, 1.0), VISITOR)  # nothing cached
    weather_client.down = True

    res = client.get("/api/weather")
    assert res.status_code == 200  # the page stays usable
    body = {c["name"]: c for c in res.get_json()}
    assert body["Ville0"]["weather"]["source"] == "stale"
    assert body["Nouvelle"]["weather"] is None
    assert "error" in body["Nouvelle"]


def test_all_weather_database_down_returns_503(client, repo):
    repo.down = True
    res = client.get("/api/weather")
    assert res.status_code == 503
    assert res.get_json()["error"] == "database_unavailable"


# --- Refresh lease ---------------------------------------------------------------------------
def test_refresh_lease_prevents_concurrent_api_calls(repo, weather_client):
    service = WeatherService(repo, weather_client, ttl_s=600)
    _add(repo, 1)
    city = repo.cities[1]
    service.weather_for(city)  # live
    _expire_all(repo)

    assert repo.try_acquire_refresh(city.id, 10)  # another instance is already refreshing
    calls_before = weather_client.weather_calls
    weather, source = service.weather_for(city)
    assert source == "refreshing"
    assert weather_client.weather_calls == calls_before  # no duplicate API call


def test_circuit_open_serves_stale_without_taking_the_lease(repo, weather_client):
    # During a known outage the source is "stale" (not "refreshing") and no lease is taken
    service = WeatherService(repo, weather_client, ttl_s=600)
    _add(repo, 1)
    city = repo.cities[1]
    service.weather_for(city)
    _expire_all(repo)
    for _ in range(weather_client.breaker.failure_threshold):
        weather_client.breaker.record_failure()
    calls_before = weather_client.weather_calls
    weather, source = service.weather_for(city)
    assert source == "stale" and weather is not None
    assert weather_client.weather_calls == calls_before
    assert city.id not in repo.leases


def test_refresh_releases_lease_after_save(repo, weather_client):
    service = WeatherService(repo, weather_client, ttl_s=600)
    _add(repo, 1)
    city = repo.cities[1]
    service.weather_for(city)
    _expire_all(repo)
    _, source = service.weather_for(city)
    assert source == "live"
    assert city.id not in repo.leases


# --- Time budget per request -------------------------------------------------------------------
def test_refresh_budget_stops_refreshing_when_exhausted(repo, weather_client):
    clock = [0.0]
    service = WeatherService(repo, weather_client, ttl_s=600, monotonic=lambda: clock[0])
    _add(repo, 3)
    service.all_cities_weather(VISITOR, budget_s=100)  # fills the cache
    _expire_all(repo)
    calls_before = weather_client.weather_calls

    original = weather_client.current_weather

    def slow_call(lat, lon):
        clock[0] += 3.0  # each call "takes" 3 s
        return original(lat, lon)

    weather_client.current_weather = slow_call
    results = service.all_cities_weather(VISITOR, budget_s=4.0)
    assert [r.source for r in results] == ["live", "live", "stale"]  # 3rd city: budget spent
    assert weather_client.weather_calls == calls_before + 2


def test_readyz_exposes_pool_stats(client):
    info = client.get("/readyz").get_json()["info"]
    assert info["db_pool"]["requests_waiting"] == 0
