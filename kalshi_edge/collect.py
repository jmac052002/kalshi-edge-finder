"""Collect public Kalshi markets into SQLite.

Two modes, one code path. The series ticker decides what is collected
(NFL games, anytime touchdowns, weather, elections); nothing here is
sport-specific.

``snapshot``
    ``GET /markets?status=open`` for each series. Upserts the market and
    appends a price snapshot stamped with the run's start time. Run it on a
    schedule and the snapshot table becomes a price history of contracts
    that were still trading. That history is the forecast side of a later
    calibration check.

``settled``
    Markets Kalshi has finished paying out. Two endpoints, because the
    exchange archives old settlements:

    * ``GET /markets?status=settled`` — recently finalized markets.
    * ``GET /historical/markets`` — markets settled before the moving cutoff
      (``GET /historical/cutoff``, field ``market_settled_ts``).

    Both are unauthenticated. Rows land in ``markets`` with ``result`` and
    ``settlement_value``. This mode does not write price snapshots. The last
    trade on a finalized game is usually about $0 or $1, because everyone
    can see who won. Recording that as if it were a pre-game forecast would
    make every market look perfectly calibrated.

A YES price of 0.45 is the market's price for a 45% chance (the contract
pays $1 if YES). The bid is the best price a buyer will pay; the ask is the
cheapest price a seller will take. The spread is the cost of crossing the
book, and a wide spread means the midpoint is a soft number.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from kalshi_edge.client import DEFAULT_BASE_URL, KalshiAPIError, KalshiClient
from kalshi_edge.parse import parse_market, parse_snapshot
from kalshi_edge.store import MarketStore

logger = logging.getLogger(__name__)

# Series tickers are short exchange symbols such as KXNFLGAME or KXNFLANYTD.
_SERIES_RE = re.compile(r"[A-Z0-9][A-Z0-9_-]{0,63}")


@dataclass(frozen=True)
class RunStats:
    mode: str
    series: tuple[str, ...]
    markets_written: int
    snapshots_written: int
    skipped: int
    pages: int
    markets_in_db: int
    snapshots_in_db: int
    with_result_in_db: int


def utc_now() -> str:
    """UTC timestamp at one-second resolution, used as the snapshot key."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_series(items: Sequence[str]) -> list[str]:
    """Uppercase, split commas, drop duplicates, reject anything that is not a ticker."""
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in items:
        for part in raw.split(","):
            ticker = part.strip().upper()
            if not ticker:
                continue
            if _SERIES_RE.fullmatch(ticker) is None:
                raise ValueError(f"not a series ticker: {part!r}")
            if ticker not in seen:
                seen.add(ticker)
                ordered.append(ticker)
    if not ordered:
        raise ValueError("provide at least one series ticker")
    return ordered


