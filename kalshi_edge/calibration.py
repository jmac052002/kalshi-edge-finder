"""Turn pre-event prices and YES/NO outcomes into a calibration table.

The question is: among contracts whose pre-event price was about X, what
fraction resolved YES? A calibrated market lands on the diagonal. The
difference ``yes_rate - avg_price`` is the gap. It is not yet a trading
profit. Buying YES pays the ask, plus the taker fee, and receives $1 only if
YES happens. A bucket can be well calibrated and still lose money.

Ties and other scalar settlements pay a fraction (0.5 on a tied NFL game).
They are not YES and not NO, so they are left out of the rate, the Brier
score, and the ROI. Counting them as half a win would mix a different bet
into the percentage.

Both sides of one game are one outcome. The table counts contracts, so the
standard error treats those two rows as independent samples and is too small.
The Wilson interval is still the right shape for a small bucket: one win out
of one try is not "100% with no uncertainty."
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from kalshi_edge.fees import taker_fee


def binary_outcome(result: str | None, settlement_value: float | None) -> int | None:
    """1 if YES paid $1, 0 if YES paid $0, None if this is not that bet.

    A settlement strictly between 0 and 1 is a scalar (a tie paid 0.5). A
    result that disagrees with the payoff is dropped rather than scored.
    """
    if settlement_value is not None and 0.001 < float(settlement_value) < 0.999:
        return None
    if result == "yes" and (settlement_value is None or float(settlement_value) >= 0.999):
        return 1
    if result == "no" and (settlement_value is None or float(settlement_value) <= 0.001):
        return 0
    return None


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson interval for a YES rate. Wider than a normal interval near 0 and 1.

    Returns None when there is nothing to estimate. The bounds are clipped
    to [0, 1].
    """
    if n <= 0:
        return None
    proportion = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (proportion + z2 / (2 * n)) / denom
    margin = z * math.sqrt(proportion * (1 - proportion) / n + z2 / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def yes_rate_se(successes: int, n: int) -> float | None:
    """Binomial standard error sqrt(p (1-p) / n). Zero when p is 0 or 1."""
    if n <= 0:
        return None
    proportion = successes / n
    return math.sqrt(proportion * (1 - proportion) / n)


def brier_score(pairs: list[tuple[float, int]]) -> float | None:
    """Mean squared error of the probability against a 0/1 outcome.

    A constant forecast of 0.5 scores 0.25. Lower is better. This is the
    score of the prices we actually used, not of the post-outcome last trade.
    """
    if not pairs:
        return None
    total = 0.0
    for price, outcome in pairs:
        total += (price - outcome) ** 2
    return total / len(pairs)


def baseline_brier(outcomes: list[int]) -> float | None:
    """Brier score of predicting the sample's own base rate for every contract.

    Beating this means the prices ranked the contracts, not just that the
    average price matched the average outcome. For a 50/50 sample it is 0.25.
    """
    if not outcomes:
        return None
    base = sum(outcomes) / len(outcomes)
    return sum((base - outcome) ** 2 for outcome in outcomes) / len(outcomes)


@dataclass(frozen=True)
class Observation:
    ticker: str
    event_ticker: str | None
    price: float
    buy_ask: float | None
    outcome: int
    source: str


@dataclass(frozen=True)
class Bucket:
    lo: float
    hi: float
    n: int
    avg_price: float | None
    yes_rate: float | None
    difference: float | None
    se: float | None
    ci_low: float | None
    ci_high: float | None
    avg_cost: float | None
    avg_roi: float | None
    n_priced: int
    flag: str


@dataclass(frozen=True)
class CalibrationReport:
    series: str
    lead_minutes: int
    lookback_minutes: int
    period_minutes: int
    max_spread: float
    fee_rate: float
    fee_multiplier: float
    n_settled: int
    n_excluded: int
    n_missing_price: int
    n_used: int
    n_events: int
    n_from_snapshot: int
    n_from_candlestick: int
    n_from_trade: int
    brier: float | None
    brier_baseline: float | None
    buckets: tuple[Bucket, ...]


def bucket_index(price: float, width: float) -> int:
    """Left-closed buckets. 0.30 belongs in 30-40, not 20-30.

    ``0.3 / 0.1`` is not exactly 3 in binary floating point, so a bare
    ``int(price / width)`` would file a 30% contract in the 20% bucket.
    """
    if width <= 0:
        raise ValueError("bucket width must be positive")
    n_buckets = max(1, math.ceil((1.0 - 1e-12) / width))
    if price >= 1:
        return n_buckets - 1
    index = int(math.floor(price / width + 1e-9))
    return min(max(index, 0), n_buckets - 1)


def _roi(outcome: int, cost: float) -> float | None:
    if cost <= 0:
        return None
    payout = 1.0 if outcome == 1 else 0.0
    return (payout - cost) / cost


def build_buckets(
    observations: list[Observation],
    *,
    width: float = 0.1,
    fee_multiplier: float = 1.0,
) -> tuple[Bucket, ...]:
    """Group by the pre-event price. ROI is a one-contract buy at the ask."""
    n_buckets = max(1, math.ceil((1.0 - 1e-12) / width))
    groups: list[list[Observation]] = [[] for _ in range(n_buckets)]
    for observation in observations:
        groups[bucket_index(observation.price, width)].append(observation)

    rows: list[Bucket] = []
    for index, group in enumerate(groups):
        lo = index * width
        hi = 1.0 if index == n_buckets - 1 else min(1.0, (index + 1) * width)
        if not group:
            rows.append(
                Bucket(
                    lo=lo,
                    hi=hi,
                    n=0,
                    avg_price=None,
                    yes_rate=None,
                    difference=None,
                    se=None,
                    ci_low=None,
                    ci_high=None,
                    avg_cost=None,
                    avg_roi=None,
                    n_priced=0,
                    flag="",
                )
            )
            continue
        successes = sum(item.outcome for item in group)
        n = len(group)
        avg_price = sum(item.price for item in group) / n
        yes_rate = successes / n
        interval = wilson_interval(successes, n)
        ci_low, ci_high = interval if interval is not None else (None, None)
        costs: list[float] = []
        rois: list[float] = []
        for item in group:
            if item.buy_ask is None or item.buy_ask <= 0 or item.buy_ask >= 1:
                continue
            fee = float(taker_fee(item.buy_ask, multiplier=fee_multiplier))
            cost = item.buy_ask + fee
            roi = _roi(item.outcome, cost)
            if roi is None:
                continue
            costs.append(cost)
            rois.append(roi)
        avg_cost = sum(costs) / len(costs) if costs else None
        avg_roi = sum(rois) / len(rois) if rois else None
        flag = ""
        if avg_cost is not None and ci_low is not None and ci_high is not None:
            # +EV only if even the low end of the YES rate clears the cost.
            # A point estimate above the cost with a CI that covers it is noise.
            if ci_low > avg_cost:
                flag = "yes+ev"
            elif ci_high < avg_cost:
                flag = "yes-ev"
        rows.append(
            Bucket(
                lo=lo,
                hi=hi,
                n=n,
                avg_price=avg_price,
                yes_rate=yes_rate,
                difference=yes_rate - avg_price,
                se=yes_rate_se(successes, n),
                ci_low=ci_low,
                ci_high=ci_high,
                avg_cost=avg_cost,
                avg_roi=avg_roi,
                n_priced=len(rois),
                flag=flag,
            )
        )
    return tuple(rows)


def build_report(
    *,
    series: str,
    observations: list[Observation],
    lead_minutes: int,
    lookback_minutes: int,
    period_minutes: int,
    max_spread: float,
    fee_rate: float,
    fee_multiplier: float,
    n_settled: int,
    n_excluded: int,
    n_missing_price: int,
    bucket_width: float,
) -> CalibrationReport:
    pairs = [(item.price, item.outcome) for item in observations]
    events = {item.event_ticker or item.ticker for item in observations}
    sources = [item.source for item in observations]
    return CalibrationReport(
        series=series,
        lead_minutes=lead_minutes,
        lookback_minutes=lookback_minutes,
        period_minutes=period_minutes,
        max_spread=max_spread,
        fee_rate=fee_rate,
        fee_multiplier=fee_multiplier,
        n_settled=n_settled,
        n_excluded=n_excluded,
        n_missing_price=n_missing_price,
        n_used=len(observations),
        n_events=len(events),
        n_from_snapshot=sources.count("snapshot"),
        n_from_candlestick=sources.count("candlestick"),
        n_from_trade=sources.count("trade"),
        brier=brier_score(pairs),
        brier_baseline=baseline_brier([item.outcome for item in observations]),
        buckets=build_buckets(
            observations, width=bucket_width, fee_multiplier=fee_multiplier
        ),
    )


def format_report(report: CalibrationReport) -> str:
    """Plain-text table. Percentages are easier to read than raw floats."""
    lines = [
        f"series: {report.series}",
        (
            "reference: last tight mid (else last trade) at or before "
            f"close_time minus {report.lead_minutes} minutes"
        ),
        (
            f"window: {report.lookback_minutes} minutes, "
            f"bars: {report.period_minutes} minutes, "
            f"max spread: {report.max_spread:.2f}"
        ),
        (
            "fee: taker round-up-to-cent of "
            f"{report.fee_rate:.2f} * {report.fee_multiplier:g} * P * (1-P) "
            "on one contract bought at the ask"
        ),
        (
            f"settled: {report.n_settled}  "
            f"excluded non-binary: {report.n_excluded}  "
            f"no pre-event price: {report.n_missing_price}  "
            f"used: {report.n_used}  "
            f"events: {report.n_events}"
        ),
        (
            f"prices from snapshots: {report.n_from_snapshot}  "
            f"candlesticks: {report.n_from_candlestick}  "
            f"trades: {report.n_from_trade}"
        ),
        f"brier: {_fmt(report.brier)}  baseline brier (constant base rate): {_fmt(report.brier_baseline)}",
        "",
        (
            f"{'bucket':<12} {'n':>5} {'avg_price':>10} {'yes_rate':>10} "
            f"{'diff':>8} {'se':>8} {'ci95':>20} {'avg_cost':>9} {'avg_roi':>9}  flag"
        ),
    ]
    for bucket in report.buckets:
        if bucket.n == 0:
            continue
        ci = ""
        if bucket.ci_low is not None and bucket.ci_high is not None:
            ci = f"{_pct(bucket.ci_low)}..{_pct(bucket.ci_high)}"
        lines.append(
            f"{_bucket_label(bucket):<12} {bucket.n:5d} {_pct(bucket.avg_price):>10} "
            f"{_pct(bucket.yes_rate):>10} {_signed_pp(bucket.difference):>8} "
            f"{_pct(bucket.se):>8} {ci:>20} {_pct(bucket.avg_cost):>9} "
            f"{_signed_pct(bucket.avg_roi):>9}  {bucket.flag}"
        )
    lines.append(
        "diff = yes_rate - avg_price. avg_cost = ask + fee. "
        "avg_roi is the equal-weighted return of buying one YES at that cost. "
        "yes+ev / yes-ev means the whole 95% Wilson interval sits on one side of avg_cost."
    )
    return "\n".join(lines)


def save_calibration_plot(report: CalibrationReport, path: str) -> None:
    """Reliability diagram. matplotlib is imported here so the rest of the tool runs without it."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "matplotlib is not installed. pip install -r requirements-plot.txt"
        ) from exc

    points = [bucket for bucket in report.buckets if bucket.n > 0 and bucket.avg_price is not None]
    figure, axis = plt.subplots(figsize=(7.5, 6.5))
    axis.plot([0, 1], [0, 1], color="#888888", linewidth=1, label="calibrated")
    if points:
        xs = [bucket.avg_price for bucket in points]
        ys = [bucket.yes_rate for bucket in points]
        yerr_low = [
            max(0.0, (bucket.yes_rate or 0.0) - (bucket.ci_low or 0.0)) for bucket in points
        ]
        yerr_high = [
            max(0.0, (bucket.ci_high or 0.0) - (bucket.yes_rate or 0.0)) for bucket in points
        ]
        axis.errorbar(
            xs,
            ys,
            yerr=[yerr_low, yerr_high],
            fmt="o",
            color="#1f4e79",
            ecolor="#5b7c99",
            capsize=3,
            label="bucket YES rate",
        )
    axis.set_xlim(0, 1)
    axis.set_ylim(0, 1)
    axis.set_xlabel("Average pre-event price")
    axis.set_ylabel("Fraction that resolved YES")
    axis.set_title(
        f"{report.series}: price {report.lead_minutes} min before close\n"
        f"Brier {_fmt(report.brier)} on {report.n_used} contracts"
    )
    axis.legend(loc="upper left", frameon=False)
    axis.set_aspect("equal", adjustable="box")
    figure.tight_layout()
    figure.savefig(path, dpi=120)
    plt.close(figure)


def _bucket_label(bucket: Bucket) -> str:
    return f"{int(round(bucket.lo * 100))}-{int(round(bucket.hi * 100))}%"


def _pct(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value * 100:.1f}%"


def _signed_pp(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value * 100:+.1f}"


def _signed_pct(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value * 100:+.1f}%"


def _fmt(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.4f}"
