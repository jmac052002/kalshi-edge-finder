"""Fit the daily-high error model to settled Kalshi markets.

Public endpoints only. Nothing here places an order or sends a Kalshi key.

::

    python -m kalshi_edge.weather_calibrate
    python -m kalshi_edge.weather_calibrate --fit-only

The first command reads settled ``KXHIGH*`` markets from the live list and
from ``/historical/markets``, pairs each city-day with an Open-Meteo GFS
high that was forecast 24 and 48 hours ahead, and writes
``kalshi_edge/weather_calibration.json``. Candlestick quotes at local noon
are cached in SQLite. ``--fit-only`` refits from that cache and does not
talk to the network, which is how a stored NWS forecast (written by
``kalshi_edge.scan``) takes over once enough of those have settled.
"""

from __future__ import annotations

import argparse
import logging
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from kalshi_edge.calibrate import pick_pre_event_candle
from kalshi_edge.client import DEFAULT_BASE_URL, KalshiAPIError, KalshiClient
from kalshi_edge.collect import utc_now
from kalshi_edge.nws import make_nws_client, station_location
from kalshi_edge.open_meteo import OPEN_METEO_MODEL, fetch_daily_maxes, make_open_meteo_client
from kalshi_edge.parse import PreEventPrice, parse_candles, parse_market
from kalshi_edge.store import MarketStore
from kalshi_edge.weather import (
    assumed_sigma_f,
    event_date,
    is_daily_high_series,
    nws_station_id,
    place_mentioned,
    settled_high,
    settlement_place,
    temperature_yes_probability,
)
from kalshi_edge.weather_error import (
    MIN_POOLED_N,
    REFERENCE_LEAD_HOURS,
    ErrorPair,
    WeatherCalibration,
    brier_score,
    fit_error_model,
    load_calibration,
    log_loss,
    package_path,
    predict_high,
    quantile,
    save_calibration,
)

logger = logging.getLogger(__name__)

ARCHIVE_START = date(2024, 1, 1)
QUOTE_SOURCE = "local_noon"
QUOTE_LOOKBACK_S = 18 * 3600
NWS_MIN_LEAD_HOURS = 12.0
SOURCE_LABEL = "Open-Meteo GFS seamless previous-day hourly maximum"
NWS_SOURCE_LABEL = "NWS hourly-max forecasts stored by kalshi_edge.scan"
SOURCE_NOTE = (
    "api.weather.gov serves only the current forecast, so this fit does not use "
    "historical NWS grids. Each settled KXHIGH daily high (Kalshi expiration_value "
    "when the exchange sends one, otherwise the midpoint of the winning two-degree "
    "bin; winning tails are dropped because they do not name a temperature) is paired "
    "with the Open-Meteo Previous Runs API, model gfs_seamless "
    "(https://open-meteo.com/en/docs/previous-runs-api). "
    "temperature_2m_previous_day1 is the temperature that run predicted 24 hours before "
    "the valid hour, and previous_day2 is 48 hours before. The forecast high is the max "
    "of those hourly values on the station's local calendar date, the same reduction the "
    "scan applies to the live NWS hourly grid. The day-0 series is a short lead and is "
    "not used. Bias is the mean of settlement minus that GFS high, and the scan adds it "
    "to the live NWS high. That transfers a GFS bias onto the NWS, and it is the wrong "
    "correction if the two forecasts do not share one. Once nws_forecasts has enough "
    "settled days, a refit uses those stored NWS highs instead. The quote in the Brier "
    "and log-loss comparison is the last two-sided hourly candle at or before local noon "
    "on the market date: after the 24-hour forecast existed, and before the usual "
    "afternoon high. That comparison fits on days before Kalshi's historical cutoff and "
    "scores quotes after it."
)


@dataclass(frozen=True)
class SettledDay:
    event_ticker: str
    series_ticker: str
    station_id: str
    market_date: date
    high_f: float
    exact: bool
    place: str | None
    close_time: datetime | None


@dataclass(frozen=True)
class ScoreBracket:
    ticker: str
    event_ticker: str
    series_ticker: str
    station_id: str
    market_date: date
    strike_type: str | None
    floor: float | None
    cap: float | None
    outcome: float
    midpoint: float | None
    forecast_f: float


@dataclass
class Collected:
    days: list[SettledDay]
    brackets: list[dict]
    notes: list[str]
    split_date: date | None


def _parse_time(value: object) -> datetime | None:
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


def _has_expiration(market: Mapping) -> bool:
    value = market.get("expiration_value")
    return isinstance(value, str) and bool(value.strip())


def _prefer(current: dict, incoming: dict) -> dict:
    """Keep the copy that has a settlement print. The live row wins a tie."""
    incoming_live = incoming.get("_live") is True
    if _has_expiration(incoming) and not _has_expiration(current):
        return incoming
    if _has_expiration(current) and not _has_expiration(incoming):
        return current
    if incoming_live:
        return incoming
    return current


