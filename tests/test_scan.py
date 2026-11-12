"""The daily scan, against fake HTTP. Nothing here touches the network."""

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from kalshi_edge.fees import series_fee
from kalshi_edge.nowcast import MIN_FINISHED_MONTHS
from kalshi_edge.nws import DailyHigh
from kalshi_edge.odds_math import contracts_for_budget, evaluate
from kalshi_edge.scan import (
    ECON_DISAGREE_GAP,
    ScanRow,
    _apply_econ_guard,
    event_year_month,
    format_chicago,
    format_report,
    report_to_json,
    run,
)
from kalshi_edge.store import MarketStore
from kalshi_edge.weather import assumed_sigma_f, temperature_yes_probability
from kalshi_edge.weather_error import constant_calibration, unfitted_calibration

NOW = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)
GENERATED = "2026-11-11T15:00:00Z"


def _series(ticker, **overrides):
    entry = {
        "ticker": ticker,
        "category": "Economics",
        "frequency": "monthly",
        "tags": [],
        "title": ticker,
        "fee_type": "quadratic",
        "fee_multiplier": 1,
    }
    entry.update(overrides)
    return entry


def _market(
    ticker,
    event,
    *,
    yes_ask="0.4000",
    no_ask="0.7000",
    yes_bid="0.3000",
    no_bid="0.6000",
    strike_type="greater",
    floor=None,
    cap=None,
    subtitle="Above 0.2%",
    rules="increases by more than 0.2% (single-decimal)",
    close="2026-12-10T13:29:00Z",
    expected="2026-12-10T14:56:00Z",
    volume="1000.00",
    oi="500.00",
    ask_size="100.00",
    title=None,
):
    return {
        "ticker": ticker,
        "event_ticker": event,
        "title": title or subtitle,
        "yes_sub_title": subtitle,
        "status": "active",
        "close_time": close,
        "expected_expiration_time": expected,
        "rules_primary": rules,
        "strike_type": strike_type,
        "floor_strike": floor,
        "cap_strike": cap,
        "yes_bid_dollars": yes_bid,
        "yes_ask_dollars": yes_ask,
        "no_bid_dollars": no_bid,
        "no_ask_dollars": no_ask,
        "volume_24h_fp": volume,
        "open_interest_fp": oi,
        "yes_ask_size_fp": ask_size,
        "notional_value_dollars": "1.0000",
    }


class FakeKalshi:
    def __init__(self, series, markets, events):
        self._series = series
        self._markets = markets
        self._events = events
        self.calls = []

    def list_series(self):
        return self._series

    def iter_market_pages(self, *, series_ticker, status=None, limit=200, max_pages=500, **filters):
        self.calls.append({"series_ticker": series_ticker, "status": status, **filters})
        rows = []
        for market in self._markets.get(series_ticker, []):
            max_close = filters.get("max_close_ts")
            if max_close is not None:
                close = datetime.fromisoformat(market["close_time"])
                if close.timestamp() > max_close:
                    continue
            rows.append(market)
        yield rows

    def iter_event_pages(self, *, status="open", limit=200, max_pages=500):
        assert status == "open"
        yield self._events


class FakeForecasts:
    def __init__(self, high):
        self.high = high
        self.calls = []

    def daily_high(self, station_id, market_day, now):
        self.calls.append((station_id, market_day, now))
        if self.high is None:
            return None, f"NWS hourly forecast for {station_id} has no remaining afternoon hour"
        return self.high, ""


