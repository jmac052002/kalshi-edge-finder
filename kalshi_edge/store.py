"""SQLite storage for markets and open-market price snapshots.

Two tables, on purpose:

* ``markets`` is one row per contract ticker. Reruns update that row. This is
  where settlement lives: ``result`` and ``settlement_value``. Query it for
  "what did this contract pay?"

* ``price_snapshots`` is one row per contract per collection time. Reruns at
  a new timestamp append. Query it for "what was the market charging while
  the contract was still open?"

* ``pre_event_prices`` is one row per contract: the quote the calibration
  backtest recovered from the candlestick history as of the event's start.
  It is a cache of public data that is expensive to refetch, not a source
  of truth; ``calibrate --refresh`` rebuilds it.

The primary keys make writes idempotent. Inserting the same market twice
updates it. Inserting the same (ticker, captured_at) snapshot twice updates
it. Nothing here deletes a row, so a second settled run cannot wipe a
snapshot taken earlier.

``source`` is ``live`` for ``GET /markets`` and ``historical`` for
``GET /historical/markets``. A live row that already has a ``result`` wins
over an overlapping historical copy, because that settlement is the one
Kalshi has not archived yet. A live row with no ``result`` is only an open
snapshot. If that contract is archived before the next settled run, the
historical row is the only copy of the payoff, so it is allowed to fill the
result in. The price snapshot taken while the market was open is left in
place; that is the forecast a calibration check compares to the payoff.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Self

from kalshi_edge.parse import ParsedMarket, PreEventPrice, PriceSnapshot

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per contract. series_ticker is the series we asked for; the market
-- payload itself does not include it. result is NULL until determination.
-- settlement_value is dollars paid to a YES holder (1 or 0 for a binary
-- contract). The quote columns are from the most recent fetch of this row,
-- which for a finalized market is after the outcome is known.
CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY,
    series_ticker TEXT NOT NULL,
    event_ticker TEXT,
    title TEXT,
    yes_sub_title TEXT,
    market_type TEXT,
    status TEXT,
    result TEXT,
    open_time TEXT,
    close_time TEXT,
    expected_expiration_time TEXT,
    occurrence_datetime TEXT,
    settlement_ts TEXT,
    expiration_value TEXT,
    settlement_value REAL,
    notional_value REAL,
    yes_bid REAL,
    yes_ask REAL,
    no_bid REAL,
    no_ask REAL,
    last_price REAL,
    volume REAL,
    open_interest REAL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

-- Quotes captured while collecting open markets. captured_at is the UTC
-- second the run started, shared by the whole run. Float columns are parsed
-- dollar prices (implied probabilities). *_dollars columns are the raw strings.
CREATE TABLE IF NOT EXISTS price_snapshots (
    ticker TEXT NOT NULL,
    series_ticker TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    yes_bid REAL,
    yes_ask REAL,
    no_bid REAL,
    no_ask REAL,
    last_price REAL,
    yes_bid_dollars TEXT,
    yes_ask_dollars TEXT,
    no_bid_dollars TEXT,
    no_ask_dollars TEXT,
    last_price_dollars TEXT,
    volume REAL,
    open_interest REAL,
    status TEXT,
    PRIMARY KEY (ticker, captured_at),
    FOREIGN KEY (ticker) REFERENCES markets (ticker)
);

-- One row per contract: the pre-event quote recovered from candlesticks.
-- cutoff_ts is the Unix second treated as the event start and start_ts the
-- beginning of the window searched. candle_end_ts and the quotes are NULL
-- when no candle with a two-sided book was found in the window; the row
-- still exists so the miss is remembered. A row only counts as a cache hit
-- when its window matches the one asked for (PreEventPrice.answers).
CREATE TABLE IF NOT EXISTS pre_event_prices (
    ticker TEXT PRIMARY KEY,
    series_ticker TEXT NOT NULL,
    cutoff_ts INTEGER NOT NULL,
    cutoff_source TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    candle_end_ts INTEGER,
    yes_bid REAL,
    yes_ask REAL,
    last_price REAL,
    fetched_at TEXT NOT NULL,
    history_missing INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (ticker) REFERENCES markets (ticker)
);

CREATE INDEX IF NOT EXISTS idx_markets_series_result
    ON markets (series_ticker, result);

CREATE INDEX IF NOT EXISTS idx_pre_event_series
    ON pre_event_prices (series_ticker);

CREATE INDEX IF NOT EXISTS idx_snapshots_series_time
    ON price_snapshots (series_ticker, captured_at);
"""

