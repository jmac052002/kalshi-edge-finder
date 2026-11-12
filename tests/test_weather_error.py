"""The fitted daily-high error: shrinkage, the guard, and the JSON round trip."""

import math
from datetime import date

from kalshi_edge.weather import assumed_sigma_f, student_t_cdf, temperature_yes_probability
from kalshi_edge.weather_error import (
    ErrorPair,
    brier_score,
    calibration_from_json,
    calibration_to_json,
    constant_calibration,
    fit_error_model,
    judge_weather_edge,
    load_calibration,
    log_loss,
    market_gate,
    predict_high,
    unfitted_calibration,
)


def _pairs(station: str, errors: list[float], *, lead: int = 24, forecast: float = 70.0):
    return [
        ErrorPair(
            station_id=station,
            lead_hours=lead,
            forecast_f=forecast,
            settlement_f=forecast + error,
            market_date=date(2024, 1, 1),
            place="Test City",
        )
        for error in errors
    ]


def test_student_t_matches_published_critical_values():
    assert student_t_cdf(0.0, 5) == 0.5
    # Cauchy: P(T <= 1) = 0.75.
    assert student_t_cdf(1.0, 1) == pytest_approx(0.75)
    assert student_t_cdf(-1.0, 1) == pytest_approx(0.25)
    # Two-sided 5% critical value at 5 degrees of freedom is 2.5706.
    assert student_t_cdf(2.5706, 5) == pytest_approx(0.975, abs=1e-4)
    # A large df is the normal. Phi(1.64485) is about 0.95.
    assert student_t_cdf(1.64485, 1000) == pytest_approx(0.95, abs=1e-3)


def test_t_brackets_still_partition_the_line():
    strikes = [
        ("less", None, 59),
        ("between", 59, 60),
        ("between", 61, 62),
        ("greater", 62, None),
    ]
    probabilities = [
        temperature_yes_probability(61.0, 2.0, strike_type=kind, floor=floor, cap=cap, df=5)
        for kind, floor, cap in strikes
    ]
    assert sum(probabilities) == pytest_approx(1.0)


def test_fit_recovers_a_shifted_normal_and_shrinks_a_thin_city():
    # Symmetric noise, mean 0, plus one thin city at +3F. Shrinkage pulls that city back.
    base = [-1.0, 0.0, 1.0] * 14
    pooled = _pairs("KAAA", base)
    thin = _pairs("KBBB", [3.0] * 8)
    noisy = _pairs("KCCC", base)
    fit = fit_error_model(
        pooled + thin + noisy,
        fitted_at="2026-11-11T00:00:00Z",
        source_label="fixture",
        source_note="fixture",
        min_pooled_n=20,
        min_city_n=8,
        shrink_k=15,
    )
    assert fit.trusted
    assert fit.distribution == "normal"
    lead = fit.leads[24]
    # 84 zeros from the two cities of [-1, 0, 1], plus 8 times +3.
    assert lead.bias_f == pytest_approx(24.0 / 92.0, abs=0.02)
    assert "KBBB" in fit.stations
    assert fit.stations["KBBB"].bias_f < 4.0
    assert fit.stations["KBBB"].bias_f > lead.bias_f
    text = calibration_to_json(fit)
    loaded = calibration_from_json(text)
    assert loaded.trusted
    assert loaded.stations["KBBB"].n == 8
    prediction = predict_high(loaded, "KBBB", 18, 70.0)
    assert prediction.trusted
    assert prediction.bias_f == pytest_approx(loaded.stations["KBBB"].bias_f)
    assert prediction.mean_f == pytest_approx(70.0 + prediction.bias_f)
    unknown = predict_high(loaded, "KZZZ", 18, 70.0)
    assert unknown.bias_f == pytest_approx(lead.bias_f)
    assert unknown.detail.startswith("fit n=")


