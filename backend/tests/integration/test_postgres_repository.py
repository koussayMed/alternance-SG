"""Integration tests that check database operations using a real PostgreSQL instance.
Verify that cities, weather caches, and database features work correctly.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
if not DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL non défini", allow_module_level=True)

psycopg = pytest.importorskip("psycopg")

from app.errors import Conflict  # noqa: E402
from app.migrate import migrate  # noqa: E402
from app.models import GeoResult, Weather  # noqa: E402
from app.repository import PostgresRepository  # noqa: E402

LILLE = GeoResult(2998324, "Lille", "France", 50.63297, 3.05858)
V1 = "11111111-1111-4111-8111-111111111111"
V2 = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(scope="module")
def repository():
    migrate(DATABASE_URL)
    migrate(DATABASE_URL)  # idempotent: the 2nd run does nothing
    return PostgresRepository(DATABASE_URL, pool_max_size=2)


@pytest.fixture(autouse=True)
def clean_tables():
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute("TRUNCATE places RESTART IDENTITY CASCADE")


def test_ping(repository):
    repository.ping()


def test_city_crud(repository):
    city = repository.add_city(LILLE, V1)
    assert city.id > 0
    assert repository.get_city(city.id) == city
    assert repository.list_cities(V1) == [city]
    assert repository.count_cities(V1) == 1
    with pytest.raises(Conflict):
        repository.add_city(LILLE, V1)
    assert repository.delete_city(city.id, V1) is True
    assert repository.delete_city(city.id, V1) is False
    assert repository.list_cities(V1) == []
    assert repository.get_city(city.id) == city  # the place stays, shared with other visitors


def test_weather_cache_upsert_and_cascade(repository):
    city = repository.add_city(LILLE, V1)
    now = datetime.now(UTC).replace(microsecond=0)
    first = Weather(10.0, 5.0, 3, now - timedelta(minutes=15), now - timedelta(minutes=1))
    second = Weather(12.5, 7.0, None, now, now)

    repository.save_weather(city.id, first)
    assert repository.get_cached_weather(city.id) == first
    repository.save_weather(city.id, second)
    assert repository.get_cached_weather(city.id) == second

    repository.delete_city(city.id, V1)  # removing from one list keeps the shared cache
    assert repository.get_cached_weather(city.id) == second

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:  # deleting the place cascades
        conn.execute("DELETE FROM places WHERE id = %s", (city.id,))
    assert repository.get_cached_weather(city.id) is None
    repository.save_weather(city.id, second)  # place deleted: ignored without error


def test_list_cities_with_weather_single_query(repository):
    lille = repository.add_city(LILLE, V1)
    paris = repository.add_city(GeoResult(2988507, "Paris", "France", 48.85341, 2.3488), V1)
    now = datetime.now(UTC).replace(microsecond=0)
    weather = Weather(11.0, 9.0, 2, now, now)
    repository.save_weather(lille.id, weather)

    rows = repository.list_cities_with_weather(V1)
    assert rows == [(lille, weather), (paris, None)]


def test_refresh_lease_is_exclusive_and_released_on_save(repository):
    city = repository.add_city(LILLE, V1)
    assert repository.try_acquire_refresh(city.id, 10) is False  # no cache row yet
    now = datetime.now(UTC).replace(microsecond=0)
    repository.save_weather(city.id, Weather(10.0, 5.0, 3, now, now))

    assert repository.try_acquire_refresh(city.id, 10) is True
    assert repository.try_acquire_refresh(city.id, 10) is False  # already taken by "another instance"
    repository.save_weather(city.id, Weather(12.0, 5.0, 3, now, now))  # releases the lease
    assert repository.try_acquire_refresh(city.id, 10) is True


def test_refresh_lease_expires(repository):
    city = repository.add_city(LILLE, V1)
    now = datetime.now(UTC).replace(microsecond=0)
    repository.save_weather(city.id, Weather(10.0, 5.0, 3, now, now))
    assert repository.try_acquire_refresh(city.id, 0.05) is True
    import time

    time.sleep(0.1)
    assert repository.try_acquire_refresh(city.id, 10) is True  # expired lease can be taken again


def test_pool_stats(repository):
    stats = repository.pool_stats()
    assert stats["pool_max"] == 2


def test_places_are_shared_between_visitors(repository):
    mine = repository.add_city(LILLE, V1)
    theirs = repository.add_city(LILLE, V2)
    assert mine.id == theirs.id  # one place, two lists
    assert repository.list_cities(V2) == [theirs]
    assert repository.delete_city(mine.id, V1) is True
    assert repository.list_cities(V1) == []
    assert repository.list_cities(V2) == [theirs]  # the other visitor's list is untouched


def test_geocode_cache_roundtrip(repository):
    assert repository.get_geocode("lil") is None
    repository.save_geocode("lil", [LILLE])
    results, age_s = repository.get_geocode("lil")
    assert results == [LILLE] and 0 <= age_s < 60
    repository.save_geocode("lil", [])  # "no city" is cached too
    assert repository.get_geocode("lil")[0] == []
