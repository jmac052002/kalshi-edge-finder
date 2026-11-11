"""Settled-high pairing and which forecast the fit is allowed to train on."""

from datetime import UTC, date, datetime

from kalshi_edge.weather import settled_high
from kalshi_edge.weather_calibrate import (
    SettledDay,
    collapse_days,
    gfs_pairs_for,
    nws_pairs_from_rows,
    select_training_pairs,
)
from kalshi_edge.weather_error import MIN_POOLED_N, ErrorPair


def test_expiration_value_beats_the_winning_bin():
    markets = [
        {
            "strike_type": "between",
            "floor_strike": 64,
            "cap_strike": 65,
            "result": "yes",
            "expiration_value": "64.00",
        },
        {"strike_type": "greater", "floor_strike": 65, "cap_strike": None, "result": "no", "expiration_value": "64.00"},
    ]
    assert settled_high(markets) == (64.0, True)


def test_a_blank_expiration_uses_the_winning_bin_midpoint():
    markets = [
        {"strike_type": "between", "floor_strike": 36, "cap_strike": 37, "result": "yes", "expiration_value": ""},
        {"strike_type": "less", "floor_strike": None, "cap_strike": 36, "result": "no", "expiration_value": ""},
    ]
    assert settled_high(markets) == (36.5, False)


def test_a_winning_tail_without_a_print_is_not_a_temperature():
    markets = [
        {"strike_type": "greater", "floor_strike": 90, "cap_strike": None, "result": "yes", "expiration_value": ""},
        {"strike_type": "between", "floor_strike": 88, "cap_strike": 89, "result": "no", "expiration_value": ""},
    ]
    assert settled_high(markets) is None


def test_disagreeing_prints_are_dropped():
    markets = [
        {"expiration_value": "70.00", "result": "yes", "strike_type": "between", "floor_strike": 70, "cap_strike": 71},
        {"expiration_value": "80.00", "result": "no", "strike_type": "greater", "floor_strike": 71, "cap_strike": None},
    ]
    assert settled_high(markets) is None


def test_collapse_keeps_one_high_and_drops_a_conflict():
    shared = {"series_ticker": "KXHIGHNY", "station_id": "KNYC", "place": "New York", "close_time": None}
    first = SettledDay(event_ticker="A", market_date=date(2026, 1, 1), high_f=70, exact=True, **shared)
    later = SettledDay(
        event_ticker="B",
        market_date=date(2026, 1, 1),
        high_f=70,
        exact=True,
        close_time=datetime(2026, 1, 2, tzinfo=UTC),
        series_ticker="KXHIGHNY",
        station_id="KNYC",
        place="New York",
    )
    kept, conflicts = collapse_days([first, later])
    assert conflicts == 0
    assert [day.event_ticker for day in kept] == ["B"]
    clash = SettledDay(event_ticker="C", market_date=date(2026, 1, 2), high_f=80, exact=True, **shared)
    other = SettledDay(event_ticker="D", market_date=date(2026, 1, 2), high_f=60, exact=True, **shared)
    kept, conflicts = collapse_days([clash, other])
    assert conflicts == 1
    assert kept == []


def test_gfs_pairs_and_stored_nws_leads():
    day = SettledDay(
        event_ticker="KXHIGHNY-26OCT25",
        series_ticker="KXHIGHNY",
        station_id="KNYC",
        market_date=date(2026, 10, 25),
        high_f=64,
        exact=True,
        place="New York City",
        close_time=None,
    )
    pairs = gfs_pairs_for([day], {("KNYC", date(2026, 10, 25)): {24: 66.0, 48: 68.0}})
    assert {(pair.lead_hours, pair.error_f) for pair in pairs} == {(24, -2.0), (48, -4.0)}
    rows = [
        {"event_ticker": day.event_ticker, "lead_hours": 3, "forecast_high_f": 64},
        {"event_ticker": day.event_ticker, "lead_hours": 17, "forecast_high_f": 62},
        {"event_ticker": day.event_ticker, "lead_hours": 34, "forecast_high_f": 61},
    ]
    nws = nws_pairs_from_rows(rows, [day])
    assert len(nws) == 1
    assert nws[0].lead_hours == 24
    # 17h is closer to the 24h bucket than 34h. The 3h row is too late to train on.
    assert nws[0].forecast_f == 62


def test_stored_nws_replaces_gfs_only_when_the_sample_is_large_enough():
    gfs = [
        ErrorPair("KNYC", 24, 70, 71, market_date=date(2024, 1, 1))
        for _ in range(MIN_POOLED_N)
    ]
    short_nws = gfs[:3]
    chosen, label, _note = select_training_pairs(gfs, short_nws)
    assert chosen is gfs
    assert "Open-Meteo" in label
    long_nws = [
        ErrorPair("KNYC", 24, 70, 72, market_date=date(2024, 6, 1))
        for _ in range(MIN_POOLED_N)
    ]
    chosen, label, _note = select_training_pairs(gfs, long_nws)
    assert chosen is long_nws
    assert label.startswith("NWS")