_UPSERT_MARKET = """
INSERT INTO markets (
    ticker, series_ticker, event_ticker, title, yes_sub_title, market_type,
    status, result, open_time, close_time, expected_expiration_time,
    occurrence_datetime, settlement_ts, expiration_value,
    settlement_value, notional_value, yes_bid, yes_ask, no_bid, no_ask,
    last_price, volume, open_interest, source, fetched_at
) VALUES (
    ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?,
    ?, ?, ?,
    ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?
)
ON CONFLICT(ticker) DO UPDATE SET
    series_ticker = excluded.series_ticker,
    event_ticker = excluded.event_ticker,
    title = excluded.title,
    yes_sub_title = excluded.yes_sub_title,
    market_type = excluded.market_type,
    status = excluded.status,
    result = excluded.result,
    open_time = excluded.open_time,
    close_time = excluded.close_time,
    expected_expiration_time = excluded.expected_expiration_time,
    occurrence_datetime = excluded.occurrence_datetime,
    settlement_ts = excluded.settlement_ts,
    expiration_value = excluded.expiration_value,
    settlement_value = excluded.settlement_value,
    notional_value = excluded.notional_value,
    yes_bid = excluded.yes_bid,
    yes_ask = excluded.yes_ask,
    no_bid = excluded.no_bid,
    no_ask = excluded.no_ask,
    last_price = excluded.last_price,
    volume = excluded.volume,
    open_interest = excluded.open_interest,
    source = excluded.source,
    fetched_at = excluded.fetched_at
-- Live always writes. A historical row may refresh another historical row.
-- It may also fill a live row that has no result yet: that row is an open
-- snapshot, and once Kalshi archives the contract the payoff exists only in
-- the historical payload. A live row that already settled is kept, so an
-- overlapping archive copy cannot blank out a result we already stored.
WHERE excluded.source = 'live'
   OR markets.source != 'live'
   OR (markets.result IS NULL AND excluded.result IS NOT NULL)
"""

_UPSERT_SNAPSHOT = """
INSERT INTO price_snapshots (
    ticker, series_ticker, captured_at,
    yes_bid, yes_ask, no_bid, no_ask, last_price,
    yes_bid_dollars, yes_ask_dollars, no_bid_dollars, no_ask_dollars, last_price_dollars,
    volume, open_interest, status
) VALUES (
    ?, ?, ?,
    ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?,
    ?, ?, ?
)
ON CONFLICT(ticker, captured_at) DO UPDATE SET
    series_ticker = excluded.series_ticker,
    yes_bid = excluded.yes_bid,
    yes_ask = excluded.yes_ask,
    no_bid = excluded.no_bid,
    no_ask = excluded.no_ask,
    last_price = excluded.last_price,
    yes_bid_dollars = excluded.yes_bid_dollars,
    yes_ask_dollars = excluded.yes_ask_dollars,
    no_bid_dollars = excluded.no_bid_dollars,
    no_ask_dollars = excluded.no_ask_dollars,
    last_price_dollars = excluded.last_price_dollars,
    volume = excluded.volume,
    open_interest = excluded.open_interest,
    status = excluded.status
"""


_UPSERT_PRE_EVENT = """
INSERT INTO pre_event_prices (
    ticker, series_ticker, cutoff_ts, cutoff_source, start_ts, candle_end_ts,
    yes_bid, yes_ask, last_price, fetched_at, history_missing
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(ticker) DO UPDATE SET
    series_ticker = excluded.series_ticker,
    cutoff_ts = excluded.cutoff_ts,
    cutoff_source = excluded.cutoff_source,
    start_ts = excluded.start_ts,
    candle_end_ts = excluded.candle_end_ts,
    yes_bid = excluded.yes_bid,
    yes_ask = excluded.yes_ask,
    last_price = excluded.last_price,
    fetched_at = excluded.fetched_at,
    history_missing = excluded.history_missing
"""

