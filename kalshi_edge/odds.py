"""Capture sportsbook moneylines from The Odds API into SQLite.

One run is one capture: every bookmaker's head-to-head price for every game
the provider lists for the sport, stamped with the run's start time. Run it
right before ``python -m kalshi_edge.collect --mode snapshot`` so the two
prices being compared were taken minutes apart, not hours.

The only secret in this project enters here. ``ODDS_API_KEY`` is read from
the environment in ``main`` and handed to the client; nothing else in the
code base reads it, the client scrubs it from logs and errors, and this
module never prints a URL. See ``kalshi_edge.sportsbook.client``.

Credits: ``/odds`` costs one credit per region per market, so the default
``--regions us,eu`` costs two per run. The free tier is 500 a month. The run
first reads the usage counters from a free endpoint and stops before
spending anything if fewer than ``--min-remaining`` are left.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from kalshi_edge.collect import utc_now
from kalshi_edge.sportsbook.client import (
    DEFAULT_BASE_URL,
    Credits,
    OddsApiClient,
    OddsApiError,
)
from kalshi_edge.sportsbook.parse import SOURCE, OddsCapture, parse_odds_payload
from kalshi_edge.store import MarketStore

logger = logging.getLogger(__name__)

ENV_VAR = "ODDS_API_KEY"


@dataclass(frozen=True)
class CaptureStats:
    sport_key: str
    regions: str
    captured_at: str
    events: int
    rows: int
    bookmakers: tuple[str, ...]
    credits: Credits


def run(
    *,
    sport_key: str,
    regions: str,
    client: OddsApiClient,
    store: MarketStore,
    now: str,
) -> CaptureStats:
    client.check_credits()
    events = client.get_odds(sport_key, regions=regions, markets="h2h")
    rows = parse_odds_payload(events, captured_at=now)
    capture = OddsCapture(
        source=SOURCE,
        sport_key=sport_key,
        captured_at=now,
        regions=regions,
        events=len({row.event_id for row in rows}),
        rows=len(rows),
    )
    # Saved even when empty: a successful run with no lines (off-season, a
    # sport key with nothing listed) must replace the previous capture, not
    # leave compare quietly pricing against it.
    store.save_odds(capture, rows)
    if not rows:
        logger.warning("%s: provider returned no %s lines; recorded an empty capture", sport_key, "h2h")
    return CaptureStats(
        sport_key=sport_key,
        regions=regions,
        captured_at=now,
        events=capture.events,
        rows=capture.rows,
        bookmakers=tuple(sorted({row.bookmaker for row in rows})),
        credits=client.credits,
    )


def format_stats(stats: CaptureStats, db_path: Path) -> str:
    return (
        f"odds  sport={stats.sport_key}  regions={stats.regions}  captured_at={stats.captured_at}\n"
        f"events={stats.events}  rows={stats.rows}  bookmakers={','.join(stats.bookmakers) or '-'}\n"
        f"credits used={stats.credits.used}  remaining={stats.credits.remaining}  "
        f"last_cost={stats.credits.last_cost}\n"
        f"database={db_path}"
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m kalshi_edge.odds",
        description=(
            "Capture sportsbook moneylines into SQLite. Reads the Odds API key from "
            f"the {ENV_VAR} environment variable; it is never a flag."
        ),
        epilog=(
            "examples:\n"
            "  python -m kalshi_edge.odds --sport americanfootball_nfl\n"
            "  python -m kalshi_edge.odds --sport americanfootball_nfl --regions us\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sport", default="americanfootball_nfl", help="Odds API sport key (default: americanfootball_nfl)."
    )
    parser.add_argument(
        "--regions",
        default="us,eu",
        help="Bookmaker regions, comma-separated. eu adds Pinnacle. One credit each (default: us,eu).",
    )
    parser.add_argument("--db", type=Path, default=Path("data/kalshi.db"), help="SQLite file (default: data/kalshi.db).")
    parser.add_argument(
        "--min-remaining",
        type=int,
        default=50,
        help="Refuse to spend when fewer credits than this remain (default: 50).",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"Odds API base URL (default: {DEFAULT_BASE_URL}).")
    args = parser.parse_args(argv)
    if args.min_remaining < 0:
        parser.error("--min-remaining must be >= 0")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 2
    api_key = os.environ.get(ENV_VAR, "").strip()
    if not api_key:
        print(
            f"error: set {ENV_VAR} in the environment (free key at https://the-odds-api.com). "
            "It is deliberately not a command-line flag: flags end up in shell history.",
            file=sys.stderr,
        )
        return 2
    try:
        client = OddsApiClient(api_key, base_url=args.base_url, min_remaining=args.min_remaining)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        with client, MarketStore(args.db) as store:
            stats = run(
                sport_key=args.sport,
                regions=args.regions,
                client=client,
                store=store,
                now=utc_now(),
            )
    except (OddsApiError, ValueError) as exc:
        logger.error("%s", exc)
        return 1
    print(format_stats(stats, args.db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
