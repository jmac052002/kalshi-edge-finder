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

    ``occurrence_datetime`` is the scheduled time of the underlying event
    (kickoff, for a game) when Kalshi publishes one, and
    ``expected_expiration_time`` is when the exchange expects to settle.
    The calibration backtest uses them to decide what "before the event"
    means for each contract. ``close_time`` alone is not enough: sports
    markets trade through the game, so the book closes after kickoff.

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
    expected_expiration_time: str | None
    occurrence_datetime: str | None
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
            self.expected_expiration_time,
            self.occurrence_datetime,
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
        expected_expiration_time=_text(payload, "expected_expiration_time"),
        occurrence_datetime=_text(payload, "occurrence_datetime"),
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


@dataclass(frozen=True)
class Candle:
    """One period of a market's price history.

    ``end_ts`` is the Unix second the period ended. Every price in the
    candle was observed before that instant, so a candle with ``end_ts`` at
    or before an event's start is a pre-event observation.

    ``yes_bid`` / ``yes_ask`` are the closing quotes of the period, and
    ``last_price`` the last trade in it. A period with no quote on a side
    leaves that side ``None``. The synthetic candle Kalshi prepends when
    asked for ``include_latest_before_start`` has null prices and is parsed
    the same way; callers that want a usable quote skip candles without one.
    """

    end_ts: int
    yes_bid: float | None
    yes_ask: float | None
    last_price: float | None
    volume: float | None
    open_interest: float | None

    @property
    def midpoint(self) -> float | None:
        """Mid of the closing bid and ask, the usual "what the market thinks".

        ``None`` unless both sides exist. A one-sided book is a market with
        no agreed price, not a market at the surviving side's price.
        """
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2


def parse_candle(payload: object) -> Candle | None:
    """Parse one candlestick object. Returns None without a usable end time."""
    if not isinstance(payload, dict):
        return None
    end_ts = payload.get("end_period_ts")
    if isinstance(end_ts, bool) or not isinstance(end_ts, (int, float, str)):
        return None
    try:
        end = int(end_ts)
    except (TypeError, ValueError):
        return None

    def close_of(key: str) -> float | None:
        ohlc = payload.get(key)
        if not isinstance(ohlc, dict):
            return None
        return parse_fixed_point(ohlc.get("close_dollars"))

    return Candle(
        end_ts=end,
        yes_bid=close_of("yes_bid"),
        yes_ask=close_of("yes_ask"),
        last_price=close_of("price"),
        volume=parse_fixed_point(payload.get("volume_fp")),
        open_interest=parse_fixed_point(payload.get("open_interest_fp")),
    )


def parse_candles(payloads: object) -> list[Candle]:
    """Parse a candlestick list, oldest first, dropping unparseable entries."""
    if not isinstance(payloads, list):
        return []
    candles = [candle for candle in map(parse_candle, payloads) if candle is not None]
    candles.sort(key=lambda candle: candle.end_ts)
    return candles


@dataclass(frozen=True)
class PreEventPrice:
    """The last pre-event quote we could find for one contract.

    ``cutoff_ts`` is the Unix second we treat as the start of the event, and
    ``cutoff_source`` says which market field it came from (see
    ``calibrate.event_cutoff``). ``start_ts`` is the start of the window that
    was searched. ``candle_end_ts`` is the end of the candle the quotes were
    read from; it is at or before the cutoff by construction. A contract with
    no usable candle in the window is stored with ``candle_end_ts`` and the
    quotes ``None`` so a rerun does not ask the exchange again.

    The row is a cache entry, and the question it answers is "last two-sided
    quote in ``[start_ts, cutoff_ts]``". :meth:`answers` says whether it
    answers that question for a different window, so a rerun with another
    ``--lead-hours`` or ``--lookback-hours`` refetches instead of silently
    reusing a quote from the wrong time. ``--refresh`` clears everything.
    """

    ticker: str
    series_ticker: str
    cutoff_ts: int
    cutoff_source: str
    start_ts: int
    candle_end_ts: int | None
    yes_bid: float | None
    yes_ask: float | None
    last_price: float | None
    fetched_at: str
    history_missing: bool = False

    @property
    def midpoint(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2

    def answers(self, *, start_ts: int, cutoff_ts: int, cutoff_source: str) -> bool:
        """Whether this cached row is the answer for the given window.

        A market the exchange has no candlestick history for at all
        (``history_missing``, a 404) answers every window: Kalshi archives
        old markets without their bars, and asking again with a different
        cutoff gets the same 404. Otherwise the cutoff and its source must
        match exactly: a quote taken as of kickoff is not the quote as of
        twelve hours before it. A hit is still valid for a narrower window
        as long as the candle it found is inside that window. A miss is
        only valid if the window it searched covered the one asked for; a
        wider search could find something.
        """
        if self.history_missing:
            return True
        if cutoff_ts != self.cutoff_ts or cutoff_source != self.cutoff_source:
            return False
        if self.candle_end_ts is not None:
            return self.candle_end_ts >= start_ts
        return self.start_ts <= start_ts

    def as_tuple(self) -> tuple:
        return (
            self.ticker,
            self.series_ticker,
            self.cutoff_ts,
            self.cutoff_source,
            self.start_ts,
            self.candle_end_ts,
            self.yes_bid,
            self.yes_ask,
            self.last_price,
            self.fetched_at,
            int(self.history_missing),
        )