# Columns added after schema version 1. A version-1 file gets them through
# ALTER TABLE so an existing collection is kept; the new columns are NULL
# until the next ``collect --mode settled`` run rewrites each market row.
_MARKET_COLUMNS_SINCE_V1 = ("expected_expiration_time", "occurrence_datetime")


class MarketStore:
    """A SQLite file. The path's parent directory is created if needed."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent and not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def _migrate(self) -> None:
        """Bring a file written by an older version up to the current schema.

        Only additive changes so far, which SQLite can do in place. The
        column names are our own constants, never user input, so building
        the ALTER statement with them is not an injection surface.
        """
        tables = {
            row[0]
            for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "markets" not in tables:
            return  # fresh file: executescript creates everything
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(markets)")}
        for column in _MARKET_COLUMNS_SINCE_V1:
            if column not in existing:
                self.conn.execute(f"ALTER TABLE markets ADD COLUMN {column} TEXT")
        # pre_event_prices is a cache of public data. A copy written before
        # the window columns existed cannot be validated, so it is dropped and
        # rebuilt by the next calibrate run rather than migrated.
        if "pre_event_prices" in tables:
            cache_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(pre_event_prices)")}
            if not {"start_ts", "history_missing"} <= cache_columns:
                self.conn.execute("DROP TABLE pre_event_prices")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def save_page(self, markets: list[ParsedMarket], snapshots: list[PriceSnapshot]) -> None:
        """Insert or update one page. Markets go in first so snapshot FKs hold.

        The whole page is one transaction: a snapshot that points at a ticker
        we did not write rolls the market inserts back too.
        """
        with self.conn:
            if markets:
                self.conn.executemany(_UPSERT_MARKET, [market.as_tuple() for market in markets])
            if snapshots:
                self.conn.executemany(
                    _UPSERT_SNAPSHOT, [snapshot.as_tuple() for snapshot in snapshots]
                )

    def settled_markets(self, series_ticker: str) -> list[sqlite3.Row]:
        """Every market in the series that Kalshi has determined.

        ``result`` is set once the exchange rules on the outcome, whether or
        not the row has since been archived. Rows are ordered by ticker so a
        run is deterministic.
        """
        return self.conn.execute(
            "SELECT * FROM markets WHERE series_ticker = ? AND result IS NOT NULL "
            "ORDER BY ticker",
            (series_ticker,),
        ).fetchall()

    def pre_event_prices(self, series_ticker: str) -> dict[str, PreEventPrice]:
        """Cached pre-event quotes for the series, keyed by ticker."""
        rows = self.conn.execute(
            "SELECT * FROM pre_event_prices WHERE series_ticker = ?",
            (series_ticker,),
        ).fetchall()
        return {
            row["ticker"]: PreEventPrice(
                ticker=row["ticker"],
                series_ticker=row["series_ticker"],
                cutoff_ts=int(row["cutoff_ts"]),
                cutoff_source=row["cutoff_source"],
                start_ts=int(row["start_ts"]),
                candle_end_ts=row["candle_end_ts"],
                yes_bid=row["yes_bid"],
                yes_ask=row["yes_ask"],
                last_price=row["last_price"],
                fetched_at=row["fetched_at"],
                history_missing=bool(row["history_missing"]),
            )
            for row in rows
        }

    def save_pre_event_price(self, price: PreEventPrice) -> None:
        """Insert or replace one cached quote. Committed at once: a long
        fetch that is interrupted keeps what it had already found."""
        with self.conn:
            self.conn.execute(_UPSERT_PRE_EVENT, price.as_tuple())

    def clear_pre_event_prices(self, series_ticker: str) -> int:
        """Forget the cached quotes for one series. Returns rows removed."""
        with self.conn:
            cursor = self.conn.execute(
                "DELETE FROM pre_event_prices WHERE series_ticker = ?", (series_ticker,)
            )
        return cursor.rowcount

    def counts(self) -> dict[str, int]:
        def one(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()[0])

        return {
            "markets": one("SELECT COUNT(*) FROM markets"),
            "snapshots": one("SELECT COUNT(*) FROM price_snapshots"),
            "with_result": one(
                "SELECT COUNT(*) FROM markets WHERE result IS NOT NULL"
            ),
        }
