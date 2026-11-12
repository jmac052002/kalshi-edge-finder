"""Archived hourly forecasts from the Open-Meteo Previous Runs API.

``api.weather.gov`` serves the current NWS grid and nothing older, so a
fit against settled Kalshi highs needs a different archive. This one is
free and unauthenticated. ``temperature_2m_previous_day1`` is the
temperature that model run predicted 24 hours before the valid hour, and
``temperature_2m_previous_day2`` is 48 hours before. The daily high used
here is the maximum of those hourly values on the station's local calendar
date, but only when the local afternoon is actually in the series. That is
the same reduction ``kalshi_edge.nws.daily_high`` applies to a live NWS grid.

The model is GFS seamless, not the National Weather Service.

``temperature_2m`` with no suffix is day 0: each hour taken from the newest
run that covers it, so its lead is a few hours at most. It is not a forecast
that was sitting on the screen the evening before, and it is never used as
one. It is read as the *floor* of the error: what is left when the forecast
has almost no lead, which is mostly the gap between a model grid cell and
the airport thermometer the contract settles on. A morning scan prices
same-day markets 3 to 10 hours before the high. That is between day 0 and
the 24-hour series, so ``weather_error.predict_high`` interpolates between
the two fits for those leads and no longer gives a three-hour forecast a
one-day error.

Docs: https://open-meteo.com/en/docs/previous-runs-api
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, timedelta

from kalshi_edge.client import KalshiClient

OPEN_METEO_BASE_URL = "https://previous-runs-api.open-meteo.com/v1"
OPEN_METEO_MODEL = "gfs_seamless"
OPEN_METEO_USER_AGENT = (
    "kalshi-edge-finder/0.1 (daily temperature calibration; "
    "https://github.com/jmac052002/kalshi-edge-finder)"
)
# Fixed leads this client asks for. The key is the lead in hours. Day 0 is
# filed under lead 0: its true lead varies by hour and is small.
SAME_DAY_LEAD_HOURS = 0
LEAD_FIELDS = {
    SAME_DAY_LEAD_HOURS: "temperature_2m",
    24: "temperature_2m_previous_day1",
    48: "temperature_2m_previous_day2",
}
MIN_HOURS = 12


def make_open_meteo_client(**kwargs: object) -> KalshiClient:
    """A paced JSON client pointed at the Previous Runs API. No key."""
    options = dict(kwargs)
    options.setdefault("base_url", OPEN_METEO_BASE_URL)
    options.setdefault("user_agent", OPEN_METEO_USER_AGENT)
    return KalshiClient(**options)  # type: ignore[arg-type]


def _field(hourly: dict, name: str) -> list | None:
    """The series, or the model-suffixed name Open-Meteo uses for a multi-model call.

    ``temperature_2m_`` is also the start of ``temperature_2m_previous_day1``.
    A suffixed match for the day-0 field must not pick up a previous-day
    series, or a missing day-0 column would be filled with the 24-hour one
    and fit as if it were same-day.
    """
    if name in hourly and isinstance(hourly[name], list):
        return hourly[name]
    prefix = name + "_"
    for key, value in hourly.items():
        if not isinstance(key, str) or not key.startswith(prefix) or not isinstance(value, list):
            continue
        if "previous_day" in key and "previous_day" not in name:
            continue
        return value
    return None


def daily_maxes(payload: object) -> dict[date, dict[int, float]]:
    """Local-date maxima of each previous-day series.

    A date is kept when it has at least ``MIN_HOURS`` finite hours and one
    of them is in the local afternoon (12 through 17). A night-only fragment
    is not a daily high. The return value maps the date to ``{lead_hours: max_f}``.
    """
    if not isinstance(payload, dict):
        return {}
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        return {}
    times = hourly.get("time")
    if not isinstance(times, list):
        return {}
    series = {lead: _field(hourly, name) for lead, name in LEAD_FIELDS.items()}
    buckets: dict[date, dict[int, list[tuple[int, float]]]] = defaultdict(lambda: defaultdict(list))
    for index, stamp in enumerate(times):
        if not isinstance(stamp, str) or len(stamp) < 13:
            continue
        try:
            day = date.fromisoformat(stamp[:10])
            hour = int(stamp[11:13])
        except ValueError:
            continue
        for lead, values in series.items():
            if values is None or index >= len(values):
                continue
            value = values[index]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            temperature = float(value)
            if math.isnan(temperature):
                continue
            buckets[day][lead].append((hour, temperature))
    found: dict[date, dict[int, float]] = {}
    for day, by_lead in buckets.items():
        maxima: dict[int, float] = {}
        for lead, rows in by_lead.items():
            hours = {hour for hour, _temperature in rows}
            if len(rows) < MIN_HOURS or not any(12 <= hour <= 17 for hour in hours):
                continue
            maxima[lead] = max(temperature for _hour, temperature in rows)
        if maxima:
            found[day] = maxima
    return found


def fetch_daily_maxes(
    client: KalshiClient,
    *,
    latitude: float,
    longitude: float,
    timezone: str,
    start: date,
    end: date,
    model: str = OPEN_METEO_MODEL,
    chunk_days: int = 120,
) -> dict[date, dict[int, float]]:
    """Previous-run daily highs for one point, chunked so one request stays small.

    ``timezone`` is an IANA name. The hours come back in that zone, so the
    calendar date matches the date in the Kalshi ticker.
    """
    if chunk_days < 1:
        raise ValueError("chunk_days must be >= 1")
    if end < start:
        return {}
    fields = ",".join(LEAD_FIELDS[lead] for lead in sorted(LEAD_FIELDS))
    found: dict[date, dict[int, float]] = {}
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + timedelta(days=chunk_days - 1))
        payload = client.get_json(
            "/forecast",
            {
                "latitude": f"{latitude:.4f}",
                "longitude": f"{longitude:.4f}",
                "hourly": fields,
                "temperature_unit": "fahrenheit",
                "timezone": timezone,
                "start_date": cursor.isoformat(),
                "end_date": stop.isoformat(),
                "models": model,
            },
        )
        for day, maxima in daily_maxes(payload).items():
            found.setdefault(day, {}).update(maxima)
        cursor = stop + timedelta(days=1)
    return found
