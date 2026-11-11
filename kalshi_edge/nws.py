"""National Weather Service forecast client.

``api.weather.gov`` asks for a User-Agent that identifies the caller. This
client is the same paced, retried GET as ``KalshiClient`` with that header
and with ``Accept`` set so a GeoJSON forecast is welcome. Nothing here is
authenticated.

The hourly forecast, not the 12-hour "daytime" period, is what a daily high
is read from. The period product is one rounded number for 6am to 6pm. The
CLI high Kalshi settles is the max on the local calendar date, and the
hourly grid is that max, still from the NWS, with the hour attached so the
lead time is the hour of the high rather than "sometime today".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from kalshi_edge.client import KalshiAPIError, KalshiClient

logger = logging.getLogger(__name__)

NWS_BASE_URL = "https://api.weather.gov"
# A contact URL, not an email. NWS asks for one; this repository is public.
NWS_USER_AGENT = (
    "kalshi-edge-finder/0.1 (daily temperature research; "
    "https://github.com/jmac052002/kalshi-edge-finder)"
)
NWS_ACCEPT = "application/geo+json, application/json"


def make_nws_client(**kwargs: object) -> KalshiClient:
    """A ``KalshiClient`` pointed at api.weather.gov.

    The class is the public JSON GET (pace, retry, no Authorization). The
    host and the User-Agent are the only things that change.
    """
    options = dict(kwargs)
    options.setdefault("base_url", NWS_BASE_URL)
    options.setdefault("user_agent", NWS_USER_AGENT)
    options.setdefault("accept", NWS_ACCEPT)
    return KalshiClient(**options)  # type: ignore[arg-type]


@dataclass(frozen=True)
class HourlyTemp:
    """One forecast hour. ``start`` carries the station's UTC offset."""

    start: datetime
    temperature_f: float


@dataclass(frozen=True)
class DailyHigh:
    """The forecast high for one local date, and how far ahead that hour is."""

    temperature_f: float
    at: datetime
    lead_hours: float
    station_name: str


def _to_fahrenheit(temperature: object, unit: object) -> float | None:
    try:
        value = float(temperature)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if unit == "C":
        value = value * 9.0 / 5.0 + 32.0
    elif unit not in ("F", None, ""):
        return None
    return value


def parse_hourly(payload: object) -> list[HourlyTemp]:
    """Hourly periods from a forecast payload, in the order the server sent."""
    if not isinstance(payload, dict):
        return []
    body = payload.get("properties") if isinstance(payload.get("properties"), dict) else payload
    if not isinstance(body, dict):
        return []
    periods = body.get("periods") or []
    if not isinstance(periods, list):
        return []
    hours: list[HourlyTemp] = []
    for period in periods:
        if not isinstance(period, dict):
            continue
        raw_start = period.get("startTime")
        if not isinstance(raw_start, str):
            continue
        try:
            start = datetime.fromisoformat(raw_start)
        except ValueError:
            continue
        if start.tzinfo is None:
            continue
        temperature = _to_fahrenheit(period.get("temperature"), period.get("temperatureUnit"))
        if temperature is None:
            continue
        hours.append(HourlyTemp(start=start, temperature_f=temperature))
    return hours


def station_location(payload: object) -> tuple[float, float, str] | None:
    """``(latitude, longitude, name)`` from a station GeoJSON object."""
    if not isinstance(payload, dict):
        return None
    geometry = payload.get("geometry")
    if not isinstance(geometry, dict):
        return None
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
        return None
    try:
        longitude = float(coordinates[0])
        latitude = float(coordinates[1])
    except (TypeError, ValueError):
        return None
    properties = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
    name = ""
    if isinstance(properties, dict) and isinstance(properties.get("name"), str):
        name = properties["name"].strip()
    return latitude, longitude, name


def hourly_forecast_url(points_payload: object) -> str | None:
    """The ``forecastHourly`` link a ``/points`` response points at."""
    if not isinstance(points_payload, dict):
        return None
    properties = points_payload.get("properties")
    if not isinstance(properties, dict):
        return None
    url = properties.get("forecastHourly")
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    return url


def daily_high(
    hours: list[HourlyTemp],
    market_day: date,
    now: datetime,
    *,
    station_name: str = "",
) -> DailyHigh | None:
    """Max forecast temperature on ``market_day``, if the afternoon is still ahead.

    The high has to be among hours whose local date is the market date, and
    at least one of those hours has to fall in the afternoon (local 12-17)
    and still be in the future (or within the last hour). A forecast that
    only has the evening left is the temperature after the high, and using
    it as the high would be the wrong number. When several hours share the
    max, the earliest one sets the lead time.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    on_day = [hour for hour in hours if hour.start.date() == market_day]
    if not on_day:
        return None
    upcoming = now - timedelta(hours=1)
    afternoon = [
        hour for hour in on_day if 12 <= hour.start.hour <= 17 and hour.start >= upcoming
    ]
    if not afternoon:
        return None
    peak_temp = max(hour.temperature_f for hour in on_day)
    peak = min(
        (hour for hour in on_day if hour.temperature_f == peak_temp),
        key=lambda hour: hour.start,
    )
    lead = (peak.start - now).total_seconds() / 3600.0
    if lead < 0:
        lead = 0.0
    return DailyHigh(
        temperature_f=peak_temp,
        at=peak.start,
        lead_hours=lead,
        station_name=station_name,
    )


class NwsForecasts:
    """Station metadata and hourly forecasts, each fetched once per scan."""

    def __init__(self, client: KalshiClient) -> None:
        self.client = client
        self._hours: dict[str, tuple[list[HourlyTemp], str]] = {}

    def daily_high(self, station_id: str, market_day: date, now: datetime) -> tuple[DailyHigh | None, str]:
        """The forecast high, and a short reason when there isn't one.

        The reason is empty when a high was found. A failure to reach NWS
        for one station is that reason, not an exception: one dead station
        should not drop the rest of the day's list.
        """
        try:
            hours, name = self._load(station_id)
        except KalshiAPIError as exc:
            logger.warning("NWS %s: %s", station_id, exc)
            return None, f"NWS request for {station_id} failed ({exc})"
        found = daily_high(hours, market_day, now, station_name=name)
        if found is None:
            return None, (
                f"NWS hourly forecast for {station_id} has no remaining afternoon "
                f"hour on {market_day.isoformat()} (the high is already outside the forecast)"
            )
        return found, ""

    def _load(self, station_id: str) -> tuple[list[HourlyTemp], str]:
        cached = self._hours.get(station_id)
        if cached is not None:
            return cached
        station = self.client.get_json(f"/stations/{station_id}")
        located = station_location(station)
        if located is None:
            raise KalshiAPIError(f"/stations/{station_id} had no coordinates")
        latitude, longitude, name = located
        points = self.client.get_json(f"/points/{latitude:.4f},{longitude:.4f}")
        url = hourly_forecast_url(points)
        if url is None:
            raise KalshiAPIError(f"/points for {station_id} had no hourly forecast link")
        hours = parse_hourly(self.client.get_json(url))
        if not hours:
            raise KalshiAPIError(f"hourly forecast for {station_id} had no temperatures")
        self._hours[station_id] = (hours, name)
        return hours, name
