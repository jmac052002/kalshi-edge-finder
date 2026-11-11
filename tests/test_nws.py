"""Hourly-forecast parsing. No requests leave the process."""

from datetime import UTC, datetime, timedelta, timezone

from kalshi_edge.nws import daily_high, forecast_covers_daily_high, parse_hourly, station_location


def _hour(local: str, temperature: int) -> dict:
    return {"startTime": local, "temperature": temperature, "temperatureUnit": "F"}


def test_daily_high_is_the_max_on_the_market_date():
    payload = {
        "properties": {
            "periods": [
                _hour("2026-11-11T08:00:00-05:00", 49),
                _hour("2026-11-11T14:00:00-05:00", 61),
                _hour("2026-11-11T15:00:00-05:00", 61),
                _hour("2026-11-11T20:00:00-05:00", 52),
                _hour("2026-11-12T14:00:00-05:00", 70),
            ]
        }
    }
    hours = parse_hourly(payload)
    now = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)  # 10:00 in the -05:00 zone
    found = daily_high(hours, market_day=hours[0].start.date(), now=now, station_name="Central Park")
    assert found is not None
    assert found.temperature_f == 61
    # The earliest hour of the tied maximum sets the lead.
    assert found.at.hour == 14
    assert found.lead_hours == 4
    assert found.station_name == "Central Park"


def test_no_high_once_the_afternoon_has_left_the_forecast():
    payload = {
        "properties": {
            "periods": [
                _hour("2026-11-11T19:00:00-05:00", 52),
                _hour("2026-11-11T21:00:00-05:00", 50),
            ]
        }
    }
    hours = parse_hourly(payload)
    now = datetime(2026, 11, 11, 23, 0, tzinfo=UTC)
    assert daily_high(hours, market_day=hours[0].start.date(), now=now) is None


def test_celsius_hours_are_converted():
    payload = {"properties": {"periods": [{"startTime": "2026-11-11T14:00:00-05:00", "temperature": 0, "temperatureUnit": "C"}]}}
    hours = parse_hourly(payload)
    assert hours[0].temperature_f == 32


def test_station_location_reads_geojson():
    payload = {
        "geometry": {"coordinates": [-73.96667, 40.78333]},
        "properties": {"name": "New York City, Central Park"},
    }
    assert station_location(payload) == (40.78333, -73.96667, "New York City, Central Park")
    assert station_location({}) is None


def test_falling_afternoon_fragment_is_not_the_daily_high():
    # 3pm local, and the grid only has 3pm onward. Noon may already have been
    # the high; the 65F still in the grid is not the calendar-day maximum.
    payload = {
        "properties": {
            "periods": [
                _hour("2026-11-11T15:00:00-05:00", 65),
                _hour("2026-11-11T16:00:00-05:00", 63),
                _hour("2026-11-11T17:00:00-05:00", 60),
            ]
        }
    }
    hours = parse_hourly(payload)
    now = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)  # 15:00 in the -05:00 zone
    assert forecast_covers_daily_high(hours, hours[0].start.date(), now) is False
    assert daily_high(hours, market_day=hours[0].start.date(), now=now) is None


def test_afternoon_already_past_is_not_a_forecast():
    start = datetime(2026, 11, 11, 14, 0, tzinfo=timezone(timedelta(hours=-5)))
    hours = parse_hourly(
        {"properties": {"periods": [{"startTime": start.isoformat(), "temperature": 61, "temperatureUnit": "F"}]}}
    )
    now = start + timedelta(hours=5)
    assert daily_high(hours, market_day=start.date(), now=now) is None