def _world(*, yes_ask, no_ask, high):
    rules = (
        "If the maximum temperature recorded at New York City (CLINYC) for Nov 11, 2026, "
        "is between 60-61° fahrenheit according to The Weather Company, then the market resolves to Yes."
    )
    weather = _market(
        "KXHIGHNY-26NOV11-B60.5",
        "KXHIGHNY-26NOV11",
        yes_ask=yes_ask,
        no_ask=no_ask,
        yes_bid="0.0100" if yes_ask == "0.1000" else "0.3000",
        no_bid="0.8000",
        strike_type="between",
        floor=60,
        cap=61,
        subtitle="60° to 61°",
        rules=rules,
        close="2026-11-12T06:00:00Z",
        expected="2026-11-12T20:00:00Z",
        title="Will the high be 60 to 61?",
    )
    # A wide book on purpose when the asks are both rich: spread flag is data, not a bug.
    cpi = _market(
        "KXCPI-26NOV-T0.2",
        "KXCPI-26NOV",
        yes_ask="0.5500",
        no_ask="0.5000",
        floor=0.2,
        subtitle="Above 0.2%",
    )
    payrolls = _market(
        "KXPAYROLLS-26NOV-T80000",
        "KXPAYROLLS-26NOV",
        floor=80000,
        subtitle="Above 80,000",
        rules="If the increase in non-farm payroll employment is above 80000, the market resolves to Yes.",
        close="2026-12-04T13:29:00Z",
        expected="2026-12-04T15:00:00Z",
    )
    soon = _market(
        "KXAIWEEK-26NOV15-T1",
        "KXAIWEEK-26NOV15",
        strike_type="custom",
        subtitle="launch",
        title="Will the model ship this week?",
        rules="Resolves on the company's blog.",
        close="2026-11-15T18:00:00Z",
        expected="2026-11-15T22:00:00Z",
        volume="10.00",
        oi="5.00",
        ask_size="1.00",
        yes_bid="0.0000",
        yes_ask="0.4000",
        no_ask="0.7000",
    )
    # Expected to settle inside the horizon, while the scheduled close is months out.
    # A close-time filter would drop it. The short list must not.
    early_settlement = _market(
        "KXAIWEEK-26NOV16-LATECLOSE",
        "KXAIWEEK-26NOV16",
        strike_type="custom",
        subtitle="late close",
        title="Settles this week, closes next year",
        rules="Resolves on the company's blog.",
        close="2027-06-01T18:00:00Z",
        expected="2026-11-16T18:00:00Z",
        volume="40.00",
        oi="20.00",
    )
    later = _market(
        "KXFINAI-27JUN-T1",
        "KXFINAI-27JUN",
        title="Far AI market",
        close="2027-06-01T18:00:00Z",
        expected="2027-06-01T20:00:00Z",
    )
    series = [
        _series(
            "KXHIGHNY",
            category="Climate and Weather",
            frequency="daily",
            tags=["Daily temperature"],
            title="Highest temperature in NYC",
        ),
        _series("KXCPI", title="CPI"),
        _series("KXPAYROLLS", title="Jobs numbers"),
        _series("KXAIWEEK", category="Science and Technology", frequency="weekly", title="Model launch"),
        _series("KXFINAI", category="Financials", tags=["AI"], title="AI acquisition"),
        _series("KXSPORT", category="Sports", tags=["Football"], title="A game"),
        _series("KXPOLAI", category="Politics", tags=["AI"], title="A hearing"),
    ]
    markets = {
        "KXHIGHNY": [weather],
        "KXCPI": [cpi],
        "KXPAYROLLS": [payrolls],
        "KXAIWEEK": [soon, early_settlement],
        "KXFINAI": [later, soon],  # soon is not this series; the far one is filtered by close time
        "KXSPORT": [_market("KXSPORT-26NOV12-T1", "KXSPORT-26NOV12", title="Sports should not appear")],
        "KXPOLAI": [_market("KXPOLAI-26NOV12-T1", "KXPOLAI-26NOV12", title="Politics should not appear")],
    }
    # The financial series has its own near-dated market plus a far one.
    markets["KXFINAI"] = [
        _market(
            "KXFINAI-26NOV20-T1",
            "KXFINAI-26NOV20",
            title="Will the lab be acquired this month?",
            close="2026-11-20T18:00:00Z",
            expected="2026-11-20T22:00:00Z",
            volume="80.00",
            oi="40.00",
        ),
        later,
    ]
    events = [
        {"series_ticker": ticker, "event_ticker": ticker + "-E", "category": "unused"}
        for ticker in markets
    ]
    return FakeKalshi(series, markets, events), FakeForecasts(high)


def _pinned_calibration():
    """Bias 0 and the old 3-hour width, with the guard wide open.

    The break-even assertions below are about fees, not about the fitted file.
    """
    sigma = assumed_sigma_f(3)
    return constant_calibration(bias_f=0.0, scale_f=sigma, extra_margin=0.0, disagree_gap=1.0)


