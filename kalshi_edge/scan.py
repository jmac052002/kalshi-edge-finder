"""A daily list of non-sports Kalshi markets.

One command, three jobs:

* Daily city-high temperature markets. The series come from ``GET /series``
  (climate, daily, tagged "Daily temperature", ticker starting with
  ``KXHIGH``). The settlement station comes from the market rules, not from
  the series title. The probability is a normal or Student-t distribution
  around the NWS hourly forecast high, shifted by the bias in
  ``weather_calibration.json``. See ``kalshi_edge.weather_error``.
* Economic releases the owner asked about: CPI, the jobs report, initial
  claims, and the Fed decision. CPI and core CPI, month-over-month and
  year-over-year, get a probability from the Cleveland Fed nowcast when that
  file covers the contract's month. Payrolls, unemployment, claims, and the
  Fed are listed with the price and the settlement time only. There is no
  free consensus feed for those, and the CME FedWatch API is paid. This
  module does not invert fed-funds futures: a futures price is an average
  over the month, not the post-meeting rate, and a wrong inversion would
  be an invented probability.
* Any other open market in the chosen categories (by default Science and
  Technology, Companies, and AI, plus series tagged AI outside sports and
  politics) whose expected settlement is within ``--horizon-days`` (30).
  Nearest first. No model, so none of them is an edge.

An edge is a model probability above the break-even of buying at the ask.
The break-even includes the taker fee on a $10 order (the series schedule
from ``GET /series``). Paying the ask is already the cost of the spread;
the spread is not added again. When no side clears that bar the report
says so, in those words.

Weather and the economic series are written to the same SQLite tables as
``collect``. The short-dated sweep is a listing, not a price history, and
is not stored.

Nothing in this module places an order.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from kalshi_edge.client import DEFAULT_BASE_URL, KalshiAPIError, KalshiClient
from kalshi_edge.fees import UnknownFeeTypeError, series_fee
from kalshi_edge.nowcast import (
    CLEVELAND_FED_BASE,
    CLEVELAND_FED_USER_AGENT,
    NOWCAST_MONTH_PATH,
    NOWCAST_YEAR_PATH,
    NowcastModel,
    parse_nowcast_file,
    probability_print_exceeds,
)
from kalshi_edge.nws import NwsForecasts, make_nws_client
from kalshi_edge.odds_math import contracts_for_budget, evaluate
from kalshi_edge.parse import (
    parse_fixed_point,
    parse_market,
    parse_series_fee,
    parse_snapshot,
)
from kalshi_edge.store import MarketStore
from kalshi_edge.weather import (
    event_date,
    is_daily_high_series,
    normal_cdf,
    nws_station_id,
    settlement_place,
    temperature_yes_probability,
)
from kalshi_edge.weather_error import (
    WeatherCalibration,
    judge_weather_edge,
    load_calibration,
    predict_high,
)

logger = logging.getLogger(__name__)

CHICAGO = ZoneInfo("America/Chicago")
DEFAULT_BUDGET = Decimal(10)
DEFAULT_HORIZON_DAYS = 30
DEFAULT_SHORT_CATEGORIES = ("Science and Technology", "Companies", "AI")
# AI-tagged series in these categories are not pulled into the short list.
# They are huge, and they are not the tech markets the list is for.
EXCLUDED_FROM_AI_TAG = frozenset(
    {"Sports", "Elections", "Entertainment", "Mentions", "Politics"}
)
THIN_VOLUME = 50.0
THIN_OPEN_INTEREST = 20.0
WIDE_SPREAD = Decimal("0.05")
CLOSEST_LIMIT = 8
EDGE_PRINT_LIMIT = 12
VERIFY_PRINT_LIMIT = 8
SHORT_PRINT_LIMIT = 12
SHORT_KEEP_LIMIT = 200
ECON_STRIKE_LIMIT = 6

_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
_EVENT_MONTH = re.compile(
    r"(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})?$"
)

# model is the nowcast kind, or None when the series is price-only on purpose.
ECON_SERIES: tuple[tuple[str, str, str | None, str], ...] = (
    ("KXCPI", "CPI, month over month", "cpi_mom", ""),
    ("KXCPICORE", "core CPI, month over month", "core_cpi_mom", ""),
    ("KXCPIYOY", "CPI, year over year", "cpi_yoy", ""),
    ("KXCPICOREYOY", "core CPI, year over year", "core_cpi_yoy", ""),
    (
        "KXPAYROLLS",
        "nonfarm payrolls",
        None,
        "no free public consensus; price and settlement only",
    ),
    (
        "KXU3",
        "unemployment rate",
        None,
        "no free public consensus; price and settlement only",
    ),
    (
        "KXJOBLESSCLAIMS",
        "initial jobless claims",
        None,
        "no free public consensus; price and settlement only",
    ),
    (
        "KXFEDDECISION",
        "Fed meeting decision",
        None,
        "CME FedWatch is a paid API; a fed-funds future is a month average, not the post-meeting rate",
    ),
    (
        "KXFED",
        "Fed funds upper bound",
        None,
        "no free probability source (same reason as the meeting decision); price and settlement only",
    ),
)

CAVEATS = (
    "an edge means the model probability beats the break-even of a $10 taker order at the ask",
    "the spread is already in that break-even: you pay the ask, and the spread is not added again",
    "weather bias and sd come from weather_calibration.json; a missing fit is not ranked as an edge",
    "a weather gap at least as large as the disagree guard is flagged VERIFY and is not an edge",
    "weather also has to beat the fee break-even by the fit's extra margin",
    "CPI uses the Cleveland Fed nowcast and that file's own errors; payrolls, claims, and the Fed are price only",
    "THIN: 24h volume under 50, open interest under 20, or ask size below the order; WIDE: spread over 5 cents",
    "NOBID: the bid is missing or zero. A thin quote can clear this bar and still be untradable at $10",
    "this tool never places an order",
)


@dataclass(frozen=True)
class ScanRow:
    """One side of one contract, or one short-dated market with no model."""

    ticker: str
    title: str
    side: str
    ask: str | None
    model: float | None
    break_even: float | None
    edge: float | None
    contracts: int | None
    settles_at: str
    settles_chicago: str
    flags: tuple[str, ...]
    why: str
    clears: bool
    series_ticker: str
    event_ticker: str
    no_ask: str | None = None
    volume_24h: float | None = None
    open_interest: float | None = None


@dataclass(frozen=True)
class StrikeQuote:
    ticker: str
    title: str
    yes_ask: str | None
    no_ask: str | None
    volume_24h: float | None
    open_interest: float | None
    flags: tuple[str, ...]


@dataclass(frozen=True)
class EconEventView:
    series_ticker: str
    event_ticker: str
    label: str
    settles_at: str
    settles_chicago: str
    within_horizon: bool
    market_count: int
    summary: str
    strikes: tuple[StrikeQuote, ...]


@dataclass
class Report:
    generated_at: str
    budget: str
    horizon_days: int
    categories: tuple[str, ...]
    modeled: list[ScanRow] = field(default_factory=list)
    econ_events: list[EconEventView] = field(default_factory=list)
    short_dated: list[ScanRow] = field(default_factory=list)
    short_dated_omitted: int = 0
    short_dated_total: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def clears(self) -> list[ScanRow]:
        return [row for row in self.modeled if row.clears]

    @property
    def verify(self) -> list[ScanRow]:
        """Weather sides the model and the ask disagree on by too much to rank."""
        rows = [row for row in self.modeled if "VERIFY" in row.flags]
        rows.sort(key=lambda row: (-(row.edge if row.edge is not None else -2.0), row.ticker, row.side))
        return rows

    @property
    def nothing_clears(self) -> bool:
        return not self.clears


def event_year_month(event_ticker: str | None) -> str | None:
    """``KXCPI-26NOV`` and ``KXJOBLESSCLAIMS-26NOV12`` both yield ``2026-11``."""
    if not event_ticker:
        return None
    match = _EVENT_MONTH.search(event_ticker)
    if match is None:
        return None
    year = 2000 + int(match.group(1))
    month = _MONTHS[match.group(2)]
    return f"{year:04d}-{month:02d}"


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def format_chicago(moment: datetime) -> str:
    return moment.astimezone(CHICAGO).strftime("%Y-%m-%d %H:%M %Z")


def dollars(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return number


def book_flags(
    *,
    bid: Decimal | None,
    ask: Decimal | None,
    volume_24h: float | None,
    open_interest: float | None,
    ask_size: float | None,
    order_contracts: int,
) -> tuple[str, ...]:
    """Liquidity flags. They do not by themselves disqualify an edge."""
    flags: list[str] = []
    if bid is None or bid <= 0:
        flags.append("NOBID")
    if bid is not None and ask is not None and ask - bid > WIDE_SPREAD:
        flags.append("WIDE")
    thin = (
        volume_24h is None
        or volume_24h < THIN_VOLUME
        or open_interest is None
        or open_interest < THIN_OPEN_INTEREST
        or (ask_size is not None and ask_size < order_contracts)
    )
    if thin:
        flags.append("THIN")
    return tuple(flags)


def activity(market: dict) -> tuple[float | None, float | None]:
    volume = parse_fixed_point(market.get("volume_24h_fp"))
    if volume is None:
        volume = parse_fixed_point(market.get("volume_fp"))
    return volume, parse_fixed_point(market.get("open_interest_fp"))


def settlement_of(market: dict) -> datetime | None:
    """When the contract is expected to pay, else when it stops trading."""
    return parse_time(market.get("expected_expiration_time")) or parse_time(market.get("close_time"))


def tradable_within(market: dict, now: datetime, horizon: timedelta) -> bool:
    """Still open, and expected to settle by ``now + horizon``."""
    close = parse_time(market.get("close_time"))
    if close is not None and close < now:
        return False
    settles = settlement_of(market)
    if settles is None:
        return False
    return settles <= now + horizon


def series_in_short_list(entry: dict, categories: frozenset[str]) -> bool:
    category = entry.get("category") or ""
    if category in categories:
        return True
    tags = entry.get("tags") or []
    return isinstance(tags, list) and "AI" in tags and category not in EXCLUDED_FROM_AI_TAG


def _collect_markets(
    client: KalshiClient,
    series_ticker: str,
    *,
    max_pages: int,
    max_close_ts: int | None = None,
) -> tuple[list[dict], str | None]:
    """Pages of open markets. A stuck cursor raises; hitting max-pages keeps the pages already read."""
    rows: list[dict] = []
    try:
        for page in client.iter_market_pages(
            series_ticker=series_ticker,
            status="open",
            limit=200,
            max_pages=max_pages,
            max_close_ts=max_close_ts,
        ):
            rows.extend(item for item in page if isinstance(item, dict))
    except KalshiAPIError as exc:
        if rows and "did not end" in str(exc):
            return rows, f"{series_ticker}: market list stopped early ({exc})"
        raise
    return rows, None


def _persist(
    store: MarketStore,
    series_ticker: str,
    markets: list[dict],
    fee_payload: dict | None,
    fetched_at: str,
) -> None:
    parsed = []
    snapshots = []
    for raw in markets:
        market = parse_market(raw, series_ticker=series_ticker, source="live", fetched_at=fetched_at)
        if market is None:
            continue
        parsed.append(market)
        if market.status in ("active", "open"):
            snapshot = parse_snapshot(raw, series_ticker=series_ticker, captured_at=fetched_at)
            if snapshot is not None:
                snapshots.append(snapshot)
    store.save_page(parsed, snapshots)
    if fee_payload is not None:
        store.save_series_fee(
            parse_series_fee(fee_payload, series_ticker=series_ticker, fetched_at=fetched_at)
        )


def _fee_for(entry: dict | None, fetched_at: str, series_ticker: str):
    if entry is None:
        return None
    return parse_series_fee(entry, series_ticker=series_ticker, fetched_at=fetched_at)


def _price_sides(
    market: dict,
    *,
    yes_probability: float,
    fee,
    budget: Decimal,
    why: str,
    title: str,
    settles: datetime,
    series_ticker: str,
) -> list[ScanRow]:
    """YES and NO at the ask, for one model probability of YES."""
    notional = dollars(market.get("notional_value_dollars")) or Decimal(1)
    if notional <= 0:
        notional = Decimal(1)
    volume, oi = activity(market)
    settles_at = settles.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    settles_local = format_chicago(settles)
    rows: list[ScanRow] = []
    sides = (
        ("YES", "yes_ask_dollars", "yes_bid_dollars", "yes_ask_size_fp", yes_probability),
        ("NO", "no_ask_dollars", "no_bid_dollars", None, 1.0 - yes_probability),
    )
    for side, ask_key, bid_key, size_key, probability in sides:
        ask = dollars(market.get(ask_key))
        bid = dollars(market.get(bid_key))
        if ask is None or not Decimal(0) < ask < notional:
            continue
        probability = min(1.0, max(0.0, probability))
        ask_size = parse_fixed_point(market.get(size_key)) if size_key else None

        def order_fee(count: int, price: Decimal = ask, schedule=fee) -> Decimal:
            return series_fee(
                price,
                count,
                fee_type=schedule.fee_type if schedule is not None else None,
                fee_multiplier=schedule.fee_multiplier if schedule is not None else None,
            )

        count = contracts_for_budget(budget, ask, order_fee)
        if count < 1:
            continue
        priced = evaluate(
            ask,
            probability,
            contracts=count,
            fee_total=order_fee(count),
            payout=notional,
        )
        flags = book_flags(
            bid=bid,
            ask=ask,
            volume_24h=volume,
            open_interest=oi,
            ask_size=ask_size,
            order_contracts=count,
        )
        rows.append(
            ScanRow(
                ticker=str(market.get("ticker") or ""),
                title=title,
                side=side,
                ask=f"{ask:.4f}",
                model=probability,
                break_even=priced.break_even,
                edge=priced.edge,
                contracts=count,
                settles_at=settles_at,
                settles_chicago=settles_local,
                flags=flags,
                why=why,
                clears=probability > priced.break_even,
                series_ticker=series_ticker,
                event_ticker=str(market.get("event_ticker") or ""),
                volume_24h=volume,
                open_interest=oi,
            )
        )
    return rows


def _representative_strikes(markets: list[dict], limit: int) -> tuple[StrikeQuote, ...]:
    """The quotes closest to 50 cents, which is where the book is actually saying something."""
    scored: list[tuple[Decimal, dict]] = []
    for market in markets:
        ask = dollars(market.get("yes_ask_dollars"))
        distance = abs(ask - Decimal("0.5")) if ask is not None else Decimal(2)
        scored.append((distance, market))
    scored.sort(key=lambda item: (item[0], str(item[1].get("ticker") or "")))
    quotes: list[StrikeQuote] = []
    for _distance, market in scored[:limit]:
        volume, oi = activity(market)
        yes_ask = dollars(market.get("yes_ask_dollars"))
        no_ask = dollars(market.get("no_ask_dollars"))
        yes_bid = dollars(market.get("yes_bid_dollars"))
        quotes.append(
            StrikeQuote(
                ticker=str(market.get("ticker") or ""),
                title=str(market.get("yes_sub_title") or market.get("title") or ""),
                yes_ask=None if yes_ask is None else f"{yes_ask:.4f}",
                no_ask=None if no_ask is None else f"{no_ask:.4f}",
                volume_24h=volume,
                open_interest=oi,
                flags=book_flags(
                    bid=yes_bid,
                    ask=yes_ask if yes_ask is not None else Decimal(0),
                    volume_24h=volume,
                    open_interest=oi,
                    ask_size=parse_fixed_point(market.get("yes_ask_size_fp")),
                    order_contracts=1,
                ),
            )
        )
    return tuple(quotes)


def _group(markets: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for market in markets:
        event = market.get("event_ticker") or market.get("ticker") or ""
        grouped[str(event)].append(market)
    return grouped


def _active(markets: list[dict]) -> list[dict]:
    kept = []
    for market in markets:
        status = market.get("status")
        if status in (None, "active", "open"):
            kept.append(market)
    return kept


def _apply_weather_guard(row: ScanRow, calibration: WeatherCalibration, *, trusted: bool) -> ScanRow:
    """Fee break-even stays. A huge gap, or an unfitted width, is not ranked."""
    ask = float(row.ask) if row.ask is not None else None
    clears, verify, note = judge_weather_edge(
        probability=row.model,
        ask=ask,
        break_even=row.break_even,
        trusted=trusted,
        extra_margin=calibration.extra_margin,
        disagree_gap=calibration.disagree_gap,
    )
    flags = row.flags
    why = row.why
    if verify and "VERIFY" not in flags:
        flags = flags + ("VERIFY",)
    if note and note not in why:
        why = f"{why} {note}"
    if clears == row.clears and flags == row.flags and why == row.why:
        return row
    return replace(row, clears=clears, flags=flags, why=why)


def _scan_weather(
    *,
    client: KalshiClient,
    store: MarketStore,
    catalog: dict[str, dict],
    forecasts: NwsForecasts,
    calibration: WeatherCalibration,
    now: datetime,
    horizon: timedelta,
    budget: Decimal,
    fetched_at: str,
    notes: list[str],
) -> list[ScanRow]:
    entries = sorted(
        (entry for entry in catalog.values() if is_daily_high_series(entry)),
        key=lambda entry: entry.get("ticker") or "",
    )
    if not entries:
        notes.append("no daily high series in GET /series (category, frequency, tag, KXHIGH prefix)")
        return []
    rows: list[ScanRow] = []
    stale_highs: list[str] = []
    for entry in entries:
        ticker = str(entry["ticker"])
        try:
            markets, warning = _collect_markets(client, ticker, max_pages=10)
        except KalshiAPIError as exc:
            notes.append(f"{ticker}: {exc}")
            continue
        if warning:
            notes.append(warning)
        markets = _active(markets)
        _persist(store, ticker, markets, entry, fetched_at)
        fee = _fee_for(entry, fetched_at, ticker)
        if not markets:
            continue
        fee_rejected = False
        for event_ticker, event_markets in sorted(_group(markets).items()):
            if fee_rejected:
                break
            day = event_date(event_ticker)
            rules = next(
                (m.get("rules_primary") for m in event_markets if isinstance(m.get("rules_primary"), str)),
                None,
            )
            station = nws_station_id(rules if isinstance(rules, str) else None)
            place = settlement_place(rules if isinstance(rules, str) else None) or ticker
            if day is None or station is None:
                notes.append(f"{event_ticker}: rules did not name a CLI station and a date")
                continue
            settles = settlement_of(event_markets[0])
            if settles is None or not tradable_within(event_markets[0], now, horizon):
                # Same forecast, but money would be tied up past the horizon, so it is not ranked.
                when = format_chicago(settles) if settles else "unknown"
                notes.append(f"{event_ticker}: settlement {when} is outside the horizon; not ranked")
                continue
            high, reason = forecasts.daily_high(station, day, now)
            if high is None:
                if "outside the forecast" in reason:
                    stale_highs.append(event_ticker)
                else:
                    notes.append(f"{event_ticker}: {reason}")
                continue
            store.save_nws_forecast(
                event_ticker=event_ticker,
                captured_at=fetched_at,
                series_ticker=ticker,
                station_id=station,
                market_date=day.isoformat(),
                forecast_high_f=high.temperature_f,
                lead_hours=high.lead_hours,
                valid_time=high.at.isoformat(),
                station_name=high.station_name,
            )
            prediction = predict_high(calibration, station, high.lead_hours, high.temperature_f)
            yes_probs: list[float] = []
            place_name = high.station_name or place
            tail = f", student-t df {prediction.df:.0f}" if prediction.df is not None else ""
            if prediction.trusted:
                why = (
                    f"NWS hourly high {high.temperature_f:.0f}F at {station} ({place_name}), "
                    f"lead {high.lead_hours:.0f}h, bias {prediction.bias_f:+.1f}F, "
                    f"sd {prediction.sd_f:.1f}F{tail} ({prediction.detail})"
                )
            else:
                why = (
                    f"NWS hourly high {high.temperature_f:.0f}F at {station} ({place_name}), "
                    f"lead {high.lead_hours:.0f}h, assumed sd {prediction.sd_f:.1f}F "
                    "(no settlement fit; not ranked as an edge)"
                )
            for market in event_markets:
                probability = temperature_yes_probability(
                    prediction.mean_f,
                    prediction.scale_f,
                    strike_type=market.get("strike_type") if isinstance(market.get("strike_type"), str) else None,
                    floor=_float_or_none(market.get("floor_strike")),
                    cap=_float_or_none(market.get("cap_strike")),
                    df=prediction.df,
                )
                if probability is None:
                    notes.append(
                        f"{market.get('ticker')}: strike {market.get('strike_type')} "
                        f"{market.get('floor_strike')}..{market.get('cap_strike')} is outside the integer-degree model"
                    )
                    continue
                yes_probs.append(probability)
                subtitle = market.get("yes_sub_title") or market.get("title") or ""
                title = f"{place}: {subtitle}"
                try:
                    priced = _price_sides(
                        market,
                        yes_probability=probability,
                        fee=fee,
                        budget=budget,
                        why=why,
                        title=title,
                        settles=settles,
                        series_ticker=ticker,
                    )
                    rows.extend(
                        _apply_weather_guard(row, calibration, trusted=prediction.trusted) for row in priced
                    )
                except (UnknownFeeTypeError, ValueError) as exc:
                    notes.append(f"{ticker}: {exc}")
                    fee_rejected = True
                    break
            if yes_probs and abs(sum(yes_probs) - 1.0) > 0.02:
                notes.append(
                    f"{event_ticker}: model probabilities sum to {sum(yes_probs):.2f} "
                    "(the ladder is not a complete partition of the integers)"
                )
    if stale_highs:
        notes.append(
            f"{len(stale_highs)} events were not priced because the NWS hourly grid no longer "
            f"contains the calendar-day high, including {stale_highs[0]}"
        )
    return rows


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _cpi_probability(model: NowcastModel, market: dict) -> tuple[float | None, str]:
    """Model probability of YES, and the clause that explains it. None if this strike is not a threshold."""
    strike_type = market.get("strike_type")
    floor = _float_or_none(market.get("floor_strike"))
    if strike_type != "greater" or floor is None:
        return None, ""
    rules = market.get("rules_primary") if isinstance(market.get("rules_primary"), str) else ""
    one_decimal = "decimal" in rules.lower()
    if one_decimal:
        probability = probability_print_exceeds(model.value, model.sigma, floor, places=1)
        detail = f"P(one-decimal print > {floor:g})"
    else:
        probability = 1.0 - normal_cdf((floor - model.value) / model.sigma)
        probability = min(1.0, max(0.0, probability))
        detail = f"P(unrounded change > {floor:g}); the rules do not say the print is one-decimal"
    why = (
        f"{model.source} {model.value:.2f} for {model.reference_month} as of {model.as_of_label or 'the file'}, "
        f"sd {model.sigma:.2f} from {model.n_months} finished months "
        f"(mean error {model.mean_error:+.2f} left in); {detail}"
    )
    return probability, why


def _scan_econ(
    *,
    client: KalshiClient,
    store: MarketStore,
    catalog: dict[str, dict],
    models: dict[str, NowcastModel | None],
    model_notes: dict[str, str],
    now: datetime,
    horizon: timedelta,
    budget: Decimal,
    fetched_at: str,
    notes: list[str],
) -> tuple[list[ScanRow], list[EconEventView]]:
    rows: list[ScanRow] = []
    events: list[EconEventView] = []
    for ticker, label, kind, price_only in ECON_SERIES:
        entry = catalog.get(ticker)
        if entry is None:
            notes.append(f"{ticker}: not in GET /series, so it was not queried")
            continue
        try:
            markets, warning = _collect_markets(client, ticker, max_pages=20)
        except KalshiAPIError as exc:
            notes.append(f"{ticker}: {exc}")
            continue
        if warning:
            notes.append(warning)
        markets = _active(markets)
        _persist(store, ticker, markets, entry, fetched_at)
        fee = _fee_for(entry, fetched_at, ticker)
        model = models.get(kind) if kind else None
        if kind and model is None:
            reason = model_notes.get(kind, "Cleveland Fed nowcast was not usable")
        else:
            reason = price_only
        for event_ticker, event_markets in sorted(_group(markets).items()):
            settles = settlement_of(event_markets[0])
            if settles is None:
                notes.append(f"{event_ticker}: no settlement time on the payload")
                continue
            within = tradable_within(event_markets[0], now, horizon)
            month = event_year_month(event_ticker)
            modeled_here = model is not None and month == model.reference_month
            if modeled_here:
                summary = (
                    f"modeled from {model.source} {model.value:.2f} "
                    f"(sd {model.sigma:.2f}, {model.n_months} months)"
                )
            elif model is not None and month and month != model.reference_month:
                summary = (
                    f"price only: the nowcast file covers {model.reference_month}, not {month}"
                )
            else:
                summary = "price only: " + (reason or "no model")
            events.append(
                EconEventView(
                    series_ticker=ticker,
                    event_ticker=event_ticker,
                    label=label,
                    settles_at=settles.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    settles_chicago=format_chicago(settles),
                    within_horizon=within,
                    market_count=len(event_markets),
                    summary=summary,
                    strikes=_representative_strikes(event_markets, ECON_STRIKE_LIMIT) if within else (),
                )
            )
            if not modeled_here or not within:
                continue
            assert model is not None
            for market in event_markets:
                probability, why = _cpi_probability(model, market)
                if probability is None:
                    continue
                subtitle = market.get("yes_sub_title") or market.get("title") or ""
                try:
                    rows.extend(
                        _price_sides(
                            market,
                            yes_probability=probability,
                            fee=fee,
                            budget=budget,
                            why=why,
                            title=f"{label}: {subtitle}",
                            settles=settles,
                            series_ticker=ticker,
                        )
                    )
                except (UnknownFeeTypeError, ValueError) as exc:
                    notes.append(f"{ticker}: {exc}")
                    break
    events.sort(key=lambda event: (not event.within_horizon, event.settles_at, event.event_ticker))
    return rows, events


def _scan_short(
    *,
    client: KalshiClient,
    catalog: dict[str, dict],
    categories: frozenset[str],
    skip: set[str],
    now: datetime,
    horizon: timedelta,
    notes: list[str],
) -> tuple[list[ScanRow], int]:
    open_series: set[str] = set()
    for page in client.iter_event_pages(status="open", limit=200, max_pages=500):
        for event in page:
            if isinstance(event, dict) and isinstance(event.get("series_ticker"), str):
                open_series.add(event["series_ticker"])
    candidates = [
        ticker
        for ticker in sorted(open_series)
        if ticker not in skip and ticker in catalog and series_in_short_list(catalog[ticker], categories)
    ]
    logger.info("short-dated series with an open event: %d", len(candidates))
    found: list[ScanRow] = []
    for ticker in candidates:
        try:
            # No max_close_ts. That parameter filters close_time, and a market
            # can be expected to settle inside the horizon while its close is
            # later. tradable_within applies the expected-settlement cutoff.
            markets, warning = _collect_markets(client, ticker, max_pages=20)
        except KalshiAPIError as exc:
            notes.append(f"{ticker}: {exc}")
            continue
        if warning:
            notes.append(warning)
        entry = catalog[ticker]
        category = entry.get("category") or ""
        for market in _active(markets):
            if not tradable_within(market, now, horizon):
                continue
            settles = settlement_of(market)
            if settles is None:
                continue
            volume, oi = activity(market)
            yes_ask = dollars(market.get("yes_ask_dollars"))
            no_ask = dollars(market.get("no_ask_dollars"))
            yes_bid = dollars(market.get("yes_bid_dollars"))
            title = str(market.get("title") or market.get("yes_sub_title") or ticker)
            found.append(
                ScanRow(
                    ticker=str(market.get("ticker") or ""),
                    title=title,
                    side="",
                    ask=None if yes_ask is None else f"{yes_ask:.4f}",
                    model=None,
                    break_even=None,
                    edge=None,
                    contracts=None,
                    settles_at=settles.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    settles_chicago=format_chicago(settles),
                    flags=book_flags(
                        bid=yes_bid,
                        ask=yes_ask if yes_ask is not None else Decimal(0),
                        volume_24h=volume,
                        open_interest=oi,
                        ask_size=parse_fixed_point(market.get("yes_ask_size_fp")),
                        order_contracts=1,
                    ),
                    why=f"price only; {category}; no model for this listing",
                    clears=False,
                    series_ticker=ticker,
                    event_ticker=str(market.get("event_ticker") or ""),
                    no_ask=None if no_ask is None else f"{no_ask:.4f}",
                    volume_24h=volume,
                    open_interest=oi,
                )
            )
    found.sort(key=lambda row: (row.settles_at, row.ticker))
    omitted = max(0, len(found) - SHORT_KEEP_LIMIT)
    return found[:SHORT_KEEP_LIMIT], omitted


def _load_nowcasts(
    month_charts: object,
    year_charts: object,
) -> tuple[dict[str, NowcastModel | None], dict[str, str]]:
    models: dict[str, NowcastModel | None] = {}
    notes: dict[str, str] = {}
    files = {"month": month_charts, "year": year_charts}
    for kind, period in (
        ("cpi_mom", "month"),
        ("core_cpi_mom", "month"),
        ("cpi_yoy", "year"),
        ("core_cpi_yoy", "year"),
    ):
        charts = files[period]
        if charts is None:
            models[kind] = None
            continue
        model = parse_nowcast_file(charts, kind=kind)
        models[kind] = model
        if model is None:
            notes[kind] = (
                f"Cleveland Fed {period} file had no usable nowcast for {kind} "
                "(missing current value, or fewer than the required finished months); price only"
            )
    return models, notes


def run(
    *,
    client: KalshiClient,
    store: MarketStore,
    now: datetime,
    generated_at: str,
    forecasts: NwsForecasts | None = None,
    calibration: WeatherCalibration | None = None,
    month_charts: object | None = None,
    year_charts: object | None = None,
    nowcast_client: KalshiClient | None = None,
    budget: Decimal = DEFAULT_BUDGET,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    categories: Sequence[str] = DEFAULT_SHORT_CATEGORIES,
    include_short: bool = True,
) -> Report:
    """Build the report. ``now`` decides the horizon and the forecast lead time.

    Nowcast charts may be passed in (tests). Otherwise they are fetched from
    the Cleveland Fed. A failure there becomes a note and CPI stays price-only.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if budget <= 0:
        raise ValueError("budget must be > 0")
    if horizon_days < 1:
        raise ValueError("horizon_days must be >= 1")
    horizon = timedelta(days=horizon_days)
    if calibration is None:
        calibration = load_calibration()
    notes: list[str] = [calibration.scan_note()]
    catalog_rows = client.list_series()
    catalog = {
        str(entry["ticker"]): entry
        for entry in catalog_rows
        if isinstance(entry, dict) and isinstance(entry.get("ticker"), str)
    }
    if month_charts is None or year_charts is None:
        fetcher = nowcast_client
        owns_fetcher = fetcher is None
        if fetcher is None:
            fetcher = KalshiClient(
                base_url=CLEVELAND_FED_BASE,
                user_agent=CLEVELAND_FED_USER_AGENT,
                timeout=60.0,
                min_interval=client.min_interval,
            )
        try:
            if month_charts is None:
                try:
                    month_charts = fetcher.get_json_list(NOWCAST_MONTH_PATH)
                except KalshiAPIError as exc:
                    notes.append(f"Cleveland Fed month nowcast was not fetched ({exc}); CPI is price only")
                    month_charts = None
            if year_charts is None:
                try:
                    year_charts = fetcher.get_json_list(NOWCAST_YEAR_PATH)
                except KalshiAPIError as exc:
                    notes.append(f"Cleveland Fed year nowcast was not fetched ({exc}); CPI year-over-year is price only")
                    year_charts = None
        finally:
            if owns_fetcher:
                fetcher.close()
    models, model_notes = _load_nowcasts(month_charts, year_charts)
    notes.extend(model_notes.values())
    if forecasts is None:
        nws_client = make_nws_client(min_interval=client.min_interval)
        forecasts = NwsForecasts(nws_client)
        close_forecasts: KalshiClient | None = nws_client
    else:
        close_forecasts = None
    try:
        weather_rows = _scan_weather(
            client=client,
            store=store,
            catalog=catalog,
            forecasts=forecasts,
            calibration=calibration,
            now=now,
            horizon=horizon,
            budget=budget,
            fetched_at=generated_at,
            notes=notes,
        )
        econ_rows, econ_events = _scan_econ(
            client=client,
            store=store,
            catalog=catalog,
            models=models,
            model_notes=model_notes,
            now=now,
            horizon=horizon,
            budget=budget,
            fetched_at=generated_at,
            notes=notes,
        )
        chosen = tuple(categories)
        short_rows: list[ScanRow] = []
        omitted = 0
        if include_short:
            skip = {row.series_ticker for row in weather_rows}
            skip.update(ticker for ticker, *_rest in ECON_SERIES)
            short_rows, omitted = _scan_short(
                client=client,
                catalog=catalog,
                categories=frozenset(chosen),
                skip=skip,
                now=now,
                horizon=horizon,
                notes=notes,
            )
    finally:
        if close_forecasts is not None:
            close_forecasts.close()
    modeled = weather_rows + econ_rows
    for row in modeled:
        if not row.series_ticker:
            # Filled above when the payload omitted it. Leave a trace if it is still blank.
            notes.append(f"{row.ticker}: series ticker missing on the row")
    modeled.sort(key=lambda row: (-(row.edge if row.edge is not None else -2.0), row.settles_at, row.ticker, row.side))
    return Report(
        generated_at=generated_at,
        budget=f"{budget:.2f}",
        horizon_days=horizon_days,
        categories=chosen,
        modeled=modeled,
        econ_events=econ_events,
        short_dated=short_rows,
        short_dated_omitted=omitted,
        short_dated_total=len(short_rows) + omitted,
        notes=notes,
    )


