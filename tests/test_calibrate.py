"""Calibration backtest tests. Candlesticks come from a fixture; no network."""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kalshi_edge.calibrate import (
    Observation,
    Report,
    brier_score,
    bucket_edges,
    bucket_observations,
    build_observations,
    event_cutoff,
    fetch_pre_event_prices,
    format_report,
    main,
    parse_timestamp,
    pick_pre_event_candle,
    report_to_json,
    run,
    side_roi,
    wilson_interval,
    write_reliability_diagram,
)
from kalshi_edge.client import KalshiAPIError
from kalshi_edge.parse import parse_candles, parse_market
from kalshi_edge.store import MarketStore

FIXTURES = Path(__file__).with_name("fixtures")
CANDLES = json.loads((FIXTURES / "candlesticks.json").read_text())["candlesticks"]
CINATL = json.loads((FIXTURES / "candlesticks_cinatl.json").read_text())["candlesticks"]
# Kalshi's occurrence_datetime is the scheduled END of the game. With the
# default --event-hours 4 the cutoff lands at 18:00Z, which the fixture
# candles are arranged around.
EVENT_END = "2026-11-08T22:00:00Z"
KICKOFF_TS = int(datetime(2026, 11, 8, 18, tzinfo=UTC).timestamp())
NOW = "2026-11-09T12:00:00Z"


def _settled(ticker, event, result, **overrides):
    payload = {
        "ticker": ticker,
        "event_ticker": event,
        "status": "finalized",
        "result": result,
        "settlement_value_dollars": "1.0000" if result == "yes" else "0.0000",
        "last_price_dollars": "0.9900" if result == "yes" else "0.0100",
        "occurrence_datetime": EVENT_END,
        "expected_expiration_time": EVENT_END,
        "close_time": "2026-11-08T22:05:30Z",
    }
    payload.update(overrides)
    return parse_market(payload, series_ticker="KXNFLGAME", source="historical", fetched_at=NOW)


class FakeClient:
    """Serves one candlestick payload per ticker and records every request."""

    def __init__(self, by_ticker):
        self.by_ticker = by_ticker
        self.calls = []

    def get_candlesticks(self, *, series_ticker, ticker, start_ts, end_ts, period_interval=60):
        self.calls.append(
            {"ticker": ticker, "start_ts": start_ts, "end_ts": end_ts, "period": period_interval}
        )
        payload = self.by_ticker.get(ticker)
        if payload is None:
            raise KalshiAPIError("404 from candlesticks", status_code=404)
        if isinstance(payload, Exception):
            raise payload
        return payload

    def close(self):
        pass


@pytest.fixture
def store(tmp_path):
    with MarketStore(tmp_path / "kalshi.db") as s:
        yield s


# --- cutoff -----------------------------------------------------------------


def test_parse_timestamp_handles_api_formats():
    KICKOFF = "2026-11-08T18:00:00Z"
    assert parse_timestamp(KICKOFF) == KICKOFF_TS
    assert parse_timestamp("2026-11-08T18:00:00.123456Z") == KICKOFF_TS
    assert parse_timestamp("2026-11-08T13:00:00-05:00") == KICKOFF_TS
    assert parse_timestamp("") is None
    assert parse_timestamp(None) is None
    assert parse_timestamp("yesterday") is None


def test_cutoff_treats_every_timestamp_as_the_event_end():
    market = {
        "occurrence_datetime": EVENT_END,
        "expected_expiration_time": "2026-11-09T01:00:00Z",
        "close_time": "2026-11-08T23:00:00Z",
    }
    assert event_cutoff(market, event_hours=4) == (KICKOFF_TS, "occurrence_datetime")

    market["occurrence_datetime"] = None
    assert event_cutoff(market, event_hours=7) == (KICKOFF_TS, "expected_expiration_time")

    market["expected_expiration_time"] = ""
    assert event_cutoff(market, event_hours=5) == (KICKOFF_TS, "close_time")

    market["close_time"] = None
    assert event_cutoff(market, event_hours=4) is None


def test_event_hours_zero_takes_the_timestamp_as_is():
    market = {
        "occurrence_datetime": "2026-11-08T18:00:00Z",
        "expected_expiration_time": None,
        "close_time": None,
    }
    assert event_cutoff(market, event_hours=0) == (KICKOFF_TS, "occurrence_datetime")