def _slim(market: Mapping, series_ticker: str, *, live: bool) -> dict | None:
    ticker = market.get("ticker")
    event = market.get("event_ticker")
    if not isinstance(ticker, str) or not isinstance(event, str):
        return None
    slim = {
        "ticker": ticker,
        "event_ticker": event,
        "series_ticker": series_ticker,
        "result": market.get("result"),
        "expiration_value": market.get("expiration_value"),
        "strike_type": market.get("strike_type"),
        "floor_strike": market.get("floor_strike"),
        "cap_strike": market.get("cap_strike"),
        "rules_primary": market.get("rules_primary"),
        "close_time": market.get("close_time"),
        "yes_sub_title": market.get("yes_sub_title"),
        "title": market.get("title"),
        "status": market.get("status"),
        "_live": live,
    }
    return slim


def consensus_station(markets: Iterable[Mapping]) -> tuple[str | None, str | None]:
    """The one CLI station a series names, and the place next to that code.

    Two different CLI codes in one series means the series moved or the rules
    disagree, and neither is applied to markets that do not name their own.
    """
    found: list[tuple[str, str | None]] = []
    for market in markets:
        rules = market.get("rules_primary")
        if not isinstance(rules, str):
            continue
        station = nws_station_id(rules)
        if station:
            found.append((station, settlement_place(rules)))
    stations = {station for station, _place in found}
    if len(stations) != 1:
        return None, None
    station = next(iter(stations))
    places = [place for _station, place in found if place]
    place = max(places, key=len) if places else None
    return station, place


def station_of(market: Mapping, series_station: str | None, series_place: str | None) -> str | None:
    rules = market.get("rules_primary") if isinstance(market.get("rules_primary"), str) else None
    direct = nws_station_id(rules)
    if direct:
        return direct
    if series_station and place_mentioned(rules, series_place):
        return series_station
    return None


def collapse_days(days: list[SettledDay]) -> tuple[list[SettledDay], int]:
    """One high per station and date. Exact prints beat a bin midpoint.

    Two exact prints that disagree by more than half a degree are dropped:
    that date is not one city's high.
    """
    chosen: dict[tuple[str, date], SettledDay] = {}
    conflicted: set[tuple[str, date]] = set()
    for day in days:
        key = (day.station_id, day.market_date)
        if key in conflicted:
            continue
        current = chosen.get(key)
        if current is None:
            chosen[key] = day
            continue
        if current.exact and day.exact and abs(current.high_f - day.high_f) > 0.51:
            conflicted.add(key)
            chosen.pop(key, None)
            continue
        # An exact print beats a bin midpoint. A later close is the copy that
        # can still have candlesticks, including when the other close is missing.
        if _day_rank(day) > _day_rank(current):
            chosen[key] = day
    return list(chosen.values()), len(conflicted)


def _day_rank(day: SettledDay) -> tuple[bool, float]:
    close = day.close_time.timestamp() if day.close_time is not None else float("-inf")
    return (day.exact, close)


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    if math.isnan(number):
        return None
    return number


def days_and_brackets(
    markets: Iterable[Mapping],
    *,
    series_ticker: str,
) -> tuple[list[SettledDay], list[dict], list[str]]:
    """Settled highs, and the brackets of events new enough to still have candles."""
    rows = [market for market in markets if isinstance(market, Mapping)]
    series_station, series_place = consensus_station(rows)
    grouped: dict[str, list[Mapping]] = defaultdict(list)
    for market in rows:
        event = market.get("event_ticker")
        if isinstance(event, str):
            grouped[event].append(market)
    days: list[SettledDay] = []
    brackets: list[dict] = []
    notes: list[str] = []
    if series_station is None:
        notes.append(f"{series_ticker}: no single CLI station in the rules; only markets that name one are used")
    for event, event_markets in grouped.items():
        market_day = event_date(event)
        if market_day is None:
            continue
        stations = {
            station
            for market in event_markets
            if (station := station_of(market, series_station, series_place)) is not None
        }
        if len(stations) != 1:
            continue
        found = settled_high(event_markets)
        if found is None:
            continue
        high, exact = found
        station = next(iter(stations))
        rules = next(
            (
                market.get("rules_primary")
                for market in event_markets
                if isinstance(market.get("rules_primary"), str)
            ),
            None,
        )
        place = settlement_place(rules if isinstance(rules, str) else None) or series_place
        close = next(
            (parsed for market in event_markets if (parsed := _parse_time(market.get("close_time"))) is not None),
            None,
        )
        days.append(
            SettledDay(
                event_ticker=event,
                series_ticker=series_ticker,
                station_id=station,
                market_date=market_day,
                high_f=high,
                exact=exact,
                place=place,
                close_time=close,
            )
        )
        for market in event_markets:
            result = str(market.get("result") or "").lower()
            if result not in ("yes", "no"):
                continue
            ticker = market.get("ticker")
            if not isinstance(ticker, str):
                continue
            brackets.append(
                {
                    "ticker": ticker,
                    "event_ticker": event,
                    "series_ticker": series_ticker,
                    "station_id": station,
                    "market_date": market_day.isoformat(),
                    "strike_type": market.get("strike_type") if isinstance(market.get("strike_type"), str) else None,
                    "floor_strike": _finite(market.get("floor_strike")),
                    "cap_strike": _finite(market.get("cap_strike")),
                    "outcome": 1.0 if result == "yes" else 0.0,
                    "close_time": close,
                    "raw": market,
                }
            )
    return days, brackets, notes


