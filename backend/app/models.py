from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# WMO weather codes returned by Open-Meteo (weather_code); labels are shown in the UI
WMO_DESCRIPTIONS = {
    0: "Ciel dégagé",
    1: "Plutôt dégagé",
    2: "Partiellement nuageux",
    3: "Couvert",
    45: "Brouillard",
    48: "Brouillard givrant",
    51: "Bruine légère",
    53: "Bruine",
    55: "Bruine dense",
    56: "Bruine verglaçante",
    57: "Bruine verglaçante dense",
    61: "Pluie faible",
    63: "Pluie",
    65: "Pluie forte",
    66: "Pluie verglaçante",
    67: "Pluie verglaçante forte",
    71: "Neige faible",
    73: "Neige",
    75: "Neige forte",
    77: "Grains de neige",
    80: "Averses faibles",
    81: "Averses",
    82: "Averses violentes",
    85: "Averses de neige",
    86: "Averses de neige fortes",
    95: "Orage",
    96: "Orage avec grêle",
    99: "Orage avec forte grêle",
}


def describe_weather(code: int | None) -> str:
    if code is None:
        return "Inconnu"
    return WMO_DESCRIPTIONS.get(code, f"Code météo {code}")


@dataclass(frozen=True)
class GeoResult:
    """A geocoding result: what we need to store a city."""

    external_id: int
    name: str
    country: str | None
    latitude: float
    longitude: float
    region: str | None = None  # admin region, to tell same-name cities apart

    def to_suggestion(self) -> dict:
        return {
            "id": self.external_id,
            "name": self.name,
            "region": self.region,
            "country": self.country,
        }


@dataclass(frozen=True)
class City:
    id: int
    external_id: int
    name: str
    country: str | None
    latitude: float
    longitude: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "country": self.country,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }


@dataclass(frozen=True)
class Weather:
    temperature_c: float
    wind_kmh: float
    weather_code: int | None
    observed_at: datetime
    fetched_at: datetime

    def to_dict(self) -> dict:
        return {
            "temperature_c": self.temperature_c,
            "wind_kmh": self.wind_kmh,
            "weather_code": self.weather_code,
            "description": describe_weather(self.weather_code),
            "observed_at": self.observed_at.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
        }
