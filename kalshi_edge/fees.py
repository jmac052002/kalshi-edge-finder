"""Kalshi trading fees.

The published fee schedule (https://kalshi.com/docs/kalshi-fee-schedule.pdf)
charges a taker fee per order of::

    fees = round_up(M x 0.07 x C x P x (1 - P))

where ``P`` is the contract price in dollars, ``C`` the number of contracts,
``M`` a per-series multiplier (1 for most series), and ``round_up`` rounds
the dollar amount up to the next cent. There is no settlement fee: a contract
held to expiry pays its notional with nothing taken off.

``P x (1 - P)`` peaks at a 50-cent price, so a coin flip is the most
expensive trade on the exchange ($1.75 per 100 contracts) and a long shot at
5 cents costs a third of that. That shape is why fees matter for a backtest:
a strategy that "wins" by a fraction of a cent per contract in the middle of
the board loses it all to the fee.

Maker orders (resting limit orders) have a separate multiplier that is 0 on
most series. The backtest assumes we cross the spread and pay the taker fee,
which is the conservative choice for a small retail account.

The arithmetic is done in ``Decimal`` from the price's string form. Doing it
in floats is wrong at the boundary: ``0.07 * 0.5 * 0.5 * 100`` is not quite
``1.75`` in binary floating point, and a ceiling applied on the wrong side
of that error is off by a whole cent.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

TAKER_RATE = Decimal("0.07")
_CENT = Decimal("0.01")


def taker_fee(
    price: float | str | Decimal, contracts: int = 1, *, multiplier: Decimal = Decimal(1)
) -> Decimal:
    """Dollars of fee for one taker order of ``contracts`` at ``price``.

    ``price`` is the per-contract price in dollars (0.45 for 45 cents). The
    result is rounded up to the cent, as the exchange does for the whole
    order, so splitting an order into single contracts costs more in fees
    than one order for the same total. Callers that model one contract at a
    time are therefore pessimistic, which is the right error to make.
    """
    if contracts < 1:
        raise ValueError("contracts must be >= 1")
    p = _as_decimal(price)
    if not Decimal(0) <= p <= Decimal(1):
        raise ValueError(f"price must be between 0 and 1 dollars, got {price!r}")
    raw = multiplier * TAKER_RATE * contracts * p * (1 - p)
    return raw.quantize(_CENT, rounding=ROUND_CEILING)


def _as_decimal(price: float | str | Decimal) -> Decimal:
    if isinstance(price, Decimal):
        return price
    if isinstance(price, str):
        return Decimal(price.strip())
    # A float price came from a four-decimal API string; restore that form
    # rather than feeding its binary expansion (0.45000000000000001) in.
    return Decimal(f"{price:.4f}")
