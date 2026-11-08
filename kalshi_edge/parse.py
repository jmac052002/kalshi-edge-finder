"""Turn Kalshi market payloads into rows we can store and query.

A Kalshi binary contract pays its notional (almost always $1) if the event
happens and $0 otherwise. The quoted YES price, in dollars, is therefore the
market's price of that chance: 0.4500 means 45 cents for a contract that pays
$1, i.e. a 45% implied probability before fees.

The API's integer cent fields (``yes_bid``, ``last_price``, ``volume``) come
back null on the public market list. The fixed-point strings (``*_dollars``,
``*_fp``) are the ones that actually carry the quote. We do not fall back to
the cent fields: a legacy value of 45 means 45 cents, and treating it as a
dollar price would turn a 45% market into a nonsense price of 45.
"""

from __future__ import annotations

from dataclasses import dataclass


def parse_fixed_point(value: object) -> float | None:
    """Parse a Kalshi fixed-point string into a float.

    ``"0.4500"`` becomes ``0.45``. Missing, blank, and non-numeric values
    become ``None`` so one bad field does not drop the whole market. Floats
    are not exact decimals; the original string is kept on price snapshots
    for anything that needs the API's exact quote. At four decimal places the
    float error is far smaller than one tick, so range queries in SQL are safe.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _text(payload: dict, key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        text = str(value).strip()
        return text or None
    text = value.strip()
    return text or None


def _raw_dollars(payload: dict, key: str) -> str | None:
    """Keep the API's dollar string unchanged. Numbers are stringified."""
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    return None


@dataclass(frozen=True)
class ParsedMarket:
    """One market, as last seen from a single API response.

    ``result`` is the exchange's determination: ``yes``, ``no``, ``scalar``,
    or ``None`` when the market has not been determined. ``settlement_value``
    is the cash a YES holder actually receives (1 or 0 on a binary market;
    a fraction on a scalar market). That payoff, not the post-game last
    trade, is what a calibration study compares to the earlier price.

    ``yes_bid`` / ``yes_ask`` / ``last_price`` are the latest quotes on this
    fetch. On a finalized market the last trade is usually near 0 or 1
    because the outcome is already known. Those quotes are not a forecast.
    """

    ticker: str
    series_ticker: str
    event_ticker: str | None
    title: str | None
    yes_sub_title: str | None
    market_type: str | None
    status: str | None
    result: str | None
    open_time: str | None
    close_time: str | None
    settlement_ts: str | None
    expiration_value: str | None
    settlement_value: float | None
    notional_value: float | None
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    last_price: float | None
    volume: float | None
    open_interest: float | None
    source: str
    fetched_at: str

    def as_tuple(self) -> tuple:
        return (
            self.ticker,
            self.series_ticker,
            self.event_ticker,
            self.title,
            self.yes_sub_title,
            self.market_type,
            self.status,
            self.result,
            self.open_time,
            self.close_time,
            self.settlement_ts,
            self.expiration_value,
            self.settlement_value,
            self.notional_value,
            self.yes_bid,
            self.yes_ask,
            self.no_bid,
            self.no_ask,
            self.last_price,
            self.volume,
            self.open_interest,
            self.source,
            self.fetched_at,
        )


@dataclass(frozen=True)
class PriceSnapshot:
    """Quotes for one open market at one collection time.

    ``captured_at`` is the start of the collection run, shared by every row
    from that run. A rerun in a later second appends history. A retry of the
    same run hits the same primary key and updates in place instead of
    inserting a second copy.

    The ``*_dollars`` columns are the raw API strings. The float columns are
    there so SQL can bucket prices (``yes_bid BETWEEN 0.40 AND 0.50``).
    """

    ticker: str
    series_ticker: str
    captured_at: str
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    last_price: float | None
    yes_bid_dollars: str | None
    yes_ask_dollars: str | None
    no_bid_dollars: str | None
    no_ask_dollars: str | None
    last_price_dollars: str | None
    volume: float | None
    open_interest: float | None
    status: str | None

    def as_tuple(self) -> tuple:
        return (
            self.ticker,
            self.series_ticker,
            self.captured_at,
            self.yes_bid,
            self.yes_ask,
            self.no_bid,
            self.no_ask,
            self.last_price,
            self.yes_bid_dollars,
            self.yes_ask_dollars,
            self.no_bid_dollars,
            self.no_ask_dollars,
            self.last_price_dollars,
            self.volume,
            self.open_interest,
            self.status,
        )


def parse_market(
    payload: object,
    *,
    series_ticker: str,
    source: str,
    fetched_at: str,
) -> ParsedMarket | None:
    """Parse one market object. Returns None when there is no ticker to key on."""
    if not isinstance(payload, dict):
        return None
    ticker = _text(payload, "ticker")
    if ticker is None:
        return None
    result = _text(payload, "result")
    if result is not None:
        result = result.lower()
    return ParsedMarket(
        ticker=ticker,
        series_ticker=series_ticker,
        event_ticker=_text(payload, "event_ticker"),
        title=_text(payload, "title"),
        yes_sub_title=_text(payload, "yes_sub_title"),
        market_type=_text(payload, "market_type"),
        status=_text(payload, "status"),
        result=result,
        open_time=_text(payload, "open_time"),
        close_time=_text(payload, "close_time"),
        settlement_ts=_text(payload, "settlement_ts"),
        expiration_value=_text(payload, "expiration_value"),
        settlement_value=parse_fixed_point(payload.get("settlement_value_dollars")),
        notional_value=parse_fixed_point(payload.get("notional_value_dollars")),
        yes_bid=parse_fixed_point(payload.get("yes_bid_dollars")),
        yes_ask=parse_fixed_point(payload.get("yes_ask_dollars")),
        no_bid=parse_fixed_point(payload.get("no_bid_dollars")),
        no_ask=parse_fixed_point(payload.get("no_ask_dollars")),
        last_price=parse_fixed_point(payload.get("last_price_dollars")),
        volume=parse_fixed_point(payload.get("volume_fp")),
        open_interest=parse_fixed_point(payload.get("open_interest_fp")),
        source=source,
        fetched_at=fetched_at,
    )


def parse_snapshot(
    payload: object,
    *,
    series_ticker: str,
    captured_at: str,
) -> PriceSnapshot | None:
    """Parse the quote fields of one market into a snapshot row."""
    if not isinstance(payload, dict):
        return None
    ticker = _text(payload, "ticker")
    if ticker is None:
        return None
    return PriceSnapshot(
        ticker=ticker,
        series_ticker=series_ticker,
        captured_at=captured_at,
        yes_bid=parse_fixed_point(payload.get("yes_bid_dollars")),
        yes_ask=parse_fixed_point(payload.get("yes_ask_dollars")),
        no_bid=parse_fixed_point(payload.get("no_bid_dollars")),
        no_ask=parse_fixed_point(payload.get("no_ask_dollars")),
        last_price=parse_fixed_point(payload.get("last_price_dollars")),
        yes_bid_dollars=_raw_dollars(payload, "yes_bid_dollars"),
        yes_ask_dollars=_raw_dollars(payload, "yes_ask_dollars"),
        no_bid_dollars=_raw_dollars(payload, "no_bid_dollars"),
        no_ask_dollars=_raw_dollars(payload, "no_ask_dollars"),
        last_price_dollars=_raw_dollars(payload, "last_price_dollars"),
        volume=parse_fixed_point(payload.get("volume_fp")),
        open_interest=parse_fixed_point(payload.get("open_interest_fp")),
        status=_text(payload, "status"),
    )
