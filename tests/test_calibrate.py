"""Backtest wiring. The settled row's own last trade must not be the forecast."""

from datetime import datetime, timedelta, timezone

import pytest

from kalshi_edge.calibrate import calibrate_series, main
from kalshi_edge.client import KalshiAPIError
from kalshi_edge.parse import parse_market, parse_snapshot
from kalshi_edge.store import MarketStore

CLOSE = "2026-11-08T17:35:30Z"
CUTOFF = datetime(2026, 11, 8, 14, 35, 30, tzinfo=timezone.utc)


def _ts(when: datetime) -> int:
    return int(when.timestamp())


def _market(store, ticker="MKT-1", result="yes", settlement="1.0000", source="live", event="EVT-1"):
    market = parse_market(
        {
            "ticker": ticker,
            "event_ticker": event,
            "title": ticker,
            "yes_sub_title": "Side",
            "status": "finalized",
            "result": result,
            "settlement_value_dollars": settlement,
            "close_time": CLOSE,
            # Post-outcome book. Using this as the forecast would fake a perfect score.
            "last_price_dollars": "0.9900" if result == "yes" else "0.0100",
            "yes_bid_dollars": "0.0000",
            "yes_ask_dollars": "1.0000",
        },
        series_ticker="KXNFLGAME",
        source=source,
        fetched_at="2026-11-08T18:00:00Z",
    )
    store.save_page([market], [])


class FakeClient:
    def __init__(self, candles, trades=None):
        self.candles = candles
        self.trades = trades or []
        self.candle_calls = 0
        self.trade_calls = 0
        self.paths = []

    def get_series(self, series_ticker):
        assert series_ticker == "KXNFLGAME"
        return {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}

    def get_candlesticks(self, **kwargs):
        self.candle_calls += 1
        self.paths.append(kwargs["historical"])
        # The requested window must end at the cutoff, not at settlement.
        assert kwargs["end_ts"] <= _ts(CUTOFF)
        return self.candles

    def get_trades(self, **kwargs):
        self.trade_calls += 1
        assert kwargs["max_ts"] <= _ts(CUTOFF)
        return self.trades

    def close(self):
        pass


def _candle(when, bid, ask, trade):
    return {
        "end_period_ts": _ts(when),
        "yes_bid": {"close_dollars": bid},
        "yes_ask": {"close_dollars": ask},
        "price": {"close_dollars": trade},
    }


@pytest.fixture
def store(tmp_path):
    db = MarketStore(tmp_path / "kalshi.db")
    try:
        yield db
    finally:
        db.close()


def test_uses_pre_cutoff_candle_not_the_settled_last_price(store):
    _market(store)
    _market(store, ticker="MKT-TIE", result="scalar", settlement="0.5000", event="EVT-TIE")
    client = FakeClient(
        [
            _candle(CUTOFF + timedelta(hours=2), "0.9700", "0.9800", "0.9800"),
            _candle(CUTOFF - timedelta(minutes=35), "0.3900", "0.4100", "0.4000"),
        ]
    )
    report = calibrate_series(
        series="KXNFLGAME",
        store=store,
        client=client,
        lead_minutes=180,
        lookback_minutes=360,
        period_minutes=60,
        max_spread=0.10,
        bucket_width=0.10,
        refresh=False,
        fetch=True,
    )
    assert report.n_settled == 2
    assert report.n_excluded == 1
    assert report.n_used == 1
    assert report.n_from_candlestick == 1
    # (0.40 - 1)^2. The settled last trade of 0.99 would have scored about 0.
    assert report.brier == pytest.approx(0.36)
    row = store.conn.execute("SELECT price, quote_source, buy_ask FROM reference_quotes").fetchone()
    assert row["quote_source"] == "candlestick"
    assert row["price"] == pytest.approx(0.40)
    assert row["buy_ask"] == pytest.approx(0.41)
    assert client.trade_calls == 0

    again = FakeClient(candles=[])
    second = calibrate_series(
        series="KXNFLGAME",
        store=store,
        client=again,
        lead_minutes=180,
        lookback_minutes=360,
        period_minutes=60,
        max_spread=0.10,
        bucket_width=0.10,
        refresh=False,
        fetch=True,
    )
    assert again.candle_calls == 0
    assert second.brier == pytest.approx(0.36)


