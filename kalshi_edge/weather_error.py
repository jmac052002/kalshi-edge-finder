"""Fitted error of a daily-high forecast, and the guard around it.

``weather_calibrate`` writes ``weather_calibration.json`` next to this module.
The scan loads that file and turns an NWS hourly high into a bias and a
scale. The fit is not an NWS error: ``api.weather.gov`` does not keep the
forecast it served yesterday. The substitute is the Open-Meteo archive of
GFS hourly temperatures at a fixed lead (24 hours and 48 hours before each
valid hour). The daily high is the max of those hours on the station's
local date, which is the same reduction the scan applies to the NWS grid.
Bias and scale measured on that GFS high are then applied to the live NWS
high. That transfer is wrong if the NWS high does not share the GFS's bias.
The scan stores each NWS high it uses so a later fit can replace the
transfer with the forecast the scan actually reads.

A city with fewer than a handful of settled days uses the pooled error.
A missing file, or a pooled sample too small to fit, falls back to
``assumed_sigma_f`` and is not ranked as an edge.

Two brakes sit on top of the fee break-even, for weather only. The model
has to clear the break-even by ``extra_margin`` (a few points, so a one-point
gap is not a trade). And a gap between the model and the ask larger than
``disagree_gap`` is flagged for a look instead of ranked. The second brake
is what stops a too-wide distribution from filling the list with NO on the
market's favorite bin.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from kalshi_edge.weather import assumed_sigma_f, tidy_place

REFERENCE_LEAD_HOURS = 24
LONG_LEAD_HOURS = 48
# Leads at or under this use the 24-hour fit. The archived series has no
# lead shorter than 24 hours, and a same-day NWS high is inside that window.
SHORT_LEAD_CUTOFF_HOURS = 36.0
# Past this, the 48-hour fit is widened with the old lead-time table so a
# multi-day NWS grid is not given a two-day width.
LONG_LEAD_CUTOFF_HOURS = 72.0
MIN_POOLED_N = 30
MIN_CITY_N = 8
SHRINK_K = 15.0
MIN_SCALE_F = 0.8
OUTLIER_ABS_F = 25.0
DEFAULT_DISAGREE_GAP = 0.15
DEFAULT_EXTRA_MARGIN = 0.03
# A t model has to beat the normal by this much per observation, and its
# degrees of freedom have to say the tails are actually heavy. A tiny
# in-sample gain is not a reason to complicate the scan.
T_LOGLIK_GAIN = 0.01
T_MAX_DF = 25.0
PACKAGE_FILENAME = "weather_calibration.json"


@dataclass(frozen=True)
class ErrorPair:
    """One city-day: the forecast that was available, and the settled high."""

    station_id: str
    lead_hours: int
    forecast_f: float
    settlement_f: float
    market_date: date | None = None
    place: str | None = None
    event_ticker: str | None = None

    @property
    def error_f(self) -> float:
        """Settlement minus forecast. Positive means the forecast was too cold."""
        return self.settlement_f - self.forecast_f


@dataclass(frozen=True)
class LeadFit:
    """Pooled error at one lead. ``scale_f`` is what the CDF standardizes by."""

    lead_hours: int
    n: int
    bias_f: float
    sd_f: float
    scale_f: float
    mae_f: float


@dataclass(frozen=True)
class CityFit:
    """A city's 24-hour error, shrunk toward the pool. ``n`` is the raw count."""

    n: int
    bias_f: float
    scale_f: float
    sd_f: float
    raw_bias_f: float
    raw_sd_f: float
    place: str | None = None


@dataclass(frozen=True)
class WeatherCalibration:
    """The file the scan reads. ``trusted`` is false when nothing was fit."""

    fitted_at: str
    trusted: bool
    source_label: str
    source_note: str
    distribution: str
    df: float | None
    bias_transfer: float
    shrink_k: float
    reference_lead_hours: int
    disagree_gap: float
    extra_margin: float
    leads: dict[int, LeadFit]
    stations: dict[str, CityFit]
    scores: dict
    rejected_outliers: int = 0

    def scan_note(self) -> str:
        """One line for the scan report."""
        if not self.trusted:
            return (
                "weather probabilities use the assumed lead-time sd; "
                "no settlement fit is loaded, so weather is not ranked as an edge"
            )
        lead = self.leads.get(self.reference_lead_hours)
        if lead is None:
            return "weather fit loaded but the reference lead is missing; weather is not ranked as an edge"
        tail = f", student-t df {self.df:.0f}" if self.df is not None else ""
        return (
            f"weather error fit to {self.source_label}: "
            f"{lead.lead_hours}h pooled bias {lead.bias_f:+.2f}F, sd {lead.sd_f:.2f}F{tail}, "
            f"n={lead.n} city-days. Applied bias is {self.bias_transfer:.0%} of that fit. "
            f"A model-minus-ask gap of {self.disagree_gap:.0%} or more is flagged "
            "verify and not ranked; weather also needs "
            f"{self.extra_margin:.0%} of extra margin over the fee break-even"
        )