def _bucket_stored_lead(lead_hours: float) -> int | None:
    """Map a stored NWS lead onto the 24-hour or 48-hour fit bucket.

    A lead under 12 hours is the high already forming, not the forecast the
    morning scan would have used, so it is left out.
    """
    if NWS_MIN_LEAD_HOURS <= lead_hours <= 36:
        return 24
    if 36 < lead_hours <= 60:
        return 48
    return None


def nws_pairs_from_rows(
    forecasts: Iterable[Mapping],
    days: Iterable[SettledDay],
) -> list[ErrorPair]:
    """One stored NWS high per event per lead bucket, joined to the settlement.

    When several scans priced the same event, the lead closest to the bucket
    (24 or 48 hours) is the one that matches how that bucket was defined.
    """
    by_event = {day.event_ticker: day for day in days}
    best: dict[tuple[str, int], tuple[float, ErrorPair]] = {}
    for row in forecasts:
        event = row.get("event_ticker")
        if not isinstance(event, str):
            continue
        day = by_event.get(event)
        if day is None:
            continue
        try:
            lead = float(row.get("lead_hours"))  # type: ignore[arg-type]
            forecast = float(row.get("forecast_high_f"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        bucket = _bucket_stored_lead(lead)
        if bucket is None:
            continue
        target = float(bucket)
        distance = abs(lead - target)
        pair = ErrorPair(
            station_id=day.station_id,
            lead_hours=bucket,
            forecast_f=forecast,
            settlement_f=day.high_f,
            market_date=day.market_date,
            place=day.place,
        )
        key = (event, bucket)
        current = best.get(key)
        if current is None or distance < current[0]:
            best[key] = (distance, pair)
    return [item[1] for item in best.values()]


def select_training_pairs(
    gfs_pairs: list[ErrorPair],
    nws_pairs: list[ErrorPair],
    *,
    min_n: int = MIN_POOLED_N,
) -> tuple[list[ErrorPair], str, str]:
    """Prefer stored NWS highs once the 24-hour bucket has enough days.

    The two sources are not pooled. Mixing a GFS bias into an NWS sample
    would put the transfer this module is trying to retire back into the fit.
    """
    nws_24 = [pair for pair in nws_pairs if pair.lead_hours == REFERENCE_LEAD_HOURS]
    if len(nws_24) >= min_n:
        return nws_pairs, NWS_SOURCE_LABEL, (
            "Enough stored NWS hourly maxima have settled to fit the scan's own "
            "forecast. Open-Meteo pairs were not mixed in."
        )
    return gfs_pairs, SOURCE_LABEL, SOURCE_NOTE


def gfs_pairs_for(
    days: Iterable[SettledDay],
    forecasts: Mapping[tuple[str, date], Mapping[int, float]],
) -> list[ErrorPair]:
    pairs: list[ErrorPair] = []
    for day in days:
        maxima = forecasts.get((day.station_id, day.market_date))
        if not maxima:
            continue
        for lead, forecast in maxima.items():
            pairs.append(
                ErrorPair(
                    station_id=day.station_id,
                    lead_hours=int(lead),
                    forecast_f=float(forecast),
                    settlement_f=day.high_f,
                    market_date=day.market_date,
                    place=day.place,
                )
            )
    return pairs


def score_brackets(
    calibration: WeatherCalibration,
    brackets: list[ScoreBracket],
    *,
    assumed_sigma: float,
) -> dict:
    """Brier and log loss of the model, the old assumed width, and the noon quote.

    All three are scored on the same contracts: those with a two-sided noon
    quote. Winning-bin log loss is one number per event, the probability on
    the bracket that settled YES.
    """
    calibrated: list[tuple[float, float]] = []
    assumed: list[tuple[float, float]] = []
    market: list[tuple[float, float]] = []
    gaps: list[float] = []
    by_event: dict[str, list[tuple[ScoreBracket, float, float]]] = defaultdict(list)
    for bracket in brackets:
        prediction = predict_high(calibration, bracket.station_id, 24, bracket.forecast_f)
        model_p = temperature_yes_probability(
            prediction.mean_f,
            prediction.scale_f,
            strike_type=bracket.strike_type,
            floor=bracket.floor,
            cap=bracket.cap,
            df=prediction.df,
        )
        assumed_p = temperature_yes_probability(
            bracket.forecast_f,
            assumed_sigma,
            strike_type=bracket.strike_type,
            floor=bracket.floor,
            cap=bracket.cap,
        )
        if model_p is None or assumed_p is None:
            continue
        by_event[bracket.event_ticker].append((bracket, model_p, assumed_p))
        if bracket.midpoint is None:
            continue
        calibrated.append((model_p, bracket.outcome))
        assumed.append((assumed_p, bracket.outcome))
        market.append((bracket.midpoint, bracket.outcome))
        gaps.append(abs(model_p - bracket.midpoint))
    win_model: list[tuple[float, float]] = []
    win_assumed: list[tuple[float, float]] = []
    win_market: list[tuple[float, float]] = []
    for rows in by_event.values():
        winners = [row for row in rows if row[0].outcome == 1.0]
        if len(winners) != 1:
            continue
        bracket, model_p, assumed_p = winners[0]
        if bracket.midpoint is None:
            continue
        win_model.append((model_p, 1.0))
        win_assumed.append((assumed_p, 1.0))
        win_market.append((bracket.midpoint, 1.0))
    return {
        "n_contracts": len(market),
        "n_winning_bins": len(win_market),
        "brier": {
            "market": brier_score(market),
            "assumed": brier_score(assumed),
            "calibrated": brier_score(calibrated),
        },
        "log_loss": {
            "market": log_loss(market),
            "assumed": log_loss(assumed),
            "calibrated": log_loss(calibrated),
        },
        "winning_bin_log_loss": {
            "market": log_loss(win_market),
            "assumed": log_loss(win_assumed),
            "calibrated": log_loss(win_model),
        },
        "abs_gap_p95": quantile(gaps, 0.95),
    }


def _clamp_gap(raw: float | None) -> float:
    if raw is None:
        return 0.15
    return min(0.20, max(0.10, raw))


def _num(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return f"{value:.4f}"


def format_fit_report(calibration: WeatherCalibration) -> str:
    """Plain-text fit, the numbers the PR and the README quote."""
    lines = [
        f"weather calibration  fitted={calibration.fitted_at or '-'}  trusted={calibration.trusted}",
        f"source: {calibration.source_label}",
        f"distribution: {calibration.distribution}"
        + (f"  df={calibration.df:.0f}" if calibration.df is not None else ""),
        f"bias transfer onto the NWS high: {calibration.bias_transfer:.0%}",
        f"outliers dropped (|error| > 25F): {calibration.rejected_outliers}",
    ]
    for lead_hours in sorted(calibration.leads):
        lead = calibration.leads[lead_hours]
        lines.append(
            f"{lead.lead_hours}h  n={lead.n}  bias={lead.bias_f:+.2f}F  "
            f"sd={lead.sd_f:.2f}F  scale={lead.scale_f:.2f}F  mae={lead.mae_f:.2f}F"
        )
    lines.append(f"cities with their own shrunk fit: {len(calibration.stations)}")
    for station in sorted(calibration.stations):
        city = calibration.stations[station]
        place = city.place or ""
        lines.append(
            f"  {station:6}  {place[:36]:<36}  n={city.n:<4}  "
            f"bias={city.bias_f:+.2f}F  sd={city.sd_f:.2f}F  "
            f"raw={city.raw_bias_f:+.2f}/{city.raw_sd_f:.2f}"
        )
    scores = calibration.scores or {}
    if scores:
        oos = scores.get("out_of_sample")
        lines.append(
            "bracket scores ("
            + ("out of sample, fit before " if oos else "in sample, fit includes ")
            + f"{scores.get('split_date') or 'the quote window'}"
            + f"): contracts={scores.get('n_contracts')}  winning_bins={scores.get('n_winning_bins')}"
        )
        for name in ("brier", "log_loss", "winning_bin_log_loss"):
            block = scores.get(name) or {}
            if not isinstance(block, dict):
                continue
            lines.append(
                f"  {name}: market={_num(block.get('market'))}  "
                f"assumed={_num(block.get('assumed'))}  calibrated={_num(block.get('calibrated'))}"
            )
        lines.append(
            "  |model-midpoint| 95th percentile "
            f"{_num(scores.get('abs_gap_p95'))}  disagree guard {calibration.disagree_gap:.0%}"
        )
    lines.append(calibration.source_note)
    return "\n".join(lines)


def _collect_series_markets(client: KalshiClient, series_ticker: str) -> list[dict]:
    merged: dict[str, dict] = {}
    for historical, live in ((True, False), (False, True)):
        kwargs: dict = {"series_ticker": series_ticker, "limit": 1000, "max_pages": 40}
        if historical:
            kwargs["historical"] = True
        else:
            kwargs["status"] = "settled"
        for page in client.iter_market_pages(**kwargs):
            for market in page:
                if not isinstance(market, dict):
                    continue
                slim = _slim(market, series_ticker, live=live)
                if slim is None:
                    continue
                ticker = slim["ticker"]
                current = merged.get(ticker)
                merged[ticker] = slim if current is None else _prefer(current, slim)
    return list(merged.values())


def _station_point(nws: KalshiClient, station_id: str) -> tuple[float, float, str, str] | None:
    try:
        payload = nws.get_json(f"/stations/{station_id}")
    except KalshiAPIError as exc:
        logger.warning("NWS %s: %s", station_id, exc)
        return None
    located = station_location(payload)
    if located is None:
        return None
    latitude, longitude, name = located
    properties = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
    timezone = properties.get("timeZone") if isinstance(properties, dict) else None
    if not isinstance(timezone, str) or not timezone:
        return None
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        logger.warning("NWS %s: unknown timezone %s", station_id, timezone)
        return None
    return latitude, longitude, timezone, name


def _fetch_quotes(
    *,
    client: KalshiClient,
    store: MarketStore,
    brackets: list[dict],
    timezones: dict[str, str],
    fetched_at: str,
    max_fetch: int | None,
) -> dict[str, float | None]:
    """Noon midpoints, cached like the NFL pre-event quotes. 404 is remembered."""
    cached = store.prices_with_source(QUOTE_SOURCE)
    quotes: dict[str, float | None] = {}
    fetched = 0
    pending_markets = []
    for bracket in brackets:
        raw = bracket.get("raw")
        if isinstance(raw, dict):
            parsed = parse_market(raw, series_ticker=bracket["series_ticker"], source="live", fetched_at=fetched_at)
            if parsed is not None:
                pending_markets.append(parsed)
    store.save_page(pending_markets, [])
    for bracket in brackets:
        ticker = bracket["ticker"]
        zone_name = timezones.get(bracket["station_id"])
        market_day = date.fromisoformat(bracket["market_date"])
        if zone_name is None:
            continue
        noon = datetime(market_day.year, market_day.month, market_day.day, 12, tzinfo=ZoneInfo(zone_name))
        cutoff_ts = int(noon.timestamp())
        start_ts = cutoff_ts - QUOTE_LOOKBACK_S
        hit = cached.get(ticker)
        if hit is not None and hit.answers(start_ts=start_ts, cutoff_ts=cutoff_ts, cutoff_source=QUOTE_SOURCE):
            quotes[ticker] = hit.midpoint
            continue
        if max_fetch is not None and fetched >= max_fetch:
            continue
        try:
            raw_candles = client.get_candlesticks(
                series_ticker=bracket["series_ticker"],
                ticker=ticker,
                start_ts=start_ts,
                end_ts=cutoff_ts,
                period_interval=60,
            )
            missing = False
        except KalshiAPIError as exc:
            if exc.status_code != 404:
                logger.warning("%s: candlesticks skipped (%s)", ticker, exc)
                continue
            raw_candles = []
            missing = True
        fetched += 1
        if fetched % 200 == 0:
            logger.info("noon quotes fetched: %d", fetched)
        candle = pick_pre_event_candle(parse_candles(raw_candles), cutoff_ts)
        price = PreEventPrice(
            ticker=ticker,
            series_ticker=bracket["series_ticker"],
            cutoff_ts=cutoff_ts,
            cutoff_source=QUOTE_SOURCE,
            start_ts=start_ts,
            candle_end_ts=None if candle is None else candle.end_ts,
            yes_bid=None if candle is None else candle.yes_bid,
            yes_ask=None if candle is None else candle.yes_ask,
            last_price=None if candle is None else candle.last_price,
            fetched_at=fetched_at,
            history_missing=missing,
        )
        store.save_pre_event_price(price)
        cached[ticker] = price
        quotes[ticker] = price.midpoint
    logger.info("noon quotes fetched this run: %d (cache included on later calls)", fetched)
    return quotes


def collect_weather(
    *,
    client: KalshiClient,
    nws: KalshiClient,
    meteo: KalshiClient,
    store: MarketStore,
    fetched_at: str,
    with_quotes: bool,
    max_candle_fetch: int | None,
) -> tuple[list[SettledDay], dict[tuple[str, date], dict[int, float]], list[dict], dict[str, float | None], date | None, list[str]]:
    """Network collection. Returns days, GFS maxima, brackets, noon quotes, split date, notes."""
    notes: list[str] = []
    try:
        cutoff_payload = client.get_json("/historical/cutoff")
    except KalshiAPIError as exc:
        cutoff_payload = {}
        notes.append(f"historical cutoff was not read ({exc}); candle quotes are skipped")
    split_at = _parse_time(cutoff_payload.get("market_settled_ts") if isinstance(cutoff_payload, dict) else None)
    split_date = split_at.date() if split_at is not None else None
    series_rows = [row for row in client.list_series() if is_daily_high_series(row)]
    series_rows.sort(key=lambda row: str(row.get("ticker") or ""))
    if not series_rows:
        notes.append("no KXHIGH daily series in GET /series")
        return [], {}, [], {}, split_date, notes
    all_days: list[SettledDay] = []
    all_brackets: list[dict] = []
    for entry in series_rows:
        ticker = str(entry["ticker"])
        try:
            markets = _collect_series_markets(client, ticker)
        except KalshiAPIError as exc:
            notes.append(f"{ticker}: {exc}")
            continue
        days, brackets, series_notes = days_and_brackets(markets, series_ticker=ticker)
        notes.extend(series_notes)
        logger.info("%s: %d settled highs, %d brackets", ticker, len(days), len(brackets))
        all_days.extend(days)
        if split_at is not None:
            all_brackets.extend(
                bracket
                for bracket in brackets
                if bracket["close_time"] is not None and bracket["close_time"] >= split_at
            )
    all_days, conflicts = collapse_days(all_days)
    if conflicts:
        notes.append(f"{conflicts} station-dates dropped because two exact settlements disagreed")
    kept_events = {day.event_ticker for day in all_days}
    all_brackets = [bracket for bracket in all_brackets if bracket["event_ticker"] in kept_events]
    stations = sorted({day.station_id for day in all_days})
    points: dict[str, tuple[float, float, str, str]] = {}
    for station in stations:
        point = _station_point(nws, station)
        if point is None:
            notes.append(f"{station}: NWS station metadata was not usable; that city's forecasts were skipped")
            continue
        points[station] = point
    timezones = {station: point[2] for station, point in points.items()}
    forecasts: dict[tuple[str, date], dict[int, float]] = {}
    archive_rows: list[tuple] = []
    for station, (latitude, longitude, timezone, _name) in points.items():
        station_days = [day for day in all_days if day.station_id == station]
        if not station_days:
            continue
        start = max(ARCHIVE_START, min(day.market_date for day in station_days))
        end = max(day.market_date for day in station_days)
        try:
            maxima = fetch_daily_maxes(
                meteo,
                latitude=latitude,
                longitude=longitude,
                timezone=timezone,
                start=start,
                end=end,
            )
        except KalshiAPIError as exc:
            notes.append(f"{station}: Open-Meteo previous runs failed ({exc})")
            continue
        for market_day, by_lead in maxima.items():
            forecasts[(station, market_day)] = dict(by_lead)
            for lead, high in by_lead.items():
                archive_rows.append((station, market_day.isoformat(), int(lead), OPEN_METEO_MODEL, high, fetched_at))
        logger.info("%s: %d archived days", station, len(maxima))
    store.save_archive_forecasts(archive_rows)
    day_rows = []
    for day in all_days:
        zone = timezones.get(day.station_id)
        day_rows.append(
            (
                day.event_ticker,
                day.series_ticker,
                day.station_id,
                day.market_date.isoformat(),
                day.high_f,
                1 if day.exact else 0,
                day.place,
                zone,
                None if day.close_time is None else day.close_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                fetched_at,
            )
        )
    store.save_weather_days(day_rows)
    bracket_rows = [
        (
            bracket["ticker"],
            bracket["event_ticker"],
            bracket["series_ticker"],
            bracket["station_id"],
            bracket["market_date"],
            bracket["strike_type"],
            bracket["floor_strike"],
            bracket["cap_strike"],
            bracket["outcome"],
        )
        for bracket in all_brackets
    ]
    store.save_weather_brackets(bracket_rows)
    quotes: dict[str, float | None] = {}
    if with_quotes and split_at is not None:
        quotes = _fetch_quotes(
            client=client,
            store=store,
            brackets=all_brackets,
            timezones=timezones,
            fetched_at=fetched_at,
            max_fetch=max_candle_fetch,
        )
    return all_days, forecasts, all_brackets, quotes, split_date, notes


def _pairs_from_store(store: MarketStore) -> tuple[list[SettledDay], list[ErrorPair], list[ScoreBracket], date | None]:
    days = [
        SettledDay(
            event_ticker=row["event_ticker"],
            series_ticker=row["series_ticker"],
            station_id=row["station_id"],
            market_date=date.fromisoformat(row["market_date"]),
            high_f=float(row["settlement_high_f"]),
            exact=bool(row["exact"]),
            place=row["place"],
            close_time=_parse_time(row["close_time"]),
        )
        for row in store.weather_days()
    ]
    forecasts: dict[tuple[str, date], dict[int, float]] = {}
    for row in store.archive_forecasts():
        if row["model"] != OPEN_METEO_MODEL:
            continue
        key = (row["station_id"], date.fromisoformat(row["market_date"]))
        forecasts.setdefault(key, {})[int(row["lead_hours"])] = float(row["forecast_high_f"])
    gfs = gfs_pairs_for(days, forecasts)
    quotes = store.prices_with_source(QUOTE_SOURCE)
    brackets: list[ScoreBracket] = []
    for row in store.weather_brackets():
        market_day = date.fromisoformat(row["market_date"])
        maxima = forecasts.get((row["station_id"], market_day)) or {}
        forecast = maxima.get(REFERENCE_LEAD_HOURS)
        if forecast is None:
            continue
        price = quotes.get(row["ticker"])
        brackets.append(
            ScoreBracket(
                ticker=row["ticker"],
                event_ticker=row["event_ticker"],
                series_ticker=row["series_ticker"],
                station_id=row["station_id"],
                market_date=market_day,
                strike_type=row["strike_type"],
                floor=row["floor_strike"],
                cap=row["cap_strike"],
                outcome=float(row["outcome"]),
                midpoint=None if price is None else price.midpoint,
                forecast_f=float(forecast),
            )
        )
    return days, gfs, brackets, None


def _score_brackets_for(
    days: list[SettledDay],
    forecasts: Mapping[tuple[str, date], Mapping[int, float]],
    brackets: list[dict],
    quotes: Mapping[str, float | None],
) -> list[ScoreBracket]:
    scored: list[ScoreBracket] = []
    for bracket in brackets:
        market_day = date.fromisoformat(bracket["market_date"]) if isinstance(bracket["market_date"], str) else bracket["market_date"]
        maxima = forecasts.get((bracket["station_id"], market_day)) or {}
        forecast = maxima.get(REFERENCE_LEAD_HOURS)
        if forecast is None:
            continue
        scored.append(
            ScoreBracket(
                ticker=bracket["ticker"],
                event_ticker=bracket["event_ticker"],
                series_ticker=bracket["series_ticker"],
                station_id=bracket["station_id"],
                market_date=market_day,
                strike_type=bracket.get("strike_type"),
                floor=bracket.get("floor_strike"),
                cap=bracket.get("cap_strike"),
                outcome=float(bracket["outcome"]),
                midpoint=quotes.get(bracket["ticker"]),
                forecast_f=float(forecast),
            )
        )
    return scored


def build_calibration(
    *,
    gfs_pairs: list[ErrorPair],
    nws_pairs: list[ErrorPair],
    brackets: list[ScoreBracket],
    split_date: date | None,
    fitted_at: str,
    bias_transfer: float,
) -> WeatherCalibration:
    """Fit on the chosen source, and score an earlier fit against later noon quotes."""
    pairs, label, note = select_training_pairs(gfs_pairs, nws_pairs)
    assumed = assumed_sigma_f(float(REFERENCE_LEAD_HOURS))
    scores: dict = {}
    disagree = 0.15
    if split_date is not None:
        early = [pair for pair in pairs if pair.market_date is not None and pair.market_date < split_date]
        late_brackets = [bracket for bracket in brackets if bracket.market_date >= split_date]
        early_fit = fit_error_model(
            early,
            fitted_at=fitted_at,
            source_label=label,
            source_note=note,
            bias_transfer=bias_transfer,
        )
        if early_fit.trusted and late_brackets:
            block = score_brackets(early_fit, late_brackets, assumed_sigma=assumed)
            block["out_of_sample"] = True
            block["split_date"] = split_date.isoformat()
            scores = block
            disagree = _clamp_gap(block.get("abs_gap_p95") if isinstance(block.get("abs_gap_p95"), float) else None)
        elif late_brackets:
            note = note + " The pre-cutoff sample was too small for an out-of-sample score."
    full = fit_error_model(
        pairs,
        fitted_at=fitted_at,
        source_label=label,
        source_note=note,
        bias_transfer=bias_transfer,
        disagree_gap=disagree,
        scores=scores,
    )
    if not scores and brackets and full.trusted:
        block = score_brackets(full, brackets, assumed_sigma=assumed)
        block["out_of_sample"] = False
        block["split_date"] = None if split_date is None else split_date.isoformat()
        full = fit_error_model(
            pairs,
            fitted_at=fitted_at,
            source_label=label,
            source_note=note,
            bias_transfer=bias_transfer,
            disagree_gap=_clamp_gap(block.get("abs_gap_p95") if isinstance(block.get("abs_gap_p95"), float) else None),
            scores=block,
        )
    return full


def run_fit(
    *,
    store: MarketStore,
    fitted_at: str,
    bias_transfer: float,
    client: KalshiClient | None = None,
    nws: KalshiClient | None = None,
    meteo: KalshiClient | None = None,
    with_quotes: bool = True,
    max_candle_fetch: int | None = None,
    fit_only: bool = False,
    split_date: date | None = None,
) -> tuple[WeatherCalibration, list[str]]:
    """Collect, or read the cache, and return the calibration the scan should load."""
    notes: list[str] = []
    if fit_only:
        days, gfs_pairs, brackets, _split = _pairs_from_store(store)
        # The cutoff is not in the cache. Pass --split-date to score quotes
        # after that day with a fit that has not seen them.
        if not gfs_pairs and not store.nws_forecasts():
            notes.append("nothing is cached; run without --fit-only first")
            return load_calibration(), notes
    else:
        if client is None or nws is None or meteo is None:
            raise ValueError("a network fit needs Kalshi, NWS, and Open-Meteo clients")
        days, forecasts, raw_brackets, quotes, collected_split, notes = collect_weather(
            client=client,
            nws=nws,
            meteo=meteo,
            store=store,
            fetched_at=fitted_at,
            with_quotes=with_quotes,
            max_candle_fetch=max_candle_fetch,
        )
        gfs_pairs = gfs_pairs_for(days, forecasts)
        brackets = _score_brackets_for(days, forecasts, raw_brackets, quotes)
        if split_date is None:
            split_date = collected_split
    nws_pairs = nws_pairs_from_rows(store.nws_forecasts(), days)
    calibration = build_calibration(
        gfs_pairs=gfs_pairs,
        nws_pairs=nws_pairs,
        brackets=brackets,
        split_date=split_date,
        fitted_at=fitted_at,
        bias_transfer=bias_transfer,
    )
    notes.append(
        f"paired city-days at 24h: {sum(1 for pair in gfs_pairs if pair.lead_hours == 24)} GFS, "
        f"{sum(1 for pair in nws_pairs if pair.lead_hours == 24)} stored NWS"
    )
    return calibration, notes


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m kalshi_edge.weather_calibrate",
        description=(
            "Fit daily-high forecast error to settled Kalshi KXHIGH markets. "
            "Public data only. Never places an order."
        ),
    )
    parser.add_argument("--db", type=Path, default=Path("data/kalshi.db"))
    parser.add_argument("--out", type=Path, default=None, help="Calibration JSON. Default: the file the scan loads.")
    parser.add_argument("--min-interval", type=float, default=0.1)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--fit-only", action="store_true", help="Refit from the SQLite cache. No network.")
    parser.add_argument(
        "--split-date",
        default=None,
        help="YYYY-MM-DD. The score fits on earlier days and evaluates quotes on or after this date.",
    )
    parser.add_argument("--skip-quotes", action="store_true", help="Fit the error and skip candlestick quotes.")
    parser.add_argument("--max-candle-fetch", type=int, default=None)
    parser.add_argument(
        "--bias-transfer",
        type=float,
        default=1.0,
        help="Fraction of the fitted bias added to the NWS high (default 1).",
    )
    args = parser.parse_args(argv)
    if args.min_interval < 0:
        parser.error("--min-interval must be >= 0")
    if args.max_candle_fetch is not None and args.max_candle_fetch < 0:
        parser.error("--max-candle-fetch must be >= 0")
    if not 0.0 <= args.bias_transfer <= 1.0:
        parser.error("--bias-transfer must be between 0 and 1")
    if args.split_date:
        try:
            args.split_date_parsed = date.fromisoformat(args.split_date)
        except ValueError:
            parser.error("--split-date must be YYYY-MM-DD")
    else:
        args.split_date_parsed = None
    return args


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 2
    fitted_at = utc_now()
    out = args.out if args.out is not None else package_path()
    client = nws = meteo = None
    if not args.fit_only:
        client = KalshiClient(base_url=args.base_url, min_interval=args.min_interval)
        nws = make_nws_client(min_interval=args.min_interval)
        meteo = make_open_meteo_client(min_interval=max(args.min_interval, 0.2))
    try:
        with MarketStore(args.db) as store:
            calibration, notes = run_fit(
                store=store,
                fitted_at=fitted_at,
                bias_transfer=args.bias_transfer,
                client=client,
                nws=nws,
                meteo=meteo,
                with_quotes=not args.skip_quotes,
                max_candle_fetch=args.max_candle_fetch,
                fit_only=args.fit_only,
                split_date=args.split_date_parsed,
            )
    except KalshiAPIError as exc:
        logger.error("%s", exc)
        return 1
    finally:
        for closer in (client, nws, meteo):
            if closer is not None:
                closer.close()
    save_calibration(calibration, out)
    text = format_fit_report(calibration)
    print(text)
    if notes:
        print("notes:")
        for note in notes:
            print(f"  - {note}")
    logger.info("wrote %s", out)
    return 0 if calibration.trusted else 2


if __name__ == "__main__":
    raise SystemExit(main())
