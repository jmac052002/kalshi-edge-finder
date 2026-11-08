"""Kalshi's taker fee, from the published fee schedule.

Buying YES at the ask is taking liquidity: the order matches immediately, so
the taker schedule applies. Resting a bid would be the cheaper maker schedule.
This backtest only prices the aggressive buy.

The general formula, effective July 7, 2026, is::

    fee = round_up(multiplier * 0.07 * contracts * price * (1 - price))

``price`` is the contract price in dollars (0.50 is fifty cents). ``multiplier``
comes from ``GET /series/{ticker}`` (``fee_multiplier``); it is 1 for
KXNFLGAME and 0 for a series the schedule marks as no-fee.

The schedule's prose says the fee is rounded so that fee plus premium lands
on a centicent ($0.0001). The worked table does not do that. One contract at
$0.50 is ``0.07 * 0.5 * 0.5 = 0.0175``, and the table charges $0.02. One
hundred contracts at $0.05 are $0.3325, and the table charges $0.34. We follow
the table: round the fee up to the next cent. There is no settlement fee.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING

# Coefficient in front of C * P * (1-P) before the series multiplier.
TAKER_FEE_RATE = Decimal("0.07")


def taker_fee(
    price: float,
    contracts: float = 1.0,
    *,
    multiplier: float = 1.0,
    rate: Decimal = TAKER_FEE_RATE,
) -> Decimal:
    """Fee in dollars for buying ``contracts`` at ``price``.

    A price of 0 or 1 has no expected earnings, so the fee is 0. A non-positive
    multiplier is a no-fee series. The result is a Decimal in whole cents.
    """
    if contracts <= 0 or multiplier <= 0 or rate <= 0:
        return Decimal("0")
    if price <= 0 or price >= 1:
        return Decimal("0")
    # Four decimal places is the quote grid the API prints ("0.6300").
    # Quantizing first keeps binary float dust out of the cent rounding.
    premium = Decimal(str(price)).quantize(Decimal("0.0001"))
    if premium <= 0 or premium >= 1:
        return Decimal("0")
    count = Decimal(str(contracts))
    raw = Decimal(str(multiplier)) * rate * count * premium * (1 - premium)
    cents = (raw * 100).quantize(Decimal("1"), rounding=ROUND_CEILING)
    return cents / Decimal("100")
