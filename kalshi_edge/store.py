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

* ``sportsbook_odds`` is one row per bookmaker outcome per capture, as the
  odds provider sent it. ``series_fees`` is the fee schedule Kalshi
  publishes per series. ``line_matches`` records which sportsbook game each
  Kalshi event was tied to, so a wrong match can be traced.

* ``nws_forecasts`` is one row per daily-high event per scan: the NWS hourly
  max the scan priced, stored so a later weather fit can use the forecast
  the scan actually read. ``weather_days`` and ``archive_forecasts`` are the
  settled highs and the archived model highs ``weather_calibrate`` paired.
  ``weather_brackets`` is the settled ladder for the contracts whose
  pre-noon quote was scored. None of these tables place an order.

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

from kalshi_edge.parse import ParsedMarket, PreEventPrice, PriceSnapshot, SeriesFee
from kalshi_edge.sportsbook.parse import OddsCapture, OddsRow

SCHEMA_VERSION = 4

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

-- One row per odds run, written even when the provider returned no
-- moneylines. The newest row here, not the newest outcome row, defines the
-- "latest capture": an empty capture must supersede older odds, otherwise
-- compare would keep pricing contracts against lines that are gone.
CREATE TABLE IF NOT EXISTS odds_captures (
    source TEXT NOT NULL,
    sport_key TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    regions TEXT NOT NULL,
    events INTEGER NOT NULL,
    rows INTEGER NOT NULL,
    PRIMARY KEY (source, sport_key, captured_at)
);

-- One row per bookmaker outcome per capture. Prices stay exactly as the
-- provider sent them (decimal odds as text). Nothing here is de-vigged:
-- that is a query-time calculation so the method can change without
-- re-collecting. captured_at is our run timestamp; book_updated is the
-- bookmaker's own last_update.
CREATE TABLE IF NOT EXISTS sportsbook_odds (
    source TEXT NOT NULL,
    event_id TEXT NOT NULL,
    sport_key TEXT NOT NULL,
    commence_time TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    bookmaker TEXT NOT NULL,
    market TEXT NOT NULL,
    outcome TEXT NOT NULL,
    price_decimal TEXT NOT NULL,
    book_updated TEXT,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (source, event_id, bookmaker, market, outcome, captured_at)
);

CREATE INDEX IF NOT EXISTS idx_sportsbook_sport_time
    ON sportsbook_odds (sport_key, captured_at);

-- Which sportsbook game a Kalshi event was tied to, and how. Rewritten on
-- every compare run; kept so a wrong match is debuggable after the fact.
-- kickoff_delta_s is commence_time minus the ticker date at midnight US
-- Eastern, so a 01:15Z Monday kickoff shows as ~73,000 s past Sunday.
CREATE TABLE IF NOT EXISTS line_matches (
    event_ticker TEXT NOT NULL,
    source TEXT NOT NULL,
    event_id TEXT NOT NULL,
    method TEXT NOT NULL,
    kickoff_delta_s INTEGER,
    matched_at TEXT NOT NULL,
    PRIMARY KEY (event_ticker, source)
);