def test_cutoff_uses_the_earlier_of_occurrence_and_expiration():
    # KXNFLGAME-26OCT11PHIJAC: the game ran long, occurrence_datetime was
    # rewritten to the final whistle, expected_expiration_time kept the schedule.
    market = {
        "occurrence_datetime": "2026-10-11T17:13:07Z",
        "expected_expiration_time": "2026-10-11T16:30:00Z",
        "close_time": "2026-10-11T17:16:07Z",
    }
    cutoff, source = event_cutoff(market, event_hours=4)
    assert source == "expected_expiration_time"
    assert cutoff == parse_timestamp("2026-10-11T12:30:00Z")


def test_lead_hours_moves_cutoff_earlier():
    market = {"occurrence_datetime": EVENT_END, "expected_expiration_time": None, "close_time": None}
    assert event_cutoff(market, event_hours=4, lead_hours=1.5) == (KICKOFF_TS - 5400, "occurrence_datetime")
    with pytest.raises(ValueError):
        event_cutoff(market, event_hours=-1)


def test_default_cutoff_does_not_read_in_game_prices():
    """Regression for the first live run: occurrence_datetime is the game's end.

    Real hourly closes of KXNFLGAME-26NOV08CINATL-CIN: 0.62 before kickoff
    (14:30Z), 0.93 and 0.98 while the game was being decided, and
    occurrence_datetime 17:30Z. Reading the last bar before that timestamp
    gave a Brier of 0.11 across the series; no pre-game market does that.
    """
    market = {
        "occurrence_datetime": "2026-11-08T17:30:00Z",
        "expected_expiration_time": "2026-11-08T17:30:00Z",
        "close_time": "2026-11-08T17:35:30Z",
    }
    candles = parse_candles(CINATL)
    cutoff_ts, _ = event_cutoff(market, event_hours=4)
    chosen = pick_pre_event_candle(candles, cutoff_ts)
    assert chosen is not None and chosen.last_price == 0.62
    assert datetime.fromtimestamp(chosen.end_ts, UTC).hour == 13

    # The bug, kept as a demonstration: event_hours 0 reads the 17:00Z bar.
    unshifted, _ = event_cutoff(market, event_hours=0)
    assert pick_pre_event_candle(candles, unshifted).last_price == 0.98


# --- candle selection -------------------------------------------------------


def test_picks_last_two_sided_candle_before_cutoff():
    candles = parse_candles(CANDLES)
    chosen = pick_pre_event_candle(candles, KICKOFF_TS)
    # 17:00 candle is one-sided, 19:00 is after kickoff; 16:00 is the answer.
    assert chosen is not None
    assert chosen.end_ts == KICKOFF_TS - 2 * 3600
    assert (chosen.yes_bid, chosen.yes_ask) == (0.42, 0.46)


def test_candle_ending_exactly_at_cutoff_is_allowed_and_later_ones_are_not():
    candles = parse_candles(CANDLES)
    at_cutoff = pick_pre_event_candle(candles, KICKOFF_TS + 3600)
    assert at_cutoff is not None and at_cutoff.end_ts == KICKOFF_TS + 3600
    assert at_cutoff.yes_bid == 0.95  # the post-kickoff spike, only when the cutoff allows it
    assert pick_pre_event_candle(candles, KICKOFF_TS - 4 * 3600) is None


# --- fetching and caching ---------------------------------------------------