@dataclass(frozen=True)
class WeatherPredict:
    """What the scan adds to one NWS hourly high."""

    mean_f: float
    scale_f: float
    sd_f: float
    df: float | None
    bias_f: float
    n: int
    pooled_n: int
    trusted: bool
    lead_hours_used: int
    detail: str


def package_path() -> Path:
    return Path(__file__).with_name(PACKAGE_FILENAME)


def unfitted_calibration() -> WeatherCalibration:
    """The fallback. The scan still prices with ``assumed_sigma_f`` and does not rank it."""
    return WeatherCalibration(
        fitted_at="",
        trusted=False,
        source_label="assumed lead-time table",
        source_note=(
            "No settlement fit is loaded. api.weather.gov has no forecast archive, "
            "and weather_calibration.json is missing or too thin to use."
        ),
        distribution="normal",
        df=None,
        bias_transfer=0.0,
        shrink_k=SHRINK_K,
        reference_lead_hours=REFERENCE_LEAD_HOURS,
        disagree_gap=DEFAULT_DISAGREE_GAP,
        extra_margin=DEFAULT_EXTRA_MARGIN,
        leads={},
        stations={},
        scores={},
    )


def constant_calibration(
    *,
    bias_f: float,
    scale_f: float,
    n: int = 100,
    df: float | None = None,
    extra_margin: float = 0.0,
    disagree_gap: float = 1.0,
    trusted: bool = True,
    source_label: str = "test calibration",
) -> WeatherCalibration:
    """One bias and scale at every lead. Tests pin a probability with this."""
    sd = _predictive_sd(scale_f, df)
    lead = LeadFit(
        lead_hours=REFERENCE_LEAD_HOURS,
        n=n,
        bias_f=bias_f,
        sd_f=sd,
        scale_f=scale_f,
        mae_f=abs(bias_f),
    )
    longer = LeadFit(
        lead_hours=LONG_LEAD_HOURS,
        n=n,
        bias_f=bias_f,
        sd_f=sd,
        scale_f=scale_f,
        mae_f=abs(bias_f),
    )
    return WeatherCalibration(
        fitted_at="test",
        trusted=trusted,
        source_label=source_label,
        source_note="constant calibration for tests",
        distribution="student_t" if df is not None else "normal",
        df=df,
        bias_transfer=1.0,
        shrink_k=SHRINK_K,
        reference_lead_hours=REFERENCE_LEAD_HOURS,
        disagree_gap=disagree_gap,
        extra_margin=extra_margin,
        leads={REFERENCE_LEAD_HOURS: lead, LONG_LEAD_HOURS: longer},
        stations={},
        scores={},
    )


def _predictive_sd(scale_f: float, df: float | None) -> float:
    """Standard deviation of the distribution the CDF uses.

    For a t, the variance is ``scale^2 * df / (df - 2)`` when ``df > 2``.
    Below that the variance is infinite and the scale itself is reported.
    """
    if df is None or df <= 2:
        return scale_f
    return scale_f * math.sqrt(df / (df - 2.0))


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _sample_sd(values: list[float], mean: float) -> float:
    if len(values) < 2:
        return MIN_SCALE_F
    var = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return max(MIN_SCALE_F, math.sqrt(var))


def _shrink(city: float, n: int, pooled: float, k: float) -> float:
    weight = n / (n + k)
    return weight * city + (1.0 - weight) * pooled


def _gaussian_loglik(errors: list[float], bias: float, sd: float) -> float:
    n = len(errors)
    residual = sum((error - bias) ** 2 for error in errors) / (sd * sd)
    return -0.5 * n * math.log(2.0 * math.pi) - n * math.log(sd) - 0.5 * residual


