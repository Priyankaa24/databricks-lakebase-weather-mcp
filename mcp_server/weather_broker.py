"""
Weather broker: HTTP adapter around Open-Meteo + NWS.

Same role as alpaca_broker.py in the reference project - all HTTP calls
and response parsing live here so the MCP tool functions in
weather_mcp_server.py can stay thin.

Data sources (both free, no API key required):
  - Open-Meteo (https://open-meteo.com/) - geocoding + global forecast data
  - NWS (https://api.weather.gov/) - US-only active alerts
"""

import os
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OPEN_METEO_BASE = "https://api.open-meteo.com/v1"
OPEN_METEO_GEOCODE_BASE = "https://geocoding-api.open-meteo.com/v1"
NWS_BASE = "https://api.weather.gov"

# NWS requires a descriptive User-Agent. Override via env var when deploying.
NWS_USER_AGENT = os.environ.get(
    "NWS_USER_AGENT",
    "weather-mcp-server (rajendrannpriyankaa@gmail.com)",
)

DEFAULT_TIMEOUT = 15

# Reusable sessions - created lazily on first call.
_open_meteo_session: requests.Session | None = None
_nws_session: requests.Session | None = None


def _get_open_meteo_session() -> requests.Session:
    global _open_meteo_session
    if _open_meteo_session is None:
        _open_meteo_session = requests.Session()
    return _open_meteo_session


def _get_nws_session() -> requests.Session:
    global _nws_session
    if _nws_session is None:
        _nws_session = requests.Session()
        _nws_session.headers.update({
            "User-Agent": NWS_USER_AGENT,
            "Accept": "application/geo+json",
        })
    return _nws_session


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

