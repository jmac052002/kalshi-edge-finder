"""Reference-price selection. A quote from after the cutoff is lookahead."""

from datetime import datetime, timedelta, timezone

import pytest

from kalshi_edge.quotes import (
    latest_at_or_before,
    quote_from_book,
    quote_from_candlestick,
    quote_from_snapshot,
    quote_from_trade,
)

CUTOFF = datetime(2026, 11, 8, 14, 35, 30, tzinfo=timezone.utc)
WINDOW = CUTOFF - timedelta(hours=6)


def _ts(when: datetime) -> int:
    return int(when.timestamp())


def test_tight_book_uses_the_mid_and_keeps_the_ask():
    quote = quote_from_book(
        as_of=CUTOFF - timedelta(hours=1),
        yes_bid=0.39,
        yes_ask=0.41,
        trade_price=0.40,
        source="candlestick",
        max_spread=0.10,
    )
    assert quote is not None
    assert quote.price == 0.40
    assert quote.buy_ask == 0.41


def test_wide_book_does_not_pretend_the_mid_is_a_forecast():
    # $0 bid and $1 ask is an empty book. 50 cents would be a fiction.
    empty = quote_from_book(
        as_of=CUTOFF,
        yes_bid=0.0,
        yes_ask=1.0,
        trade_price=None,
        source="candlestick",
        max_spread=0.10,
    )
    assert empty is None
    traded = quote_from_book(
        as_of=CUTOFF,
        yes_bid=0.0,
        yes_ask=1.0,
        trade_price=0.42,
        source="candlestick",
        max_spread=0.10,
    )
    assert traded is not None
    assert traded.price == 0.42
    assert traded.buy_ask is None


def test_candlestick_reads_live_and_archived_field_names():
    live = quote_from_candlestick(
        {
            "end_period_ts": _ts(CUTOFF - timedelta(hours=1)),
            "yes_bid": {"close_dollars": "0.6100"},
            "yes_ask": {"close_dollars": "0.6300"},
            "price": {"close_dollars": "0.6200"},
        },
        max_spread=0.10,
    )
    assert live is not None
    assert live.price == 0.62
    archived = quote_from_candlestick(
        {
            "end_period_ts": _ts(CUTOFF - timedelta(hours=1)),
            "yes_bid": {"close": "0.1000"},
            "yes_ask": {"close": "0.1100"},
            "price": {"close": "0.1100"},
        },
        max_spread=0.10,
    )
    assert archived is not None
    assert archived.price == pytest.approx(0.105)
    assert archived.buy_ask == 0.11


def test_bar_that_ends_after_the_cutoff_is_dropped():
    late = quote_from_candlestick(
        {
            "end_period_ts": _ts(CUTOFF + timedelta(minutes=30)),
            "yes_bid": {"close_dollars": "0.9700"},
            "yes_ask": {"close_dollars": "0.9800"},
            "price": {"close_dollars": "0.9800"},
        },
        max_spread=0.10,
    )
    early = quote_from_candlestick(
        {
            "end_period_ts": _ts(CUTOFF - timedelta(hours=1)),
            "yes_bid": {"close_dollars": "0.6100"},
            "yes_ask": {"close_dollars": "0.6300"},
            "price": {"close_dollars": "0.6200"},
        },
        max_spread=0.10,
    )
    assert late is not None and early is not None
    chosen = latest_at_or_before([late, early], CUTOFF, WINDOW)
    assert chosen is early
    assert chosen.price == 0.62


def test_trade_after_the_cutoff_is_dropped_and_the_newest_legal_trade_wins():
    late = quote_from_trade(
        {"created_time": "2026-11-08T15:00:00Z", "yes_price_dollars": "0.9900"}
    )
    older = quote_from_trade(
        {"created_time": "2026-11-08T12:00:00Z", "yes_price_dollars": "0.4000"}
    )
    newer = quote_from_trade(
        {"created_time": "2026-11-08T14:30:00Z", "yes_price_dollars": "0.4400"}
    )
    assert late is not None and older is not None and newer is not None
    chosen = latest_at_or_before([late, older, newer], CUTOFF, WINDOW)
    assert chosen is newer
    assert chosen.price == 0.44
    assert chosen.buy_ask is None


def test_snapshot_after_the_cutoff_is_not_a_forecast():
    late = quote_from_snapshot(
        {
            "captured_at": "2026-11-08T16:00:00Z",
            "yes_bid": 0.97,
            "yes_ask": 0.98,
            "last_price": 0.99,
        },
        max_spread=0.10,
    )
    early = quote_from_snapshot(
        {
            "captured_at": "2026-11-08T14:00:00Z",
            "yes_bid": 0.50,
            "yes_ask": 0.52,
            "last_price": 0.51,
        },
        max_spread=0.10,
    )
    assert late is not None and early is not None
    chosen = latest_at_or_before([late, early], CUTOFF, WINDOW)
    assert chosen is early
    assert chosen.price == 0.51
    assert chosen.source == "snapshot"