def _t_loglik(errors: list[float], bias: float, scale: float, df: float) -> float:
    n = len(errors)
    constant = (
        math.lgamma((df + 1.0) / 2.0)
        - math.lgamma(df / 2.0)
        - 0.5 * math.log(df * math.pi)
        - math.log(scale)
    )
    total = n * constant
    for error in errors:
        z2 = ((error - bias) / scale) ** 2
        total -= ((df + 1.0) / 2.0) * math.log1p(z2 / df)
    return total


def _mle_t_scale(errors: list[float], bias: float, df: float, sd: float) -> float:
    """Scale MLE at a fixed location and degrees of freedom.

    The score ``(df+1) * sum(z^2 / (df + z^2)) - n`` is positive when the
    scale is too small and negative when it is too large.
    """
    n = len(errors)
    lo = max(MIN_SCALE_F * 0.25, sd * 0.2)
    hi = max(sd * 2.0, lo * 4)

    def score(scale: float) -> float:
        acc = 0.0
        for error in errors:
            z2 = ((error - bias) / scale) ** 2
            acc += z2 / (df + z2)
        return acc * (df + 1.0) - n

    if score(hi) > 0:
        return hi
    if score(lo) < 0:
        return lo
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if score(mid) > 0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _select_distribution(errors: list[float], bias: float, sd: float) -> tuple[float | None, float]:
    """Return ``(df, scale)``. ``df`` is None when the normal is kept.

    The normal's scale is its standard deviation. A t is kept only when the
    tails are heavy and the likelihood gain is large enough to matter.
    """
    if len(errors) < MIN_POOLED_N:
        return None, sd
    z4 = sum(((error - bias) / sd) ** 4 for error in errors) / len(errors)
    excess = z4 - 3.0
    if excess < 0.5:
        return None, sd
    gauss = _gaussian_loglik(errors, bias, sd)
    best_df: float | None = None
    best_scale = sd
    best_ll = gauss
    df = 4.0
    while df <= 40.0:
        scale = _mle_t_scale(errors, bias, df, sd)
        ll = _t_loglik(errors, bias, scale, df)
        if ll > best_ll:
            best_ll = ll
            best_df = df
            best_scale = scale
        df += 1.0
    if best_df is None or best_df > T_MAX_DF:
        return None, sd
    if (best_ll - gauss) / len(errors) < T_LOGLIK_GAIN:
        return None, sd
    return best_df, best_scale


def _lead_fit(pairs: list[ErrorPair], lead: int, df: float | None, scale_override: float | None) -> LeadFit | None:
    errors = [pair.error_f for pair in pairs]
    if len(errors) < 2:
        return None
    bias = _mean(errors)
    sd = _sample_sd(errors, bias)
    scale = scale_override if scale_override is not None else sd
    if df is not None and scale_override is None:
        scale = _mle_t_scale(errors, bias, df, sd)
    scale = max(MIN_SCALE_F, scale)
    mae = _mean([abs(error - bias) for error in errors])
    return LeadFit(
        lead_hours=lead,
        n=len(errors),
        bias_f=bias,
        sd_f=_predictive_sd(scale, df),
        scale_f=scale,
        mae_f=mae,
    )