def _run(tmp_path, *, yes_ask, no_ask, high, calibration=None, month_charts=()):
    client, forecasts = _world(yes_ask=yes_ask, no_ask=no_ask, high=high)
    with MarketStore(tmp_path / "kalshi.db") as store:
        report = run(
            client=client,
            store=store,
            now=NOW,
            generated_at=GENERATED,
            forecasts=forecasts,
            calibration=_pinned_calibration() if calibration is None else calibration,
            month_charts=list(month_charts),
            year_charts=[],
            budget=Decimal(10),
            horizon_days=30,
        )
        fee = store.series_fee("KXHIGHNY")
        snapshots = store.counts()["snapshots"]
    return report, client, forecasts, fee, snapshots


def test_nothing_clears_when_the_ask_is_richer_than_the_model(tmp_path):
    # Both sides are priced above the model, which is the usual book.
    report, client, forecasts, fee, snapshots = _run(
        tmp_path, yes_ask="0.4500", no_ask="0.8000", high=_high()
    )
    text = format_report(report)
    assert "nothing clears costs today" in text.splitlines()
    assert report.nothing_clears
    assert report.clears == []
    assert forecasts.calls[0][0] == "KNYC"
    assert forecasts.calls[0][1].isoformat() == "2026-11-11"
    assert any(row.ticker.startswith("KXHIGHNY") for row in report.modeled)
    sigma = assumed_sigma_f(3)
    expected = temperature_yes_probability(61, sigma, strike_type="between", floor=60, cap=61)
    yes = next(row for row in report.modeled if row.side == "YES")
    assert yes.model == pytest_approx(expected)
    assert yes.clears is False
    assert f"sd {sigma:.1f}F" in yes.why
    assert "bias +0.0F" in yes.why
    assert "KNYC" in yes.why
    with MarketStore(tmp_path / "kalshi.db") as store:
        stored = store.nws_forecasts()
    assert len(stored) == 1
    assert stored[0]["station_id"] == "KNYC"
    assert stored[0]["forecast_high_f"] == 61
    assert stored[0]["market_date"] == "2026-11-11"
    assert yes.settles_chicago == "2026-11-12 14:00 CST"
    # Break-even is the ask plus the taker fee on the $10 order, from the stored schedule.
    assert fee is not None and fee.fee_type == "quadratic"
    ask = Decimal("0.4500")

    def order_fee(count, price=ask):
        return series_fee(price, count, fee_type="quadratic", fee_multiplier="1")

    count = contracts_for_budget(Decimal(10), ask, order_fee)
    priced = evaluate(ask, expected, contracts=count, fee_total=order_fee(count))
    assert yes.contracts == count
    assert yes.break_even == pytest_approx(priced.break_even)
    assert yes.edge == pytest_approx(priced.edge)
    assert snapshots > 0
    # Sports and politics were not fetched. The AI-tagged financial series was.
    fetched = {call["series_ticker"] for call in client.calls}
    assert "KXSPORT" not in fetched
    assert "KXPOLAI" not in fetched
    assert "KXFINAI" in fetched
    assert "KXAIWEEK" in fetched
    tickers = [row.ticker for row in report.short_dated]
    assert tickers == [
        "KXAIWEEK-26NOV15-T1",
        "KXAIWEEK-26NOV16-LATECLOSE",
        "KXFINAI-26NOV20-T1",
    ]
    assert all(call.get("max_close_ts") is None for call in client.calls)
    assert report.short_dated[0].clears is False
    assert "THIN" in report.short_dated[0].flags
    assert "NOBID" in report.short_dated[0].flags
    payrolls = next(event for event in report.econ_events if event.series_ticker == "KXPAYROLLS")
    assert payrolls.within_horizon
    assert payrolls.summary.startswith("price only:")
    assert "no free public consensus" in payrolls.summary
    cpi = next(event for event in report.econ_events if event.series_ticker == "KXCPI")
    assert "price only" in cpi.summary
    assert event_year_month("KXCPI-26NOV") == "2026-11"
    payload = report_to_json(report)
    assert '"nothing_clears": true' in payload


