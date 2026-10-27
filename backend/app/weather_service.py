from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import metrics
from .errors import DatabaseUnavailable, WeatherApiUnavailable
from .models import City, Weather

log = logging.getLogger(__name__)

_NOT_LOADED = object()


class TTLCache:
    def __init__(self, max_entries: int = 1024, clock: Callable[[], float] = time.monotonic):
        self.max_entries = max_entries
        self._clock = clock
        self._data: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key) -> tuple[object, float] | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            self._data.move_to_end(key)
            value, stored_at = item
            return value, self._clock() - stored_at

    def set(self, key, value) -> None:
        with self._lock:
            self._data[key] = (value, self._clock())
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)


@dataclass(frozen=True)
class CityWeather:
    city: City
    weather: Weather | None
    source: str | None
    error: str | None = None


class WeatherService:
    def __init__(
        self,
        repository,
        client,
        ttl_s: int,
        lease_s: float = 10.0,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.repository = repository
        self.client = client
        self.ttl = timedelta(seconds=ttl_s)
        self.lease_s = lease_s
        self._now = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic
        self.position_cache = TTLCache(clock=monotonic)

    def weather_for(self, city: City, cached=_NOT_LOADED, allow_refresh: bool = True) -> tuple[Weather, str]:
        try:
            weather, source = self._weather_for(city, cached, allow_refresh)
        except WeatherApiUnavailable:
            metrics.WEATHER_SOURCE.labels("unavailable").inc()
            raise
        metrics.WEATHER_SOURCE.labels(source).inc()
        return weather, source

    def _weather_for(self, city: City, cached=_NOT_LOADED, allow_refresh: bool = True) -> tuple[Weather, str]:
        if cached is _NOT_LOADED:
            cached = self.repository.get_cached_weather(city.id)

        if cached and self._now() - cached.fetched_at < self.ttl:
            return cached, "cache"

        if not allow_refresh:
            if cached:
                return cached, "stale"
            raise WeatherApiUnavailable("Mise à jour reportée : réessayez dans un instant")

        # Circuit open: the API is known to be down, serve the last value as stale
        # (otherwise the UI would show "refreshing" during the whole outage).
        if cached and not self._api_available():
            return cached, "stale"

        # Only one instance refreshes a given city; the others serve the last value
        if cached and not self.repository.try_acquire_refresh(city.id, self.lease_s):
            return cached, "refreshing"

        try:
            fresh = self.client.current_weather(city.latitude, city.longitude)
        except WeatherApiUnavailable:
            if cached:
                log.warning("serving stale weather", extra={"city_id": city.id})
                return cached, "stale"
            raise

        self.repository.save_weather(city.id, fresh)  # also releases the lease
        return fresh, "live"

    def _api_available(self) -> bool:
        breaker = getattr(self.client, "breaker", None)
        return breaker is None or breaker.allow()  # half_open lets one trial call through

    def weather_at(self, latitude: float, longitude: float) -> tuple[Weather, str]:
        try:
            weather, source = self._weather_at(latitude, longitude)
        except WeatherApiUnavailable:
            metrics.WEATHER_SOURCE.labels("unavailable").inc()
            raise
        metrics.WEATHER_SOURCE.labels(source).inc()
        return weather, source

    def _weather_at(self, latitude: float, longitude: float) -> tuple[Weather, str]:
        key = (latitude, longitude)
        cached = self.position_cache.get(key)
        if cached and cached[1] < self.ttl.total_seconds():
            return cached[0], "cache"
        try:
            fresh = self.client.current_weather(latitude, longitude)
        except WeatherApiUnavailable:
            if cached:
                return cached[0], "stale"
            raise
        self.position_cache.set(key, fresh)
        return fresh, "live"

    def all_cities_weather(self, visitor_id: str, budget_s: float) -> list[CityWeather]:

        deadline = self._monotonic() + budget_s
        results = []
        for city, cached in self.repository.list_cities_with_weather(visitor_id):
            try:
                weather, source = self.weather_for(city, cached, allow_refresh=self._monotonic() < deadline)
                results.append(CityWeather(city, weather, source))
            except WeatherApiUnavailable as exc:
                results.append(CityWeather(city, None, None, exc.message))
        return results


class PlaceSearch:
    _STRIPES = 64  # striped locks: bounded count whatever users type

    def __init__(
        self,
        client,
        repository=None,
        ttl_s: int = 24 * 3600,
        max_entries: int = 2048,
        monotonic=time.monotonic,
        wait_s: float = 8.0,
    ):
        self.client = client
        self.repository = repository
        self.ttl_s = ttl_s
        self.wait_s = wait_s  # max wait for a call already in progress
        self.cache = TTLCache(max_entries=max_entries, clock=monotonic)
        self._locks = [threading.Lock() for _ in range(self._STRIPES)]

    @staticmethod
    def _key(text: str) -> str:
        return " ".join(text.split()).casefold()

    def _fresh(self, entry) -> bool:
        return entry is not None and entry[1] < self.ttl_s

    def _read_shared(self, key: str):
        if self.repository is None:
            return None
        try:
            return self.repository.get_geocode(key)
        except DatabaseUnavailable:
            return None  # suggestions do not need the database: go to the API

    def _write_shared(self, key: str, results: list) -> None:
        if self.repository is None:
            return
        try:
            self.repository.save_geocode(key, results)
        except DatabaseUnavailable:
            log.info("geocode cache not saved", extra={"reason": "database_unavailable"})

    def suggest(self, text: str) -> list:
        key = self._key(text)
        memory = self.cache.get(key)
        if self._fresh(memory):
            metrics.SUGGESTIONS.labels("memory").inc()
            return memory[0]

        lock = self._locks[hash(key) % self._STRIPES]
        if not lock.acquire(timeout=self.wait_s):
            if memory:
                metrics.SUGGESTIONS.labels("stale").inc()
                return memory[0]
            metrics.SUGGESTIONS.labels("unavailable").inc()
            raise WeatherApiUnavailable("Suggestions momentanément indisponibles")
        try:
            # Another thread may have fetched it while we waited
            memory = self.cache.get(key)
            if self._fresh(memory):
                metrics.SUGGESTIONS.labels("memory").inc()
                return memory[0]
            shared = self._read_shared(key)
            if self._fresh(shared):
                self.cache.set(key, shared[0])
                metrics.SUGGESTIONS.labels("shared").inc()
                return shared[0]
            try:
                results = self.client.search(text, count=8)
            except WeatherApiUnavailable:
                stale = memory or shared
                if stale:
                    log.info("serving stale suggestions", extra={"dependency": "open-meteo-geocoding"})
                    metrics.SUGGESTIONS.labels("stale").inc()
                    return stale[0]
                metrics.SUGGESTIONS.labels("unavailable").inc()
                raise
            self.cache.set(key, results)
            self._write_shared(key, results)
            metrics.SUGGESTIONS.labels("api").inc()
            return results
        finally:
            lock.release()

    def resolve(self, name: str, external_id: int | None = None):

        results = self.suggest(name)
        if external_id is None:
            return results[0] if results else None
        return next((r for r in results if r.external_id == external_id), None)
