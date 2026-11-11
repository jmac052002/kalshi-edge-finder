"""Daily high-temperature brackets from an NWS forecast.

Kalshi's daily high contracts (series such as ``KXHIGHNY``) settle on the
maximum temperature The Weather Company reports for a CLI station named in
the market rules, for example ``New York City (CLINYC)``. The station id
the National Weather Service uses for that same report is ``K`` plus the
last three letters of the CLI code (``CLINYC`` -> ``KNYC``, Central Park).
That mapping was checked against ``api.weather.gov`` for every open daily
high series when this module was written.

The forecast is the highest hourly temperature the NWS grid still shows for
the market's local calendar date, and only when an afternoon hour of that
date is still in the forecast. Once the afternoon has dropped out of the
forecast, the day's high is no longer a forecast and this module refuses
to invent one.

The uncertainty is an assumption. ``api.weather.gov`` serves the current
forecast only, so there is no archive here to fit a lead-time error to, and
this repository does not pretend otherwise. A normal distribution is centered
on the forecast high. Its standard deviation comes from a lead-time table of
mean absolute errors in the neighborhood of published NWS max-temperature
errors (about 2 to 3 F a day ahead, larger after that), converted with the
normal-distribution identity ``sd = MAE * sqrt(pi / 2)``, then combined in
quadrature with half a degree for the gap between an NWS grid point and the
Weather Company CLI print.

Settlement is a whole number of degrees. A "65 to 66" contract is the
integers 65 and 66, which is a latent temperature in ``[64.5, 66.5)``.
"Greater than 66" is 67 or above, a latent temperature above 66.5.
"""

from __future__ import annotations

import math
import re
from datetime import date

# (maximum lead hours, assumed MAE in degrees Fahrenheit). The last row is
# the open-ended bucket. These are round figures, not a regression.
ASSUMED_MAX_TEMP_MAE_F: tuple[tuple[float, float], ...] = (
    (12.0, 2.0),
    (24.0, 2.5),
    (48.0, 3.2),
    (72.0, 3.8),
    (96.0, 4.4),
    (math.inf, 5.0),
)

# Weather Company CLI versus the NWS hourly grid, in degrees Fahrenheit.
# Added in quadrature so it does not dominate a short-lead forecast and
# does not disappear at long lead.
REPORTING_SD_F = 0.5

_CLI_CODE = re.compile(r"\((CLI[A-Z0-9]{3,})\)")
_PLACE = re.compile(r"recorded at ([^(]+?)\s*\(")
_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
_EVENT_DAY = re.compile(
    r"(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})$"
)


def normal_cdf(z: float) -> float:
    """Standard normal CDF. ``math.erf`` keeps this off any extra dependency."""
    if math.isnan(z):
        return math.nan
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def assumed_sigma_f(lead_hours: float) -> float:
    """Assumed standard deviation of the high, in degrees Fahrenheit.

    ``lead_hours`` is how far ahead the forecast hour of the high is.
    Negative lead is rejected: a high that has already dropped out of the
    forecast is not given a probability.
    """
    if lead_hours < 0 or math.isnan(lead_hours):
        raise ValueError(f"lead_hours must be >= 0, got {lead_hours!r}")
    mae = ASSUMED_MAX_TEMP_MAE_F[-1][1]
    for limit, value in ASSUMED_MAX_TEMP_MAE_F:
        if lead_hours <= limit:
            mae = value
            break
    forecast_sd = mae * math.sqrt(math.pi / 2.0)
    return math.sqrt(forecast_sd * forecast_sd + REPORTING_SD_F * REPORTING_SD_F)


def _whole_degree(value: float | None) -> float | None:
    """The strike as a whole degree, or None when it is missing or fractional."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if abs(number - round(number)) > 1e-6:
        return None
    return float(round(number))


def _clamp(probability: float) -> float:
    if probability < 0.0:
        return 0.0
    if probability > 1.0:
        return 1.0
    return probability


def temperature_yes_probability(
    mean_f: float,
    sigma_f: float,
    *,
    strike_type: str | None,
    floor: float | None,
    cap: float | None,
) -> float | None:
    """Probability the integer reported high lands in this contract.

    Returns None for a strike type or a fractional strike this model does
    not describe, so the caller can leave the contract unpriced instead of
    guessing. The brackets of one event are a partition of the integers and
    sum to 1 when every contract is one of ``less``, ``between``, ``greater``
    on whole degrees.
    """
    if not math.isfinite(mean_f) or not math.isfinite(sigma_f) or sigma_f <= 0:
        raise ValueError("mean and sigma must be finite and sigma > 0")

    def cdf(bound: float) -> float:
        return normal_cdf((bound - mean_f) / sigma_f)

    kind = (strike_type or "").lower()
    low = _whole_degree(floor)
    high = _whole_degree(cap)
    if kind == "less":
        if high is None:
            return None
        # "less than 59" is 58 or below: latent temperature below 58.5.
        return _clamp(cdf(high - 0.5))
    if kind == "greater":
        if low is None:
            return None
        # "greater than 66" is 67 or above: latent temperature above 66.5.
        return _clamp(1.0 - cdf(low + 0.5))
    if kind == "between":
        if low is None or high is None or high < low:
            return None
        return _clamp(cdf(high + 0.5) - cdf(low - 0.5))
    return None


def cli_code(rules_primary: str | None) -> str | None:
    """The ``CLI...`` station code in parentheses in the market rules."""
    if not rules_primary:
        return None
    match = _CLI_CODE.search(rules_primary)
    if match is None:
        return None
    return match.group(1)


def nws_station_id(rules_primary: str | None) -> str | None:
    """NWS station id for the CLI code in the rules, or None if it is absent."""
    code = cli_code(rules_primary)
    if code is None:
        return None
    return "K" + code[-3:]


def settlement_place(rules_primary: str | None) -> str | None:
    """The place named just before the CLI code, such as ``New York City``."""
    if not rules_primary:
        return None
    match = _PLACE.search(rules_primary)
    if match is None:
        return None
    place = match.group(1).strip()
    return place or None


def event_date(event_ticker: str | None) -> date | None:
    """Calendar date encoded at the end of a daily event ticker.

    ``KXHIGHNY-26NOV11`` is 11 November 2026. A ticker with no day
    (``KXCPI-26NOV``) does not match; that is a month, not a weather date.
    """
    if not event_ticker:
        return None
    match = _EVENT_DAY.search(event_ticker)
    if match is None:
        return None
    year = 2000 + int(match.group(1))
    month = _MONTHS[match.group(2)]
    day = int(match.group(3))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def is_daily_high_series(entry: dict) -> bool:
    """True for the daily city-high series the catalog actually labels as such.

    Title text is not used. ``KXHIGHTSDF`` is titled like San Antonio and the
    rules settle Louisville; the category, the frequency, the tag, and the
    ``KXHIGH`` prefix are what the exchange itself files these under.
    """
    if not isinstance(entry, dict):
        return False
    ticker = entry.get("ticker") or ""
    tags = entry.get("tags") or []
    return (
        entry.get("category") == "Climate and Weather"
        and entry.get("frequency") == "daily"
        and isinstance(tags, list)
        and "Daily temperature" in tags
        and isinstance(ticker, str)
        and ticker.startswith("KXHIGH")
    )
