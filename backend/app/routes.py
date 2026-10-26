from __future__ import annotations

import logging
import re

from flask import Blueprint, Response, current_app, jsonify, request

from . import metrics
from .errors import BadRequest, Conflict, DatabaseUnavailable, NotFound
from .instance import instance_info

log = logging.getLogger(__name__)

bp = Blueprint("api", __name__)

# Letters (accents included), spaces, apostrophes, dots, hyphens: « Saint-Étienne », « L'Haÿ-les-Roses »
_CITY_NAME_RE = re.compile(r"[^\W\d_](?:[^\W\d_]|[\s'’.-])*")
# Anonymous browser id (UUID made by the client): separates lists, authenticates nobody
_VISITOR_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _repo():
    return current_app.extensions["repository"]


def _settings():
    return current_app.config["SETTINGS"]


def _visitor_id() -> str:
    raw = request.headers.get("X-Visitor-Id", "").strip().lower()
    if not _VISITOR_ID_RE.match(raw):
        raise BadRequest("En-tête X-Visitor-Id manquant ou invalide (UUID attendu)")
    return raw


def _coordinate(name: str, low: float, high: float) -> float:
    try:
        value = float(request.args.get(name, ""))
    except ValueError:
        raise BadRequest(f"Paramètre '{name}' numérique attendu") from None
    if not low <= value <= high:
        raise BadRequest(f"Paramètre '{name}' hors limites ({low} à {high})")
    # Rounded to 0.01° (~1 km): never the exact position, and neighbours share the cache
    return round(value, 2)


def _validate_city_name(raw: object) -> str:
    if not isinstance(raw, str):
        raise BadRequest("Le champ 'name' (texte) est obligatoire")
    name = " ".join(raw.split())
    if not 1 <= len(name) <= 80 or not _CITY_NAME_RE.fullmatch(name):
        raise BadRequest("Nom de ville invalide (1 à 80 lettres, espaces, tirets ou apostrophes)")
    return name


# --- Health ----------------------------------------------------------------------
@bp.get("/healthz")
def healthz():
    """Liveness: the process answers. Never checks a dependency (a DB outage would restart every replica)."""
    return jsonify(status="ok")


@bp.get("/readyz")
def readyz():
    """Readiness: the database answers. The weather API is not checked: degraded mode handles it."""
    try:
        _repo().ping()
        database = "ok"
    except DatabaseUnavailable:
        database = "fail"
    ready = database == "ok"
    body = {
        "status": "ready" if ready else "not_ready",
        "checks": {"database": database},
        "info": {
            "weather_api_circuit": current_app.extensions["weather_client"].breaker.state,
            "geocoding_api_circuit": current_app.extensions["weather_client"].geocoding_breaker.state,
            # This worker's pool: requests_waiting > 0 = threads waiting for a DB connection
            "db_pool": _repo().pool_stats(),
        },
    }
    return jsonify(body), (200 if ready else 503)


# --- API --------------------------------------------------------------------------
@bp.get("/metrics")
def prometheus_metrics():
    """This process's Prometheus metrics (not public: nginx only proxies /api/)."""
    body, content_type = metrics.exposition()
    return Response(body, mimetype=content_type)


@bp.get("/api/info")
def info():
    settings = _settings()
    return jsonify(
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        instance=instance_info(),
    )


@bp.get("/api/weather")
def all_weather():
    """All the visitor's cities with their weather, in one call (one SQL query instead of 1 + N)."""
    service = current_app.extensions["weather_service"]
    items = []
    for entry in service.all_cities_weather(_visitor_id(), budget_s=_settings().refresh_budget_s):
        item = entry.city.to_dict()
        item["weather"] = {**entry.weather.to_dict(), "source": entry.source} if entry.weather else None
        if entry.error:
            item["error"] = entry.error
        items.append(item)
    return jsonify(items)


@bp.get("/api/weather/here")
def weather_here():
    """Weather at the visitor's position (rounded coordinates, never stored or logged)."""
    lat = _coordinate("lat", -90, 90)
    lon = _coordinate("lon", -180, 180)
    weather, source = current_app.extensions["weather_service"].weather_at(lat, lon)
    return jsonify(latitude=lat, longitude=lon, source=source, **weather.to_dict())


@bp.get("/api/cities")
def list_cities():
    return jsonify([city.to_dict() for city in _repo().list_cities(_visitor_id())])


@bp.get("/api/geocode")
def geocode():
    """Suggestions while typing. Under 2 characters: empty list, no external call."""
    raw = request.args.get("q", "")
    text = " ".join(raw.split())
    if len(text) < 2:
        return jsonify([])
    name = _validate_city_name(text)
    results = current_app.extensions["place_search"].suggest(name)
    return jsonify([r.to_suggestion() for r in results])


@bp.post("/api/cities")
def add_city():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise BadRequest('Corps JSON attendu : {"name": "Lille"}')
    name = _validate_city_name(body.get("name"))
    external_id = body.get("id")
    if external_id is not None and (not isinstance(external_id, int) or isinstance(external_id, bool)):
        raise BadRequest("Le champ 'id' doit être un entier (identifiant d'une suggestion)")
    visitor = _visitor_id()

    repo = _repo()
    if repo.count_cities(visitor) >= _settings().max_cities:
        raise Conflict(f"Limite de {_settings().max_cities} villes atteinte")

    # With an id: the chosen suggestion; without: the best match for the typed name
    geo = current_app.extensions["place_search"].resolve(name, external_id)
    if geo is None:
        raise NotFound(f"Ville introuvable : {name}")

    city = repo.add_city(geo, visitor)
    log.info("city added", extra={"city_id": city.id, "city": city.name})
    return jsonify(city.to_dict()), 201, {"Location": f"/api/cities/{city.id}"}


@bp.delete("/api/cities/<int:city_id>")
def delete_city(city_id: int):
    if not _repo().delete_city(city_id, _visitor_id()):
        raise NotFound(f"Ville {city_id} absente de ta liste")
    log.info("city deleted", extra={"city_id": city_id})
    return "", 204


@bp.get("/api/cities/<int:city_id>/weather")
def city_weather(city_id: int):
    city = _repo().get_city(city_id)
    if city is None:
        raise NotFound(f"Ville {city_id} introuvable")
    weather, source = current_app.extensions["weather_service"].weather_for(city)
    return jsonify(city_id=city.id, source=source, **weather.to_dict())
