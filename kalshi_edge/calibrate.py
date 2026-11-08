"""Calibration backtest: did contracts priced at p resolve YES about p of the time?

The question a prediction market invites is whether its prices are honest
probabilities. This module answers it for one series, from data already in
the SQLite file plus the public candlestick history::

    python -m kalshi_edge.calibrate --series KXNFLGAME

Steps, each a function below so it can be tested on its own:

1. Load the settled markets for the series from ``markets`` (so run
   ``collect --mode settled`` first).
2. For each one, decide the **pre-event cutoff**: the instant the outcome
   started to be revealed. Quotes after that are not forecasts. See
   :func:`event_cutoff` for why no Kalshi timestamp is taken at face value.
3. Fetch the hourly candlesticks ending at or before the cutoff, take the
   last candle with a two-sided book, and cache its closing bid and ask in
   ``pre_event_prices``. The second run of the backtest is free.
4. Turn each contract into an observation: the midpoint as the market's
   probability, the ask on each side as the price a taker would have paid,
   and the settlement as the outcome. One contract per event by default,
   because a game-winner event lists both teams, and "A wins" and "B wins"
   are one coin, not two.
5. Bucket by probability, and report count, mean predicted probability,
   realized YES rate, and fee-adjusted ROI of buying each side at the ask.
   The Brier score summarizes the whole series; the score of a constant
   forecast at the base rate is printed next to it as a sanity floor.

Why midpoint for the probability and ask for the ROI: the midpoint is the
market's estimate with the cost of trading stripped out. The ask is what you
actually pay to take that side, and the taker fee comes on top. A bucket can
be perfectly calibrated and still lose money for a taker, which is the usual
finding and the reason the ROI columns exist.

The ROI numbers describe one contract per market held to settlement. They
say nothing about parlays: a multi-leg combination multiplies each leg's
expected loss, so a long-shot combo built from fairly priced legs is
expected to lose more, not less, than the legs on their own.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from sqlite3 import Row

from kalshi_edge.client import DEFAULT_BASE_URL, KalshiAPIError, KalshiClient
from kalshi_edge.collect import normalize_series, utc_now
from kalshi_edge.fees import taker_fee
from kalshi_edge.parse import Candle, PreEventPrice, parse_candles
from kalshi_edge.store import MarketStore

logger = logging.getLogger(__name__)

BINARY_RESULTS = {"yes": 1.0, "no": 0.0}


@dataclass(frozen=True)
class Observation:
    """One contract with a pre-event quote and a binary outcome."""

    ticker: str
    event_ticker: str | None
    probability: float  # midpoint of the pre-event bid and ask
    yes_ask: float  # price paid to buy YES
    no_ask: float  # price paid to buy NO, i.e. 1 - yes_bid
    outcome: float  # 1.0 if the contract settled YES, else 0.0
    cutoff_source: str


@dataclass(frozen=True)
class Bucket:
    lower: float
    upper: float
    count: int
    predicted: float | None  # mean midpoint
    actual: float | None  # realized YES rate
    yes_roi: float | None  # fee-adjusted return of buying YES at the ask
    no_roi: float | None  # fee-adjusted return of buying NO at the ask


@dataclass(frozen=True)
class Report:
    series: str
    settled: int
    with_quote: int
    observations: int
    one_per_event: bool
    contracts: int
    cutoff_sources: dict[str, int]
    skipped: dict[str, int]
    fetched: int
    cached: int
    buckets: list[Bucket]
    brier: float | None
    base_rate: float | None
    base_rate_brier: float | None
    generated_at: str = field(default_factory=utc_now)


def parse_timestamp(text: object) -> int | None:
    """ISO-8601 from the API (``2026-11-08T18:00:00Z``) to a Unix second."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def event_cutoff(
    market: Row | dict, *, event_hours: float, lead_hours: float = 0.0
) -> tuple[int, str] | None:
    """The Unix second after which quotes on this market are not forecasts.

    None of the timestamps Kalshi publishes is the start of the event. On
    ``KXNFLGAME`` (checked against the live API on 2026-11-08):

    * ``occurrence_datetime`` is the scheduled *end* of the game, kickoff
      plus three hours. The name suggests kickoff; the hourly candles say
      otherwise: a contract quoted at 0.62 before kickoff was 0.98 in the
      bar that ended just before its ``occurrence_datetime``.
    * ``expected_expiration_time`` is the same instant, or three hours
      later on early-season rows. When a game ran long Kalshi rewrote
      ``occurrence_datetime`` to the actual final whistle and left the
      expiration at the scheduled one, so the earlier of the two is the
      scheduled end.
    * ``close_time`` is when the book actually closed, minutes after that.

    So every source is treated as the end of the event and pushed back by
    ``event_hours`` (default 4, which lands an hour before an NFL kickoff).
    The earlier of ``occurrence_datetime`` and ``expected_expiration_time``
    is used, then ``close_time`` as a last resort, and the field's name is
    returned so the report can say which one each contract used. A series
    whose ``occurrence_datetime`` really is the start can run with
    ``--event-hours 0``. ``lead_hours`` moves every cutoff earlier again
    for a "what did the market say that morning" question.

    The first version of this function trusted ``occurrence_datetime`` as
    kickoff and used it unshifted. That run scored a Brier of 0.11 on NFL
    games, which no pre-game market achieves; the quotes were in-game.
    """
    if event_hours < 0 or lead_hours < 0:
        raise ValueError("event_hours and lead_hours must be >= 0")
    shift = int(event_hours * 3600) + int(lead_hours * 3600)
    scheduled = [
        (stamp, key)
        for key in ("occurrence_datetime", "expected_expiration_time")
        if (stamp := parse_timestamp(market[key])) is not None
    ]
    if scheduled:
        # On a tie the first listed field wins, so the source stays stable.
        stamp, key = min(scheduled, key=lambda item: item[0])
        return stamp - shift, key
    close = parse_timestamp(market["close_time"])
    if close is not None:
        return close - shift, "close_time"
    return None


