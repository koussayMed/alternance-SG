"""API errors, turned into JSON responses (HTTP status + stable code). Messages are shown in the UI."""

from __future__ import annotations


class ApiError(Exception):
    status = 500
    code = "internal_error"
    default_message = "Erreur interne"

    def __init__(self, message: str | None = None):
        super().__init__(message or self.default_message)
        self.message = message or self.default_message


class BadRequest(ApiError):
    status = 400
    code = "bad_request"
    default_message = "Requête invalide"


class NotFound(ApiError):
    status = 404
    code = "not_found"
    default_message = "Ressource introuvable"


class Conflict(ApiError):
    status = 409
    code = "conflict"
    default_message = "Conflit"


class DatabaseUnavailable(ApiError):
    status = 503
    code = "database_unavailable"
    default_message = "Base de données indisponible"


class WeatherApiUnavailable(ApiError):
    status = 503
    code = "weather_api_unavailable"
    default_message = "Service météo externe indisponible"