def fit_error_model(
    pairs: list[ErrorPair],
    *,
    fitted_at: str,
    source_label: str,
    source_note: str,
    min_pooled_n: int = MIN_POOLED_N,
    min_city_n: int = MIN_CITY_N,
    shrink_k: float = SHRINK_K,
    bias_transfer: float = 1.0,
    disagree_gap: float = DEFAULT_DISAGREE_GAP,
    extra_margin: float = DEFAULT_EXTRA_MARGIN,
    scores: dict | None = None,
) -> WeatherCalibration:
    """Bias and scale by lead, with per-city shrinkage at the 24-hour lead.

    Pairs more than ``OUTLIER_ABS_F`` from the forecast are dropped. A bust
    that large is much more often a bad join than a real forecast, and one
    of them moves a city's bias.
    """
    kept = [pair for pair in pairs if abs(pair.error_f) <= OUTLIER_ABS_F]
    rejected = len(pairs) - len(kept)
    by_lead: dict[int, list[ErrorPair]] = {REFERENCE_LEAD_HOURS: [], LONG_LEAD_HOURS: []}
    for pair in kept:
        if pair.lead_hours in by_lead:
            by_lead[pair.lead_hours].append(pair)
    reference = by_lead[REFERENCE_LEAD_HOURS]
    if len(reference) < min_pooled_n:
        empty = unfitted_calibration()
        return WeatherCalibration(
            fitted_at=fitted_at,
            trusted=False,
            source_label=source_label,
            source_note=source_note + " The pooled 24-hour sample was too small to fit.",
            distribution=empty.distribution,
            df=None,
            bias_transfer=bias_transfer,
            shrink_k=shrink_k,
            reference_lead_hours=REFERENCE_LEAD_HOURS,
            disagree_gap=disagree_gap,
            extra_margin=extra_margin,
            leads={},
            stations={},
            scores=scores or {},
            rejected_outliers=rejected,
        )
    ref_errors = [pair.error_f for pair in reference]
    ref_bias = _mean(ref_errors)
    ref_sd = _sample_sd(ref_errors, ref_bias)
    df, ref_scale = _select_distribution(ref_errors, ref_bias, ref_sd)
    leads: dict[int, LeadFit] = {}
    fitted_24 = _lead_fit(reference, REFERENCE_LEAD_HOURS, df, ref_scale)
    if fitted_24 is None:
        return unfitted_calibration()
    leads[REFERENCE_LEAD_HOURS] = fitted_24
    longer_pairs = by_lead[LONG_LEAD_HOURS]
    if len(longer_pairs) >= min_pooled_n:
        longer = _lead_fit(longer_pairs, LONG_LEAD_HOURS, df, None)
        if longer is not None:
            leads[LONG_LEAD_HOURS] = longer
    stations: dict[str, CityFit] = {}
    grouped: dict[str, list[ErrorPair]] = {}
    for pair in reference:
        grouped.setdefault(pair.station_id, []).append(pair)
    for station, city_pairs in grouped.items():
        if len(city_pairs) < min_city_n:
            continue
        errors = [pair.error_f for pair in city_pairs]
        raw_bias = _mean(errors)
        raw_sd = _sample_sd(errors, raw_bias)
        bias = _shrink(raw_bias, len(errors), fitted_24.bias_f, shrink_k)
        variance = _shrink(raw_sd * raw_sd, len(errors), fitted_24.sd_f**2, shrink_k)
        sd = max(MIN_SCALE_F, math.sqrt(max(variance, MIN_SCALE_F**2)))
        scale = sd if df is None or df <= 2 else sd / math.sqrt(df / (df - 2.0))
        scale = max(MIN_SCALE_F, scale)
        places = [tidy_place(pair.place) for pair in city_pairs]
        places = [place for place in places if place]
        stations[station] = CityFit(
            n=len(errors),
            bias_f=bias,
            scale_f=scale,
            sd_f=_predictive_sd(scale, df),
            raw_bias_f=raw_bias,
            raw_sd_f=raw_sd,
            place=max(places, key=len) if places else None,
        )
    return WeatherCalibration(
        fitted_at=fitted_at,
        trusted=True,
        source_label=source_label,
        source_note=source_note,
        distribution="student_t" if df is not None else "normal",
        df=df,
        bias_transfer=bias_transfer,
        shrink_k=shrink_k,
        reference_lead_hours=REFERENCE_LEAD_HOURS,
        disagree_gap=disagree_gap,
        extra_margin=extra_margin,
        leads=leads,
        stations=stations,
        scores=scores or {},
        rejected_outliers=rejected,
    )


def _applied_bias(bias_f: float, transfer: float) -> float:
    return bias_f * transfer


