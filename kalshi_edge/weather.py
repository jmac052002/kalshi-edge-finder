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

The live scan centers that distribution on the NWS hourly high and takes
the bias and the width from ``weather_calibration.json`` when that file is
present (see ``kalshi_edge.weather_error``). The table below is only the
fallback for a checkout with no fit. It is a lead-time table of mean
absolute errors in the neighborhood of published NWS max-temperature errors
(about 2 to 3 F a day ahead, larger after that), converted with the
normal-distribution identity ``sd = MAE * sqrt(pi / 2)``, then combined in
quadrature with half a degree for the gap between an NWS grid point and the
Weather Company CLI print. ``api.weather.gov`` itself keeps no forecast
archive, so that fallback is still an assumption and the scan does not rank
it as an edge.

Settlement is a whole number of degrees. A "65 to 66" contract is the
integers 65 and 66, which is a latent temperature in ``[64.5, 66.5)``.
"Greater than 66" is 67 or above, a latent temperature above 66.5.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
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
# The place is the words after "recorded at/in" and before either a CLI code
# or the date. Some rules put the date before the parenthesis:
# "recorded at Atlanta for Aug 13, 2026, is greater than 99° ... Report (Daily)".
_PLACE_CLAUSE = re.compile(
    r"recorded (?:at|in)\s+(.+?)(?:\s*\(\s*CLI|\s+for\s+)",
    re.IGNORECASE,
)
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


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Numerical Recipes)."""
    max_iter = 200
    eps = 3e-14
    tiny = 1e-30
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def regularized_incomplete_beta(x: float, a: float, b: float) -> float:
    """``I_x(a, b)``. Both shape parameters must be positive."""
    if a <= 0 or b <= 0 or math.isnan(x):
        return math.nan
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_bt = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log(1.0 - x)
    )
    bt = math.exp(log_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def student_t_cdf(z: float, df: float) -> float:
    """CDF of Student's t with ``df`` degrees of freedom.

    ``df`` is the tail weight, not a temperature. The scale is 1: callers
    standardize ``(x - location) / scale`` first. A normal is the limit of
    large ``df``; this is here so a fitted daily-high error can have heavier
    tails than a normal without adding a statistics dependency.
    """
    if math.isnan(z) or math.isnan(df) or df <= 0:
        return math.nan
    x = df / (df + z * z)
    tail = 0.5 * regularized_incomplete_beta(x, 0.5 * df, 0.5)
    if z >= 0:
        return 1.0 - tail
    return tail


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
    df: float | None = None,
) -> float | None:
    """Probability the integer reported high lands in this contract.

    ``sigma_f`` is the scale of the latent temperature: the standard
    deviation when ``df`` is None, and the Student-t scale otherwise.
    Returns None for a strike type or a fractional strike this model does
    not describe, so the caller can leave the contract unpriced instead of
    guessing. The brackets of one event are a partition of the integers and
    sum to 1 when every contract is one of ``less``, ``between``, ``greater``
    on whole degrees.
    """
    if not math.isfinite(mean_f) or not math.isfinite(sigma_f) or sigma_f <= 0:
        raise ValueError("mean and sigma must be finite and sigma > 0")
    if df is not None and (not math.isfinite(df) or df <= 0):
        raise ValueError("df must be positive when a t distribution is used")

    def cdf(bound: float) -> float:
        z = (bound - mean_f) / sigma_f
        if df is None:
            return normal_cdf(z)
        return student_t_cdf(z, df)

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


def place_mentioned(rules_primary: str | None, place: str | None) -> bool:
    """Whether the rules text names ``place``.

    Older archived markets describe the same station in a sentence ("Central
    Park, New York") and do not carry a ``(CLINYC)`` code. The longest word
    of the place the modern rules name ("York" in "New York City") is enough
    to tell those apart from a series that later moved to a different city.
    """
    if not rules_primary or not place:
        return False
    text = rules_primary.lower()
    if place.lower() in text:
        return True
    words = [word for word in re.split(r"[^a-z0-9]+", place.lower()) if len(word) >= 4]
    if not words:
        return False
    return max(words, key=len) in text


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
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


def settled_high(markets: Iterable[Mapping]) -> tuple[float, bool] | None:
    """The reported daily high, and whether that number is the exact print.

    ``expiration_value`` is the print when Kalshi sends one: a whole number
    of degrees, repeated on every bracket of the event. Older archived
    markets leave it blank. The high is then known only up to the winning
    bracket. "64 to 65" YES is 64 or 65, and this returns their midpoint
    with the exact flag false. A winning tail ("63 or below", "72 or above")
    does not name a temperature, so those events return None.
    """
    rows = [market for market in markets if isinstance(market, Mapping)]
    prints: list[float] = []
    for market in rows:
        number = _finite_float(market.get("expiration_value"))
        if number is None or not -80.0 <= number <= 150.0:
            continue
        prints.append(number)
    if prints:
        if max(prints) - min(prints) > 0.2:
            return None
        return sum(prints) / len(prints), True
    winners = [market for market in rows if str(market.get("result") or "").lower() == "yes"]
    if len(winners) != 1:
        return None
    winner = winners[0]
    if str(winner.get("strike_type") or "").lower() != "between":
        return None
    floor = _whole_degree(_finite_float(winner.get("floor_strike")))
    cap = _whole_degree(_finite_float(winner.get("cap_strike")))
    if floor is None or cap is None or cap < floor:
        return None
    if floor == cap:
        return floor, True
    return (floor + cap) / 2.0, False


def tidy_place(place: str | None) -> str | None:
    """Drop a date clause that the rules glued onto the station name."""
    if not place:
        return None
    place = re.split(r"\s+for\s+", place, maxsplit=1)[0]
    place = re.split(r"\s+as reported\b", place, maxsplit=1, flags=re.IGNORECASE)[0]
    place = place.strip(" ,.")
    return place or None


def settlement_place(rules_primary: str | None) -> str | None:
    """The place the rules name, such as ``New York City`` or ``Louisville``.

    Stops at the CLI code or at ``for <date>``. A rule that continues into
    the strike ("Atlanta for Aug 13, 2026, is greater than 99° ...") would
    otherwise swallow the whole sentence.
    """
    if not rules_primary:
        return None
    match = _PLACE_CLAUSE.search(rules_primary)
    if match is None:
        match = _PLACE.search(rules_primary)
        if match is None:
            return None
    return tidy_place(match.group(1))


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
