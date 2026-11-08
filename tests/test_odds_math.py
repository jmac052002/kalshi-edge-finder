"""De-vig, break-even and EV arithmetic, checked against the design doc's worked numbers."""

import math
from decimal import Decimal

import pytest

from kalshi_edge.fees import taker_fee
from kalshi_edge.odds_math import (
    combo,
    contracts_for_budget,
    devig,
    devig_multiplicative,
    devig_power,
    evaluate,
    implied_probability,
    overround,
)


def test_implied_probability_is_one_over_odds():
    assert implied_probability("2.00") == 0.5
    assert implied_probability(Decimal(4)) == 0.25
    assert overround(["1.80", "2.10"]) == pytest.approx(1.0317, abs=1e-4)


def test_rejects_non_prices():
    for bad in ("1.00", "0.5", "-2", "abc"):
        with pytest.raises(ValueError):
            implied_probability(bad)
    with pytest.raises(TypeError):
        implied_probability(1.8)  # floats are a formatting accident; pass the API's text


def test_two_way_examples_from_design_doc():
    # Pinnacle-style 1.80 / 2.10
    assert devig_multiplicative(["1.80", "2.10"]) == pytest.approx([0.5385, 0.4615], abs=5e-5)
    assert devig_power(["1.80", "2.10"]) == pytest.approx([0.5403, 0.4597], abs=5e-5)
    # Lopsided 1.20 / 5.00: the methods disagree by 1.6 points on the long shot.
    assert devig_multiplicative(["1.20", "5.00"])[1] == pytest.approx(0.1935, abs=5e-5)
    assert devig_power(["1.20", "5.00"])[1] == pytest.approx(0.1777, abs=5e-5)


@pytest.mark.parametrize("odds", [["1.80", "2.10"], ["1.20", "5.00"], ["2.5", "3.4", "2.9"], ["1.01", "15"]])
def test_devigged_probabilities_sum_to_one(odds):
    for method in ("power", "multiplicative"):
        assert math.fsum(devig(odds, method)) == pytest.approx(1.0, abs=1e-9)


def test_power_shrinks_long_shots_more_than_multiplicative():
    power = devig_power(["1.20", "5.00"])
    mult = devig_multiplicative(["1.20", "5.00"])
    assert power[1] < mult[1]  # the long shot
    assert power[0] > mult[0]  # the favorite


def test_methods_agree_when_there_is_no_vig():
    fair = ["2.00", "2.00"]
    assert devig_power(fair) == devig_multiplicative(fair) == [0.5, 0.5]


def test_power_handles_an_arbitrage_book():
    # Raw probabilities sum to less than one; the root is below k = 1.
    q = devig_power(["2.10", "2.10"])
    assert q == pytest.approx([0.5, 0.5])


def test_devig_needs_two_outcomes_and_a_known_method():
    with pytest.raises(ValueError):
        devig_power(["1.5"])
    with pytest.raises(ValueError):
        devig(["1.5", "2.5"], "shin")  # type: ignore[arg-type]


def test_evaluate_matches_design_doc_worked_example():
    # Bengals YES at 0.45, fair 0.4597, ten contracts: fee $0.18 -> $0.018 each.
    fee = taker_fee("0.45", 10)
    assert fee == Decimal("0.18")
    result = evaluate(Decimal("0.45"), 0.4597, contracts=10, fee_total=fee)
    assert result.fee_per_contract == Decimal("0.018")
    assert result.cost == Decimal("0.468")
    assert result.break_even == pytest.approx(0.468)
    assert result.edge == pytest.approx(-0.0083, abs=1e-4)
    assert result.ev == pytest.approx(-0.0083, abs=1e-4)
    assert result.ev_total == pytest.approx(-0.083, abs=1e-3)
    assert result.stake == Decimal("4.680")
    assert result.roi == pytest.approx(-0.0083 / 0.468, abs=1e-3)


def test_ev_sign_flips_exactly_at_break_even():
    cost = Decimal("0.45") + Decimal("0.02")
    at = evaluate(Decimal("0.45"), float(cost), contracts=1, fee_total=Decimal("0.02"))
    assert at.ev == pytest.approx(0.0, abs=1e-12)
    above = evaluate(Decimal("0.45"), float(cost) + 0.01, contracts=1, fee_total=Decimal("0.02"))
    below = evaluate(Decimal("0.45"), float(cost) - 0.01, contracts=1, fee_total=Decimal("0.02"))
    assert above.ev > 0 > below.ev


def test_evaluate_rejects_impossible_inputs():
    with pytest.raises(ValueError):
        evaluate(Decimal("1.00"), 0.5, contracts=1, fee_total=Decimal(0))
    with pytest.raises(ValueError):
        evaluate(Decimal("0.50"), 1.5, contracts=1, fee_total=Decimal(0))
    with pytest.raises(ValueError):
        evaluate(Decimal("0.50"), 0.5, contracts=0, fee_total=Decimal(0))


def test_contracts_for_budget_includes_the_fee():
    fee = lambda n: taker_fee("0.45", n)
    # 22 x 0.45 = 9.90 fits the price alone; the $0.39 fee pushes it over $10.
    assert contracts_for_budget(Decimal(10), Decimal("0.45"), fee) == 21
    assert contracts_for_budget(Decimal("0.10"), Decimal("0.45"), fee) == 0
    fee99 = lambda n: taker_fee("0.99", n)
    assert contracts_for_budget(Decimal(10), Decimal("0.99"), fee99) == 10  # 9.90 + $0.01 fee


def test_combo_compounds_and_single_leg_equals_single_bet():
    one = combo([0.55], offered_multiplier=1.8, stake=Decimal(10))
    assert one.fair_probability == 0.55
    assert one.ev == pytest.approx(10 * (0.55 * 1.8 - 1))
    three = combo([0.55, 0.55, 0.55], offered_multiplier=5.2, stake=Decimal(10))
    assert three.fair_probability == pytest.approx(0.166375)
    assert three.fair_multiplier == pytest.approx(6.01, abs=0.01)
    assert three.ev == pytest.approx(-1.35, abs=0.01)  # the design doc's "about -$1.35"
    assert three.hold == pytest.approx(1 - 5.2 / 6.0105, abs=1e-3)
    with pytest.raises(ValueError):
        combo([], offered_multiplier=2, stake=Decimal(1))
