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


def test_version_1_file_is_migrated_in_place(tmp_path):
    """A database from the first release gains the new columns and table."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta VALUES ('version', '1');
        CREATE TABLE markets (
            ticker TEXT PRIMARY KEY, series_ticker TEXT NOT NULL, event_ticker TEXT,
            title TEXT, yes_sub_title TEXT, market_type TEXT, status TEXT, result TEXT,
            open_time TEXT, close_time TEXT, settlement_ts TEXT, expiration_value TEXT,
            settlement_value REAL, notional_value REAL, yes_bid REAL, yes_ask REAL,
            no_bid REAL, no_ask REAL, last_price REAL, volume REAL, open_interest REAL,
            source TEXT NOT NULL, fetched_at TEXT NOT NULL
        );
        INSERT INTO markets (ticker, series_ticker, result, source, fetched_at)
            VALUES ('OLD-1', 'KXNFLGAME', 'yes', 'live', '2026-11-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    with MarketStore(path) as store:
        row = store.conn.execute("SELECT * FROM markets WHERE ticker = 'OLD-1'").fetchone()
        assert row["occurrence_datetime"] is None
        assert row["result"] == "yes"
        version = store.conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()[0]
        assert version == "3"
        # The new table exists and the old row can be re-saved with the new fields.
        store.save_page([_market("OLD-1", occurrence_datetime="2026-11-08T18:00:00Z")], [])
        row = store.conn.execute("SELECT occurrence_datetime FROM markets WHERE ticker = 'OLD-1'").fetchone()
        assert row[0] == "2026-11-08T18:00:00Z"
        assert store.pre_event_prices("KXNFLGAME") == {}


def test_pre_event_price_cache_roundtrip(store):
    from kalshi_edge.parse import PreEventPrice

    store.save_page([_market("MKT-1", result="yes"), _market("MKT-2", result="no")], [])
    hit = PreEventPrice(
        ticker="MKT-1", series_ticker="KXNFLGAME", cutoff_ts=1794160800,
        cutoff_source="occurrence_datetime", start_ts=1793901600, candle_end_ts=1794153600,
        yes_bid=0.42, yes_ask=0.46, last_price=0.44, fetched_at="2026-11-09T00:00:00Z",
    )
    miss = PreEventPrice(
        ticker="MKT-2", series_ticker="KXNFLGAME", cutoff_ts=1794160800,
        cutoff_source="close_time", start_ts=1793901600, candle_end_ts=None,
        yes_bid=None, yes_ask=None, last_price=None, fetched_at="2026-11-09T00:00:00Z",
    )
    store.save_pre_event_price(hit)
    store.save_pre_event_price(miss)
    cached = store.pre_event_prices("KXNFLGAME")
    assert cached == {"MKT-1": hit, "MKT-2": miss}
    assert not cached["MKT-2"].history_missing
    assert cached["MKT-1"].midpoint == pytest.approx(0.44)

    # Upsert replaces; the ticker must exist in markets.
    store.save_pre_event_price(hit.__class__(**{**hit.__dict__, "yes_ask": 0.48}))
    assert store.pre_event_prices("KXNFLGAME")["MKT-1"].yes_ask == 0.48
    with pytest.raises(sqlite3.IntegrityError):
        store.save_pre_event_price(hit.__class__(**{**hit.__dict__, "ticker": "NOPE"}))
    assert store.clear_pre_event_prices("KXNFLGAME") == 2
    assert store.pre_event_prices("KXNFLGAME") == {}


def test_settled_markets_query(store):
    store.save_page(
        [_market("B", result="no"), _market("A", result="yes"), _market("C"), _market("D", result="yes")],
        [],
    )
    store.conn.execute("UPDATE markets SET series_ticker = 'OTHER' WHERE ticker = 'D'")
    assert [r["ticker"] for r in store.settled_markets("KXNFLGAME")] == ["A", "B"]


def test_pre_event_price_answers_only_its_window():
    from kalshi_edge.parse import PreEventPrice

    base = {
        "ticker": "T",
        "series_ticker": "S",
        "cutoff_ts": 1000,
        "cutoff_source": "occurrence_datetime",
        "start_ts": 500,
        "fetched_at": "now",
        "yes_bid": 0.4,
        "yes_ask": 0.42,
        "last_price": 0.41,
    }
    hit = PreEventPrice(candle_end_ts=900, **base)
    assert hit.answers(start_ts=500, cutoff_ts=1000, cutoff_source="occurrence_datetime")
    assert hit.answers(start_ts=900, cutoff_ts=1000, cutoff_source="occurrence_datetime")
    assert not hit.answers(start_ts=901, cutoff_ts=1000, cutoff_source="occurrence_datetime")
    assert not hit.answers(start_ts=500, cutoff_ts=999, cutoff_source="occurrence_datetime")
    assert not hit.answers(start_ts=500, cutoff_ts=1000, cutoff_source="close_time")

    miss = PreEventPrice(candle_end_ts=None, **{**base, "yes_bid": None, "yes_ask": None, "last_price": None})
    assert miss.answers(start_ts=500, cutoff_ts=1000, cutoff_source="occurrence_datetime")
    assert miss.answers(start_ts=600, cutoff_ts=1000, cutoff_source="occurrence_datetime")
    assert not miss.answers(start_ts=400, cutoff_ts=1000, cutoff_source="occurrence_datetime")

    gone = PreEventPrice(candle_end_ts=None, history_missing=True,
                         **{**base, "yes_bid": None, "yes_ask": None, "last_price": None})
    assert gone.answers(start_ts=1, cutoff_ts=5, cutoff_source="close_time")


def test_cache_table_without_window_columns_is_rebuilt(tmp_path):
    path = tmp_path / "cache.db"
    with MarketStore(path) as store:
        store.conn.executescript(
            """
            DROP TABLE pre_event_prices;
            CREATE TABLE pre_event_prices (ticker TEXT PRIMARY KEY, series_ticker TEXT NOT NULL,
                cutoff_ts INTEGER NOT NULL, cutoff_source TEXT NOT NULL, candle_end_ts INTEGER,
                yes_bid REAL, yes_ask REAL, last_price REAL, fetched_at TEXT NOT NULL);
            INSERT INTO pre_event_prices VALUES ('X', 'S', 1, 'close_time', NULL, NULL, NULL, NULL, 'now');
            """
        )
        store.conn.commit()
    with MarketStore(path) as store:
        columns = {row[1] for row in store.conn.execute("PRAGMA table_info(pre_event_prices)")}
        assert "start_ts" in columns
        assert store.pre_event_prices("S") == {}