def test_fetch_caches_quotes_and_misses(store):
    cin = _settled("KXNFLGAME-26NOV08CINATL-CIN", "KXNFLGAME-26NOV08CINATL", "yes")
    atl = _settled("KXNFLGAME-26NOV08CINATL-ATL", "KXNFLGAME-26NOV08CINATL", "no")
    store.save_page([cin, atl], [])
    client = FakeClient({cin.ticker: CANDLES, atl.ticker: []})

    prices, stats = fetch_pre_event_prices(
        store=store, client=client, series_ticker="KXNFLGAME",
        markets=store.settled_markets("KXNFLGAME"), now=NOW, lookback_hours=72,
    )
    assert stats.fetched == 2 and stats.cached == 0 and stats.no_candle == 1
    assert stats.not_found == 0 and not prices[atl.ticker].history_missing
    assert prices[cin.ticker].yes_ask == 0.46
    assert prices[cin.ticker].cutoff_source == "occurrence_datetime"
    assert prices[atl.ticker].yes_bid is None and prices[atl.ticker].candle_end_ts is None
    call = next(c for c in client.calls if c["ticker"] == cin.ticker)
    assert call["end_ts"] == KICKOFF_TS and call["start_ts"] == KICKOFF_TS - 72 * 3600
    assert call["period"] == 60

    # Second run: nothing is refetched, including the miss.
    _, again = fetch_pre_event_prices(
        store=store, client=client, series_ticker="KXNFLGAME",
        markets=store.settled_markets("KXNFLGAME"), now=NOW,
    )
    assert again.fetched == 0 and again.cached == 2 and len(client.calls) == 2

    # --refresh drops the cache and asks again.
    _, refreshed = fetch_pre_event_prices(
        store=store, client=client, series_ticker="KXNFLGAME",
        markets=store.settled_markets("KXNFLGAME"), now=NOW, refresh=True,
    )
    assert refreshed.fetched == 2 and len(client.calls) == 4


def test_cache_is_not_reused_for_a_different_window(store):
    """Codex finding on PR #3: a --lead-hours rerun must not reuse kickoff quotes."""
    cin = _settled("KXNFLGAME-26NOV08CINATL-CIN", "KXNFLGAME-26NOV08CINATL", "yes")
    store.save_page([cin], [])
    client = FakeClient({cin.ticker: CANDLES})
    markets = store.settled_markets("KXNFLGAME")
    kwargs = {
        "store": store,
        "client": client,
        "series_ticker": "KXNFLGAME",
        "markets": markets,
        "now": NOW,
    }

    prices, _ = fetch_pre_event_prices(**kwargs)
    assert prices[cin.ticker].candle_end_ts == KICKOFF_TS - 2 * 3600

    # Twelve hours earlier: a new window, so a new request, and the 15:00 and
    # 16:00 candles are now after the cutoff.
    prices, stats = fetch_pre_event_prices(**kwargs, lead_hours=12)
    assert stats.fetched == 1 and stats.cached == 0 and len(client.calls) == 2
    assert client.calls[-1]["end_ts"] == KICKOFF_TS - 12 * 3600
    assert prices[cin.ticker].cutoff_ts == KICKOFF_TS - 12 * 3600
    assert prices[cin.ticker].candle_end_ts is None  # fixture has nothing that early

    # Same lead again is a hit. A wider lookback after a miss must search again.
    _, stats = fetch_pre_event_prices(**kwargs, lead_hours=12)
    assert stats.cached == 1 and len(client.calls) == 2
    _, stats = fetch_pre_event_prices(**kwargs, lead_hours=12, lookback_hours=200)
    assert stats.fetched == 1 and len(client.calls) == 3

    # Back to kickoff: refetched, then a narrower window that excludes the
    # found candle is refetched too, while one that still contains it is a hit.
    prices, stats = fetch_pre_event_prices(**kwargs)
    assert stats.fetched == 1 and len(client.calls) == 4
    _, stats = fetch_pre_event_prices(**kwargs, lookback_hours=3)
    assert stats.cached == 1 and len(client.calls) == 4
    _, stats = fetch_pre_event_prices(**kwargs, lookback_hours=1)
    assert stats.fetched == 1 and len(client.calls) == 5

    # A changed event time (fallback source) is also a different window.
    store.conn.execute("UPDATE markets SET occurrence_datetime = NULL WHERE ticker = ?", (cin.ticker,))
    store.conn.commit()
    kwargs["markets"] = store.settled_markets("KXNFLGAME")
    prices, stats = fetch_pre_event_prices(**kwargs)
    assert stats.fetched == 1 and prices[cin.ticker].cutoff_source == "expected_expiration_time"


def test_stale_cache_row_is_not_returned_when_max_fetch_stops_early(store):
    cin = _settled("KXNFLGAME-26NOV08CINATL-CIN", "KXNFLGAME-26NOV08CINATL", "yes")
    store.save_page([cin], [])
    client = FakeClient({cin.ticker: CANDLES})
    markets = store.settled_markets("KXNFLGAME")
    fetch_pre_event_prices(store=store, client=client, series_ticker="KXNFLGAME", markets=markets, now=NOW)
    prices, stats = fetch_pre_event_prices(
        store=store, client=client, series_ticker="KXNFLGAME", markets=markets, now=NOW,
        lead_hours=12, max_fetch=0,
    )
    assert stats.fetched == 0 and cin.ticker not in prices


