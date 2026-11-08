"""Parsing tests. Payloads are fixtures shaped like the public API, not live calls."""

from kalshi_edge.parse import parse_fixed_point, parse_market, parse_snapshot

OPEN_MARKET = {
    "ticker": "KXNFLGAME-26NOV16LACBAL-LAC",
    "event_ticker": "KXNFLGAME-26NOV16LACBAL",
    "title": "Los Angeles C wins",
    "yes_sub_title": "Los Angeles C",
    "status": "active",
    "result": "",
    "market_type": "binary",
    "open_time": "2026-11-10T00:00:00Z",
    "close_time": "2026-11-19T01:15:00Z",
    "yes_bid_dollars": "0.4500",
    "yes_ask_dollars": "0.4700",
    "no_bid_dollars": "0.5300",
    "no_ask_dollars": "0.5500",
    "last_price_dollars": "0.4700",
    "notional_value_dollars": "1.0000",
    "volume_fp": "14483.21",
    "open_interest_fp": "8662.43",
    # Legacy cent / count fields. The public list currently returns these as null.
    "yes_bid": None,
    "yes_ask": None,
    "last_price": None,
    "volume": None,
}

SETTLED_MARKET = {
    "ticker": "KXNFLGAME-26NOV08CINATL-CIN",
    "event_ticker": "KXNFLGAME-26NOV08CINATL",
    "title": "Cincinnati wins",
    "yes_sub_title": "Cincinnati",
    "status": "finalized",
    "result": "yes",
    "market_type": "binary",
    "close_time": "2026-11-08T17:35:30Z",
    "settlement_ts": "2026-11-08T17:37:32.373747Z",
    "expiration_value": "winner",
    "settlement_value_dollars": "1.0000",
    "last_price_dollars": "0.9900",
    "volume_fp": "6463723.22",
}


def test_fixed_point_blanks_and_garbage():
    assert parse_fixed_point(None) is None
    assert parse_fixed_point("") is None
    assert parse_fixed_point("  ") is None
    assert parse_fixed_point("nope") is None
    assert parse_fixed_point(True) is None
    assert parse_fixed_point("0.4500") == 0.45
    assert parse_fixed_point(0.5) == 0.5


def test_open_market_quotes_come_from_dollar_strings():
    market = parse_market(
        OPEN_MARKET,
        series_ticker="KXNFLGAME",
        source="live",
        fetched_at="2026-11-08T19:00:00Z",
    )
    assert market is not None
    assert market.ticker == "KXNFLGAME-26NOV16LACBAL-LAC"
    assert market.series_ticker == "KXNFLGAME"
    assert market.yes_sub_title == "Los Angeles C"
    assert market.status == "active"
    # A blank result means the contract is not determined yet, not a third outcome.
    assert market.result is None
    assert market.yes_bid == 0.45
    assert market.yes_ask == 0.47
    assert market.last_price == 0.47
    assert market.volume == 14483.21
    assert market.open_interest == 8662.43
    assert market.notional_value == 1.0
    assert market.settlement_value is None


def test_cent_fields_are_not_used_when_dollar_strings_are_missing():
    # 45 cents must not become a price of 45. We only trust *_dollars.
    payload = {
        "ticker": "MKT-1",
        "yes_bid": 45,
        "yes_ask": 47,
        "last_price": 46,
        "volume": 100,
        "result": "",
    }
    market = parse_market(payload, series_ticker="KXNFLGAME", source="live", fetched_at="t")
    assert market is not None
    assert market.yes_bid is None
    assert market.yes_ask is None
    assert market.last_price is None
    assert market.volume is None


def test_settled_market_keeps_result_and_payoff():
    market = parse_market(
        SETTLED_MARKET,
        series_ticker="KXNFLGAME",
        source="live",
        fetched_at="2026-11-08T19:00:00Z",
    )
    assert market is not None
    assert market.result == "yes"
    assert market.status == "finalized"
    # YES holders are paid $1. That is the outcome a calibration study scores.
    assert market.settlement_value == 1.0
    assert market.settlement_ts == "2026-11-08T17:37:32.373747Z"
    # The last trade is near $1 because the game is over. It is not the forecast.
    assert market.last_price == 0.99


def test_result_is_normalized_and_scalar_is_kept():
    payload = {"ticker": "MKT-1", "result": "YES"}
    market = parse_market(payload, series_ticker="KX", source="historical", fetched_at="t")
    assert market is not None
    assert market.result == "yes"
    scalar = parse_market(
        {"ticker": "MKT-2", "result": "scalar", "settlement_value_dollars": "0.4000"},
        series_ticker="KX",
        source="historical",
        fetched_at="t",
    )
    assert scalar is not None
    assert scalar.result == "scalar"
    assert scalar.settlement_value == 0.4


def test_missing_ticker_and_non_dict_are_skipped():
    assert parse_market({}, series_ticker="KX", source="live", fetched_at="t") is None
    assert parse_market({"ticker": "  "}, series_ticker="KX", source="live", fetched_at="t") is None
    assert parse_market(["not", "a", "market"], series_ticker="KX", source="live", fetched_at="t") is None
    assert parse_snapshot("nope", series_ticker="KX", captured_at="t") is None


def test_garbage_dollar_string_does_not_drop_the_market():
    payload = {"ticker": "MKT-1", "yes_bid_dollars": "not-a-price", "title": "Still here"}
    market = parse_market(payload, series_ticker="KX", source="live", fetched_at="t")
    assert market is not None
    assert market.title == "Still here"
    assert market.yes_bid is None


def test_snapshot_keeps_raw_dollar_strings():
    snapshot = parse_snapshot(
        OPEN_MARKET,
        series_ticker="KXNFLGAME",
        captured_at="2026-11-08T19:00:00Z",
    )
    assert snapshot is not None
    assert snapshot.yes_bid_dollars == "0.4500"
    assert snapshot.yes_ask == 0.47
    assert snapshot.no_bid == 0.53
    assert snapshot.captured_at == "2026-11-08T19:00:00Z"
    assert snapshot.series_ticker == "KXNFLGAME"
