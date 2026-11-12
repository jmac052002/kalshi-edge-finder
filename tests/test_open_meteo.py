"""Open-Meteo previous-run highs, from a fixture. No network."""

import json
from datetime import date
from pathlib import Path

from kalshi_edge.open_meteo import daily_maxes

FIXTURE = Path(__file__).parent / "fixtures" / "open_meteo_previous.json"


def test_daily_max_needs_an_afternoon_and_uses_each_lead():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # The fixture day has only five hours, under the minimum, so pad it.
    hours = payload["hourly"]
    extra_times = [f"2026-10-25T{hour:02d}:00" for hour in range(1, 12)]
    hours["time"] = extra_times + hours["time"]
    hours["temperature_2m_previous_day1"] = [55.0] * len(extra_times) + hours["temperature_2m_previous_day1"]
    hours["temperature_2m_previous_day2"] = [54.0] * len(extra_times) + hours["temperature_2m_previous_day2"]
    found = daily_maxes(payload)
    assert found[date(2026, 10, 25)][24] == 68
    assert found[date(2026, 10, 25)][48] == 67
    # October 26 in the fixture has no 00-11 hours besides what we did not add, and
    # the original six hours include the afternoon, but not 12 total. It stays out.
    assert date(2026, 10, 26) not in found


def test_a_night_fragment_is_not_a_daily_high():
    times = [f"2026-10-25T{hour:02d}:00" for hour in range(12)]
    payload = {
        "hourly": {
            "time": times,
            "temperature_2m_previous_day1": [40.0] * 12,
            "temperature_2m_previous_day2": [41.0] * 12,
        }
    }
    assert daily_maxes(payload) == {}


def test_model_suffixed_names_are_read():
    times = [f"2026-10-25T{hour:02d}:00" for hour in range(18)]
    values = [50.0] * 18
    values[15] = 77.0
    payload = {
        "hourly": {
            "time": times,
            "temperature_2m_previous_day1_gfs_seamless": values,
        }
    }
    assert daily_maxes(payload)[date(2026, 10, 25)][24] == 77.0


def test_day_zero_is_read_as_lead_zero_beside_the_previous_day_series():
    times = [f"2026-10-25T{hour:02d}:00" for hour in range(18)]
    same_day = [50.0] * 18
    same_day[15] = 71.0
    day_ahead = [50.0] * 18
    day_ahead[14] = 68.0
    payload = {
        "hourly": {
            "time": times,
            "temperature_2m": same_day,
            "temperature_2m_previous_day1": day_ahead,
        }
    }
    assert daily_maxes(payload)[date(2026, 10, 25)] == {0: 71.0, 24: 68.0}


def test_a_previous_day_series_is_never_mistaken_for_day_zero():
    """``temperature_2m_`` is a prefix of the previous-day names.

    If only a previous-day column came back, filing it under lead 0 would fit
    the same-day error from day-ahead forecasts and nothing would look wrong.
    """
    times = [f"2026-10-25T{hour:02d}:00" for hour in range(18)]
    values = [50.0] * 18
    values[15] = 77.0
    payload = {"hourly": {"time": times, "temperature_2m_previous_day1_gfs_seamless": values}}
    assert daily_maxes(payload)[date(2026, 10, 25)] == {24: 77.0}
    payload["hourly"]["temperature_2m_gfs_seamless"] = [60.0] * 18
    assert daily_maxes(payload)[date(2026, 10, 25)] == {0: 60.0, 24: 77.0}