def test_a_fat_sample_can_select_a_t_and_a_thin_pool_does_not():
    # A few huge busts on an otherwise tight error. n is large enough to fit.
    errors = [0.2] * 80 + [12.0, -12.0, 11.0, -11.0]
    fit = fit_error_model(
        _pairs("KAAA", errors),
        fitted_at="t",
        source_label="fixture",
        source_note="fixture",
        min_pooled_n=30,
    )
    assert fit.trusted
    # Either the t wins or the normal is kept; both must price a finite tail.
    prediction = predict_high(fit, "KAAA", 20, 60.0)
    probability = temperature_yes_probability(
        prediction.mean_f,
        prediction.scale_f,
        strike_type="greater",
        floor=80,
        cap=None,
        df=prediction.df,
    )
    assert probability is not None
    assert 0.0 < probability < 0.5


def test_too_few_days_is_not_trusted_and_the_scan_uses_the_assumed_width():
    fit = fit_error_model(
        _pairs("KAAA", [1.0, -1.0, 0.5]),
        fitted_at="short",
        source_label="fixture",
        source_note="fixture",
    )
    assert not fit.trusted
    prediction = predict_high(fit, "KAAA", 6, 55.0)
    assert prediction.trusted is False
    assert prediction.scale_f == pytest_approx(assumed_sigma_f(6))
    assert prediction.mean_f == 55.0
    fallback = predict_high(unfitted_calibration(), "KAAA", 6, 55.0)
    assert fallback.scale_f == pytest_approx(assumed_sigma_f(6))


def test_long_lead_is_wider_than_the_day_ahead_fit():
    day = _pairs("KAAA", [0.0, 1.0, -1.0, 0.5] * 10, lead=24)
    longer = _pairs("KAAA", [0.0, 2.0, -2.0, 1.0] * 10, lead=48, forecast=71.0)
    fit = fit_error_model(
        day + longer,
        fitted_at="leads",
        source_label="fixture",
        source_note="fixture",
        min_pooled_n=20,
        min_city_n=8,
    )
    short = predict_high(fit, "KAAA", 18, 70)
    medium = predict_high(fit, "KAAA", 50, 70)
    far = predict_high(fit, "KAAA", 96, 70)
    assert short.lead_hours_used == 24
    assert medium.lead_hours_used == 48
    assert far.scale_f > medium.scale_f


def test_outliers_are_dropped():
    pairs = _pairs("KAAA", [0.5] * 40) + _pairs("KAAA", [40.0])
    fit = fit_error_model(
        pairs,
        fitted_at="out",
        source_label="fixture",
        source_note="fixture",
        min_pooled_n=20,
        min_city_n=8,
    )
    assert fit.rejected_outliers == 1
    assert fit.leads[24].n == 40
    assert fit.leads[24].bias_f == pytest_approx(0.5)


def test_guard_requires_margin_and_flags_a_huge_gap():
    clears, verify, note = judge_weather_edge(
        probability=0.40,
        ask=0.30,
        break_even=0.32,
        trusted=True,
        extra_margin=0.03,
        disagree_gap=0.15,
    )
    assert clears is True and verify is False and note == ""
    clears, verify, note = judge_weather_edge(
        probability=0.34,
        ask=0.30,
        break_even=0.32,
        trusted=True,
        extra_margin=0.03,
        disagree_gap=0.15,
    )
    assert clears is False and verify is False
    clears, verify, note = judge_weather_edge(
        probability=0.70,
        ask=0.30,
        break_even=0.32,
        trusted=True,
        extra_margin=0.03,
        disagree_gap=0.15,
    )
    assert clears is False and verify is True
    assert note == "model disagrees strongly, verify"
    clears, verify, _note = judge_weather_edge(
        probability=0.90,
        ask=0.10,
        break_even=0.12,
        trusted=False,
        extra_margin=0.0,
        disagree_gap=1.0,
    )
    assert clears is False and verify is True


def _scored(brier, log_loss_pair, **extra):
    from dataclasses import replace

    scores = {
        "brier": {"calibrated": brier[0], "market": brier[1]},
        "log_loss": {"calibrated": log_loss_pair[0], "market": log_loss_pair[1]},
        **extra,
    }
    return replace(constant_calibration(bias_f=0.0, scale_f=2.0), scores=scores)


