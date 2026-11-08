"""Calibration backtest: when the market said X%, how often did YES happen?

The prices stored on a settled ``markets`` row are not used. They are the
book after the result is in. Each contract is scored on a quote taken at or
before ``close_time`` minus ``--lead-minutes`` (default 180). A snapshot
inside that window is used as-is. Otherwise we read public candlesticks,
then public trades, from the live endpoint or the historical one depending
on where the collector found the market. No API key.

Scalar results, including ties that paid 50 cents, are excluded. The table
prints a Wilson 95% interval so a bucket of five contracts is not read as a
law of nature. ``yes+ev`` means that even the bottom of that interval clears
the cost of buying one YES at the ask after the taker fee.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

from kalshi_edge.calibration import (
    CalibrationReport,
    Observation,
    binary_outcome,
    build_report,
    format_report,
    save_calibration_plot,
)
from kalshi_edge.client import DEFAULT_BASE_URL, KalshiAPIError, KalshiClient
from kalshi_edge.collect import resolve_series
from kalshi_edge.fees import TAKER_FEE_RATE
from kalshi_edge.quotes import (
    Quote,
    format_time,
    latest_at_or_before,
    parse_time,
    quote_from_candlestick,
    quote_from_snapshot,
    quote_from_trade,
    reference_rule,
)
from kalshi_edge.store import MarketStore

logger = logging.getLogger(__name__)


def _fee_multiplier(client: KalshiClient | None, series: str, fetch: bool) -> float:
    if client is None or not fetch:
        logger.info("%s fee multiplier defaulted to 1 (no series lookup)", series)
        return 1.0
    try:
        series_info = client.get_series(series)
    except KalshiAPIError as exc:
        logger.warning("%s fee lookup failed (%s); using multiplier 1", series, exc)
        return 1.0
    raw = series_info.get("fee_multiplier")
    try:
        multiplier = float(raw)
    except (TypeError, ValueError):
        logger.warning("%s fee_multiplier %r is not a number; using 1", series, raw)
        return 1.0
    if multiplier < 0:
        return 1.0
    fee_type = series_info.get("fee_type")
    logger.info("%s fee_type=%s fee_multiplier=%s", series, fee_type, multiplier)
    return multiplier


def _cached_observation(row, market) -> Observation | None:
    price = row["price"]
    if price is None or row["quote_source"] == "missing":
        return None
    outcome = binary_outcome(market["result"], market["settlement_value"])
    if outcome is None:
        return None
    return Observation(
        ticker=market["ticker"],
        event_ticker=market["event_ticker"],
        price=float(price),
        buy_ask=None if row["buy_ask"] is None else float(row["buy_ask"]),
        outcome=outcome,
        source=row["quote_source"],
    )


def _try_both(call, historical_first: bool):
    """Call ``call(historical)``. On 404, try the other archive tier."""
    order = (True, False) if historical_first else (False, True)
    last_404: KalshiAPIError | None = None
    for historical in order:
        try:
            return call(historical)
        except KalshiAPIError as exc:
            if exc.status_code == 404:
                last_404 = exc
                continue
            raise
    if last_404 is not None:
        return []
    return []


def fetch_quote(
    client: KalshiClient,
    market,
    *,
    cutoff: datetime,
    lookback_minutes: int,
    period_minutes: int,
    max_spread: float,
) -> Quote | None:
    """Candlesticks first, then trades. Both are cut off at ``cutoff``."""
    window_start = cutoff - timedelta(minutes=lookback_minutes)
    start_ts = int(window_start.timestamp())
    end_ts = int(cutoff.timestamp())
    historical_first = market["source"] == "historical"

    def candles(historical: bool):
        return client.get_candlesticks(
            series_ticker=market["series_ticker"],
            ticker=market["ticker"],
            start_ts=start_ts,
            end_ts=end_ts,
            period_interval=period_minutes,
            historical=historical,
        )

    parsed_candles = []
    for candle in _try_both(candles, historical_first):
        quote = quote_from_candlestick(candle, max_spread=max_spread)
        if quote is not None:
            parsed_candles.append(quote)
    chosen = latest_at_or_before(parsed_candles, cutoff, window_start)
    if chosen is not None:
        return chosen

    def trades(historical: bool):
        return client.get_trades(
            ticker=market["ticker"],
            min_ts=start_ts,
            max_ts=end_ts,
            historical=historical,
            limit=100,
        )

    parsed_trades = []
    for trade in _try_both(trades, historical_first):
        quote = quote_from_trade(trade)
        if quote is not None:
            parsed_trades.append(quote)
    return latest_at_or_before(parsed_trades, cutoff, window_start)


def _snapshot_quote(rows, cutoff: datetime, window_start: datetime, max_spread: float) -> Quote | None:
    parsed = []
    for row in rows:
        quote = quote_from_snapshot(
            {
                "captured_at": row["captured_at"],
                "yes_bid": row["yes_bid"],
                "yes_ask": row["yes_ask"],
                "last_price": row["last_price"],
            },
            max_spread=max_spread,
        )
        if quote is not None:
            parsed.append(quote)
    return latest_at_or_before(parsed, cutoff, window_start)


def _save(store: MarketStore, market, rule: str, cutoff: datetime, quote: Quote | None) -> None:
    if quote is None:
        store.save_reference_quote(
            ticker=market["ticker"],
            rule=rule,
            series_ticker=market["series_ticker"],
            cutoff=format_time(cutoff),
            as_of=None,
            yes_bid=None,
            yes_ask=None,
            trade_price=None,
            price=None,
            buy_ask=None,
            quote_source="missing",
        )
        return
    store.save_reference_quote(
        ticker=market["ticker"],
        rule=rule,
        series_ticker=market["series_ticker"],
        cutoff=format_time(cutoff),
        as_of=format_time(quote.as_of),
        yes_bid=quote.yes_bid,
        yes_ask=quote.yes_ask,
        trade_price=quote.trade_price,
        price=quote.price,
        buy_ask=quote.buy_ask,
        quote_source=quote.source,
    )


def calibrate_series(
    *,
    series: str,
    store: MarketStore,
    client: KalshiClient | None,
    lead_minutes: int,
    lookback_minutes: int,
    period_minutes: int,
    max_spread: float,
    bucket_width: float,
    refresh: bool,
    fetch: bool,
) -> CalibrationReport:
    """Score one series. Network calls happen only for contracts with no cached quote."""
    rule = reference_rule(lead_minutes, lookback_minutes, period_minutes, max_spread)
    multiplier = _fee_multiplier(client, series, fetch)
    markets = store.settled_markets(series)
    snapshots = store.snapshots_by_ticker(series)
    cached = {} if refresh else store.cached_quotes(series, rule)
    observations: list[Observation] = []
    n_excluded = 0
    n_missing = 0
    fetched = 0

    for index, market in enumerate(markets, start=1):
        outcome = binary_outcome(market["result"], market["settlement_value"])
        if outcome is None:
            n_excluded += 1
            continue
        ticker = market["ticker"]
        cached_row = cached.get(ticker)
        if cached_row is not None and cached_row["quote_source"] != "missing":
            observation = _cached_observation(cached_row, market)
            if observation is None:
                n_missing += 1
            else:
                observations.append(observation)
            continue
        close_raw = market["close_time"]
        if not isinstance(close_raw, str) or not close_raw.strip():
            n_missing += 1
            continue
        cutoff = parse_time(close_raw) - timedelta(minutes=lead_minutes)
        window_start = cutoff - timedelta(minutes=lookback_minutes)
        # A snapshot inside the window is a price we recorded ourselves.
        # Prefer it over the API, and let one show up later without --refresh.
        quote = _snapshot_quote(snapshots.get(ticker, []), cutoff, window_start, max_spread)
        if quote is None and cached_row is not None:
            # We already asked the API and it had nothing in this window.
            n_missing += 1
            continue
        if quote is None and fetch and client is not None:
            quote = fetch_quote(
                client,
                market,
                cutoff=cutoff,
                lookback_minutes=lookback_minutes,
                period_minutes=period_minutes,
                max_spread=max_spread,
            )
            fetched += 1
            if fetched % 50 == 0:
                logger.info("%s fetched pre-event prices for %d contracts", series, fetched)
        elif quote is None:
            n_missing += 1
            continue
        _save(store, market, rule, cutoff, quote)
        if quote is None:
            n_missing += 1
            continue
        observations.append(
            Observation(
                ticker=ticker,
                event_ticker=market["event_ticker"],
                price=quote.price,
                buy_ask=quote.buy_ask,
                outcome=outcome,
                source=quote.source,
            )
        )
        if index % 200 == 0:
            logger.info("%s scored %d / %d settled rows", series, index, len(markets))

    return build_report(
        series=series,
        observations=observations,
        lead_minutes=lead_minutes,
        lookback_minutes=lookback_minutes,
        period_minutes=period_minutes,
        max_spread=max_spread,
        fee_rate=float(TAKER_FEE_RATE),
        fee_multiplier=multiplier,
        n_settled=len(markets),
        n_excluded=n_excluded,
        n_missing_price=n_missing,
        bucket_width=bucket_width,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m kalshi_edge.calibrate",
        description=(
            "Bucket settled Kalshi contracts by their pre-event price and "
            "compare each bucket to how often YES actually happened. "
            "Does not use the post-outcome quotes on the markets table."
        ),
        epilog=(
            "examples:\n"
            "  python -m kalshi_edge.calibrate --series KXNFLGAME\n"
            "  python -m kalshi_edge.calibrate --series KXNFLGAME --lead-minutes 180 "
            "--plot data/calibration.png\n"
            "  python -m kalshi_edge.calibrate --series KXNFLGAME --lead-minutes 0\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--series", nargs="+", help="Series tickers, e.g. KXNFLGAME.")
    parser.add_argument("--config", type=Path, help="JSON file with a series list.")
    parser.add_argument("--db", type=Path, default=Path("data/kalshi.db"))
    parser.add_argument(
        "--lead-minutes",
        type=int,
        default=180,
        help=(
            "Score the last quote at or before close_time minus this many minutes. "
            "Default 180: for a game that trades until the end, the price at close "
            "already reflects the score. Three hours earlier is near kickoff."
        ),
    )
    parser.add_argument(
        "--lookback-minutes",
        type=int,
        default=360,
        help="How far before the cutoff a quote may be and still count (default 360).",
    )
    parser.add_argument(
        "--period",
        type=int,
        default=60,
        choices=(1, 60, 1440),
        help="Candlestick size in minutes when no snapshot is in the window (default 60).",
    )
    parser.add_argument(
        "--max-spread",
        type=float,
        default=0.10,
        help="Widest bid/ask, in dollars, still treated as a real mid (default 0.10).",
    )
    parser.add_argument(
        "--bucket-width",
        type=float,
        default=0.10,
        help="Probability bucket width (default 0.10, so 0-10%%, 10-20%%, ...).",
    )
    parser.add_argument("--plot", type=Path, help="Write a PNG reliability diagram. Needs matplotlib.")
    parser.add_argument("--refresh", action="store_true", help="Ignore cached reference quotes and refetch.")
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Use only snapshots and quotes already stored. Do not call the API.",
    )
    parser.add_argument("--min-interval", type=float, default=0.25)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args(argv)
    if args.lead_minutes < 0:
        parser.error("--lead-minutes must be >= 0")
    if args.lookback_minutes < 1:
        parser.error("--lookback-minutes must be >= 1")
    if not 0 < args.max_spread <= 1:
        parser.error("--max-spread must be in (0, 1]")
    if not 0 < args.bucket_width <= 1:
        parser.error("--bucket-width must be in (0, 1]")
    if args.min_interval < 0:
        parser.error("--min-interval must be >= 0")
    if not args.series and args.config is None:
        parser.error("provide --series or --config")
    return args


def _plot_path(path: Path, series: str, count: int) -> Path:
    if count == 1:
        return path
    return path.with_name(f"{path.stem}-{series}{path.suffix}")


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 2
    try:
        series_list = resolve_series(args.series, args.config)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    fetch = not args.no_fetch
    client = None if not fetch else KalshiClient(base_url=args.base_url, min_interval=args.min_interval)
    reports: list[CalibrationReport] = []
    try:
        with MarketStore(args.db) as store:
            for series in series_list:
                try:
                    report = calibrate_series(
                        series=series,
                        store=store,
                        client=client,
                        lead_minutes=args.lead_minutes,
                        lookback_minutes=args.lookback_minutes,
                        period_minutes=args.period,
                        max_spread=args.max_spread,
                        bucket_width=args.bucket_width,
                        refresh=args.refresh,
                        fetch=fetch,
                    )
                except KalshiAPIError as exc:
                    logger.error("%s", exc)
                    return 1
                reports.append(report)
                print(format_report(report))
                print()
                if report.n_settled == 0:
                    print(
                        f"No settled {series} rows in {args.db}. "
                        "Run: python -m kalshi_edge.collect --series "
                        f"{series} --mode settled",
                        file=sys.stderr,
                    )
    finally:
        if client is not None:
            client.close()

    if args.plot is not None:
        for report in reports:
            if report.n_used == 0:
                continue
            destination = _plot_path(args.plot, report.series, len(reports))
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                save_calibration_plot(report, str(destination))
            except RuntimeError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            print(f"wrote {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