def pick_pre_event_candle(candles: Iterable[Candle], cutoff_ts: int) -> Candle | None:
    """The latest candle that ended at or before the cutoff with both quotes.

    A candle ending after the cutoff contains prices from after it, so it is
    excluded even if it is the only one. A candle with a one-sided book has
    no midpoint; the one before it is used instead.
    """
    chosen: Candle | None = None
    for candle in candles:
        if candle.end_ts > cutoff_ts:
            continue
        if candle.midpoint is None:
            continue
        if chosen is None or candle.end_ts > chosen.end_ts:
            chosen = candle
    return chosen


@dataclass
class FetchStats:
    fetched: int = 0
    cached: int = 0
    no_event_time: int = 0
    no_candle: int = 0
    not_found: int = 0


def fetch_pre_event_prices(
    *,
    store: MarketStore,
    client: KalshiClient,
    series_ticker: str,
    markets: Sequence[Row],
    now: str,
    lookback_hours: float = 72.0,
    event_hours: float = 4.0,
    lead_hours: float = 0.0,
    refresh: bool = False,
    max_fetch: int | None = None,
) -> tuple[dict[str, PreEventPrice], FetchStats]:
    """Make sure every settled market has a cached pre-event quote.

    The cutoff is computed first and a cached row is reused only when it
    answers that exact window (same cutoff and source, and a window that
    covers the request). Otherwise a run with ``--lead-hours 12`` after a
    default run would quietly reuse kickoff quotes and report an
    earlier-time backtest it never performed. Each market that is not
    served from the cache costs one request. Results are committed one row
    at a time so an interrupted run keeps its progress. A market the
    exchange answers 404 for (markets archived past Kalshi's historical
    cutoff have no candlesticks at all) is cached as missing history and
    never asked for again, whatever window a later run uses.
    """
    if lookback_hours <= 0:
        raise ValueError("lookback_hours must be > 0")
    if refresh:
        removed = store.clear_pre_event_prices(series_ticker)
        logger.info("refresh: dropped %d cached pre-event quotes for %s", removed, series_ticker)
    cached = store.pre_event_prices(series_ticker)
    stats = FetchStats()
    lookback = int(lookback_hours * 3600)
    for market in markets:
        ticker = market["ticker"]
        cutoff = event_cutoff(market, event_hours=event_hours, lead_hours=lead_hours)
        if cutoff is None:
            stats.no_event_time += 1
            # A stale row from a run that still had an event time would be
            # a quote for a window we can no longer define.
            cached.pop(ticker, None)
            logger.warning("%s has no event time; skipping", ticker)
            continue
        cutoff_ts, source = cutoff
        start_ts = cutoff_ts - lookback
        hit = cached.get(ticker)
        if hit is not None and hit.answers(start_ts=start_ts, cutoff_ts=cutoff_ts, cutoff_source=source):
            stats.cached += 1
            continue
        # Anything left in ``cached`` is handed back as the answer, so a row
        # for the wrong window must not survive a max_fetch cutoff.
        cached.pop(ticker, None)
        if max_fetch is not None and stats.fetched >= max_fetch:
            break
        try:
            raw = client.get_candlesticks(
                series_ticker=series_ticker,
                ticker=ticker,
                start_ts=start_ts,
                end_ts=cutoff_ts,
                period_interval=60,
            )
        except KalshiAPIError as exc:
            if exc.status_code != 404:
                raise
            logger.warning("%s: no candlestick history (404)", ticker)
            stats.not_found += 1
            raw = []
            history_missing = True
        else:
            history_missing = False
        stats.fetched += 1
        candle = pick_pre_event_candle(parse_candles(raw), cutoff_ts)
        if candle is None and not history_missing:
            stats.no_candle += 1
        price = PreEventPrice(
            ticker=ticker,
            series_ticker=series_ticker,
            cutoff_ts=cutoff_ts,
            cutoff_source=source,
            start_ts=start_ts,
            candle_end_ts=None if candle is None else candle.end_ts,
            yes_bid=None if candle is None else candle.yes_bid,
            yes_ask=None if candle is None else candle.yes_ask,
            last_price=None if candle is None else candle.last_price,
            fetched_at=now,
            history_missing=history_missing,
        )
        store.save_pre_event_price(price)
        cached[ticker] = price
    return cached, stats