def test_fetch_404_is_cached_and_other_errors_propagate(store):
    missing = _settled("KXNFLGAME-26AUG29CHITEN-CHI", "KXNFLGAME-26AUG29CHITEN", "no")
    broken = _settled("KXNFLGAME-26AUG29CHITEN-TEN", "KXNFLGAME-26AUG29CHITEN", "yes")
    store.save_page([missing, broken], [])
    client = FakeClient({broken.ticker: KalshiAPIError("503", status_code=503)})

    with pytest.raises(KalshiAPIError):
        fetch_pre_event_prices(
            store=store, client=client, series_ticker="KXNFLGAME",
            markets=store.settled_markets("KXNFLGAME"), now=NOW,
        )
    # The 404 row (CHI sorts before TEN) was committed before the 503 raised.
    cached = store.pre_event_prices("KXNFLGAME")
    assert list(cached) == [missing.ticker]
    assert cached[missing.ticker].candle_end_ts is None
    assert cached[missing.ticker].history_missing

    # Missing history is window-independent: a different lead does not re-ask.
    client.by_ticker[broken.ticker] = CANDLES
    _, stats = fetch_pre_event_prices(
        store=store, client=client, series_ticker="KXNFLGAME",
        markets=store.settled_markets("KXNFLGAME"), now=NOW, lead_hours=12,
    )
    assert stats.cached == 1 and stats.fetched == 1 and stats.not_found == 0
    assert sum(1 for c in client.calls if c["ticker"] == missing.ticker) == 1


def test_fetch_skips_markets_without_event_time_and_honours_max_fetch(store):
    rows = [
        _settled(f"KXNFLGAME-26NOV08X-{i}", f"E{i}", "yes") for i in range(3)
    ]
    rows.append(
        _settled(
            "KXNFLGAME-26NOV08Z-NOTIME", "EZ", "yes",
            occurrence_datetime=None, expected_expiration_time=None, close_time=None,
        )
    )
    store.save_page(rows, [])
    client = FakeClient({r.ticker: CANDLES for r in rows})
    _, stats = fetch_pre_event_prices(
        store=store, client=client, series_ticker="KXNFLGAME",
        markets=store.settled_markets("KXNFLGAME"), now=NOW, max_fetch=2,
    )
    assert stats.fetched == 2
    assert len(client.calls) == 2


# --- observations and metrics -----------------------------------------------


def _obs(p, outcome, ticker="T", event="E", ask=None, no_ask=None):
    return Observation(
        ticker=ticker, event_ticker=event, probability=p,
        yes_ask=min(1.0, p + 0.02) if ask is None else ask,
        no_ask=min(1.0, 1 - (p - 0.02)) if no_ask is None else no_ask,
        outcome=outcome, cutoff_source="occurrence_datetime",
    )


def test_build_observations_dedupes_events_and_skips_bad_rows(store):
    cin = _settled("KXNFLGAME-26NOV08CINATL-CIN", "KXNFLGAME-26NOV08CINATL", "yes")
    atl = _settled("KXNFLGAME-26NOV08CINATL-ATL", "KXNFLGAME-26NOV08CINATL", "no")
    tie = _settled("KXNFLGAME-26NOV08DALNYG-DAL", "KXNFLGAME-26NOV08DALNYG", "scalar",
                   settlement_value_dollars="0.5000")
    store.save_page([cin, atl, tie], [])
    client = FakeClient({cin.ticker: CANDLES, atl.ticker: CANDLES, tie.ticker: CANDLES})
    markets = store.settled_markets("KXNFLGAME")
    prices, _ = fetch_pre_event_prices(
        store=store, client=client, series_ticker="KXNFLGAME", markets=markets, now=NOW,
    )

    observations, skipped = build_observations(markets, prices)
    assert [o.ticker for o in observations] == [atl.ticker]  # lowest ticker in the event
    assert skipped == {"not_binary": 1, "no_quote": 0, "duplicate_event": 1}
    (atl_obs,) = observations
    assert atl_obs.probability == pytest.approx(0.44)
    assert atl_obs.yes_ask == 0.46 and atl_obs.no_ask == pytest.approx(0.58)
    assert atl_obs.outcome == 0.0

    both, skipped_all = build_observations(markets, prices, one_per_event=False)
    assert len(both) == 2 and skipped_all["duplicate_event"] == 0