def test_an_edge_is_model_above_the_break_even(tmp_path):
    report, _client, _forecasts, _fee, _snapshots = _run(
        tmp_path, yes_ask="0.0500", no_ask="0.9900", high=_high()
    )
    text = format_report(report)
    assert "nothing clears costs today" not in text.splitlines()
    assert any(row.clears and row.side == "YES" for row in report.clears)
    yes = next(row for row in report.clears if row.side == "YES")
    assert yes.model > yes.break_even
    assert "edges that clear costs" in text


def test_no_forecast_means_no_weather_edge(tmp_path):
    report, _client, _forecasts, _fee, _snapshots = _run(
        tmp_path, yes_ask="0.0500", no_ask="0.0500", high=None
    )
    assert report.modeled == []
    assert report.nothing_clears
    assert any("no remaining afternoon" in note for note in report.notes)
    assert "nothing clears costs today" in format_report(report).splitlines()


def test_a_large_weather_gap_is_not_ranked(tmp_path):
    sigma = assumed_sigma_f(3)
    calibration = constant_calibration(
        bias_f=0.0, scale_f=sigma, extra_margin=0.03, disagree_gap=0.15
    )
    report, _client, _forecasts, _fee, _snapshots = _run(
        tmp_path, yes_ask="0.0500", no_ask="0.9900", high=_high(), calibration=calibration
    )
    yes = next(row for row in report.modeled if row.side == "YES")
    assert yes.clears is False
    assert "VERIFY" in yes.flags
    assert "model disagrees strongly, verify" in yes.why
    assert report.clears == []
    text = format_report(report)
    assert "model disagrees strongly, verify" in text
    assert "nothing clears costs today" in text.splitlines()


def test_unfitted_weather_is_not_an_edge(tmp_path):
    report, _client, _forecasts, _fee, _snapshots = _run(
        tmp_path,
        yes_ask="0.0500",
        no_ask="0.9900",
        high=_high(),
        calibration=unfitted_calibration(),
    )
    yes = next(row for row in report.modeled if row.side == "YES")
    assert yes.clears is False
    assert "VERIFY" in yes.flags
    assert "assumed sd" in yes.why
    assert "not ranked as an edge" in yes.why
    assert report.nothing_clears


def test_extra_margin_blocks_a_tiny_weather_edge(tmp_path):
    sigma = assumed_sigma_f(3)
    probability = temperature_yes_probability(61, sigma, strike_type="between", floor=60, cap=61)
    # Far enough under the model to beat the fee, and inside the extra margin.
    ask = f"{max(0.01, probability - 0.04):.4f}"
    calibration = constant_calibration(
        bias_f=0.0, scale_f=sigma, extra_margin=0.03, disagree_gap=0.50
    )
    report, _client, _forecasts, _fee, _snapshots = _run(
        tmp_path, yes_ask=ask, no_ask="0.9900", high=_high(), calibration=calibration
    )
    yes = next(row for row in report.modeled if row.side == "YES")
    assert yes.model > yes.break_even
    assert yes.clears is False
    assert "VERIFY" not in yes.flags


def _nowcast_chart(month, now_values, actual):
    """One month of the Cleveland Fed file: a nowcast path and, once the month is done, the print."""
    labels = ["11/10" for _ in now_values] + ([f"CPI {month}"] if actual is not None else [])
    now_data = [{"value": f"{value:.4f}"} for value in now_values]
    actual_data = [{"value": ""} for _ in now_values]
    if actual is not None:
        now_data.append({"value": ""})
        actual_data.append({"value": f"{actual:.4f}"})
    return {
        "chart": {"subcaption": month, "_comment": "2026-11-10 00:00"},
        "categories": [{"category": [{"label": label} for label in labels]}],
        "dataset": [
            {"seriesname": "CPI Inflation", "data": now_data},
            {"seriesname": "Core CPI Inflation", "data": now_data},
            {"seriesname": "Actual CPI Inflation", "data": actual_data},
            {"seriesname": "Actual Core CPI Inflation", "data": actual_data},
        ],
    }


def _month_charts(nowcast):
    """Enough finished months for a model, each nowcast 0.10 off, then the open month."""
    finished = [
        _nowcast_chart(f"{2024 + index // 12}-{index % 12 + 1:02d}", [0.30 + 0.10 * (-1) ** index, 0.30], 0.30)
        for index in range(MIN_FINISHED_MONTHS)
    ]
    return [*finished, _nowcast_chart("2026-11", [nowcast], None)]


