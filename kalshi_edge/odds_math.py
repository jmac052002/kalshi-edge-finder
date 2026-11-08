"""Sportsbook odds arithmetic: implied probability, vig removal, break-even, EV.

A sportsbook quotes decimal odds ``o``: a $1 stake returns ``o`` dollars if
the bet wins. The raw implied probability is ``r = 1 / o``. Across the
outcomes of one market those raw probabilities sum to more than one; the
excess, ``V - 1``, is the book's margin (the "vig" or "overround"). To use a
book's line as a probability estimate the margin has to be taken back out,
and there is more than one defensible way to do it:

* **Multiplicative**: ``q_i = r_i / V``. Spreads the margin in proportion to
  the raw probability. Simple and common, but books do not price margin
  that way: they shade long shots more (the favorite-longshot bias), so
  after normalizing, the long shot still looks more likely than it is.
* **Power**: find ``k`` with ``sum(r_i ** k) == 1`` and set ``q_i = r_i ** k``.
  With ``V > 1`` the solution has ``k > 1``, and raising to a power above
  one shrinks small probabilities proportionally more than large ones,
  which is the direction the bias calls for. This is the default.

Probabilities are ``float``: they are estimates, and a float carries far
more precision than any book's line. Prices, fees and costs are
``decimal.Decimal``: they are ledger amounts that round at the cent, and a
binary float cannot represent 0.45 exactly.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

DevigMethod = Literal["power", "multiplicative"]
DEVIG_METHODS: tuple[DevigMethod, ...] = ("power", "multiplicative")

_SUM_TOLERANCE = 1e-12


def as_decimal_odds(value: Decimal | str | int) -> Decimal:
    """Parse one decimal-odds quote. Anything at or below 1.0 is not a price."""
    if isinstance(value, float):
        # A float like 1.909999 is a formatting accident waiting to happen;
        # The Odds API sends numbers that should be kept as their JSON text.
        raise TypeError("decimal odds must be Decimal, str or int, not float")
    try:
        odds = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"not decimal odds: {value!r}") from exc
    if not odds.is_finite() or odds <= 1:
        raise ValueError(f"decimal odds must be greater than 1, got {value!r}")
    return odds


def implied_probability(decimal_odds: Decimal | str | int) -> float:
    """Raw implied probability ``1 / o``, margin still included."""
    return 1.0 / float(as_decimal_odds(decimal_odds))


def overround(decimal_odds: Iterable[Decimal | str | int]) -> float:
    """Sum of the raw implied probabilities (1.04 means a 4% margin)."""
    return sum(implied_probability(o) for o in decimal_odds)


def devig_multiplicative(decimal_odds: Sequence[Decimal | str | int]) -> list[float]:
    """Normalize the raw probabilities so they sum to one."""
    raw = [implied_probability(o) for o in _at_least_two(decimal_odds)]
    total = sum(raw)
    return [r / total for r in raw]


def devig_power(
    decimal_odds: Sequence[Decimal | str | int],
    *,
    tolerance: float = _SUM_TOLERANCE,
    max_iterations: int = 200,
) -> list[float]:
    """Raise each raw probability to the power ``k`` that makes them sum to one.

    ``f(k) = sum(r_i ** k) - 1`` is strictly decreasing in ``k`` because every
    ``r_i`` is in (0, 1), so bisection on a bracket that straddles zero is
    guaranteed to converge; no SciPy needed. ``V > 1`` puts the root above
    ``k = 1``; a book quoting an arbitrage (``V < 1``) puts it below.
    """
    raw = [implied_probability(o) for o in _at_least_two(decimal_odds)]
    total = sum(raw)
    if abs(total - 1.0) <= tolerance:
        return raw

    def excess(k: float) -> float:
        return sum(r**k for r in raw) - 1.0

    low, high = (1.0, 2.0) if total > 1.0 else (0.5, 1.0)
    # Widen until the bracket straddles the root. Doubling terminates because
    # excess(k) -> -1 as k grows and -> n-1 > 0 as k -> 0.
    while excess(high) > 0:
        high *= 2.0
    while excess(low) < 0:
        low /= 2.0
    for _ in range(max_iterations):
        mid = (low + high) / 2.0
        value = excess(mid)
        if abs(value) <= tolerance:
            break
        if value > 0:
            low = mid
        else:
            high = mid
    k = (low + high) / 2.0
    return [r**k for r in raw]


def devig(decimal_odds: Sequence[Decimal | str | int], method: DevigMethod) -> list[float]:
    if method == "power":
        return devig_power(decimal_odds)
    if method == "multiplicative":
        return devig_multiplicative(decimal_odds)
    raise ValueError(f"unknown devig method {method!r}; expected one of {DEVIG_METHODS}")


def _at_least_two(decimal_odds: Sequence[Decimal | str | int]) -> Sequence[Decimal | str | int]:
    if len(decimal_odds) < 2:
        raise ValueError("a market needs at least two outcomes to remove the vig")
    return decimal_odds


@dataclass(frozen=True)
class Evaluation:
    """One side of one binary contract, bought at the ask, with the fee.

    ``price``, ``fee_per_contract``, ``cost`` and ``payout`` are dollars per
    contract as exact decimals. ``break_even`` is the win probability at
    which the trade returns exactly what it cost. ``edge`` is the fair
    probability minus that break-even, in probability points; ``ev`` is the
    same thing in dollars per contract; ``roi`` is ``ev / cost``.
    """

    price: Decimal
    contracts: int
    fee_total: Decimal
    fee_per_contract: Decimal
    cost: Decimal
    payout: Decimal
    break_even: float
    fair_probability: float
    edge: float
    ev: float
    roi: float

    @property
    def ev_total(self) -> float:
        """Expected profit on the whole order, in dollars."""
        return self.ev * self.contracts

    @property
    def stake(self) -> Decimal:
        """Dollars out of pocket for the whole order, fee included."""
        return self.cost * self.contracts


def evaluate(
    price: Decimal,
    fair_probability: float,
    *,
    contracts: int,
    fee_total: Decimal,
    payout: Decimal = Decimal(1),
) -> Evaluation:
    """Cost, break-even and expected value of buying ``contracts`` at ``price``.

    ``fee_total`` is the exchange fee for the whole order (see
    ``kalshi_edge.fees``); it is spread evenly here because the exchange
    rounds once per order, not once per contract. ``payout`` is the
    contract's notional, $1 on every Kalshi game market seen so far.
    """
    if contracts < 1:
        raise ValueError("contracts must be >= 1")
    if not 0.0 <= fair_probability <= 1.0:
        raise ValueError(f"fair_probability must be within [0, 1], got {fair_probability!r}")
    if not Decimal(0) < price < payout:
        raise ValueError(f"price must be between 0 and the payout {payout}, got {price}")
    if fee_total < 0:
        raise ValueError("fee_total must be >= 0")
    fee_per_contract = fee_total / contracts
    cost = price + fee_per_contract
    break_even = float(cost / payout)
    ev = fair_probability * float(payout) - float(cost)
    return Evaluation(
        price=price,
        contracts=contracts,
        fee_total=fee_total,
        fee_per_contract=fee_per_contract,
        cost=cost,
        payout=payout,
        break_even=break_even,
        fair_probability=fair_probability,
        edge=fair_probability - break_even,
        ev=ev,
        roi=ev / float(cost),
    )


def contracts_for_budget(
    budget: Decimal, price: Decimal, fee: Callable[[int], Decimal], *, maximum: int = 10_000
) -> int:
    """Largest whole number of contracts whose price plus fee fits in ``budget``.

    ``fee(n)`` returns the order fee for ``n`` contracts. The fee grows with
    ``n``, so start from the fee-free count and step down. Returns 0 when
    even one contract does not fit.
    """
    if budget <= 0 or price <= 0:
        return 0
    count = min(int(budget // price), maximum)
    while count > 0 and price * count + fee(count) > budget:
        count -= 1
    return count


@dataclass(frozen=True)
class ComboEvaluation:
    legs: tuple[float, ...]
    fair_probability: float
    fair_multiplier: float
    offered_multiplier: float
    stake: Decimal
    ev: float

    @property
    def hold(self) -> float:
        """Share of the stake the combo's pricing keeps, 1 - offered/fair."""
        return 1.0 - self.offered_multiplier / self.fair_multiplier


def combo(
    fair_probabilities: Sequence[float], *, offered_multiplier: float, stake: Decimal
) -> ComboEvaluation:
    """Expected value of a parlay whose legs are assumed independent.

    ``q = prod(q_i)`` is the joint win probability, so the fair payout is
    ``1 / q`` times the stake and ``EV = stake * (q * offered - 1)``. The
    independence assumption is stated in the output because it is the part
    most likely to be wrong: correlated legs make the product an over- or
    under-estimate, and books void or reprice the correlated ones.
    """
    if not fair_probabilities:
        raise ValueError("a combo needs at least one leg")
    if any(not 0.0 < q <= 1.0 for q in fair_probabilities):
        raise ValueError("every leg probability must be in (0, 1]")
    if offered_multiplier <= 0:
        raise ValueError("offered_multiplier must be > 0")
    if stake <= 0:
        raise ValueError("stake must be > 0")
    joint = math.prod(fair_probabilities)
    return ComboEvaluation(
        legs=tuple(fair_probabilities),
        fair_probability=joint,
        fair_multiplier=1.0 / joint,
        offered_multiplier=offered_multiplier,
        stake=stake,
        ev=float(stake) * (joint * offered_multiplier - 1.0),
    )
