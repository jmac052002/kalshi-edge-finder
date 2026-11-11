"""Cleveland Fed nowcast file: point value, historical error, one-decimal print."""

import math

import pytest

from kalshi_edge.nowcast import blend_sigma, parse_nowcast_file, probability_print_exceeds


def _chart(month: str, now_values: list[float], actual: float | None, *, label: str = "11/10") -> dict:
    labels = [label for _ in now_values]
    if actual is not None:
        labels = labels + [f"CPI {month}"]
    now_data = [{"value": f"{value:.4f}"} for value in now_values]
    actual_data = [{"value": ""} for _ in now_values]
    if actual is not None:
        now_data = now_data + [{"value": ""}]
        actual_data = actual_data + [{"value": f"{actual:.4f}"}]
    core_actual = actual_data
    return {
        "chart": {"subcaption": month, "_comment": "2026-11-10 00:00"},
        "categories": [{"category": [{"label": item} for item in labels]}],
        "dataset": [
            {"seriesname": "CPI Inflation", "data": now_data},
            {"seriesname": "Core CPI Inflation", "data": now_data},
            {"seriesname": "Actual CPI Inflation", "data": actual_data},
            {"seriesname": "Actual Core CPI Inflation", "data": core_actual},
        ],
    }


def test_nowcast_uses_the_open_month_and_the_file_error():
    finished = []
    # Three finished months. First nowcast is 0.10 below the actual, last is 0.00 off.
    for index, month in enumerate(("2026-07", "2026-08", "2026-09")):
        actual = 0.30 + index * 0.10
        finished.append(_chart(month, [actual - 0.10, actual], actual))
    current = _chart("2026-11", [0.14], None, label="11/10")
    model = parse_nowcast_file(finished + [current], kind="cpi_mom", min_months=3)
    assert model is not None
    assert model.reference_month == "2026-11"
    assert model.as_of_label == "11/10"
    assert model.value == pytest.approx(0.14)
    assert model.n_months == 3
    # Early error is +0.10 every month, so the early sd is 0 and the floor applies
    # once the path is short. The final error is 0. A one-point path is the early end.
    assert model.early_sigma == pytest.approx(0.0)
    assert model.final_sigma == pytest.approx(0.0)
    assert model.sigma == pytest.approx(0.05)
    assert model.mean_error == pytest.approx(0.10)
    assert "Cleveland Fed" in model.source


def test_too_few_months_is_not_a_model():
    charts = [
        _chart("2026-10", [0.2, 0.3], 0.3),
        _chart("2026-11", [0.14], None),
    ]
    assert parse_nowcast_file(charts, kind="cpi_mom", min_months=3) is None
    assert parse_nowcast_file({"not": "a list"}, kind="cpi_mom") is None


def test_one_decimal_print_gets_less_likely_as_the_strike_rises():
    low = probability_print_exceeds(0.14, 0.15, 0.0)
    mid = probability_print_exceeds(0.14, 0.15, 0.1)
    high = probability_print_exceeds(0.14, 0.15, 0.4)
    assert low > mid > high
    # "More than 0.1" is a print of 0.2 or higher, i.e. an unrounded change >= 0.15.
    expected = 1.0 - 0.5 * (1.0 + math.erf((0.15 - 0.14) / 0.15 / math.sqrt(2.0)))
    assert mid == pytest.approx(expected)


def test_blend_moves_from_early_to_final():
    assert blend_sigma(1, 10, 0.20, 0.10) == pytest.approx(0.20)
    assert blend_sigma(10, 10, 0.20, 0.10) == pytest.approx(0.10)
    assert blend_sigma(1, 10, 0.0, 0.0) == pytest.approx(0.05)