def _pp(value: float) -> str:
    return f"{value * 100:+.1f}pp"


def _flags(flags: tuple[str, ...]) -> str:
    return ",".join(flags) if flags else "-"


def format_report(report: Report, *, short_limit: int = SHORT_PRINT_LIMIT) -> str:
    """Plain-text daily list. The first verdict is whether anything clears costs."""
    lines = [
        (
            f"scan  generated={report.generated_at}  budget=${report.budget}  "
            f"horizon={report.horizon_days}d  zone=America/Chicago"
        ),
        (
            f"modeled sides={len(report.modeled)}  clear={len(report.clears)}  "
            f"verify={len(report.verify)}  "
            f"econ events={len(report.econ_events)}  "
            f"short-dated={report.short_dated_total}"
        ),
    ]
    if report.nothing_clears:
        lines.append("nothing clears costs today")
    else:
        shown_edges = report.clears[:EDGE_PRINT_LIMIT]
        if len(report.clears) > EDGE_PRINT_LIMIT:
            lines.append(
                f"edges that clear costs ({len(report.clears)}); "
                f"showing the best {len(shown_edges)}. The rest are in --json."
            )
        else:
            lines.append(f"edges that clear costs ({len(report.clears)}), best first:")
        for row in shown_edges:
            lines.extend(_format_modeled(row))
    if report.verify:
        shown_verify = report.verify[:VERIFY_PRINT_LIMIT]
        lines.append(
            f"model disagrees strongly, verify ({len(report.verify)}), not ranked as edges:"
        )
        if len(report.verify) > VERIFY_PRINT_LIMIT:
            lines.append(f"  showing {len(shown_verify)}; the rest are in --json.")
        for row in shown_verify:
            lines.extend(_format_modeled(row))
    # Near misses matter when nothing cleared. When many sides clear, the top of
    # that list is the daily read and the near misses are noise under it.
    # VERIFY rows are already listed above; they are not near misses.
    if report.nothing_clears:
        closest = [
            row for row in report.modeled if not row.clears and "VERIFY" not in row.flags
        ][:CLOSEST_LIMIT]
        if closest:
            lines.append("closest modeled, do not clear costs:")
            for row in closest:
                lines.extend(_format_modeled(row))
    lines.append("economic releases (price only except where a nowcast month matches):")
    if not report.econ_events:
        lines.append("  none")
    for event in report.econ_events:
        horizon = "inside horizon" if event.within_horizon else "outside horizon, not ranked"
        lines.append(
            f"  {event.event_ticker}  {event.label}  settles {event.settles_chicago}  "
            f"{event.market_count} strikes  {horizon}"
        )
        lines.append(f"    {event.summary}")
        for strike in event.strikes:
            lines.append(
                f"    {strike.ticker}  {strike.title}  YES ask {strike.yes_ask or '-'}  "
                f"NO ask {strike.no_ask or '-'}  vol24h {_num(strike.volume_24h)}  {_flags(strike.flags)}"
            )
    preview, event_count = _one_per_event(report.short_dated, short_limit)
    lines.append(
        "short-dated "
        + ", ".join(report.categories)
        + ", nearest first, one strike per event (the yes ask closest to 50 cents). "
        + f"price only, not edges. shown {len(preview)} events of {event_count} "
        + f"({report.short_dated_total} markets)."
    )
    if report.short_dated_omitted:
        lines.append(
            f"  {report.short_dated_omitted} further markets were past the {SHORT_KEEP_LIMIT} kept in this report"
        )
    if not preview:
        lines.append("  no open market in those categories settles inside the horizon")
    for row in preview:
        lines.append(
            f"  {row.ticker}  settles {row.settles_chicago}  YES ask {row.ask or '-'}  "
            f"NO ask {row.no_ask or '-'}  vol24h {_num(row.volume_24h)}  {_flags(row.flags)}"
        )
        lines.append(f"    {row.title}")
    if report.notes:
        lines.append("notes:")
        for note in report.notes:
            lines.append(f"  - {note}")
    lines.append("read this honestly:")
    for caveat in CAVEATS:
        lines.append(f"  - {caveat}")
    return "\n".join(lines)


