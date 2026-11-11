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