def build_observations(
    markets: Sequence[Row],
    prices: dict[str, PreEventPrice],
    *,
    one_per_event: bool = True,
) -> tuple[list[Observation], dict[str, int]]:
    """Join settled markets to their cached quotes.

    Skips, counted by reason: a result that is not ``yes``/``no`` (a tie
    settles as ``scalar`` at 0.5 and is neither outcome), no cached quote,
    or a cached miss. With ``one_per_event`` the lowest ticker in each event
    is kept. Ticker order is by team abbreviation, which does not know who
    was favored, so the choice does not tilt the sample toward either side.
    """
    skipped = {"not_binary": 0, "no_quote": 0, "duplicate_event": 0}
    kept: list[Observation] = []
    seen_events: set[str] = set()
    for market in markets:
        outcome = BINARY_RESULTS.get(str(market["result"] or "").lower())
        if outcome is None:
            skipped["not_binary"] += 1
            continue
        price = prices.get(market["ticker"])
        if price is None or price.yes_bid is None or price.yes_ask is None:
            skipped["no_quote"] += 1
            continue
        event = market["event_ticker"]
        if one_per_event and event:
            if event in seen_events:
                skipped["duplicate_event"] += 1
                continue
            seen_events.add(event)
        kept.append(
            Observation(
                ticker=market["ticker"],
                event_ticker=event,
                probability=(price.yes_bid + price.yes_ask) / 2,
                yes_ask=price.yes_ask,
                no_ask=round(1.0 - price.yes_bid, 4),
                outcome=outcome,
                cutoff_source=price.cutoff_source,
            )
        )
    return kept, skipped


def brier_score(observations: Sequence[Observation]) -> float | None:
    """Mean squared error of the probability against the 0/1 outcome.

    0 is perfect, 0.25 is what a constant 50% forecast scores, and the score
    of a constant forecast at the realized base rate is the floor a forecast
    with no information about individual games would reach.
    """
    if not observations:
        return None
    return sum((o.probability - o.outcome) ** 2 for o in observations) / len(observations)


def side_roi(prices_and_payoffs: Sequence[tuple[float, float]], contracts: int) -> float | None:
    """Fee-adjusted return on buying ``contracts`` of each (price, payoff).

    Cost is the price times contracts plus the taker fee on that order;
    payoff is the contract count times the settlement. Return is total
    profit over total cost across the whole bucket, which is the number you
    would see on a statement after betting every contract in it.
    """
    if not prices_and_payoffs:
        return None
    cost = Decimal(0)
    payoff = Decimal(0)
    for price, settlement in prices_and_payoffs:
        stake = Decimal(f"{price:.4f}") * contracts
        cost += stake + taker_fee(price, contracts)
        payoff += Decimal(f"{settlement:.4f}") * contracts
    if cost == 0:
        return None
    return float((payoff - cost) / cost)


def bucket_edges(count: int) -> list[float]:
    if count < 1:
        raise ValueError("bucket count must be >= 1")
    return [i / count for i in range(count + 1)]