def _one_per_event(rows: list[ScanRow], limit: int) -> tuple[list[ScanRow], int]:
    """One market per event: the yes ask closest to 50 cents, events nearest first."""
    chosen: dict[str, ScanRow] = {}
    for row in rows:
        key = row.event_ticker or row.ticker
        current = chosen.get(key)
        if current is None or _ask_distance(row) < _ask_distance(current):
            chosen[key] = row
    ordered = sorted(chosen.values(), key=lambda row: (row.settles_at, row.event_ticker, row.ticker))
    return ordered[:limit], len(ordered)


def _ask_distance(row: ScanRow) -> Decimal:
    if row.ask is None:
        return Decimal(2)
    try:
        return abs(Decimal(row.ask) - Decimal("0.5"))
    except InvalidOperation:
        return Decimal(2)


def _format_modeled(row: ScanRow) -> list[str]:
    model = "-" if row.model is None else f"{row.model:.3f}"
    break_even = "-" if row.break_even is None else f"{row.break_even:.3f}"
    edge = "-" if row.edge is None else _pp(row.edge)
    return [
        (
            f"  {row.ticker}  {row.side or '-'}  {row.title}  ask {row.ask or '-'}  "
            f"model {model}  brkeven {break_even}  edge {edge}  "
            f"settles {row.settles_chicago}  {_flags(row.flags)}"
        ),
        f"    {row.why}",
    ]


