"""Pick one pre-event quote without using anything that happened later.

A settled market's own bid, ask, and last trade are the book after the
outcome is known. On a game that trades until the whistle those prints sit
near $0 or $1, so a calibration study that used them would "discover" that
markets are perfectly calibrated. The forecast is a quote from before a
cutoff, and the cutoff is ``close_time`` minus a lead.

``close_time`` is when trading stops. For KXNFLGAME that is about when the
game ends, not when it starts (the market opens days earlier and stays open
through the game). A lead of 0 is the last price before the close, which
already knows the score. The default lead is 180 minutes: three hours before
a game ends is around kickoff, so the price is a pre-event forecast. Other
series close on other clocks; pass a different lead rather than baking in
football.

A candlestick is usable only when its period *ends* at or before the cutoff.
The close of a bar that ends later includes trades from after the cutoff.
The same rule applies to snapshots and trades: ``as_of`` must be <= cutoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from kalshi_edge.parse import parse_fixed_point


@dataclass(frozen=True)
class Quote:
    """One forecast price and, when the book was real, the ask a buyer pays.

    ``price`` is the probability we score: the mid of a tight book, otherwise
    the last trade. ``buy_ask`` is set only when the spread is tight enough
    that hitting the ask is a plausible fill. A $0 bid and a $1 ask is an
    empty book; its mid is 0.50 and is not a forecast.
    """

    price: float
    as_of: datetime
    yes_bid: float | None
    yes_ask: float | None
    trade_price: float | None
    buy_ask: float | None
    source: str


def parse_time(value: str) -> datetime:
    """Parse an API timestamp into aware UTC."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_time(value: datetime) -> str:
    """UTC timestamp at second resolution, matching the snapshot keys."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reference_rule(lead_minutes: int, lookback_minutes: int, period_minutes: int, max_spread: float) -> str:
    """Cache key. A different clock or bar size is a different study."""
    spread_cents = int(round(max_spread * 100))
    return f"preclose-lead{lead_minutes}m-lb{lookback_minutes}m-bar{period_minutes}m-spread{spread_cents}c"


def _in_window(as_of: datetime, cutoff: datetime, window_start: datetime | None) -> bool:
    if as_of > cutoff:
        return False
    if window_start is not None and as_of < window_start:
        return False
    return True


def quote_from_book(
    *,
    as_of: datetime,
    yes_bid: float | None,
    yes_ask: float | None,
    trade_price: float | None,
    source: str,
    max_spread: float,
) -> Quote | None:
    """Turn a book (and an optional last trade) into a forecast.

    The mid is the market's probability only when both sides are present and
    the spread is at most ``max_spread``. Wider than that, we use the last
    trade if there is one. We do not average a $0 bid and a $1 ask into 50%.
    """
    bid = yes_bid if yes_bid is not None and 0 <= yes_bid <= 1 else None
    ask = yes_ask if yes_ask is not None and 0 <= yes_ask <= 1 else None
    trade = trade_price if trade_price is not None and 0 <= trade_price <= 1 else None
    if (
        bid is not None
        and ask is not None
        and ask >= bid
        and (ask - bid) <= max_spread + 1e-9
    ):
        price = (bid + ask) / 2
        # A zero ask is not a fill you can buy. Leave ROI blank.
        buy_ask = ask if ask > 0 else None
    elif trade is not None:
        price = trade
        buy_ask = None
    else:
        return None
    return Quote(
        price=price,
        as_of=as_of,
        yes_bid=bid,
        yes_ask=ask,
        trade_price=trade,
        buy_ask=buy_ask,
        source=source,
    )


def _level(block: object, *keys: str) -> float | None:
    """Read a price from a candle sub-object.

    Live candlesticks say ``close_dollars``. Archived ones say ``close``.
    Either may be missing on a bar with no quotes.
    """
    if not isinstance(block, dict):
        return None
    for key in keys:
        parsed = parse_fixed_point(block.get(key))
        if parsed is not None:
            return parsed
    return None


def quote_from_candlestick(candle: dict, *, max_spread: float) -> Quote | None:
    """A bar is timestamped at its inclusive end. The caller drops late bars."""
    end_ts = candle.get("end_period_ts")
    if isinstance(end_ts, bool) or not isinstance(end_ts, (int, float)):
        return None
    as_of = datetime.fromtimestamp(int(end_ts), timezone.utc)
    return quote_from_book(
        as_of=as_of,
        yes_bid=_level(candle.get("yes_bid"), "close_dollars", "close"),
        yes_ask=_level(candle.get("yes_ask"), "close_dollars", "close"),
        trade_price=_level(candle.get("price"), "close_dollars", "close"),
        source="candlestick",
        max_spread=max_spread,
    )


def quote_from_trade(trade: dict) -> Quote | None:
    """A trade has a price and a time, not a book, so there is no ask to buy."""
    created = trade.get("created_time")
    if not isinstance(created, str) or not created.strip():
        return None
    price = parse_fixed_point(trade.get("yes_price_dollars"))
    if price is None or not 0 <= price <= 1:
        return None
    return Quote(
        price=price,
        as_of=parse_time(created),
        yes_bid=None,
        yes_ask=None,
        trade_price=price,
        buy_ask=None,
        source="trade",
    )


def quote_from_snapshot(row: dict, *, max_spread: float) -> Quote | None:
    captured = row.get("captured_at")
    if not isinstance(captured, str) or not captured.strip():
        return None
    return quote_from_book(
        as_of=parse_time(captured),
        yes_bid=_as_float(row.get("yes_bid")),
        yes_ask=_as_float(row.get("yes_ask")),
        trade_price=_as_float(row.get("last_price")),
        source="snapshot",
        max_spread=max_spread,
    )


def latest_at_or_before(
    quotes: list[Quote],
    cutoff: datetime,
    window_start: datetime | None = None,
) -> Quote | None:
    """Newest quote that does not cross the cutoff, optionally inside a window."""
    best: Quote | None = None
    for quote in quotes:
        if not _in_window(quote.as_of, cutoff, window_start):
            continue
        if best is None or quote.as_of > best.as_of:
            best = quote
    return best


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return parse_fixed_point(value)