def bucket_observations(
    observations: Sequence[Observation],
    edges: Sequence[float],
    *,
    contracts: int = 1,
) -> list[Bucket]:
    """Group by probability into ``[lower, upper)`` bins; the last is closed.

    A probability of exactly 1.0 lands in the top bucket rather than falling
    off the end. Empty buckets are reported with a count of zero so the
    table always has the same rows.
    """
    if len(edges) < 2 or any(b <= a for a, b in pairwise(edges)):
        raise ValueError("edges must be strictly increasing with at least two values")
    groups: list[list[Observation]] = [[] for _ in range(len(edges) - 1)]
    last = len(groups) - 1
    for observation in observations:
        p = observation.probability
        index = last
        for i in range(last):
            if edges[i] <= p < edges[i + 1]:
                index = i
                break
        groups[index].append(observation)
    buckets = []
    for i, group in enumerate(groups):
        n = len(group)
        buckets.append(
            Bucket(
                lower=edges[i],
                upper=edges[i + 1],
                count=n,
                predicted=sum(o.probability for o in group) / n if n else None,
                actual=sum(o.outcome for o in group) / n if n else None,
                yes_roi=side_roi([(o.yes_ask, o.outcome) for o in group], contracts),
                no_roi=side_roi([(o.no_ask, 1.0 - o.outcome) for o in group], contracts),
            )
        )
    return buckets


def run(
    *,
    series_ticker: str,
    client: KalshiClient,
    store: MarketStore,
    now: str,
    buckets: int = 10,
    contracts: int = 1,
    one_per_event: bool = True,
    lookback_hours: float = 72.0,
    event_hours: float = 4.0,
    lead_hours: float = 0.0,
    refresh: bool = False,
    max_fetch: int | None = None,
) -> Report:
    markets = store.settled_markets(series_ticker)
    prices, fetch_stats = fetch_pre_event_prices(
        store=store,
        client=client,
        series_ticker=series_ticker,
        markets=markets,
        now=now,
        lookback_hours=lookback_hours,
        event_hours=event_hours,
        lead_hours=lead_hours,
        refresh=refresh,
        max_fetch=max_fetch,
    )
    observations, skipped = build_observations(markets, prices, one_per_event=one_per_event)
    skipped.update(
        {
            "no_event_time": fetch_stats.no_event_time,
            "no_candle": fetch_stats.no_candle,
            "not_found": fetch_stats.not_found,
        }
    )
    with_quote = sum(1 for p in prices.values() if p.yes_bid is not None and p.yes_ask is not None)
    sources: dict[str, int] = {}
    for observation in observations:
        sources[observation.cutoff_source] = sources.get(observation.cutoff_source, 0) + 1
    base_rate = (
        sum(o.outcome for o in observations) / len(observations) if observations else None
    )
    base_rate_brier = (
        sum((base_rate - o.outcome) ** 2 for o in observations) / len(observations)
        if observations and base_rate is not None
        else None
    )
    return Report(
        series=series_ticker,
        settled=len(markets),
        with_quote=with_quote,
        observations=len(observations),
        one_per_event=one_per_event,
        contracts=contracts,
        cutoff_sources=sources,
        skipped=skipped,
        fetched=fetch_stats.fetched,
        cached=fetch_stats.cached,
        buckets=bucket_observations(observations, bucket_edges(buckets), contracts=contracts),
        brier=brier_score(observations),
        base_rate=base_rate,
        base_rate_brier=base_rate_brier,
    )