def test_brier_score():
    assert brier_score([]) is None
    assert brier_score([_obs(1.0, 1.0), _obs(0.0, 0.0)]) == 0.0
    assert brier_score([_obs(0.5, 1.0), _obs(0.5, 0.0)]) == pytest.approx(0.25)
    assert brier_score([_obs(0.9, 0.0)]) == pytest.approx(0.81)


def test_side_roi_includes_taker_fee():
    # Buy one YES at 0.45, it wins: payoff 1.00, cost 0.45 + 0.02 fee.
    assert side_roi([(0.45, 1.0)], 1) == pytest.approx((1.0 - 0.47) / 0.47)
    # Same at 100 contracts: fee is 1.74 (0.07*100*0.45*0.55 = 1.7325 rounded up).
    assert side_roi([(0.45, 1.0)], 100) == pytest.approx((100 - 46.74) / 46.74)
    # A loss is -100%.
    assert side_roi([(0.45, 0.0)], 1) == pytest.approx(-1.0)
    assert side_roi([], 1) is None


def test_wilson_interval():
    assert wilson_interval(0, 0) is None
    # 50 of 100: the normal approximation gives 40.2%..59.8%; Wilson is close.
    low, high = wilson_interval(50, 100)
    assert low == pytest.approx(0.4038, abs=1e-3) and high == pytest.approx(0.5962, abs=1e-3)
    # Two of two: a naive interval would be 100%..100%.
    low, high = wilson_interval(2, 2)
    assert low == pytest.approx(0.342, abs=1e-3) and high == 1.0
    low, high = wilson_interval(0, 2)
    assert low == 0.0 and high == pytest.approx(0.658, abs=1e-3)
    # All-YES and all-NO buckets must hit the bounds exactly, not a few ulps
    # off, or the plot's error-bar lengths go negative (Codex finding on #7).
    assert wilson_interval(6, 6)[1] == 1.0
    assert wilson_interval(0, 11)[0] == 0.0
    with pytest.raises(ValueError):
        wilson_interval(3, 2)


def test_bucket_edges_and_assignment():
    assert bucket_edges(4) == [0.0, 0.25, 0.5, 0.75, 1.0]
    with pytest.raises(ValueError):
        bucket_edges(0)
    observations = [
        _obs(0.05, 0.0, ticker="a"),
        _obs(0.25, 1.0, ticker="b"),  # exactly on an edge goes up
        _obs(0.26, 0.0, ticker="c"),
        _obs(1.0, 1.0, ticker="d"),  # top edge stays in the last bucket
    ]
    buckets = bucket_observations(observations, bucket_edges(4))
    assert [b.count for b in buckets] == [1, 2, 0, 1]
    assert buckets[1].predicted == pytest.approx(0.255)
    assert buckets[1].actual == pytest.approx(0.5)
    assert buckets[1].actual_low == pytest.approx(0.0945, abs=1e-3)
    assert buckets[1].actual_high == pytest.approx(0.9055, abs=1e-3)
    assert buckets[2].predicted is None and buckets[2].yes_roi is None
    assert buckets[2].actual_low is None and buckets[2].actual_high is None
    assert buckets[3].actual == 1.0
    with pytest.raises(ValueError):
        bucket_observations(observations, [0.5, 0.5])


# --- end to end -------------------------------------------------------------


def _fixture_db(store):
    rows = [
        _settled("KXNFLGAME-26NOV08CINATL-ATL", "KXNFLGAME-26NOV08CINATL", "no"),
        _settled("KXNFLGAME-26NOV08CINATL-CIN", "KXNFLGAME-26NOV08CINATL", "yes"),
        _settled("KXNFLGAME-26NOV08DALNYG-DAL", "KXNFLGAME-26NOV08DALNYG", "yes"),
        _settled("KXNFLGAME-26NOV08DALNYG-NYG", "KXNFLGAME-26NOV08DALNYG", "no"),
    ]
    store.save_page(rows, [])
    return {r.ticker: CANDLES for r in rows}


