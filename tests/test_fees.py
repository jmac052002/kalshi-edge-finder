"""Fee formula checks against the worked examples in Kalshi's fee schedule."""

from decimal import Decimal

import pytest

from kalshi_edge.fees import taker_fee


@pytest.mark.parametrize(
    ("price", "contracts", "expected"),
    [
        ("0.50", 1, "0.02"),  # 0.0175 rounds up to the next cent
        ("0.50", 100, "1.75"),  # the schedule's own worst case
        ("0.01", 1, "0.01"),  # 0.000693 still rounds up to a cent
        ("0.01", 100, "0.07"),  # schedule example: $0.07 on 100 contracts at 1c
        ("0.99", 100, "0.07"),  # symmetric in P and 1-P
        ("0.10", 10, "0.07"),  # 0.063 -> 0.07
        ("0.0000", 1, "0.00"),  # nothing to charge at the boundary
        ("1.0000", 1, "0.00"),
    ],
)
def test_taker_fee_matches_schedule(price, contracts, expected):
    assert taker_fee(price, contracts) == Decimal(expected)


def test_float_price_is_treated_as_four_decimal_quote():
    # 0.07 * 0.5 * 0.5 * 100 = 1.75 exactly in decimal; a float ceiling could land on 1.76.
    assert taker_fee(0.5, 100) == Decimal("1.75")
    assert taker_fee(0.45, 1) == Decimal("0.02")


def test_multiplier_scales_the_fee():
    assert taker_fee("0.50", 100, multiplier=Decimal("0.5")) == Decimal("0.88")


def test_rejects_bad_inputs():
    with pytest.raises(ValueError):
        taker_fee("0.50", 0)
    with pytest.raises(ValueError):
        taker_fee("1.5", 1)
    with pytest.raises(ValueError):
        taker_fee(-0.1, 1)
