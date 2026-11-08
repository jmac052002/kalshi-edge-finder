"""The fee table in Kalshi's July 7, 2026 schedule, not the centicent sentence."""

from decimal import Decimal

from kalshi_edge.fees import taker_fee


def test_one_contract_matches_the_published_table():
    # 0.07 * 0.50 * 0.50 = 0.0175, and the table charges $0.02, not $0.0175.
    assert taker_fee(0.50) == Decimal("0.02")
    assert taker_fee(0.10) == Decimal("0.01")
    assert taker_fee(0.20) == Decimal("0.02")
    assert taker_fee(0.15) == Decimal("0.01")
    assert taker_fee(0.99) == Decimal("0.01")
    assert taker_fee(0.01) == Decimal("0.01")


def test_hundred_contracts_match_the_published_table():
    assert taker_fee(0.50, 100) == Decimal("1.75")
    assert taker_fee(0.40, 100) == Decimal("1.68")
    assert taker_fee(0.10, 100) == Decimal("0.63")
    # 0.07 * 100 * 0.05 * 0.95 = 0.3325, rounded up to $0.34.
    assert taker_fee(0.05, 100) == Decimal("0.34")
    assert taker_fee(0.01, 100) == Decimal("0.07")


def test_no_fee_when_there_is_nothing_to_charge():
    assert taker_fee(0.0) == Decimal("0")
    assert taker_fee(1.0) == Decimal("0")
    assert taker_fee(0.50, multiplier=0) == Decimal("0")
    assert taker_fee(0.50, contracts=0) == Decimal("0")