def predict_high(
    calibration: WeatherCalibration,
    station_id: str | None,
    lead_hours: float,
    forecast_f: float,
) -> WeatherPredict:
    """Location, scale, and whether the scan may rank an edge from it.

    The 24-hour fit covers leads up to 36 hours, which is the same-day NWS
    high the daily scan usually sees. The archive has no shorter lead, so
    those hours are not given a tighter width than the 24-hour error. Leads
    past 72 hours keep the 48-hour fit and then widen with the old table.
    """
    if not math.isfinite(lead_hours) or lead_hours < 0:
        raise ValueError(f"lead_hours must be >= 0, got {lead_hours!r}")
    if not calibration.trusted or REFERENCE_LEAD_HOURS not in calibration.leads:
        sigma = assumed_sigma_f(lead_hours)
        return WeatherPredict(
            mean_f=forecast_f,
            scale_f=sigma,
            sd_f=sigma,
            df=None,
            bias_f=0.0,
            n=0,
            pooled_n=0,
            trusted=False,
            lead_hours_used=0,
            detail="assumed lead-time sd, no settlement fit",
        )
    short = calibration.leads[REFERENCE_LEAD_HOURS]
    long = calibration.leads.get(LONG_LEAD_HOURS)
    city = calibration.stations.get(station_id or "")
    if lead_hours <= SHORT_LEAD_CUTOFF_HOURS or long is None:
        base = short
        bias = city.bias_f if city is not None else short.bias_f
        scale = city.scale_f if city is not None else short.scale_f
        widen = 1.0
        if long is None and lead_hours > SHORT_LEAD_CUTOFF_HOURS:
            widen = assumed_sigma_f(lead_hours) / assumed_sigma_f(float(short.lead_hours))
        used = short.lead_hours
    else:
        base = long
        city_bias_gap = (city.bias_f - short.bias_f) if city is not None else 0.0
        city_scale_ratio = (city.scale_f / short.scale_f) if city is not None and short.scale_f else 1.0
        bias = long.bias_f + city_bias_gap
        scale = long.scale_f * city_scale_ratio
        widen = 1.0
        if lead_hours > LONG_LEAD_CUTOFF_HOURS:
            widen = assumed_sigma_f(lead_hours) / assumed_sigma_f(float(long.lead_hours))
        used = long.lead_hours
    scale = max(MIN_SCALE_F, scale * widen)
    applied = _applied_bias(bias, calibration.bias_transfer)
    n = city.n if city is not None and used == short.lead_hours else base.n
    pooled_n = base.n
    who = station_id if city is not None else "pooled"
    detail = (
        f"fit n={n} ({who}) / {pooled_n} pooled at {used}h, {calibration.source_label}"
    )
    return WeatherPredict(
        mean_f=forecast_f + applied,
        scale_f=scale,
        sd_f=_predictive_sd(scale, calibration.df),
        df=calibration.df,
        bias_f=applied,
        n=n,
        pooled_n=pooled_n,
        trusted=True,
        lead_hours_used=used,
        detail=detail,
    )


def judge_weather_edge(
    *,
    probability: float | None,
    ask: float | None,
    break_even: float | None,
    trusted: bool,
    extra_margin: float,
    disagree_gap: float,
) -> tuple[bool, bool, str]:
    """``(clears, verify, note)`` for one weather side.

    ``clears`` is the fee break-even plus the extra margin. ``verify`` is a
    gap too large to rank: the side is shown and is not an edge. An unfitted
    model never clears.
    """
    if not trusted:
        return False, True, ""
    if probability is None or ask is None or break_even is None:
        return False, False, ""
    if not math.isfinite(probability) or not math.isfinite(ask) or not math.isfinite(break_even):
        return False, False, ""
    if probability - ask >= disagree_gap:
        return False, True, "model disagrees strongly, verify"
    return probability > break_even + extra_margin, False, ""


def brier_score(pairs: list[tuple[float, float]]) -> float | None:
    """Mean squared error of probabilities against 0/1 outcomes. Lower is better."""
    if not pairs:
        return None
    return sum((probability - outcome) ** 2 for probability, outcome in pairs) / len(pairs)


def log_loss(pairs: list[tuple[float, float]]) -> float | None:
    """Mean negative log probability of the outcome. Probabilities are clipped."""
    if not pairs:
        return None
    total = 0.0
    for probability, outcome in pairs:
        p = min(1.0 - 1e-6, max(1e-6, probability))
        total += -(outcome * math.log(p) + (1.0 - outcome) * math.log(1.0 - p))
    return total / len(pairs)


def quantile(values: list[float], q: float) -> float | None:
    """Nearest-rank quantile. ``q`` is between 0 and 1."""
    if not values:
        return None
    ordered = sorted(values)
    index = round(q * (len(ordered) - 1))
    index = min(len(ordered) - 1, max(0, index))
    return ordered[index]


def crps_normal(mean_f: float, sd_f: float, observation_f: float) -> float:
    """Closed-form CRPS of a normal prediction, in degrees. Lower is better."""
    if sd_f <= 0:
        return abs(observation_f - mean_f)
    z = (observation_f - mean_f) / sd_f
    phi = math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
    return sd_f * (z * (2.0 * normal_cdf_local(z) - 1.0) + 2.0 * phi - 1.0 / math.sqrt(math.pi))


