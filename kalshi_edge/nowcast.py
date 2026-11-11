"""Cleveland Fed inflation nowcast, and the error in that same file.

The Fed publishes the daily nowcast as FusionCharts JSON:

* month-over-month: ``nowcast_month.json``
* year-over-year: ``nowcast_year.json``

Each file is a list of charts, one per reference month. A finished month
has an "Actual CPI Inflation" point. The current month has a nowcast and
no actual. That is the whole public source this module will use. It does
not invent a consensus, and it does not turn the point nowcast into a
certainty.

The probability model is a normal distribution centered on the latest
nowcast. Its standard deviation is estimated from the same file: for every
finished month, the actual minus the first nowcast of that month, and the
actual minus the last nowcast before the actual printed. A nowcast early
in the month uses the early-month error; one that has been updated all the
way to the release uses the final error; in between, the two variances are
blended by how far along the current path is compared with a typical
finished month. The historical mean error is not subtracted. Removing it
would be a second model, and the file's mean is close to zero anyway; the
report says the mean was left in.

Kalshi's CPI contracts settle on the one-decimal published print ("more
than 0.2%" is a print of 0.3 or higher, not a print of 0.2). The probability
is the normal probability that the unrounded change rounds to such a print.
The boundary has probability zero, so half-up versus half-even does not matter.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from kalshi_edge.weather import normal_cdf

NOWCAST_MONTH_PATH = "/-/media/files/webcharts/inflationnowcasting/nowcast_month.json"
NOWCAST_YEAR_PATH = "/-/media/files/webcharts/inflationnowcasting/nowcast_year.json"
CLEVELAND_FED_BASE = "https://www.clevelandfed.org"
CLEVELAND_FED_USER_AGENT = (
    "kalshi-edge-finder/0.1 (inflation nowcast research; "
    "https://github.com/jmac052002/kalshi-edge-finder)"
)

# A nowcast file with fewer finished months than this is not used as a
# probability. The live file has well over a hundred; a short file is a
# shape change, and the honest response is to show the price only.
MIN_FINISHED_MONTHS = 24

# Floor so a parsing accident cannot collapse the distribution to a spike.
MIN_SIGMA = 0.05

_CPI_SERIES = {
    "cpi_mom": ("month", "CPI Inflation", "Actual CPI Inflation"),
    "core_cpi_mom": ("month", "Core CPI Inflation", "Actual Core CPI Inflation"),
    "cpi_yoy": ("year", "CPI Inflation", "Actual CPI Inflation"),
    "core_cpi_yoy": ("year", "Core CPI Inflation", "Actual Core CPI Inflation"),
}


@dataclass(frozen=True)
class NowcastModel:
    """One usable nowcast and the error model that goes with it."""

    kind: str
    series_name: str
    reference_month: str
    as_of_label: str
    value: float
    sigma: float
    early_sigma: float
    final_sigma: float
    n_months: int
    n_points: int
    mean_error: float

    @property
    def source(self) -> str:
        period = "month-over-month" if self.kind.endswith("mom") else "year-over-year"
        return f"Cleveland Fed {period} nowcast ({self.series_name})"


def _numeric(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number):
        return None
    return number


def _series_points(chart: dict, name: str) -> list[float | None]:
    for dataset in chart.get("dataset") or []:
        if isinstance(dataset, dict) and dataset.get("seriesname") == name:
            data = dataset.get("data") or []
            if not isinstance(data, list):
                return []
            return [_numeric(point.get("value")) if isinstance(point, dict) else None for point in data]
    return []


def _labels(chart: dict) -> list[str]:
    categories = chart.get("categories") or []
    if not categories or not isinstance(categories[0], dict):
        return []
    rows = categories[0].get("category") or []
    if not isinstance(rows, list):
        return []
    labels: list[str] = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("label"), str):
            labels.append(row["label"])
        else:
            labels.append("")
    return labels


def _subcaption(chart: dict) -> str:
    header = chart.get("chart")
    if isinstance(header, dict) and isinstance(header.get("subcaption"), str):
        return header["subcaption"].strip()
    return ""


def _month_key(subcaption: str) -> tuple[int, int] | None:
    parts = subcaption.split("-")
    if len(parts) != 2:
        return None
    try:
        year = int(parts[0])
        month = int(parts[1])
    except ValueError:
        return None
    if not 1990 <= year <= 2100 or not 1 <= month <= 12:
        return None
    return year, month


def blend_sigma(n_points: int, typical_points: float, early_sigma: float, final_sigma: float) -> float:
    """Blend early-month and final-nowcast error by how long the path is.

    ``n_points == 1`` is the early distribution. A path as long as a typical
    finished month is the final distribution. The blend is on the variances.
    """
    if typical_points <= 1:
        blended = early_sigma
    else:
        fraction = (n_points - 1) / (typical_points - 1)
        fraction = min(1.0, max(0.0, fraction))
        blended = math.sqrt((1.0 - fraction) * early_sigma**2 + fraction * final_sigma**2)
    if blended < MIN_SIGMA:
        return MIN_SIGMA
    return blended


def parse_nowcast_file(
    charts: object,
    *,
    kind: str,
    min_months: int = MIN_FINISHED_MONTHS,
) -> NowcastModel | None:
    """Build the model for ``kind`` from one nowcast file, or None.

    None means the file does not support a probability: too few finished
    months, no current nowcast, or a shape this parser does not recognize.
    Callers then show the Kalshi price and the settlement date and say why.
    """
    if kind not in _CPI_SERIES:
        raise ValueError(f"unknown nowcast kind {kind!r}")
    if not isinstance(charts, list):
        return None
    _period, now_name, actual_name = _CPI_SERIES[kind]
    finished: list[tuple[float, float, int]] = []
    current: tuple[tuple[int, int], float, str, int] | None = None
    for chart in charts:
        if not isinstance(chart, dict):
            continue
        key = _month_key(_subcaption(chart))
        if key is None:
            continue
        now_points = _series_points(chart, now_name)
        actual_points = _series_points(chart, actual_name)
        labels = _labels(chart)
        now_indexed = [(index, value) for index, value in enumerate(now_points) if value is not None]
        actual_values = [value for value in actual_points if value is not None]
        if not now_indexed:
            continue
        if actual_values:
            actual = actual_values[-1]
            first = now_indexed[0][1]
            last = now_indexed[-1][1]
            finished.append((actual - first, actual - last, len(now_indexed)))
            continue
        label = ""
        last_index = now_indexed[-1][0]
        if last_index < len(labels):
            label = labels[last_index]
        candidate = (key, now_indexed[-1][1], label, len(now_indexed))
        if current is None or candidate[0] >= current[0]:
            current = candidate
    if current is None or len(finished) < min_months:
        return None
    early_errors = [row[0] for row in finished]
    final_errors = [row[1] for row in finished]
    early_sigma = statistics.stdev(early_errors)
    final_sigma = statistics.stdev(final_errors)
    typical = statistics.median(row[2] for row in finished)
    _key, value, label, n_points = current
    sigma = blend_sigma(n_points, float(typical), early_sigma, final_sigma)
    year, month = _key
    # The mean left in the model is the early-month mean when the blend is
    # closer to that distribution, and the final mean otherwise. It is
    # reported, not removed.
    mean_error = statistics.fmean(early_errors if n_points <= typical / 2 else final_errors)
    return NowcastModel(
        kind=kind,
        series_name=_CPI_SERIES[kind][1],
        reference_month=f"{year:04d}-{month:02d}",
        as_of_label=label,
        value=value,
        sigma=sigma,
        early_sigma=early_sigma,
        final_sigma=final_sigma,
        n_months=len(finished),
        n_points=n_points,
        mean_error=mean_error,
    )


def probability_print_exceeds(mean: float, sigma: float, threshold: float, *, places: int = 1) -> float:
    """Probability a rounded print is strictly greater than ``threshold``.

    Rounding to ``places`` decimals maps a print of ``threshold + one tick``
    to unrounded values at or above ``threshold + half a tick``. For the
    one-decimal CPI print, "more than 0.2" is a print of at least 0.3, which
    is an unrounded change at or above 0.25.
    """
    if places < 0:
        raise ValueError("places must be >= 0")
    if not math.isfinite(mean) or not math.isfinite(sigma) or sigma <= 0:
        raise ValueError("mean and sigma must be finite and sigma > 0")
    if not math.isfinite(threshold):
        raise ValueError("threshold must be finite")
    half_tick = 0.5 * (10 ** (-places))
    cutoff = threshold + half_tick
    probability = 1.0 - normal_cdf((cutoff - mean) / sigma)
    if probability < 0.0:
        return 0.0
    if probability > 1.0:
        return 1.0
    return probability