def test_the_gate_opens_only_when_the_fit_scores_at_least_as_well_as_the_market():
    assert market_gate(_scored((0.07, 0.08), (0.23, 0.24))) == (True, "")
    assert market_gate(_scored((0.08, 0.08), (0.24, 0.24))) == (True, "")  # a tie is not worse
    may_rank, reason = market_gate(_scored((0.132, 0.077), (0.424, 0.241), out_of_sample=True))
    assert may_rank is False
    assert "out of sample" in reason
    assert "brier 0.132 against the market's 0.077" in reason and "log loss 0.424 against the market's 0.241" in reason
    # Better on one score and worse on the other is still shut, and says which.
    may_rank, reason = market_gate(_scored((0.07, 0.08), (0.30, 0.24)))
    assert may_rank is False and "log loss" in reason and "brier" not in reason and "in sample" in reason


def test_an_unscored_fit_is_not_ranked_either():
    """Not having been tested against the market is not a pass."""
    from dataclasses import replace

    base = constant_calibration(bias_f=0.0, scale_f=2.0)
    for scores in (
        {},
        {"brier": {"calibrated": 0.05}},
        {"brier": {"calibrated": 0.05, "market": 0.08}},  # log loss missing
        {"brier": {"calibrated": "0.05", "market": 0.08}, "log_loss": {"calibrated": 0.2, "market": 0.3}},
        {"brier": {"calibrated": float("nan"), "market": 0.08}, "log_loss": {"calibrated": 0.2, "market": 0.3}},
        {"brier": {"calibrated": True, "market": 0.08}, "log_loss": {"calibrated": 0.2, "market": 0.3}},
        {"brier": "n/a", "log_loss": None},
    ):
        may_rank, reason = market_gate(replace(base, scores=scores))
        assert may_rank is False and "has not been scored" in reason
    # An unfitted calibration is shut too, and predict_high already says why.
    assert market_gate(unfitted_calibration()) == (False, "")


def test_the_test_helper_can_stand_on_either_side_of_the_gate():
    assert market_gate(constant_calibration(bias_f=0.0, scale_f=2.0))[0] is True
    shut = constant_calibration(bias_f=0.0, scale_f=2.0, beats_market=False)
    assert market_gate(shut)[0] is False
    assert "Not ranked today" in shut.scan_note() and "Not ranked today" not in constant_calibration(
        bias_f=0.0, scale_f=2.0
    ).scan_note()


def test_the_packaged_file_is_gated_by_its_own_scores():
    """Whatever the shipped numbers are, the gate has to agree with them."""
    loaded = load_calibration()
    brier, loss = loaded.scores["brier"], loaded.scores["log_loss"]
    expected = brier["calibrated"] <= brier["market"] and loss["calibrated"] <= loss["market"]
    assert market_gate(loaded)[0] is expected


def test_brier_and_log_loss_prefer_the_outcome():
    good = [(0.9, 1.0), (0.1, 0.0)]
    bad = [(0.1, 1.0), (0.9, 0.0)]
    assert brier_score(good) < brier_score(bad)
    assert log_loss(good) < log_loss(bad)
    assert brier_score([]) is None


def test_packaged_calibration_loads_and_prices_a_high():
    """The file shipped with the package is a real fit, not the fallback."""
    loaded = load_calibration()
    assert loaded.trusted
    assert loaded.leads[24].n >= 30
    assert 0.10 <= loaded.disagree_gap <= 0.20
    prediction = predict_high(loaded, "NO_SUCH_STATION", 18, 70.0)
    assert prediction.trusted
    assert prediction.scale_f > 0.5
    assert prediction.mean_f != 70.0 or prediction.bias_f == 0.0


def test_a_broken_file_is_the_unfitted_fallback(tmp_path):
    path = tmp_path / "weather_calibration.json"
    path.write_text("{", encoding="utf-8")
    assert calibration_from_json(path.read_text(encoding="utf-8")).trusted is False
    pinned = constant_calibration(bias_f=1.0, scale_f=2.0)
    assert math.isfinite(predict_high(pinned, "KNYC", 10, 60).mean_f)


def pytest_approx(value, abs=1e-6):
    import pytest

    return pytest.approx(value, abs=abs)