def normal_cdf_local(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def calibration_to_json(calibration: WeatherCalibration) -> str:
    leads = {
        str(lead.lead_hours): {
            "n": lead.n,
            "bias_f": lead.bias_f,
            "sd_f": lead.sd_f,
            "scale_f": lead.scale_f,
            "mae_f": lead.mae_f,
        }
        for lead in calibration.leads.values()
    }
    stations = {
        station: {
            "n": city.n,
            "bias_f": city.bias_f,
            "scale_f": city.scale_f,
            "sd_f": city.sd_f,
            "raw_bias_f": city.raw_bias_f,
            "raw_sd_f": city.raw_sd_f,
            "place": city.place,
        }
        for station, city in sorted(calibration.stations.items())
    }
    payload = {
        "version": 1,
        "fitted_at": calibration.fitted_at,
        "trusted": calibration.trusted,
        "source_label": calibration.source_label,
        "source_note": calibration.source_note,
        "distribution": calibration.distribution,
        "df": calibration.df,
        "bias_transfer": calibration.bias_transfer,
        "shrink_k": calibration.shrink_k,
        "reference_lead_hours": calibration.reference_lead_hours,
        "disagree_gap": calibration.disagree_gap,
        "extra_margin": calibration.extra_margin,
        "rejected_outliers": calibration.rejected_outliers,
        "leads": leads,
        "stations": stations,
        "scores": calibration.scores,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def calibration_from_json(text: str) -> WeatherCalibration:
    """Read a calibration file. A file that is not a fit comes back unfitted."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return unfitted_calibration()
    if not isinstance(payload, dict):
        return unfitted_calibration()
    try:
        return _calibration_from_payload(payload)
    except (TypeError, ValueError, KeyError):
        return unfitted_calibration()


def _calibration_from_payload(payload: dict) -> WeatherCalibration:
    leads: dict[int, LeadFit] = {}
    raw_leads = payload.get("leads") or {}
    if not isinstance(raw_leads, dict):
        raise TypeError("leads")
    for key, row in raw_leads.items():
        if not isinstance(row, dict):
            raise TypeError("lead")
        lead_hours = int(key)
        leads[lead_hours] = LeadFit(
            lead_hours=lead_hours,
            n=int(row["n"]),
            bias_f=float(row["bias_f"]),
            sd_f=float(row["sd_f"]),
            scale_f=float(row["scale_f"]),
            mae_f=float(row["mae_f"]),
        )
    stations: dict[str, CityFit] = {}
    raw_stations = payload.get("stations") or {}
    if not isinstance(raw_stations, dict):
        raise TypeError("stations")
    for station, row in raw_stations.items():
        if not isinstance(row, dict):
            raise TypeError("station")
        place = row.get("place")
        stations[str(station)] = CityFit(
            n=int(row["n"]),
            bias_f=float(row["bias_f"]),
            scale_f=float(row["scale_f"]),
            sd_f=float(row["sd_f"]),
            raw_bias_f=float(row["raw_bias_f"]),
            raw_sd_f=float(row["raw_sd_f"]),
            place=place if isinstance(place, str) else None,
        )
    df = payload.get("df")
    scores = payload.get("scores") if isinstance(payload.get("scores"), dict) else {}
    trusted = bool(payload.get("trusted")) and REFERENCE_LEAD_HOURS in leads
    return WeatherCalibration(
        fitted_at=str(payload.get("fitted_at") or ""),
        trusted=trusted,
        source_label=str(payload.get("source_label") or ""),
        source_note=str(payload.get("source_note") or ""),
        distribution=str(payload.get("distribution") or "normal"),
        df=None if df is None else float(df),
        bias_transfer=float(payload.get("bias_transfer", 1.0)),
        shrink_k=float(payload.get("shrink_k", SHRINK_K)),
        reference_lead_hours=int(payload.get("reference_lead_hours", REFERENCE_LEAD_HOURS)),
        disagree_gap=float(payload.get("disagree_gap", DEFAULT_DISAGREE_GAP)),
        extra_margin=float(payload.get("extra_margin", DEFAULT_EXTRA_MARGIN)),
        leads=leads,
        stations=stations,
        scores=scores,
        rejected_outliers=int(payload.get("rejected_outliers") or 0),
    )


def load_calibration(path: Path | None = None) -> WeatherCalibration:
    """The packaged fit, or the unfitted fallback when the file is absent."""
    file = path if path is not None else package_path()
    if not file.is_file():
        return unfitted_calibration()
    return calibration_from_json(file.read_text(encoding="utf-8"))


def save_calibration(calibration: WeatherCalibration, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(calibration_to_json(calibration), encoding="utf-8")


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
