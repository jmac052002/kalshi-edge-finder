"""Storage tests against a temporary SQLite file. No network."""

import sqlite3

import pytest

from kalshi_edge.parse import parse_market, parse_snapshot
from kalshi_edge.store import MarketStore


def _market(ticker="MKT-1", **overrides):
    payload = {
        "ticker": ticker,
        "event_ticker": "EVT-1",
        "title": "Original",
        "yes_sub_title": "Yes side",
        "status": "active",
        "result": "",
        "yes_bid_dollars": "0.4000",
        "yes_ask_dollars": "0.4200",
        "last_price_dollars": "0.4100",
        "volume_fp": "10.00",
    }
    payload.update(overrides)
    return parse_market(payload, series_ticker="KXNFLGAME", source="live", fetched_at="2026-11-08T19:00:00Z")


def _snapshot(ticker="MKT-1", captured_at="2026-11-08T19:00:00Z", **overrides):
    payload = {
        "ticker": ticker,
        "status": "active",
        "yes_bid_dollars": "0.4000",
        "yes_ask_dollars": "0.4200",
        "last_price_dollars": "0.4100",
    }
    payload.update(overrides)
    return parse_snapshot(payload, series_ticker="KXNFLGAME", captured_at=captured_at)


@pytest.fixture
def store(tmp_path):
    db = MarketStore(tmp_path / "kalshi.db")
    try:
        yield db
    finally:
        db.close()


def test_market_upsert_is_idempotent_and_updates(store):
    market = _market()
    store.save_page([market], [])
    updated = parse_market(
        {
            "ticker": "MKT-1",
            "title": "Updated",
            "status": "active",
            "result": "",
            "yes_bid_dollars": "0.5500",
        },
        series_ticker="KXNFLGAME",
        source="live",
        fetched_at="2026-11-08T19:05:00Z",
    )
    store.save_page([updated], [])
    counts = store.counts()
    assert counts["markets"] == 1
    row = store.conn.execute("SELECT title, yes_bid, fetched_at FROM markets").fetchone()
    assert row["title"] == "Updated"
    assert row["yes_bid"] == pytest.approx(0.55)
    assert row["fetched_at"] == "2026-11-08T19:05:00Z"


def test_snapshot_same_timestamp_does_not_duplicate(store):
    store.save_page([_market()], [_snapshot()])
    store.save_page([_market()], [_snapshot(yes_bid_dollars="0.6100")])
    assert store.counts()["snapshots"] == 1
    row = store.conn.execute("SELECT yes_bid, yes_bid_dollars FROM price_snapshots").fetchone()
    assert row["yes_bid"] == pytest.approx(0.61)
    assert row["yes_bid_dollars"] == "0.6100"


def test_snapshot_new_timestamp_appends_history(store):
    store.save_page([_market()], [_snapshot(captured_at="2026-11-08T19:00:00Z")])
    store.save_page([_market()], [_snapshot(captured_at="2026-11-08T19:01:00Z", yes_bid_dollars="0.7000")])
    assert store.counts()["markets"] == 1
    assert store.counts()["snapshots"] == 2
    prices = [
        row["yes_bid"]
        for row in store.conn.execute(
            "SELECT yes_bid FROM price_snapshots ORDER BY captured_at"
        )
    ]
    assert prices == pytest.approx([0.40, 0.70])