def _num(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.0f}"


def report_to_json(report: Report) -> str:
    def convert(value: object) -> object:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [convert(item) for item in value]
        return value

    data = convert(asdict(report))
    assert isinstance(data, dict)
    data["nothing_clears"] = report.nothing_clears
    data["clear_count"] = len(report.clears)
    data["verify_count"] = len(report.verify)
    data["caveats"] = list(CAVEATS)
    return json.dumps(data, indent=2, sort_keys=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m kalshi_edge.scan",
        description=(
            "Rank non-sports Kalshi markets for a small, short-dated account. "
            "Public Kalshi data, the NWS forecast, and the Cleveland Fed nowcast. "
            "No API key. Never places an order."
        ),
        epilog=(
            "examples:\n"
            "  python -m kalshi_edge.scan\n"
            "  python -m kalshi_edge.scan --horizon-days 14 --json scan.json\n"
            "  python -m kalshi_edge.scan --skip-short\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", type=Path, default=Path("data/kalshi.db"))
    parser.add_argument("--budget", default="10", help="Dollars for the order the break-even is sized to (default: 10).")
    parser.add_argument("--horizon-days", type=int, default=DEFAULT_HORIZON_DAYS)
    parser.add_argument(
        "--category",
        action="append",
        dest="categories",
        help=(
            "Category for the short-dated list. Repeat to set several. "
            f"Default: {', '.join(DEFAULT_SHORT_CATEGORIES)}. Series tagged AI are included too."
        ),
    )
    parser.add_argument(
        "--skip-short",
        action="store_true",
        help="Skip the category sweep. Weather and the economic series still run.",
    )
    parser.add_argument("--json", type=Path, help="Also write the report as JSON to this path.")
    parser.add_argument("--min-interval", type=float, default=0.25)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--short-limit", type=int, default=SHORT_PRINT_LIMIT, help="How many short-dated rows to print.")
    args = parser.parse_args(argv)
    if args.min_interval < 0:
        parser.error("--min-interval must be >= 0")
    if args.horizon_days < 1:
        parser.error("--horizon-days must be >= 1")
    if args.short_limit < 0:
        parser.error("--short-limit must be >= 0")
    try:
        budget = Decimal(str(args.budget).strip())
    except InvalidOperation:
        parser.error("--budget must be a number of dollars")
    if not budget.is_finite() or budget <= 0:
        parser.error("--budget must be > 0")
    args.budget_decimal = budget
    return args


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 2
    now = datetime.now(UTC).replace(microsecond=0)
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    client = KalshiClient(base_url=args.base_url, min_interval=args.min_interval)
    try:
        with MarketStore(args.db) as store:
            report = run(
                client=client,
                store=store,
                now=now,
                generated_at=generated_at,
                budget=args.budget_decimal,
                horizon_days=args.horizon_days,
                categories=tuple(args.categories) if args.categories else DEFAULT_SHORT_CATEGORIES,
                include_short=not args.skip_short,
            )
    except KalshiAPIError as exc:
        logger.error("%s", exc)
        return 1
    except UnknownFeeTypeError as exc:
        logger.error("%s; update kalshi_edge/fees.py before trusting any edge", exc)
        return 1
    finally:
        client.close()
    text = format_report(report, short_limit=args.short_limit)
    print(text)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(report_to_json(report) + "\n", encoding="utf-8")
        logger.info("wrote %s", args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
