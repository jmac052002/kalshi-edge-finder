"""Buckets, Brier, and the fee-adjusted YES-buyer flag."""

import pytest

from kalshi_edge.calibration import (
    Observation,
    baseline_brier,
    binary_outcome,
    brier_score,
    bucket_index,
    build_buckets,
    wilson_interval,
)


def _obs(price, outcome, ask=None, ticker="T", event="E"):
    return Observation(
        ticker=ticker,
        event_ticker=event,
        price=price,
        buy_ask=ask,
        outcome=outcome,
        source="candlestick",
    )


def test_scalar_and_tie_payoffs_are_not_yes_or_no():
    assert binary_outcome("scalar", 0.5) is None
    assert binary_outcome("yes", 0.5) is None
    assert binary_outcome("no", 0.5) is None
    assert binary_outcome("yes", 1.0) == 1
    assert binary_outcome("no", 0.0) == 0
    assert binary_outcome("yes", None) == 1
    assert binary_outcome("no", 1.0) is None


def test_bucket_edges_do_not_slip_on_binary_floats():
    assert bucket_index(0.0, 0.1) == 0
    assert bucket_index(0.1, 0.1) == 1
    assert bucket_index(0.3, 0.1) == 3
    assert bucket_index(0.999, 0.1) == 9
    assert bucket_index(1.0, 0.1) == 9


def test_brier_of_a_coin_flip_forecast():
    pairs = [(0.5, 1), (0.5, 0)]
    assert brier_score(pairs) == 0.25
    assert baseline_brier([1, 0]) == 0.25
    assert brier_score([]) is None


def test_one_win_is_not_a_sure_thing():
    low, high = wilson_interval(1, 1)
    assert low < 0.5
    assert high == 1


def test_large_bucket_flags_a_yes_buy_only_when_the_interval_clears_the_cost():
    # 200 contracts priced at 40 cents, ask 40 cents, every one won.
    # Fee on a 40-cent contract is 2 cents, so the cost is 42 cents.
    # A 100% YES rate with this n sits entirely above that cost.
    winners = [_obs(0.40, 1, ask=0.40, ticker=str(i)) for i in range(200)]
    bucket = build_buckets(winners)[4]
    assert bucket.n == 200
    assert bucket.yes_rate == 1
    assert bucket.avg_price == 0.40
    assert bucket.flag == "yes+ev"
    assert bucket.avg_roi is not None and bucket.avg_roi > 0

    # One win out of one try is the same point estimate and is not a flag.
    one = build_buckets([_obs(0.40, 1, ask=0.40)])[4]
    assert one.yes_rate == 1
    assert one.flag == ""


def test_bucket_that_never_wins_is_negative_ev_for_a_yes_buyer():
    losers = [_obs(0.40, 0, ask=0.40, ticker=str(i)) for i in range(50)]
    bucket = build_buckets(losers)[4]
    assert bucket.yes_rate == 0
    assert bucket.flag == "yes-ev"
    assert bucket.avg_roi is not None and bucket.avg_roi < 0


def test_plot_writes_a_png(tmp_path):
    pytest.importorskip("matplotlib")
    from kalshi_edge.calibration import build_report, save_calibration_plot

    report = build_report(
        series="KXNFLGAME",
        observations=[_obs(0.40, 1, ask=0.40, ticker=str(i)) for i in range(20)],
        lead_minutes=180,
        lookback_minutes=360,
        period_minutes=60,
        max_spread=0.10,
        fee_rate=0.07,
        fee_multiplier=1,
        n_settled=20,
        n_excluded=0,
        n_missing_price=0,
        bucket_width=0.10,
    )
    path = tmp_path / "calibration.png"
    save_calibration_plot(report, str(path))
    assert path.stat().st_size > 1000


def test_roi_uses_the_ask_plus_the_taker_fee():
    # Ask 0.40, fee 0.02, cost 0.42. YES pays $1. ROI = 0.58 / 0.42.
    bucket = build_buckets([_obs(0.40, 1, ask=0.40)] * 30)[4]
    assert bucket.avg_cost == pytest.approx(0.42)
    assert bucket.avg_roi == pytest.approx((1 - 0.42) / 0.42)