def test_historical_settlement_fills_snapshotted_market(store):
    """Regression: an open snapshot must not block a later archive settlement.

    Snapshot mode writes source='live' with no result. If Kalshi archives the
    contract before the next settled run, the historical payload is the only
    copy of the payoff. Rejecting it leaves status='active' and result NULL,
    so the pre-event snapshot can never be scored. The snapshot row itself
    stays; that price is the forecast, and the result is the outcome.
    """
    store.save_page([_market()], [_snapshot()])
    historical = parse_market(
        {
            "ticker": "MKT-1",
            "title": "Archived after the snapshot",
            "status": "finalized",
            "result": "yes",
            "settlement_value_dollars": "1.0000",
        },
        series_ticker="KXNFLGAME",
        source="historical",
        fetched_at="2026-11-09T12:00:00Z",
    )
    store.save_page([historical], [])
    row = store.conn.execute(
        """
        SELECT status, result, settlement_value, source, fetched_at
        FROM markets
        """
    ).fetchone()
    assert row["status"] == "finalized"
    assert row["result"] == "yes"
    assert row["settlement_value"] == pytest.approx(1.0)
    assert row["source"] == "historical"
    assert row["fetched_at"] == "2026-11-09T12:00:00Z"
    assert store.counts()["markets"] == 1
    assert store.counts()["with_result"] == 1
    # The quote captured while the market was open is still the forecast.
    assert store.counts()["snapshots"] == 1
    snap = store.conn.execute("SELECT yes_bid, captured_at FROM price_snapshots").fetchone()
    assert snap["yes_bid"] == pytest.approx(0.40)
    assert snap["captured_at"] == "2026-11-08T19:00:00Z"


def test_settled_live_row_wins_over_historical(store):
    """A live settlement is newer than an overlapping archive copy."""
    live = parse_market(
        {
            "ticker": "MKT-1",
            "title": "Live settled",
            "status": "finalized",
            "result": "yes",
            "settlement_value_dollars": "1.0000",
        },
        series_ticker="KXNFLGAME",
        source="live",
        fetched_at="2026-11-08T19:00:00Z",
    )
    store.save_page([live], [])
    historical = parse_market(
        {
            "ticker": "MKT-1",
            "title": "Stale archive copy",
            "status": "finalized",
            "result": "no",
            "settlement_value_dollars": "0.0000",
        },
        series_ticker="KXNFLGAME",
        source="historical",
        fetched_at="2026-11-08T18:00:00Z",
    )
    store.save_page([historical], [])
    row = store.conn.execute(
        "SELECT title, source, result, settlement_value, status FROM markets"
    ).fetchone()
    assert row["title"] == "Live settled"
    assert row["source"] == "live"
    assert row["status"] == "finalized"
    assert row["result"] == "yes"
    assert row["settlement_value"] == pytest.approx(1.0)
    assert store.counts()["markets"] == 1


def test_historical_row_is_upgraded_by_live(store):
    historical = parse_market(
        {"ticker": "MKT-1", "title": "Archive", "result": "yes", "status": "finalized"},
        series_ticker="KXNFLGAME",
        source="historical",
        fetched_at="2026-11-01T00:00:00Z",
    )
    store.save_page([historical], [])
    store.save_page([_market()], [])
    row = store.conn.execute("SELECT title, source FROM markets").fetchone()
    assert row["source"] == "live"
    assert row["title"] == "Original"


def test_orphan_snapshot_rolls_back_the_page(store):
    market = _market(ticker="MKT-1")
    snapshot = _snapshot(ticker="MKT-OTHER")
    with pytest.raises(sqlite3.IntegrityError):
        store.save_page([market], [snapshot])
    assert store.counts() == {"markets": 0, "snapshots": 0, "with_result": 0}


def test_settled_result_is_queryable(store):
    settled = parse_market(
        {
            "ticker": "MKT-1",
            "title": "Cincinnati wins",
            "yes_sub_title": "Cincinnati",
            "status": "finalized",
            "result": "yes",
            "settlement_value_dollars": "1.0000",
        },
        series_ticker="KXNFLGAME",
        source="live",
        fetched_at="2026-11-08T19:00:00Z",
    )
    store.save_page([settled], [])
    assert store.counts()["with_result"] == 1
    row = store.conn.execute(
        "SELECT result, settlement_value FROM markets WHERE series_ticker = ?",
        ("KXNFLGAME",),
    ).fetchone()
    assert row["result"] == "yes"
    assert row["settlement_value"] == pytest.approx(1.0)
