"""Corridor (weather) conditions.

Reports ambient temperature and wind speed only. Weather is context for explanations; it is
not a congestion measure and no rule or action depends on it. Port congestion comes only from
the telemetry column `PORT_Q_IX`.

The live Open-Meteo adapter is off by default. Tests and replay inject `fetch` instead.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Returns the Open-Meteo `current_weather` object: {"temperature": ..., "windspeed": ...}.
WeatherFetch = Callable[[float, float], dict[str, Any]]


@dataclass(frozen=True)
class CorridorConditions:
    latitude: float
    longitude: float
    available: bool
    source: str
    ambient_temp_c: float | None = None
    wind_speed_kmh: float | None = None
    note: str = ""

    def as_text(self) -> str:
        if not self.available:
            body = f"status: unavailable ({self.note})"
        else:
            body = (
                f"ambient_temp_c: {self.ambient_temp_c}\n"
                f"wind_speed_kmh: {self.wind_speed_kmh}\n"
                "(weather only; this is not port congestion)"
            )
        return (
            "<corridor_conditions>\n"
            f"location: {self.latitude}, {self.longitude}\nsource: {self.source}\n{body}\n"
            "</corridor_conditions>"
        )


def open_meteo_fetch(latitude: float, longitude: float) -> dict[str, Any]:
    params: dict[str, float | str] = {
        "latitude": latitude,
        "longitude": longitude,
        "current_weather": "true",
    }
    response = requests.get(OPEN_METEO_URL, params=params, timeout=6)
    response.raise_for_status()
    current: dict[str, Any] = response.json().get("current_weather", {})
    return current


def get_corridor_conditions(
    latitude: float, longitude: float, fetch: WeatherFetch | None
) -> CorridorConditions:
    """`fetch=None` means no weather source is enabled; the result says so."""
    if fetch is None:
        return CorridorConditions(
            latitude,
            longitude,
            available=False,
            source="none",
            note="live weather adapter disabled (CORRIDOR_LIVE_ENABLED=false)",
        )
    try:
        current = fetch(latitude, longitude)
        wind = float(current["windspeed"])
        temp = float(current["temperature"])
    except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
        return CorridorConditions(
            latitude,
            longitude,
            available=False,
            source="weather",
            note=f"weather source error: {type(exc).__name__}",
        )
    return CorridorConditions(
        latitude,
        longitude,
        available=True,
        source="weather",
        ambient_temp_c=temp,
        wind_speed_kmh=wind,
    )