def _pct(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "-"
    return f"{value * 100:+.1f}%" if signed else f"{value * 100:.1f}%"


def _num(value: float | None) -> str:
    return "-" if value is None else f"{value:.4f}"


def format_report(report: Report) -> str:
    sample = "one contract per event" if report.one_per_event else "every contract"
    sources = ", ".join(f"{k}={v}" for k, v in sorted(report.cutoff_sources.items())) or "-"
    skipped = ", ".join(f"{k}={v}" for k, v in report.skipped.items() if v)
    lines = [
        f"calibration  series={report.series}  generated={report.generated_at}",
        (
            f"settled={report.settled}  with_pre_event_quote={report.with_quote}  "
            f"observations={report.observations} ({sample})"
        ),
        f"candles fetched={report.fetched}  cached={report.cached}  skipped: {skipped or 'none'}",
        f"cutoff source: {sources}",
        (
            f"brier={_num(report.brier)}  base_rate={_num(report.base_rate)}  "
            f"base_rate_brier={_num(report.base_rate_brier)}"
        ),
        "",
        (
            f"{'bucket':>9}  {'n':>5}  {'predicted':>9}  {'actual':>8}  "
            f"{'yes ROI':>8}  {'no ROI':>8}   (buy at ask, taker fee, {report.contracts} contract(s))"
        ),
    ]
    for b in report.buckets:
        label = f"{b.lower * 100:.0f}-{b.upper * 100:.0f}%"
        lines.append(
            f"{label:>9}  {b.count:>5}  {_pct(b.predicted):>9}  {_pct(b.actual):>8}  "
            f"{_pct(b.yes_roi, signed=True):>8}  {_pct(b.no_roi, signed=True):>8}"
        )
    return "\n".join(lines)


def report_to_json(report: Report) -> str:
    return json.dumps(asdict(report), indent=2, sort_keys=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m kalshi_edge.calibrate",
        description=(
            "Compare pre-event Kalshi prices with how the contracts settled. "
            "Reads settled markets from the SQLite file written by "
            "kalshi_edge.collect --mode settled, fetches each one's price "
            "history from the public API, and prints a calibration table."
        ),
        epilog=(
            "examples:\n"
            "  python -m kalshi_edge.collect --series KXNFLGAME --mode settled\n"
            "  python -m kalshi_edge.calibrate --series KXNFLGAME\n"
            "  python -m kalshi_edge.calibrate --series KXNFLGAME --lead-hours 12 --json out.json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--series", required=True, help="One series ticker, e.g. KXNFLGAME.")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data/kalshi.db"),
        help="SQLite file (default: data/kalshi.db).",
    )
    parser.add_argument(
        "--buckets", type=int, default=10, help="Equal-width probability buckets (default: 10)."
    )
    parser.add_argument(
        "--contracts", type=int, default=1, help="Contracts per market for the ROI columns (default: 1)."
    )
    parser.add_argument(
        "--all-contracts",
        action="store_true",
        help="Keep every contract instead of one per event. Both sides of a game then count twice.",
    )
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=72.0,
        help="How far before the cutoff to look for the last candle (default: 72).",
    )
    parser.add_argument(
        "--event-hours",
        type=float,
        default=4.0,
        help=(
            "Hours between the event's start and the timestamp Kalshi publishes "
            "(occurrence_datetime is the scheduled end of an NFL game, not kickoff). "
            "The cutoff is that timestamp minus this (default: 4)."
        ),
    )
    parser.add_argument(
        "--lead-hours",
        type=float,
        default=0.0,
        help="Move the cutoff this many hours earlier still (default: 0).",
    )
    parser.add_argument("--refresh", action="store_true", help="Discard cached pre-event quotes and refetch.")
    parser.add_argument(
        "--max-fetch", type=int, help="Stop after this many candlestick requests (for a quick first look)."
    )
    parser.add_argument("--json", type=Path, help="Also write the report as JSON to this path.")
    parser.add_argument(
        "--min-interval", type=float, default=0.25, help="Seconds between requests (default: 0.25)."
    )
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL, help=f"Trade API base URL (default: {DEFAULT_BASE_URL})."
    )
    args = parser.parse_args(argv)
    if args.buckets < 1:
        parser.error("--buckets must be >= 1")
    if args.contracts < 1:
        parser.error("--contracts must be >= 1")
    if args.lookback_hours <= 0:
        parser.error("--lookback-hours must be > 0")
    if args.event_hours < 0 or args.lead_hours < 0:
        parser.error("--event-hours and --lead-hours must be >= 0")
    if args.max_fetch is not None and args.max_fetch < 0:
        parser.error("--max-fetch must be >= 0")
    if args.min_interval < 0:
        parser.error("--min-interval must be >= 0")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 2
    try:
        (series_ticker,) = normalize_series([args.series])
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not args.db.exists():
        print(
            f"error: {args.db} does not exist; run "
            f"python -m kalshi_edge.collect --series {series_ticker} --mode settled first",
            file=sys.stderr,
        )
        return 2

    client = KalshiClient(base_url=args.base_url, min_interval=args.min_interval)
    try:
        with MarketStore(args.db) as store:
            report = run(
                series_ticker=series_ticker,
                client=client,
                store=store,
                now=utc_now(),
                buckets=args.buckets,
                contracts=args.contracts,
                one_per_event=not args.all_contracts,
                lookback_hours=args.lookback_hours,
                event_hours=args.event_hours,
                lead_hours=args.lead_hours,
                refresh=args.refresh,
                max_fetch=args.max_fetch,
            )
    except KalshiAPIError as exc:
        logger.error("%s", exc)
        return 1
    finally:
        client.close()
    if report.settled == 0:
        print(
            f"no settled markets for {series_ticker} in {args.db}; run "
            f"python -m kalshi_edge.collect --series {series_ticker} --mode settled",
            file=sys.stderr,
        )
        return 1
    print(format_report(report))
    if args.json is not None:
        args.json.write_text(report_to_json(report), encoding="utf-8")
        logger.info("wrote %s", args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