def test_run_produces_report(store):
    client = FakeClient(_fixture_db(store))
    report = run(series_ticker="KXNFLGAME", client=client, store=store, now=NOW, buckets=10)
    assert report.settled == 4 and report.with_quote == 4 and report.observations == 2
    assert report.cutoff_sources == {"occurrence_datetime": 2}
    assert report.skipped["duplicate_event"] == 2
    assert report.base_rate == 0.5
    # Both kept contracts are priced at 0.44; one won and one lost.
    assert report.brier == pytest.approx(((0.44 - 0) ** 2 + (0.44 - 1) ** 2) / 2)
    assert report.base_rate_brier == pytest.approx(0.25)
    populated = [b for b in report.buckets if b.count]
    assert len(populated) == 1 and populated[0].lower == pytest.approx(0.4)
    assert populated[0].count == 2 and populated[0].actual == 0.5
    # Two YES at 0.46 (+0.02 fee each): one pays 1.00. (1 - 0.96) / 0.96.
    assert populated[0].yes_roi == pytest.approx((1.0 - 0.96) / 0.96)

    text = format_report(report)
    assert "series=KXNFLGAME" in text
    assert "40-50%" in text and "occurrence_datetime=2" in text
    assert "brier=" in text and "one contract per event" in text
    assert "95% CI" in text and "9.5%..90.5%" in text
    parsed = json.loads(report_to_json(report))
    assert parsed["observations"] == 2 and len(parsed["buckets"]) == 10


def test_main_reports_missing_database(tmp_path, capsys):
    code = main(["--series", "KXNFLGAME", "--db", str(tmp_path / "missing.db")])
    assert code == 2
    assert "collect" in capsys.readouterr().err


def test_main_rejects_bad_arguments(tmp_path):
    assert main(["--series", "KXNFLGAME", "--buckets", "0"]) == 2
    assert main(["--series", "bad ticker!", "--db", str(tmp_path / "x.db")]) == 2


def test_main_reports_empty_series(tmp_path, capsys, monkeypatch):
    db = tmp_path / "kalshi.db"
    MarketStore(db).close()
    monkeypatch.setattr("kalshi_edge.calibrate.KalshiClient", lambda **kwargs: FakeClient({}))
    assert main(["--series", "KXNFLGAME", "--db", str(db)]) == 1
    assert "no settled markets" in capsys.readouterr().err


def test_main_end_to_end_with_fake_client(tmp_path, capsys, monkeypatch):
    db = tmp_path / "kalshi.db"
    with MarketStore(db) as store:
        payloads = _fixture_db(store)
    monkeypatch.setattr("kalshi_edge.calibrate.KalshiClient", lambda **kwargs: FakeClient(payloads))
    out = tmp_path / "report.json"
    assert main(["--series", "kxnflgame", "--db", str(db), "--json", str(out)]) == 0
    assert "observations=2" in capsys.readouterr().out
    assert json.loads(out.read_text())["series"] == "KXNFLGAME"


def test_reliability_diagram_writes_png(store, tmp_path):
    pytest.importorskip("matplotlib")
    client = FakeClient(_fixture_db(store))
    report = run(series_ticker="KXNFLGAME", client=client, store=store, now=NOW)
    out = tmp_path / "calibration.png"
    write_reliability_diagram(report, out)
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_reliability_diagram_handles_all_yes_and_all_no_buckets(tmp_path):
    pytest.importorskip("matplotlib")
    observations = [_obs(0.85, 1.0, ticker=f"y{i}") for i in range(6)]
    observations += [_obs(0.15, 0.0, ticker=f"n{i}") for i in range(11)]
    buckets = bucket_observations(observations, bucket_edges(10))
    report = Report(
        series="KXNFLGAME",
        settled=17,
        with_quote=17,
        observations=17,
        one_per_event=True,
        contracts=1,
        cutoff_sources={},
        skipped={},
        fetched=0,
        cached=17,
        buckets=buckets,
        brier=0.1,
        base_rate=6 / 17,
        base_rate_brier=0.2,
    )
    out = tmp_path / "edges.png"
    write_reliability_diagram(report, out)
    assert out.exists()


def test_plot_without_matplotlib_is_a_clear_error(store, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "matplotlib", None)  # makes `import matplotlib` raise ImportError
    client = FakeClient(_fixture_db(store))
    report = run(series_ticker="KXNFLGAME", client=client, store=store, now=NOW)
    with pytest.raises(RuntimeError, match="requirements-plot.txt"):
        write_reliability_diagram(report, tmp_path / "calibration.png")