def load_series_file(path: Path) -> list[str]:
    """Read a JSON config. Accepts ``{"series": ["KXNFLGAME"]}`` or a bare list."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"config not found: {path}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"config is not JSON: {path}") from exc
    raw = data.get("series") if isinstance(data, dict) else data
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError("config series must be a list of ticker strings")
    return raw


def resolve_series(cli_series: Sequence[str] | None, config: Path | None) -> list[str]:
    items: list[str] = []
    if config is not None:
        items.extend(load_series_file(config))
    if cli_series:
        items.extend(cli_series)
    return normalize_series(items)


def run(
    *,
    series: Sequence[str],
    mode: str,
    client: KalshiClient,
    store: MarketStore,
    now: str,
    limit: int = 200,
    max_pages: int = 500,
) -> RunStats:
    """Collect ``series`` in ``mode`` and return counts for this run and the file.

    ``now`` is the snapshot timestamp for the whole run. Passing it in keeps
    tests deterministic and keeps a multi-page run as one observation: a
    retried page lands on the same primary key.
    """
    if mode == "snapshot":
        # Open markets only. Their lifecycle status comes back as "active".
        jobs = (
            {"status": "open", "historical": False, "source": "live", "snapshots": True},
        )
    elif mode == "settled":
        # Archive first, then the live settled list, so a ticker present in
        # both keeps the live copy (the store also enforces that).
        jobs = (
            {
                "status": None,
                "historical": True,
                "source": "historical",
                "snapshots": False,
            },
            {
                "status": "settled",
                "historical": False,
                "source": "live",
                "snapshots": False,
            },
        )
    else:
        raise ValueError(f"unknown mode: {mode}")

    markets_written = 0
    snapshots_written = 0
    skipped = 0
    pages = 0

    for series_ticker in series:
        for job in jobs:
            label = "historical" if job["historical"] else str(job["status"])
            for page in client.iter_market_pages(
                series_ticker=series_ticker,
                status=job["status"],
                historical=bool(job["historical"]),
                limit=limit,
                max_pages=max_pages,
            ):
                pages += 1
                markets = []
                snapshots = []
                for payload in page:
                    market = parse_market(
                        payload,
                        series_ticker=series_ticker,
                        source=str(job["source"]),
                        fetched_at=now,
                    )
                    if market is None:
                        skipped += 1
                        logger.warning("skipping %s %s payload with no ticker", series_ticker, label)
                        continue
                    markets.append(market)
                    if job["snapshots"]:
                        snapshot = parse_snapshot(
                            payload,
                            series_ticker=series_ticker,
                            captured_at=now,
                        )
                        if snapshot is not None:
                            snapshots.append(snapshot)
                store.save_page(markets, snapshots)
                markets_written += len(markets)
                snapshots_written += len(snapshots)

    totals = store.counts()
    return RunStats(
        mode=mode,
        series=tuple(series),
        markets_written=markets_written,
        snapshots_written=snapshots_written,
        skipped=skipped,
        pages=pages,
        markets_in_db=totals["markets"],
        snapshots_in_db=totals["snapshots"],
        with_result_in_db=totals["with_result"],
    )


def format_stats(stats: RunStats, db_path: Path) -> str:
    series = ",".join(stats.series)
    return (
        f"{stats.mode}  series={series}  pages={stats.pages}  "
        f"markets_written={stats.markets_written}  "
        f"snapshots_written={stats.snapshots_written}  skipped={stats.skipped}\n"
        f"database={db_path}  markets={stats.markets_in_db}  "
        f"snapshots={stats.snapshots_in_db}  with_result={stats.with_result_in_db}"
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m kalshi_edge.collect",
        description=(
            "Collect public Kalshi market data into SQLite. "
            "No API key. snapshot stores open-market prices; "
            "settled stores determined markets and their results."
        ),
        epilog=(
            "examples:\n"
            "  python -m kalshi_edge.collect --series KXNFLGAME --mode snapshot\n"
            "  python -m kalshi_edge.collect --series KXNFLGAME KXNFLANYTD --mode settled\n"
            "  python -m kalshi_edge.collect --config config/series.example.json --mode snapshot\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--series",
        nargs="+",
        help="Series tickers, e.g. KXNFLGAME KXNFLANYTD. Commas are also split.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help='JSON file with {"series": ["KXNFLGAME"]}. Combined with --series.',
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("snapshot", "settled"),
        help="snapshot: open markets and a price row. settled: results for backtesting.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data/kalshi.db"),
        help="SQLite file to create or update (default: data/kalshi.db).",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=0.25,
        help="Seconds to wait between HTTP requests (default: 0.25).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Page size, 1 to 1000 (default: 200).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=500,
        help="Stop with an error if a series exceeds this many pages (default: 500).",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Trade API base URL (default: {DEFAULT_BASE_URL}).",
    )
    args = parser.parse_args(argv)
    if args.min_interval < 0:
        parser.error("--min-interval must be >= 0")
    if not 1 <= args.limit <= 1000:
        parser.error("--limit must be between 1 and 1000")
    if args.max_pages < 1:
        parser.error("--max-pages must be >= 1")
    if not args.series and args.config is None:
        parser.error("provide --series or --config")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 2
    try:
        series = resolve_series(args.series, args.config)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    client = KalshiClient(base_url=args.base_url, min_interval=args.min_interval)
    try:
        with MarketStore(args.db) as store:
            stats = run(
                series=series,
                mode=args.mode,
                client=client,
                store=store,
                now=utc_now(),
                limit=args.limit,
                max_pages=args.max_pages,
            )
    except KalshiAPIError as exc:
        logger.error("%s", exc)
        return 1
    finally:
        client.close()
    print(format_stats(stats, args.db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
