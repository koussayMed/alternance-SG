import json
from datetime import timedelta

import pytest
from fakes import VISITOR


# --- Health ---------------------------------------------------------------------------
def test_healthz_ok_even_if_database_down(client, repo):
    repo.down = True
    res = client.get("/healthz")
    assert res.status_code == 200


def test_readyz_ready(client):
    res = client.get("/readyz")
    assert res.status_code == 200
    body = res.get_json()
    assert body["checks"]["database"] == "ok"
    assert body["info"]["weather_api_circuit"] == "closed"


def test_readyz_not_ready_when_database_down(client, repo):
    repo.down = True
    res = client.get("/readyz")
    assert res.status_code == 503
    assert res.get_json()["checks"]["database"] == "fail"


def test_readyz_ignores_weather_api_outage(client, weather_client):
    weather_client.down = True
    assert client.get("/readyz").status_code == 200


# --- Cities -----------------------------------------------------------------------------
def test_list_cities_empty(client):
    res = client.get("/api/cities")
    assert res.status_code == 200
    assert res.get_json() == []


def test_add_city_geocodes_and_stores(client):
    res = client.post("/api/cities", json={"name": "  lille "})
    assert res.status_code == 201
    body = res.get_json()
    assert body["name"] == "Lille"
    assert body["country"] == "France"
    assert res.headers["Location"] == f"/api/cities/{body['id']}"
    assert [c["name"] for c in client.get("/api/cities").get_json()] == ["Lille"]


def test_add_duplicate_city_conflicts(client, lille):
    res = client.post("/api/cities", json={"name": "Lille"})
    assert res.status_code == 409
    assert res.get_json()["error"] == "conflict"


def test_add_unknown_city_404(client):
    res = client.post("/api/cities", json={"name": "Atlantide"})
    assert res.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [None, [], {}, {"name": ""}, {"name": 42}, {"name": "x" * 81}, {"name": "<script>"}, {"name": "75001"}],
)
def test_add_city_validation(client, payload):
    res = client.post("/api/cities", data=json.dumps(payload), content_type="application/json")
    assert res.status_code == 400
    assert res.get_json()["error"] == "bad_request"


def test_add_city_accepts_accents_and_hyphens(client, monkeypatch):
    from fakes import KNOWN_PLACES

    from app.models import GeoResult

    monkeypatch.setitem(
        KNOWN_PLACES, "saint-étienne", GeoResult(2980291, "Saint-Étienne", "France", 45.43389, 4.39)
    )
    res = client.post("/api/cities", json={"name": "Saint-Étienne"})
    assert res.status_code == 201


def test_add_city_limit(client, repo, settings):
    from app.models import GeoResult

    for i in range(settings.max_cities):
        repo.add_city(GeoResult(i, f"Ville{i}", None, 0.0, 0.0), VISITOR)
    res = client.post("/api/cities", json={"name": "Lille"})
    assert res.status_code == 409


def test_add_city_when_geocoding_down_returns_503(client, weather_client):
    weather_client.down = True
    res = client.post("/api/cities", json={"name": "Lille"})
    assert res.status_code == 503
    assert res.get_json()["error"] == "weather_api_unavailable"


def test_delete_city(client, lille):
    assert client.delete(f"/api/cities/{lille['id']}").status_code == 204
    assert client.get("/api/cities").get_json() == []
    assert client.delete(f"/api/cities/{lille['id']}").status_code == 404


# --- Weather: cache and degraded mode -----------------------------------------------------
def test_weather_live_then_cache(client, lille, weather_client):
    first = client.get(f"/api/cities/{lille['id']}/weather").get_json()
    second = client.get(f"/api/cities/{lille['id']}/weather").get_json()
    assert first["source"] == "live"
    assert second["source"] == "cache"
    assert weather_client.weather_calls == 1
    assert first["description"] == "Couvert"
    assert first["temperature_c"] == 14.2


def test_weather_refreshed_after_ttl(client, lille, repo, weather_client, settings):
    client.get(f"/api/cities/{lille['id']}/weather")
    _age_cache(repo, lille["id"], settings.weather_cache_ttl_s + 1)
    weather_client.temperature = 9.0
    body = client.get(f"/api/cities/{lille['id']}/weather").get_json()
    assert body["source"] == "live"
    assert body["temperature_c"] == 9.0


def test_weather_stale_when_api_down(client, lille, repo, weather_client, settings):
    client.get(f"/api/cities/{lille['id']}/weather")
    _age_cache(repo, lille["id"], settings.weather_cache_ttl_s + 1)
    weather_client.down = True
    res = client.get(f"/api/cities/{lille['id']}/weather")
    assert res.status_code == 200
    assert res.get_json()["source"] == "stale"


def test_weather_503_when_api_down_and_no_cache(client, lille, weather_client):
    weather_client.down = True
    res = client.get(f"/api/cities/{lille['id']}/weather")
    assert res.status_code == 503
    assert res.get_json()["error"] == "weather_api_unavailable"


def test_weather_unknown_city_404(client):
    assert client.get("/api/cities/999/weather").status_code == 404


def _age_cache(repo, city_id, seconds):
    from dataclasses import replace

    cached = repo.cache[city_id]
    repo.cache[city_id] = replace(cached, fetched_at=cached.fetched_at - timedelta(seconds=seconds))


# --- Database outages ----------------------------------------------------------------------
def test_database_down_returns_503_json(client, repo):
    repo.down = True
    res = client.get("/api/cities")
    assert res.status_code == 503
    body = res.get_json()
    assert body["error"] == "database_unavailable"
    assert body["request_id"]


# --- Errors, correlation, logs ----------------------------------------------------------------
def test_unknown_route_json_404(client):
    res = client.get("/api/nope")
    assert res.status_code == 404
    assert res.get_json()["error"] == "not_found"


def test_unhandled_error_hides_details(client, repo, monkeypatch):
    def boom():
        raise RuntimeError("secret detail")

    monkeypatch.setattr(repo, "list_cities", boom)
    res = client.get("/api/cities")
    assert res.status_code == 500
    assert "secret detail" not in res.get_data(as_text=True)


def test_info_exposes_version_and_instance(client):
    body = client.get("/api/info").get_json()
    assert body["version"] == "test-sha"
    assert {"replica", "revision", "pid", "uptime_s"} <= body["instance"].keys()


def test_request_id_propagated(client):
    res = client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert res.headers["X-Request-ID"] == "abc-123"


def test_unsafe_request_id_replaced(client):
    unsafe = "bad id;<script>" + "x" * 80
    res = client.get("/healthz", headers={"X-Request-ID": unsafe})
    assert res.headers["X-Request-ID"] != unsafe
    assert len(res.headers["X-Request-ID"]) == 32


def test_access_log_is_json_with_request_id_and_route(capsys, client, lille):
    capsys.readouterr()
    client.get(f"/api/cities/{lille['id']}/weather", headers={"X-Request-ID": "trace-42"})
    entries = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    access = [e for e in entries if e["message"] == "request completed"][-1]
    assert access["request_id"] == "trace-42"
    assert access["status"] == 200
    assert access["route"] == "/api/cities/<int:city_id>/weather"
    assert access["service"] == "backend"