-- Fee schedule per series from GET /series/{ticker}. Raw strings: the
-- multiplier is parsed with Decimal where it is used.
CREATE TABLE IF NOT EXISTS series_fees (
    series_ticker TEXT PRIMARY KEY,
    fee_type TEXT,
    fee_multiplier TEXT,
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_markets_series_result
    ON markets (series_ticker, result);

CREATE INDEX IF NOT EXISTS idx_pre_event_series
    ON pre_event_prices (series_ticker);

CREATE INDEX IF NOT EXISTS idx_snapshots_series_time
    ON price_snapshots (series_ticker, captured_at);

-- One NWS hourly-max per event per scan. captured_at is the scan's UTC
-- timestamp. lead_hours is how far ahead that high hour was. Later fits
-- read this instead of guessing what the grid used to say.
CREATE TABLE IF NOT EXISTS nws_forecasts (
    event_ticker TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    series_ticker TEXT NOT NULL,
    station_id TEXT NOT NULL,
    market_date TEXT NOT NULL,
    forecast_high_f REAL NOT NULL,
    lead_hours REAL NOT NULL,
    valid_time TEXT,
    station_name TEXT,
    source TEXT NOT NULL,
    PRIMARY KEY (event_ticker, captured_at)
);

CREATE INDEX IF NOT EXISTS idx_nws_forecasts_station_date
    ON nws_forecasts (station_id, market_date);

-- One settled daily high per event, from expiration_value or the winning bin.
CREATE TABLE IF NOT EXISTS weather_days (
    event_ticker TEXT PRIMARY KEY,
    series_ticker TEXT NOT NULL,
    station_id TEXT NOT NULL,
    market_date TEXT NOT NULL,
    settlement_high_f REAL NOT NULL,
    exact INTEGER NOT NULL,
    place TEXT,
    timezone TEXT,
    close_time TEXT,
    fetched_at TEXT NOT NULL
);

-- Archived model high (Open-Meteo previous runs), one row per lead.
CREATE TABLE IF NOT EXISTS archive_forecasts (
    station_id TEXT NOT NULL,
    market_date TEXT NOT NULL,
    lead_hours INTEGER NOT NULL,
    model TEXT NOT NULL,
    forecast_high_f REAL NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (station_id, market_date, lead_hours, model)
);

-- Settled brackets that the weather score can join to pre_event_prices.
CREATE TABLE IF NOT EXISTS weather_brackets (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT NOT NULL,
    series_ticker TEXT NOT NULL,
    station_id TEXT NOT NULL,
    market_date TEXT NOT NULL,
    strike_type TEXT,
    floor_strike REAL,
    cap_strike REAL,
    outcome REAL
);
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

_UPSERT_ODDS = """
INSERT INTO sportsbook_odds (
    source, event_id, sport_key, commence_time, home_team, away_team,
    bookmaker, market, outcome, price_decimal, book_updated, captured_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(source, event_id, bookmaker, market, outcome, captured_at) DO UPDATE SET
    sport_key = excluded.sport_key,
    commence_time = excluded.commence_time,
    home_team = excluded.home_team,
    away_team = excluded.away_team,
    price_decimal = excluded.price_decimal,
    book_updated = excluded.book_updated
"""

_UPSERT_CAPTURE = """
INSERT INTO odds_captures (source, sport_key, captured_at, regions, events, rows)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(source, sport_key, captured_at) DO UPDATE SET
    regions = excluded.regions,
    events = excluded.events,
    rows = excluded.rows
"""

_UPSERT_MATCH = """
INSERT INTO line_matches (event_ticker, source, event_id, method, kickoff_delta_s, matched_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(event_ticker, source) DO UPDATE SET
    event_id = excluded.event_id,
    method = excluded.method,
    kickoff_delta_s = excluded.kickoff_delta_s,
    matched_at = excluded.matched_at
"""

_UPSERT_SERIES_FEE = """
INSERT INTO series_fees (series_ticker, fee_type, fee_multiplier, fetched_at)
VALUES (?, ?, ?, ?)
ON CONFLICT(series_ticker) DO UPDATE SET
    fee_type = excluded.fee_type,
    fee_multiplier = excluded.fee_multiplier,
    fetched_at = excluded.fetched_at
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

    def save_series_fee(self, fee: SeriesFee) -> None:
        with self.conn:
            self.conn.execute(_UPSERT_SERIES_FEE, fee.as_tuple())

    def series_fee(self, series_ticker: str) -> SeriesFee | None:
        row = self.conn.execute(
            "SELECT * FROM series_fees WHERE series_ticker = ?", (series_ticker,)
        ).fetchone()
        if row is None:
            return None
        return SeriesFee(
            series_ticker=row["series_ticker"],
            fee_type=row["fee_type"],
            fee_multiplier=row["fee_multiplier"],
            fetched_at=row["fetched_at"],
        )

    def save_odds(self, capture: OddsCapture, rows: list[OddsRow]) -> None:
        """Record one capture and its rows in one transaction.

        The capture row is written even when ``rows`` is empty, so a run
        that found no moneylines still becomes the latest capture. A crash
        mid-write leaves neither half behind.
        """
        with self.conn:
            self.conn.execute(_UPSERT_CAPTURE, capture.as_tuple())
            if rows:
                self.conn.executemany(_UPSERT_ODDS, [row.as_tuple() for row in rows])

    def latest_odds_capture(self, sport_key: str) -> tuple[str | None, list[sqlite3.Row]]:
        """The newest capture for one sport: its timestamp and every row in it.

        "Newest" comes from ``odds_captures``, so an empty capture returns
        its timestamp with no rows rather than falling back to older odds.
        """
        latest = self.conn.execute(
            "SELECT MAX(captured_at) FROM odds_captures WHERE sport_key = ?", (sport_key,)
        ).fetchone()[0]
        if latest is None:
            return None, []
        rows = self.conn.execute(
            "SELECT * FROM sportsbook_odds WHERE sport_key = ? AND captured_at = ? "
            "ORDER BY event_id, bookmaker, outcome",
            (sport_key, latest),
        ).fetchall()
        return latest, rows

    def latest_snapshots(self, series_ticker: str) -> tuple[str | None, list[sqlite3.Row]]:
        """Quotes from the newest snapshot run of one series, joined to the market row.

        Only the newest run: an edge is a comparison of two prices at about
        the same time, and an older snapshot would be a comparison with the past.
        """
        latest = self.conn.execute(
            "SELECT MAX(captured_at) FROM price_snapshots WHERE series_ticker = ?",
            (series_ticker,),
        ).fetchone()[0]
        if latest is None:
            return None, []
        rows = self.conn.execute(
            "SELECT s.ticker, s.captured_at, s.yes_ask_dollars, s.no_ask_dollars, "
            "s.yes_bid_dollars, s.no_bid_dollars, s.status, "
            "m.event_ticker, m.yes_sub_title, m.notional_value, m.result "
            "FROM price_snapshots AS s JOIN markets AS m ON m.ticker = s.ticker "
            "WHERE s.series_ticker = ? AND s.captured_at = ? ORDER BY s.ticker",
            (series_ticker, latest),
        ).fetchall()
        return latest, rows

    def save_line_matches(self, rows: list[tuple[str, str, str, str, int | None, str]]) -> None:
        with self.conn:
            self.conn.executemany(_UPSERT_MATCH, rows)

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

    def save_nws_forecast(
        self,
        *,
        event_ticker: str,
        captured_at: str,
        series_ticker: str,
        station_id: str,
        market_date: str,
        forecast_high_f: float,
        lead_hours: float,
        valid_time: str | None,
        station_name: str,
        source: str = "nws-hourly-max",
    ) -> None:
        """Remember the hourly max a scan priced, so a later fit can use it."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO nws_forecasts ("
                "event_ticker, captured_at, series_ticker, station_id, market_date, "
                "forecast_high_f, lead_hours, valid_time, station_name, source"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(event_ticker, captured_at) DO UPDATE SET "
                "series_ticker = excluded.series_ticker, "
                "station_id = excluded.station_id, "
                "market_date = excluded.market_date, "
                "forecast_high_f = excluded.forecast_high_f, "
                "lead_hours = excluded.lead_hours, "
                "valid_time = excluded.valid_time, "
                "station_name = excluded.station_name, "
                "source = excluded.source",
                (
                    event_ticker,
                    captured_at,
                    series_ticker,
                    station_id,
                    market_date,
                    forecast_high_f,
                    lead_hours,
                    valid_time,
                    station_name,
                    source,
                ),
            )

    def nws_forecasts(self) -> list[sqlite3.Row]:
        """Every stored NWS hourly max, oldest capture first."""
        return self.conn.execute(
            "SELECT * FROM nws_forecasts ORDER BY event_ticker, captured_at"
        ).fetchall()

    def save_weather_days(self, rows: list[tuple]) -> None:
        """Upsert settled daily highs. One tuple per ``weather_days`` row."""
        if not rows:
            return
        with self.conn:
            self.conn.executemany(
                "INSERT INTO weather_days ("
                "event_ticker, series_ticker, station_id, market_date, "
                "settlement_high_f, exact, place, timezone, close_time, fetched_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(event_ticker) DO UPDATE SET "
                "series_ticker = excluded.series_ticker, "
                "station_id = excluded.station_id, "
                "market_date = excluded.market_date, "
                "settlement_high_f = excluded.settlement_high_f, "
                "exact = excluded.exact, "
                "place = excluded.place, "
                "timezone = excluded.timezone, "
                "close_time = excluded.close_time, "
                "fetched_at = excluded.fetched_at",
                rows,
            )

    def save_archive_forecasts(self, rows: list[tuple]) -> None:
        """Upsert archived model highs. Columns match ``archive_forecasts``."""
        if not rows:
            return
        with self.conn:
            self.conn.executemany(
                "INSERT INTO archive_forecasts ("
                "station_id, market_date, lead_hours, model, forecast_high_f, fetched_at"
                ") VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(station_id, market_date, lead_hours, model) DO UPDATE SET "
                "forecast_high_f = excluded.forecast_high_f, "
                "fetched_at = excluded.fetched_at",
                rows,
            )

    def save_weather_brackets(self, rows: list[tuple]) -> None:
        """Upsert settled brackets used by the weather score."""
        if not rows:
            return
        with self.conn:
            self.conn.executemany(
                "INSERT INTO weather_brackets ("
                "ticker, event_ticker, series_ticker, station_id, market_date, "
                "strike_type, floor_strike, cap_strike, outcome"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ticker) DO UPDATE SET "
                "event_ticker = excluded.event_ticker, "
                "series_ticker = excluded.series_ticker, "
                "station_id = excluded.station_id, "
                "market_date = excluded.market_date, "
                "strike_type = excluded.strike_type, "
                "floor_strike = excluded.floor_strike, "
                "cap_strike = excluded.cap_strike, "
                "outcome = excluded.outcome",
                rows,
            )

    def weather_days(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM weather_days ORDER BY market_date, event_ticker"
        ).fetchall()

    def archive_forecasts(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM archive_forecasts ORDER BY station_id, market_date, lead_hours"
        ).fetchall()

    def weather_brackets(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM weather_brackets ORDER BY ticker"
        ).fetchall()

    def prices_with_source(self, cutoff_source: str) -> dict[str, PreEventPrice]:
        """Cached quotes whose cutoff was this rule, keyed by ticker."""
        rows = self.conn.execute(
            "SELECT * FROM pre_event_prices WHERE cutoff_source = ?",
            (cutoff_source,),
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
