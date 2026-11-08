"""Compare open Kalshi game contracts with sportsbook moneylines.

For every open contract in one series this answers four questions:

1. What does the sportsbook market think the fair probability is, after the
   book's margin is removed? (``kalshi_edge.odds_math.devig``)
2. What does the Kalshi contract cost right now at the ask, fee included?
   (``kalshi_edge.fees``; the ask, never the midpoint or last trade, because
   the midpoint is not a price anyone will sell at)
3. What win probability makes that trade break even?
4. Is the sportsbook's fair probability above or below that, and by how much
   per contract?

It reads only from SQLite: the newest ``collect --mode snapshot`` run for
the series and the newest ``odds`` capture for the sport. No network, so it
runs the same against a fixture database as against a live one.

Honesty rules baked into the output: the reference is named (Pinnacle or a
median of the other books), the de-vig method is named and the other method
is shown beside it, the fee schedule's source is named, the time gap between
the two captures is printed and flagged when it is wide, and the caveats
(ties, depth, one reference not the truth) are printed every run. A positive
number here means "cheaper than one sportsbook's view", not "a sure thing".
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from kalshi_edge.collect import normalize_series, utc_now
from kalshi_edge.fees import UnknownFeeTypeError, series_fee
from kalshi_edge.odds_math import (
    DEVIG_METHODS,
    DevigMethod,
    Evaluation,
    contracts_for_budget,
    devig,
    evaluate,
)
from kalshi_edge.parse import SeriesFee
from kalshi_edge.sportsbook.match import (
    KalshiEvent,
    LineMatch,
    SportsbookEvent,
    Unmatched,
    match_events,
    parse_iso_utc,
)
from kalshi_edge.sportsbook.parse import SOURCE
from kalshi_edge.sportsbook.teams import DEFAULT_TABLE_PATH, TeamTable
from kalshi_edge.store import MarketStore

logger = logging.getLogger(__name__)

Reference = Literal["pinnacle", "median"]
REFERENCES: tuple[Reference, ...] = ("pinnacle", "median")
PINNACLE = "pinnacle"

CAVEATS = (
    "fair probabilities come from one reference (a sportsbook), not from the truth",
    "edge is per contract at the current ask; book depth at that price is unknown",
    "NFL ties are ignored: a book pushes, Kalshi pays 0.5 (about 0.2% of games)",
    "a wide time gap between captures means the 'edge' may just be line movement",
)


@dataclass(frozen=True)
class Contract:
    ticker: str
    code: str
    yes_ask: Decimal | None
    no_ask: Decimal | None
    payout: Decimal


@dataclass(frozen=True)
class SideReport:
    ticker: str
    code: str
    team: str
    side: str  # YES or NO
    evaluation: Evaluation
    alt_fair: float


@dataclass(frozen=True)
class EventReport:
    event_ticker: str
    event_id: str
    home_team: str
    away_team: str
    kickoff: str
    reference: str
    reference_updated: str | None
    books: int
    sides: list[SideReport]
    skipped: list[str]


@dataclass
class Report:
    series_ticker: str
    sport_key: str
    generated_at: str
    devig: DevigMethod
    reference: Reference
    contracts: int | None
    budget: Decimal | None
    kalshi_captured_at: str | None
    odds_captured_at: str | None
    gap_minutes: float | None
    max_gap_minutes: float
    fee_source: str
    open_contracts: int
    events: list[EventReport] = field(default_factory=list)
    unmatched: list[Unmatched] = field(default_factory=list)

    @property
    def stale(self) -> bool:
        return self.gap_minutes is not None and self.gap_minutes > self.max_gap_minutes


def _decimal(text: object) -> Decimal | None:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def load_kalshi(store: MarketStore, series_ticker: str) -> tuple[str | None, dict[str, list[Contract]]]:
    """Open contracts from the newest snapshot, grouped by event ticker."""
    captured_at, rows = store.latest_snapshots(series_ticker)
    events: dict[str, list[Contract]] = {}
    for row in rows:
        if row["result"] is not None or not row["event_ticker"]:
            continue
        notional = row["notional_value"]
        payout = Decimal(str(notional)) if notional is not None else Decimal(1)
        contract = Contract(
            ticker=row["ticker"],
            code=row["ticker"].rsplit("-", 1)[-1],
            yes_ask=_decimal(row["yes_ask_dollars"]),
            no_ask=_decimal(row["no_ask_dollars"]),
            payout=payout,
        )
        events.setdefault(row["event_ticker"], []).append(contract)
    return captured_at, events


def load_odds(
    store: MarketStore, sport_key: str
) -> tuple[str | None, list[SportsbookEvent], dict[str, dict[str, dict[str, Decimal]]], dict[str, dict[str, str | None]]]:
    """Newest capture as events, ``prices[event_id][bookmaker][outcome]`` and book update times."""
    captured_at, rows = store.latest_odds_capture(sport_key)
    events: dict[str, SportsbookEvent] = {}
    prices: dict[str, dict[str, dict[str, Decimal]]] = {}
    updated: dict[str, dict[str, str | None]] = {}
    for row in rows:
        if row["market"] != "h2h":
            continue
        price = _decimal(row["price_decimal"])
        if price is None or price <= 1:
            continue
        event_id = row["event_id"]
        events.setdefault(
            event_id,
            SportsbookEvent(
                event_id=event_id,
                sport_key=row["sport_key"],
                commence_time=row["commence_time"],
                home_team=row["home_team"],
                away_team=row["away_team"],
            ),
        )
        prices.setdefault(event_id, {}).setdefault(row["bookmaker"], {})[row["outcome"]] = price
        updated.setdefault(event_id, {})[row["bookmaker"]] = row["book_updated"]
    return captured_at, list(events.values()), prices, updated


def fair_probabilities(
    books: dict[str, dict[str, Decimal]],
    team: str,
    other: str,
    *,
    reference: Reference,
    method: DevigMethod,
) -> tuple[float, str, int] | None:
    """De-vigged probability that ``team`` wins, the reference used, and the book count.

    ``pinnacle`` uses Pinnacle's two prices when the capture has them and
    falls back to the median otherwise. ``median`` is the median across every
    other bookmaker's de-vigged price. On a two-way market the medians of
    the two sides sum to one, so the NO side is just ``1 - q``.
    """
    two_way = {
        key: book for key, book in books.items() if team in book and other in book
    }
    if reference == PINNACLE and PINNACLE in two_way:
        q = devig([two_way[PINNACLE][team], two_way[PINNACLE][other]], method)[0]
        return q, PINNACLE, len(two_way)
    others = [book for key, book in two_way.items() if key != PINNACLE]
    if not others:
        return None
    qs = [devig([book[team], book[other]], method)[0] for book in others]
    return statistics.median(qs), f"median of {len(others)} books", len(two_way)


def _other_method(method: DevigMethod) -> DevigMethod:
    return "multiplicative" if method == "power" else "power"


def build_event_report(
    match: LineMatch,
    contracts: list[Contract],
    books: dict[str, dict[str, Decimal]],
    book_updated: dict[str, str | None],
    table: TeamTable,
    *,
    reference: Reference,
    method: DevigMethod,
    fee: SeriesFee | None,
    fixed_contracts: int | None,
    budget: Decimal | None,
) -> EventReport:
    sides: list[SideReport] = []
    skipped: list[str] = []
    reference_used = "none"
    reference_updated: str | None = None
    book_count = 0
    event = match.event
    for contract in sorted(contracts, key=lambda c: c.ticker):
        try:
            team = table.name(contract.code)
        except ValueError as exc:
            skipped.append(f"{contract.ticker}: {exc}")
            continue
        if team not in event.teams:
            skipped.append(f"{contract.ticker}: {team} is not in this game")
            continue
        other = next(iter(event.teams - {team}))
        primary = fair_probabilities(books, team, other, reference=reference, method=method)
        alternate = fair_probabilities(books, team, other, reference=reference, method=_other_method(method))
        if primary is None or alternate is None:
            skipped.append(f"{contract.ticker}: no bookmaker priced both teams")
            continue
        q_yes, reference_used, book_count = primary
        q_alt = alternate[0]
        reference_updated = book_updated.get(PINNACLE) if reference_used == PINNACLE else None
        for side, price, q, alt in (
            ("YES", contract.yes_ask, q_yes, q_alt),
            ("NO", contract.no_ask, 1.0 - q_yes, 1.0 - q_alt),
        ):
            if price is None or not Decimal(0) < price < contract.payout:
                skipped.append(f"{contract.ticker} {side}: no ask to buy at")
                continue

            def order_fee(count: int, price: Decimal = price) -> Decimal:
                fee_type = fee.fee_type if fee else None
                multiplier = fee.fee_multiplier if fee else None
                return series_fee(price, count, fee_type=fee_type, fee_multiplier=multiplier)

            count = fixed_contracts
            if count is None:
                assert budget is not None
                count = contracts_for_budget(budget, price, order_fee)
                if count < 1:
                    skipped.append(f"{contract.ticker} {side}: one contract at {price} exceeds the budget")
                    continue
            evaluation = evaluate(
                price, q, contracts=count, fee_total=order_fee(count), payout=contract.payout
            )
            sides.append(
                SideReport(
                    ticker=contract.ticker,
                    code=contract.code,
                    team=team,
                    side=side,
                    evaluation=evaluation,
                    alt_fair=alt,
                )
            )
    return EventReport(
        event_ticker=match.event_ticker,
        event_id=event.event_id,
        home_team=event.home_team,
        away_team=event.away_team,
        kickoff=event.commence_time,
        reference=reference_used,
        reference_updated=reference_updated,
        books=book_count,
        sides=sides,
        skipped=skipped,
    )


def run(
    *,
    series_ticker: str,
    store: MarketStore,
    table: TeamTable,
    now: str,
    devig_method: DevigMethod = "power",
    reference: Reference = PINNACLE,
    contracts: int | None = None,
    budget: Decimal | None = Decimal(10),
    max_gap_minutes: float = 15.0,
) -> Report:
    if (contracts is None) == (budget is None):
        raise ValueError("pass exactly one of contracts or budget")
    kalshi_at, kalshi_events = load_kalshi(store, series_ticker)
    odds_at, sportsbook_events, prices, updated = load_odds(store, table.sport_key)
    fee = store.series_fee(series_ticker)
    if fee is None or fee.fee_type is None:
        fee_source = "published default (quadratic, multiplier 1); series_fees row missing, run collect"
        logger.warning("%s: no fee schedule stored; using the published default", series_ticker)
    else:
        fee_source = f"{fee.fee_type} x{fee.fee_multiplier or '1'} from GET /series (fetched {fee.fetched_at})"
    # Fail before printing a table if the exchange reports a schedule we cannot model.
    series_fee("0.50", 1, fee_type=fee.fee_type if fee else None, fee_multiplier=fee.fee_multiplier if fee else None)

    gap = None
    if kalshi_at and odds_at:
        gap = abs((parse_iso_utc(kalshi_at) - parse_iso_utc(odds_at)).total_seconds()) / 60.0

    report = Report(
        series_ticker=series_ticker,
        sport_key=table.sport_key,
        generated_at=now,
        devig=devig_method,
        reference=reference,
        contracts=contracts,
        budget=budget,
        kalshi_captured_at=kalshi_at,
        odds_captured_at=odds_at,
        gap_minutes=gap,
        max_gap_minutes=max_gap_minutes,
        fee_source=fee_source,
        open_contracts=sum(len(c) for c in kalshi_events.values()),
    )
    if not kalshi_events or not sportsbook_events:
        return report

    matched = match_events(
        [KalshiEvent(ticker, frozenset(c.code for c in cs)) for ticker, cs in kalshi_events.items()],
        sportsbook_events,
        table,
    )
    report.unmatched = matched.unmatched
    for match in matched.matches:
        report.events.append(
            build_event_report(
                match,
                kalshi_events[match.event_ticker],
                prices.get(match.event.event_id, {}),
                updated.get(match.event.event_id, {}),
                table,
                reference=reference,
                method=devig_method,
                fee=fee,
                fixed_contracts=contracts,
                budget=budget,
            )
        )
    store.save_line_matches(
        [(m.event_ticker, SOURCE, m.event.event_id, m.method, m.kickoff_delta_s, now) for m in matched.matches]
    )
    return report


def _pp(value: float) -> str:
    return f"{value * 100:+.1f}pp"


def _pct(value: float) -> str:
    return f"{value * 100:+.1f}%"


def _short_time(text: str | None) -> str:
    if not text:
        return "-"
    try:
        return parse_iso_utc(text).strftime("%H:%MZ")
    except ValueError:
        return text


def format_report(report: Report) -> str:
    sizing = f"contracts={report.contracts}" if report.contracts else f"budget=${report.budget}"
    if report.gap_minutes is None:
        gap = "gap=n/a"
    else:
        gap = f"gap={report.gap_minutes:.0f}m"
        if report.stale:
            gap += "  STALE (over the limit)"
    lines = [
        f"compare  series={report.series_ticker}  sport={report.sport_key}  generated={report.generated_at}",
        f"devig={report.devig}  reference={report.reference}  {sizing}",
        (
            f"kalshi snapshot={report.kalshi_captured_at or 'none'}  "
            f"odds capture={report.odds_captured_at or 'none'}  {gap}  (max {report.max_gap_minutes:g}m)"
        ),
        f"fees: {report.fee_source}",
        (
            f"open contracts={report.open_contracts}  matched events={len(report.events)}  "
            f"unmatched={len(report.unmatched)}"
        ),
    ]
    if report.kalshi_captured_at is None:
        lines.append("no open-market snapshot for this series; run collect --mode snapshot first")
    if report.odds_captured_at is None:
        lines.append("no sportsbook capture for this sport; run kalshi_edge.odds first")
    for event in report.events:
        lines.append("")
        lines.append(
            f"{event.event_ticker}  {event.away_team} at {event.home_team}  "
            f"kickoff {event.kickoff}  ref={event.reference}"
            + (f" ({_short_time(event.reference_updated)})" if event.reference_updated else "")
            + f"  books={event.books}"
        )
        if event.sides:
            lines.append(
                f" {'contract':8} {'side':4} {'ask':>6} {'n':>4} {'fee/ct':>7} {'brkeven':>7} "
                f"{'fair':>6} {'alt':>6} {'edge':>7} {'ev/ct':>8} {'roi':>7}"
            )
        for side in event.sides:
            e = side.evaluation
            lines.append(
                f" {side.code:8} {side.side:4} {e.price:>6.2f} {e.contracts:>4} "
                f"{e.fee_per_contract:>7.4f} {e.break_even:>7.4f} {e.fair_probability:>6.4f} "
                f"{side.alt_fair:>6.4f} {_pp(e.edge):>7} {e.ev:>+8.4f} {_pct(e.roi):>7}"
            )
        for note in event.skipped:
            lines.append(f"  skipped {note}")
    if report.unmatched:
        lines.append("")
        lines.append(f"unmatched ({len(report.unmatched)}):")
        for item in report.unmatched:
            lines.append(f"  {item.event_ticker}: {item.reason}")
    lines.append("")
    lines.append(
        f"columns: ask = Kalshi ask; n = contracts; fee/ct = taker fee per contract; "
        f"brkeven = win probability needed to break even; fair = de-vigged "
        f"reference probability ({report.devig}); alt = same with "
        f"{_other_method(report.devig)}; edge = fair - brkeven; ev/ct = expected "
        f"profit per contract in dollars; roi = ev / cost."
    )
    lines.append("read this honestly:")
    lines.extend(f"  - {caveat}" for caveat in CAVEATS)
    return "\n".join(lines)


def report_to_json(report: Report) -> str:
    def convert(value: object) -> object:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [convert(v) for v in value]
        return value

    data = convert(asdict(report))
    assert isinstance(data, dict)
    data["stale"] = report.stale
    data["caveats"] = list(CAVEATS)
    return json.dumps(data, indent=2, sort_keys=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m kalshi_edge.compare",
        description=(
            "Compare open Kalshi contracts with de-vigged sportsbook moneylines. Reads only "
            "the SQLite file: the newest collect --mode snapshot run and the newest "
            "kalshi_edge.odds capture. No network."
        ),
        epilog=(
            "examples:\n"
            "  python -m kalshi_edge.compare --series KXNFLGAME\n"
            "  python -m kalshi_edge.compare --series KXNFLGAME --devig multiplicative --reference median\n"
            "  python -m kalshi_edge.compare --series KXNFLGAME --contracts 10 --json out.json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--series", required=True, help="One series ticker, e.g. KXNFLGAME.")
    parser.add_argument("--db", type=Path, default=Path("data/kalshi.db"), help="SQLite file (default: data/kalshi.db).")
    parser.add_argument(
        "--teams",
        type=Path,
        default=DEFAULT_TABLE_PATH,
        help="Team alias table JSON; also names the Odds API sport (default: config/teams/nfl.json).",
    )
    parser.add_argument("--devig", choices=DEVIG_METHODS, default="power", help="Vig removal method (default: power).")
    parser.add_argument(
        "--reference",
        choices=REFERENCES,
        default=PINNACLE,
        help="Fair-price source: pinnacle (falls back to median when absent) or median of the other books.",
    )
    sizing = parser.add_mutually_exclusive_group()
    sizing.add_argument("--contracts", type=int, help="Fixed contracts per order for the fee calculation.")
    sizing.add_argument(
        "--budget",
        type=Decimal,
        default=Decimal(10),
        help="Dollars per order; contracts = what fits at the ask including fee (default: 10).",
    )
    parser.add_argument(
        "--max-gap-minutes",
        type=float,
        default=15.0,
        help="Flag the report STALE when the two captures are further apart than this (default: 15).",
    )
    parser.add_argument("--json", type=Path, help="Also write the report as JSON to this path.")
    args = parser.parse_args(argv)
    if args.contracts is not None and args.contracts < 1:
        parser.error("--contracts must be >= 1")
    if args.contracts is None and (not args.budget.is_finite() or args.budget <= 0):
        parser.error("--budget must be > 0")
    if args.max_gap_minutes < 0:
        parser.error("--max-gap-minutes must be >= 0")
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
        table = TeamTable.load(args.teams)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not args.db.exists():
        print(f"error: {args.db} does not exist; run collect and odds first", file=sys.stderr)
        return 2
    try:
        with MarketStore(args.db) as store:
            report = run(
                series_ticker=series_ticker,
                store=store,
                table=table,
                now=utc_now(),
                devig_method=args.devig,
                reference=args.reference,
                contracts=args.contracts,
                budget=None if args.contracts is not None else args.budget,
                max_gap_minutes=args.max_gap_minutes,
            )
    except UnknownFeeTypeError as exc:
        logger.error("%s; update kalshi_edge/fees.py before trusting any EV", exc)
        return 1
    print(format_report(report))
    if args.json is not None:
        args.json.write_text(report_to_json(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