def _econ_row(model, ask, *, flags=(), clears=True, why="nowcast", break_even=None, edge=None):
    return ScanRow(
        ticker="KXCPI-26NOV-T0.2",
        title="CPI: Above 0.2%",
        side="YES",
        ask=ask,
        model=model,
        break_even=break_even,
        edge=edge,
        contracts=10,
        settles_at="2026-12-10T14:56:00Z",
        settles_chicago="2026-12-10 08:56 CST",
        flags=flags,
        why=why,
        clears=clears,
        series_ticker="KXCPI",
        event_ticker="KXCPI-26NOV",
    )


def test_the_cpi_guard_flags_a_gap_at_or_past_the_threshold_and_leaves_the_rest_alone():
    under = _econ_row(0.36, "0.2200")
    assert _apply_econ_guard(under) is under  # 14 points: ranked as before
    at = _apply_econ_guard(_econ_row(0.22 + ECON_DISAGREE_GAP, "0.2200"))
    assert at.clears is False and at.flags == ("VERIFY",)
    over = _apply_econ_guard(_econ_row(0.608, "0.2200", flags=("THIN",), break_even=0.232, edge=0.376))
    assert over.clears is False and over.flags == ("THIN", "VERIFY")
    assert over.why == "nowcast model disagrees strongly, verify"
    assert (over.model, over.edge, over.break_even) == (0.608, 0.376, 0.232)  # the numbers are left as they were
    assert _apply_econ_guard(over) == over  # applying it twice adds nothing
    # A model far *below* the ask is not this guard's business: that side was never an edge.
    cheap = _econ_row(0.10, "0.6000", clears=False)
    assert _apply_econ_guard(cheap) is cheap
    for odd in (_econ_row(None, "0.2200"), _econ_row(0.9, None), _econ_row(0.9, "n/a"), _econ_row(float("nan"), "0.2")):
        assert _apply_econ_guard(odd) is odd


def test_a_large_cpi_gap_is_listed_for_a_look_and_not_ranked(tmp_path):
    """The nowcast says a 0.3 print is near certain; the book asks 55 cents for it."""
    report, _client, _forecasts, _fee, _snapshots = _run(
        tmp_path, yes_ask="0.9900", no_ask="0.9900", high=None, month_charts=_month_charts(0.60)
    )
    yes = next(row for row in report.modeled if row.series_ticker == "KXCPI" and row.side == "YES")
    assert yes.model > 0.99 and yes.ask == "0.5500"
    assert yes.clears is False and "VERIFY" in yes.flags
    assert "model disagrees strongly, verify" in yes.why
    assert yes in report.verify and yes not in report.clears
    text = format_report(report)
    assert "model disagrees strongly, verify (1), not ranked as edges:" in text
    assert any("CPI gap" in caveat for caveat in json.loads(report_to_json(report))["caveats"])


def test_a_modest_cpi_gap_still_clears(tmp_path):
    """A nowcast of 0.27 with the file's 0.10 error: about 58% for a 0.3 print, against a 55 cent ask."""
    report, _client, _forecasts, _fee, _snapshots = _run(
        tmp_path, yes_ask="0.9900", no_ask="0.9900", high=None, month_charts=_month_charts(0.27)
    )
    yes = next(row for row in report.modeled if row.series_ticker == "KXCPI" and row.side == "YES")
    assert 0 < yes.model - 0.55 < ECON_DISAGREE_GAP
    assert "VERIFY" not in yes.flags
    assert yes.clears is (yes.model > yes.break_even)


def test_chicago_settlement_time():
    moment = datetime(2026, 11, 12, 20, 0, tzinfo=UTC)
    assert format_chicago(moment) == "2026-11-12 14:00 CST"


def _high():
    return DailyHigh(
        temperature_f=61,
        at=datetime(2026, 11, 11, 14, 0, tzinfo=timezone(timedelta(hours=-5))),
        lead_hours=3,
        station_name="Central Park",
    )


def pytest_approx(value):
    import pytest

    return pytest.approx(value)