def test_snapshot_inside_the_window_skips_the_api(store):
    _market(store)
    snapshot = parse_snapshot(
        {
            "ticker": "MKT-1",
            "status": "active",
            "yes_bid_dollars": "0.5000",
            "yes_ask_dollars": "0.5200",
            "last_price_dollars": "0.5100",
        },
        series_ticker="KXNFLGAME",
        captured_at="2026-11-08T14:00:00Z",
    )
    market = parse_market(
        {
            "ticker": "MKT-1",
            "event_ticker": "EVT-1",
            "result": "yes",
            "status": "finalized",
            "settlement_value_dollars": "1.0000",
            "close_time": CLOSE,
        },
        series_ticker="KXNFLGAME",
        source="live",
        fetched_at="t",
    )
    store.save_page([market], [snapshot])
    client = FakeClient([_candle(CUTOFF - timedelta(hours=1), "0.1000", "0.1100", "0.1000")])
    report = calibrate_series(
        series="KXNFLGAME",
        store=store,
        client=client,
        lead_minutes=180,
        lookback_minutes=360,
        period_minutes=60,
        max_spread=0.10,
        bucket_width=0.10,
        refresh=False,
        fetch=True,
    )
    assert client.candle_calls == 0
    assert report.n_from_snapshot == 1
    assert report.buckets[5].avg_price == pytest.approx(0.51)


def test_snapshot_after_the_cutoff_is_ignored(store):
    market = parse_market(
        {
            "ticker": "MKT-1",
            "event_ticker": "EVT-1",
            "result": "no",
            "status": "finalized",
            "settlement_value_dollars": "0.0000",
            "close_time": CLOSE,
            "last_price_dollars": "0.0100",
        },
        series_ticker="KXNFLGAME",
        source="historical",
        fetched_at="t",
    )
    late = parse_snapshot(
        {
            "ticker": "MKT-1",
            "yes_bid_dollars": "0.0100",
            "yes_ask_dollars": "0.0200",
            "last_price_dollars": "0.0100",
        },
        series_ticker="KXNFLGAME",
        captured_at="2026-11-08T16:00:00Z",
    )
    store.save_page([market], [late])
    client = FakeClient(
        [],
        trades=[{"created_time": "2026-11-08T14:20:00Z", "yes_price_dollars": "0.3300"}],
    )
    report = calibrate_series(
        series="KXNFLGAME",
        store=store,
        client=client,
        lead_minutes=180,
        lookback_minutes=360,
        period_minutes=60,
        max_spread=0.10,
        bucket_width=0.10,
        refresh=False,
        fetch=True,
    )
    assert client.paths == [True]
    assert report.n_from_trade == 1
    assert report.brier == pytest.approx(0.33**2)
    assert report.buckets[3].n == 1
    assert report.buckets[3].avg_roi is None


def test_main_prints_the_table(tmp_path, monkeypatch, capsys):
    db = tmp_path / "kalshi.db"
    with MarketStore(db) as store:
        _market(store)

    class Client(FakeClient):
        def __init__(self, **kwargs):
            super().__init__(
                [_candle(CUTOFF - timedelta(minutes=35), "0.3900", "0.4100", "0.4000")]
            )

    monkeypatch.setattr("kalshi_edge.calibrate.KalshiClient", Client)
    code = main(
        ["--series", "KXNFLGAME", "--db", str(db), "--no-fetch"]
    )
    # --no-fetch has no candle, so the contract is unused. Run with fetch.
    assert code == 0
    code = main(["--series", "KXNFLGAME", "--db", str(db)])
    assert code == 0
    output = capsys.readouterr().out
    assert "brier: 0.3600" in output
    assert "30-40%" in output or "40-50%" in output
    # Mid of 0.39 and 0.41 is 0.40, which is the 40-50 bucket.
    assert "40-50%" in output


def test_main_reports_api_failure(tmp_path, monkeypatch):
    db = tmp_path / "kalshi.db"
    with MarketStore(db) as store:
        _market(store)

    class Boom:
        def __init__(self, **kwargs):
            pass

        def get_series(self, series):
            return {"fee_multiplier": 1, "fee_type": "quadratic"}

        def get_candlesticks(self, **kwargs):
            raise KalshiAPIError("down", status_code=503)

        def get_trades(self, **kwargs):
            raise AssertionError("should not fall through a 503")

        def close(self):
            pass

    monkeypatch.setattr("kalshi_edge.calibrate.KalshiClient", Boom)
    assert main(["--series", "KXNFLGAME", "--db", str(db)]) == 1
