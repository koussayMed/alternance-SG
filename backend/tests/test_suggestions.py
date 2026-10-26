import threading
import time

import pytest
import requests
from fakes import KNOWN_PLACES, FakeRepository, FakeWeatherClient

from app.errors import WeatherApiUnavailable
from app.models import GeoResult
from app.weather_client import CircuitBreaker, OpenMeteoClient
from app.weather_service import PlaceSearch

SFAX = GeoResult(2467454, "Sfax", "Tunisie", 34.74, 10.76, "Sfax")
SFANTU = GeoResult(667268, "Sfântu Gheorghe", "Roumanie", 45.87, 25.79)
SAINT_DENIS_IDF = GeoResult(2980916, "Saint-Denis", "France", 48.94, 2.36, "Île-de-France")
SAINT_DENIS_REUNION = GeoResult(935264, "Saint-Denis", "France", -20.88, 55.45, "La Réunion")


@pytest.fixture(autouse=True)
def more_places(monkeypatch):
    # Two cities starting with "sf", and two "Saint-Denis"
    monkeypatch.setitem(KNOWN_PLACES, "sfax", SFAX)
    monkeypatch.setitem(KNOWN_PLACES, "sfântu gheorghe", SFANTU)
    monkeypatch.setitem(KNOWN_PLACES, "saint-denis", SAINT_DENIS_IDF)
    monkeypatch.setitem(KNOWN_PLACES, "saint-denis (réunion)", SAINT_DENIS_REUNION)


def test_suggestions_by_prefix(client):
    res = client.get("/api/geocode?q=sf")
    assert res.status_code == 200
    names = [s["name"] for s in res.get_json()]
    assert names == ["Sfax", "Sfântu Gheorghe"]
    assert res.get_json()[0] == {"id": 2467454, "name": "Sfax", "region": "Sfax", "country": "Tunisie"}


def test_suggestions_are_cached(client, weather_client):
    client.get("/api/geocode?q=sf")
    client.get("/api/geocode?q=SF")  # same text, other case: same cache entry
    client.get("/api/geocode?q=%20sf%20")
    assert weather_client.search_calls == 1


@pytest.mark.parametrize("query", ["", "s", "%20%20"])
def test_too_short_returns_empty_without_api_call(client, weather_client, query):
    res = client.get(f"/api/geocode?q={query}")
    assert res.status_code == 200
    assert res.get_json() == []
    assert weather_client.search_calls == 0


def test_invalid_query_rejected(client):
    assert client.get("/api/geocode?q=<script>").status_code == 400


def test_suggestions_when_api_down(client, weather_client):
    weather_client.down = True
    res = client.get("/api/geocode?q=sf")
    assert res.status_code == 503
    assert res.get_json()["error"] == "weather_api_unavailable"


def test_add_chosen_suggestion_by_id(client):
    # The visitor types "saint-denis" and picks the one in La Réunion, not the first result
    client.get("/api/geocode?q=saint-denis")
    res = client.post("/api/cities", json={"name": "saint-denis", "id": 935264})
    assert res.status_code == 201
    body = res.get_json()
    assert (body["name"], body["latitude"]) == ("Saint-Denis", -20.88)


def test_add_without_id_takes_best_match(client):
    res = client.post("/api/cities", json={"name": "saint-denis"})
    assert res.get_json()["latitude"] == 48.94


def test_add_with_unknown_id_is_404(client):
    res = client.post("/api/cities", json={"name": "sfax", "id": 123})
    assert res.status_code == 404


@pytest.mark.parametrize("bad_id", ["935264", 1.5, True])
def test_add_with_invalid_id_type_is_400(client, bad_id):
    res = client.post("/api/cities", json={"name": "sfax", "id": bad_id})
    assert res.status_code == 400


def test_client_search_parses_region():
    class Session:
        def get(self, url, params=None, timeout=None):
            class R:
                status_code = 200

                def raise_for_status(self):
                    pass

                def json(self):
                    return {
                        "results": [
                            {
                                "id": 1,
                                "name": "Sfax",
                                "country": "Tunisie",
                                "admin1": "Sfax",
                                "latitude": 34.7,
                                "longitude": 10.7,
                            }
                        ]
                    }

            assert params["count"] == 8
            return R()

    client = OpenMeteoClient("https://f", "https://g", session=Session(), breaker=CircuitBreaker())
    [geo] = client.search("sfa")
    assert (geo.name, geo.region, geo.country) == ("Sfax", "Sfax", "Tunisie")


# --- Suggestions under load (regression found by the k6 load test) ---------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class SlowClient(FakeWeatherClient):
    """A slightly slow API, so concurrent requests have time to pile up."""

    def search(self, name, count=8):
        time.sleep(0.2)
        return super().search(name, count)


def test_simultaneous_requests_make_a_single_api_call():
    # 30 visitors type "sf" at once on a fresh replica (empty cache)
    client = SlowClient()
    search = PlaceSearch(client, FakeRepository())
    results = []
    threads = [threading.Thread(target=lambda: results.append(search.suggest("sf"))) for _ in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert client.search_calls == 1
    assert len(results) == 30 and all(r == [SFAX, SFANTU] for r in results)


def test_new_replica_reuses_the_shared_cache():
    client, repo = FakeWeatherClient(), FakeRepository()
    PlaceSearch(client, repo).suggest("sf")  # replica 1
    assert PlaceSearch(client, repo).suggest("sf") == [SFAX, SFANTU]  # replica 2, empty memory cache
    assert client.search_calls == 1


def test_stale_suggestions_served_when_api_down():
    clock, client = Clock(), FakeWeatherClient()
    search = PlaceSearch(client, None, ttl_s=60, monotonic=clock)
    search.suggest("sf")
    clock.now += 3600  # result expired
    client.down = True
    assert search.suggest("sf") == [SFAX, SFANTU]


def test_stale_shared_suggestions_served_to_a_new_replica_when_api_down():
    clock, client, repo = Clock(), FakeWeatherClient(), FakeRepository()
    repo.clock = clock
    PlaceSearch(client, repo, ttl_s=60, monotonic=clock).suggest("sf")
    clock.now += 3600
    client.down = True
    assert PlaceSearch(client, repo, ttl_s=60, monotonic=clock).suggest("sf") == [SFAX, SFANTU]


def test_suggestions_work_without_the_database():
    client, repo = FakeWeatherClient(), FakeRepository()
    repo.down = True
    assert PlaceSearch(client, repo).suggest("sf") == [SFAX, SFANTU]


def test_geocoding_failures_do_not_block_weather():
    # One breaker per API: geocoding 429s do not block weather refreshes
    class Session:
        def get(self, url, params=None, timeout=None):
            if url == "https://g":
                raise requests.ConnectionError("429")

            class R:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {
                        "current": {"temperature_2m": 9.5, "wind_speed_10m": 5, "time": "2026-10-24T10:00"}
                    }

            return R()

    client = OpenMeteoClient(
        "https://f",
        "https://g",
        session=Session(),
        breaker=CircuitBreaker(failure_threshold=2),
        geocoding_breaker=CircuitBreaker(failure_threshold=2),
    )
    for _ in range(3):
        with pytest.raises(WeatherApiUnavailable):
            client.search("par")
    assert client.geocoding_breaker.state == "open"
    assert client.current_weather(48.85, 2.35).temperature_c == 9.5
    assert client.breaker.state == "closed"
