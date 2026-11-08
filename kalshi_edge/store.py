"""SQLite storage for markets and open-market price snapshots.

Two tables, on purpose:

* ``markets`` is one row per contract ticker. Reruns update that row. This is
  where settlement lives: ``result`` and ``settlement_value``. Query it for
  "what did this contract pay?"

* ``price_snapshots`` is one row per contract per collection time. Reruns at
  a new timestamp append. Query it for "what was the market charging while
  the contract was still open?"

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

from kalshi_edge.parse import ParsedMarket, PriceSnapshot

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

CREATE INDEX IF NOT EXISTS idx_markets_series_result
    ON markets (series_ticker, result);

CREATE INDEX IF NOT EXISTS idx_snapshots_series_time
    ON price_snapshots (series_ticker, captured_at);

-- Pre-event quote used for calibration. The quote columns on markets are the
-- latest fetch, which for a finalized contract is after the outcome is known.
-- price is the probability we score. buy_ask is what a YES buyer would have
-- paid, and only when the book was tight enough to be a real offer.
-- quote_source 'missing' means we already looked and found nothing before
-- the cutoff, so a rerun does not hammer the API.
CREATE TABLE IF NOT EXISTS reference_quotes (
    ticker TEXT NOT NULL,
    rule TEXT NOT NULL,
    series_ticker TEXT NOT NULL,
    cutoff TEXT NOT NULL,
    as_of TEXT,
    yes_bid REAL,
    yes_ask REAL,
    trade_price REAL,
    price REAL,
    buy_ask REAL,
    quote_source TEXT NOT NULL,
    PRIMARY KEY (ticker, rule),
    FOREIGN KEY (ticker) REFERENCES markets (ticker)
);

CREATE INDEX IF NOT EXISTS idx_reference_series_rule
    ON reference_quotes (series_ticker, rule);
"""

_UPSERT_MARKET = """
INSERT INTO markets (
    ticker, series_ticker, event_ticker, title, yes_sub_title, market_type,
    status, result, open_time, close_time, settlement_ts, expiration_value,
    settlement_value, notional_value, yes_bid, yes_ask, no_bid, no_ask,
    last_price, volume, open_interest, source, fetched_at
) VALUES (
    ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?,
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


class MarketStore:
    """A SQLite file. The path's parent directory is created if needed."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent and not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('version', '1')"
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> MarketStore:
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

    def settled_markets(self, series_ticker: str) -> list[sqlite3.Row]:
        """Contracts with a determination. Includes scalars; the caller decides."""
        return list(
            self.conn.execute(
                """
                SELECT ticker, series_ticker, event_ticker, title, yes_sub_title,
                       result, settlement_value, close_time, source, last_price
                FROM markets
                WHERE series_ticker = ? AND result IS NOT NULL
                ORDER BY close_time, ticker
                """,
                (series_ticker,),
            )
        )

    def snapshots_by_ticker(self, series_ticker: str) -> dict[str, list[sqlite3.Row]]:
        grouped: dict[str, list[sqlite3.Row]] = {}
        rows = self.conn.execute(
            """
            SELECT ticker, captured_at, yes_bid, yes_ask, last_price
            FROM price_snapshots
            WHERE series_ticker = ?
            ORDER BY captured_at
            """,
            (series_ticker,),
        )
        for row in rows:
            grouped.setdefault(row["ticker"], []).append(row)
        return grouped

    def cached_quotes(self, series_ticker: str, rule: str) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute(
            """
            SELECT ticker, cutoff, as_of, yes_bid, yes_ask, trade_price,
                   price, buy_ask, quote_source
            FROM reference_quotes
            WHERE series_ticker = ? AND rule = ?
            """,
            (series_ticker, rule),
        )
        return {row["ticker"]: row for row in rows}

    def save_reference_quote(
        self,
        *,
        ticker: str,
        rule: str,
        series_ticker: str,
        cutoff: str,
        as_of: str | None,
        yes_bid: float | None,
        yes_ask: float | None,
        trade_price: float | None,
        price: float | None,
        buy_ask: float | None,
        quote_source: str,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO reference_quotes (
                    ticker, rule, series_ticker, cutoff, as_of,
                    yes_bid, yes_ask, trade_price, price, buy_ask, quote_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, rule) DO UPDATE SET
                    series_ticker = excluded.series_ticker,
                    cutoff = excluded.cutoff,
                    as_of = excluded.as_of,
                    yes_bid = excluded.yes_bid,
                    yes_ask = excluded.yes_ask,
                    trade_price = excluded.trade_price,
                    price = excluded.price,
                    buy_ask = excluded.buy_ask,
                    quote_source = excluded.quote_source
                """,
                (
                    ticker,
                    rule,
                    series_ticker,
                    cutoff,
                    as_of,
                    yes_bid,
                    yes_ask,
                    trade_price,
                    price,
                    buy_ask,
                    quote_source,
                ),
            )