def geocode(location: str) -> dict[str, Any]:
    """Resolve a location name to lat/lon + timezone via Open-Meteo."""
    session = _get_open_meteo_session()
    resp = session.get(
        f"{OPEN_METEO_GEOCODE_BASE}/search",
        params={"name": location, "count": 1, "language": "en", "format": "json"},
        timeout=DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        raise ValueError(f"No location found for query: {location!r}")
    top = results[0]
    return {
        "query": location,
        "name": top.get("name"),
        "country": top.get("country"),
        "admin1": top.get("admin1"),
        "latitude": top.get("latitude"),
        "longitude": top.get("longitude"),
        "timezone": top.get("timezone"),
    }


def _short_name(geo: dict) -> str:
    parts = [geo.get("name")]
    if geo.get("admin1"):
        parts.append(geo["admin1"])
    elif geo.get("country"):
        parts.append(geo["country"])
    return ", ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Current weather
# ---------------------------------------------------------------------------

def get_current(location: str) -> dict[str, Any]:
    """Current conditions at the given location (Open-Meteo)."""
    geo = geocode(location)
    session = _get_open_meteo_session()
    resp = session.get(
        f"{OPEN_METEO_BASE}/forecast",
        params={
            "latitude": geo["latitude"],
            "longitude": geo["longitude"],
            "current": ",".join([
                "temperature_2m",
                "relative_humidity_2m",
                "apparent_temperature",
                "precipitation",
                "weather_code",
                "wind_speed_10m",
                "wind_direction_10m",
                "cloud_cover",
            ]),
            "timezone": geo["timezone"] or "auto",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
        },
        timeout=DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    current = resp.json().get("current", {})
    return {
        "location": _short_name(geo),
        "latitude": geo["latitude"],
        "longitude": geo["longitude"],
        "observed_at": current.get("time"),
        "temperature_f": current.get("temperature_2m"),
        "feels_like_f": current.get("apparent_temperature"),
        "humidity_pct": current.get("relative_humidity_2m"),
        "precipitation_in": current.get("precipitation"),
        "wind_mph": current.get("wind_speed_10m"),
        "wind_direction_deg": current.get("wind_direction_10m"),
        "cloud_cover_pct": current.get("cloud_cover"),
        "conditions": _weather_code_to_text(current.get("weather_code")),
    }


# ---------------------------------------------------------------------------
# Daily forecast
# ---------------------------------------------------------------------------

def get_daily_forecast(location: str, days: int = 3) -> dict[str, Any]:
    """Daily forecast for the next N days (1-7)."""
    days = max(1, min(7, int(days)))
    geo = geocode(location)
    session = _get_open_meteo_session()
    resp = session.get(
        f"{OPEN_METEO_BASE}/forecast",
        params={
            "latitude": geo["latitude"],
            "longitude": geo["longitude"],
            "daily": ",".join([
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_sum",
                "precipitation_probability_max",
                "weather_code",
                "wind_speed_10m_max",
                "sunrise",
                "sunset",
            ]),
            "forecast_days": days,
            "timezone": geo["timezone"] or "auto",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
        },
        timeout=DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    daily = resp.json().get("daily", {})
    dates = daily.get("time", [])
    periods = []
    for i, date in enumerate(dates):
        periods.append({
            "date": date,
            "high_f": daily.get("temperature_2m_max", [None])[i],
            "low_f": daily.get("temperature_2m_min", [None])[i],
            "precipitation_in": daily.get("precipitation_sum", [None])[i],
            "precipitation_chance_pct": daily.get("precipitation_probability_max", [None])[i],
            "max_wind_mph": daily.get("wind_speed_10m_max", [None])[i],
            "conditions": _weather_code_to_text(daily.get("weather_code", [None])[i]),
            "sunrise": daily.get("sunrise", [None])[i],
            "sunset": daily.get("sunset", [None])[i],
        })
    return {
        "location": _short_name(geo),
        "latitude": geo["latitude"],
        "longitude": geo["longitude"],
        "days": periods,
    }


# ---------------------------------------------------------------------------
# Hourly forecast
# ---------------------------------------------------------------------------

def get_hourly_forecast(location: str, hours: int = 24) -> dict[str, Any]:
    """Hourly forecast for the next N hours (1-72)."""
    hours = max(1, min(72, int(hours)))
    geo = geocode(location)
    session = _get_open_meteo_session()
    resp = session.get(
        f"{OPEN_METEO_BASE}/forecast",
        params={
            "latitude": geo["latitude"],
            "longitude": geo["longitude"],
            "hourly": ",".join([
                "temperature_2m",
                "precipitation_probability",
                "precipitation",
                "weather_code",
                "wind_speed_10m",
            ]),
            "forecast_hours": hours,
            "timezone": geo["timezone"] or "auto",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
        },
        timeout=DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    hourly = resp.json().get("hourly", {})
    times = hourly.get("time", [])
    periods = []
    for i, t in enumerate(times):
        periods.append({
            "time": t,
            "temperature_f": hourly.get("temperature_2m", [None])[i],
            "precipitation_chance_pct": hourly.get("precipitation_probability", [None])[i],
            "precipitation_in": hourly.get("precipitation", [None])[i],
            "wind_mph": hourly.get("wind_speed_10m", [None])[i],
            "conditions": _weather_code_to_text(hourly.get("weather_code", [None])[i]),
        })
    return {
        "location": _short_name(geo),
        "latitude": geo["latitude"],
        "longitude": geo["longitude"],
        "hours": periods,
    }


# ---------------------------------------------------------------------------
# Active alerts (NWS, US-only)
# ---------------------------------------------------------------------------

def get_active_alerts(location: str) -> dict[str, Any]:
    """Active NWS alerts for a US location. Returns empty list for non-US."""
    geo = geocode(location)
    session = _get_nws_session()
    try:
        resp = session.get(
            f"{NWS_BASE}/alerts/active",
            params={"point": f"{geo['latitude']},{geo['longitude']}"},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return {
            "location": _short_name(geo),
            "alerts": [],
            "note": "No alerts available (NWS covers US locations only).",
        }

    features = resp.json().get("features", []) or []
    alerts = []
    for feature in features:
        props = feature.get("properties", {})
        alerts.append({
            "event": props.get("event"),
            "headline": props.get("headline"),
            "severity": props.get("severity"),
            "urgency": props.get("urgency"),
            "certainty": props.get("certainty"),
            "sent": props.get("sent"),
            "expires": props.get("expires"),
            "description": (props.get("description") or "")[:800],
            "instruction": props.get("instruction"),
        })
    return {
        "location": _short_name(geo),
        "alerts": alerts,
    }


# ---------------------------------------------------------------------------
# WMO weather code -> text
# ---------------------------------------------------------------------------

_WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow fall", 73: "Moderate snow fall", 75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}


def _weather_code_to_text(code: int | None) -> str | None:
    if code is None:
        return None
    return _WEATHER_CODES.get(int(code), f"Unknown ({code})")