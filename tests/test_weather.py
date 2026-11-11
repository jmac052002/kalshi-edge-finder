"""Temperature brackets, the assumed error, and the station id in the rules."""

import math

import pytest

from kalshi_edge.weather import (
    ASSUMED_MAX_TEMP_MAE_F,
    assumed_sigma_f,
    cli_code,
    event_date,
    is_daily_high_series,
    nws_station_id,
    settlement_place,
    temperature_yes_probability,
)


def test_integer_brackets_partition_the_line():
    strikes = [
        ("less", None, 59),
        ("between", 59, 60),
        ("between", 61, 62),
        ("between", 63, 64),
        ("between", 65, 66),
        ("greater", 66, None),
    ]
    probabilities = [
        temperature_yes_probability(61.0, 3.0, strike_type=kind, floor=floor, cap=cap)
        for kind, floor, cap in strikes
    ]
    assert all(p is not None for p in probabilities)
    assert sum(probabilities) == pytest.approx(1.0)
    # A forecast of 61F is mostly the 61-62 bracket, not the tails.
    assert probabilities[2] > probabilities[0]
    assert probabilities[2] > probabilities[5]


def test_greater_than_66_is_67_or_above():
    # Mean far above 67: the "greater than 66" contract is almost sure.
    high = temperature_yes_probability(80, 1.0, strike_type="greater", floor=66, cap=None)
    low = temperature_yes_probability(50, 1.0, strike_type="greater", floor=66, cap=None)
    assert high == pytest.approx(1.0, abs=1e-6)
    assert low == pytest.approx(0.0, abs=1e-6)


def test_fractional_strike_is_not_priced():
    assert temperature_yes_probability(70, 2, strike_type="between", floor=70.5, cap=71.5) is None
    assert temperature_yes_probability(70, 2, strike_type="custom", floor=70, cap=71) is None


def test_assumed_sigma_grows_with_lead_and_is_the_documented_table():
    short = assumed_sigma_f(6)
    day = assumed_sigma_f(24)
    long = assumed_sigma_f(120)
    assert short < day < long
    # 6 hours uses the 12-hour MAE. sd = sqrt((MAE * sqrt(pi/2))^2 + 0.5^2).
    mae = ASSUMED_MAX_TEMP_MAE_F[0][1]
    forecast_sd = mae * math.sqrt(math.pi / 2.0)
    assert short == pytest.approx(math.sqrt(forecast_sd**2 + 0.5**2))
    with pytest.raises(ValueError):
        assumed_sigma_f(-1)


def test_station_comes_from_the_rules_not_the_title():
    rules = (
        "If the maximum temperature recorded at Louisville (CLISDF) for Nov 11, 2026, "
        "is greater than 72, the market resolves to Yes."
    )
    assert cli_code(rules) == "CLISDF"
    assert nws_station_id(rules) == "KSDF"
    assert settlement_place(rules) == "Louisville"
    dated = (
        "If the highest temperature recorded at Atlanta for Aug 13, 2026, is greater than 99, "
        "according to the National Weather Service's Climatological Report (Daily), the market resolves to Yes."
    )
    assert settlement_place(dated) == "Atlanta"
    assert nws_station_id("no station here") is None


def test_event_date_needs_a_day():
    assert event_date("KXHIGHNY-26NOV11") == __import__("datetime").date(2026, 11, 11)
    assert event_date("KXCPI-26NOV") is None
    assert event_date("KXHIGHNY-26NOV99") is None


def test_daily_high_series_filter():
    yes = {
        "ticker": "KXHIGHNY",
        "category": "Climate and Weather",
        "frequency": "daily",
        "tags": ["Daily temperature"],
    }
    assert is_daily_high_series(yes)
    assert not is_daily_high_series({**yes, "ticker": "HIGHNY"})
    assert not is_daily_high_series({**yes, "frequency": "hourly"})
    assert not is_daily_high_series({**yes, "category": "Sports"})
    assert not is_daily_high_series({**yes, "tags": ["Snow and rain"]})
