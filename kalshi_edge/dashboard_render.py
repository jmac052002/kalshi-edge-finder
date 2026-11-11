"""Turn the reports and the database summary into one HTML page.

Everything here is a pure function from data to a string, so the tests can
build a page without a file system or a clock.

The security rule for this module is short: **every value that came from a
report file or the database goes through ``esc`` before it is written into
the page.** Market titles, tickers, team names, and notes all start life in
somebody else's API response. A title containing ``<script>`` must show up on
screen as those eight characters, not run. Numbers are parsed to ``float``
first and formatted by us, so a string in a numeric field cannot smuggle
markup through either. Nothing from the data is ever placed inside the
``<style>`` or ``<script>`` blocks or inside a URL.

The page's Content-Security-Policy is the second lock on the same door. It
allows the one stylesheet and the one script this module writes, each by
hash, and nothing else: no network requests, no other inline script, no
inline ``style=""`` attributes. If an escaping bug ever did let markup
through, the browser would still refuse to run it. That is also why sizes
are set with SVG attributes here and never with ``style=""``.

The reports are read as plain dictionaries with ``.get``, not as the
dataclasses that wrote them. The JSON file is the contract between the
commands, and a report written by a newer or older version of ``scan`` should
still render what it has and show a dash for what it lacks.
"""

from __future__ import annotations

import base64
import hashlib
import html
import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from kalshi_edge.dashboard_assets import CSS, JS

DASH = "–"
BAR_WIDTH = 96
BAR_HALF = 46.0
# A clearing edge above this gets a caution on the page. Ten probability
# points against a book that people are trading is a large claim: the
# sportsbook comparison prints edges of a point or two. When a model disagrees
# with a traded market by more than this, the usual explanation is something
# the model does not know, not free money.
LARGE_EDGE = 0.10

_ICONS = {
    "good": '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7"/>'
    '<path d="M4.5 8.3l2.3 2.3 4.7-5" fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round" '
    'stroke-linejoin="round"/></svg>',
    "warning": '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.2l7.2 13H.8z"/>'
    '<path d="M8 6v4.2M8 12v.6" fill="none" stroke="#000" stroke-width="1.6" stroke-linecap="round"/></svg>',
    "critical": '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="1" y="1" width="14" height="14" rx="3"/>'
    '<path d="M5 5l6 6M11 5l-6 6" fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/></svg>',
    "none": '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7"/>'
    '<path d="M5 8h6" fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/></svg>',
}


@dataclass(frozen=True)
class SeriesHealth:
    series_ticker: str
    markets: int
    open_markets: int
    settled: int
    snapshots: int
    snapshot_runs: int
    last_snapshot: str | None
    fee_type: str | None
    fee_multiplier: str | None


@dataclass(frozen=True)
class OddsHealth:
    sport_key: str
    captures: int
    last_capture: str | None
    last_events: int | None
    last_rows: int | None


@dataclass(frozen=True)
class Health:
    """What is on disk, read without changing it."""

    name: str  # file name only: the page gets sent around, a full path would leak the home directory
    schema_version: str | None
    markets: int
    settled: int
    snapshots: int
    pre_event_prices: int
    series: tuple[SeriesHealth, ...] = ()
    odds: tuple[OddsHealth, ...] = ()

    @property
    def last_snapshot(self) -> str | None:
        stamps = [s.last_snapshot for s in self.series if s.last_snapshot]
        return max(stamps) if stamps else None


@dataclass
class DashboardData:
    """Everything one page is built from. Any part may be absent."""

    built_at: datetime
    tz: ZoneInfo
    scan: Mapping[str, object] | None = None
    compares: list[Mapping[str, object]] = field(default_factory=list)
    calibrations: list[Mapping[str, object]] = field(default_factory=list)
    health: Health | None = None
    stale_hours: float = 26.0
    calibration_stale_days: float = 8.0
    title: str = "Kalshi Edge Finder"


@dataclass(frozen=True)
class Freshness:
    label: str
    status: str  # good, warning, critical, none
    word: str
    detail: str


# ---------------------------------------------------------------------------
# Escaping and number handling
# ---------------------------------------------------------------------------


def esc(value: object) -> str:
    """Text safe to place in HTML content or inside a quoted attribute."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def as_float(value: object) -> float | None:
    """A finite float, or None. ``True`` is not a number here."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def as_list(value: object) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def as_dict(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def fmt_pct(value: object, digits: int = 1) -> str:
    number = as_float(value)
    return DASH if number is None else f"{number * 100:.{digits}f}%"


def fmt_pp(value: object) -> str:
    """Probability points, signed: 0.031 -> +3.1 pp."""
    number = as_float(value)
    return DASH if number is None else f"{number * 100:+.1f} pp"


def fmt_money(value: object, digits: int = 2) -> str:
    number = as_float(value)
    if number is None:
        return DASH
    sign = "-" if number < 0 else ""
    return f"{sign}${abs(number):.{digits}f}"


def fmt_signed_money(value: object, digits: int = 3) -> str:
    number = as_float(value)
    if number is None:
        return DASH
    return f"{'+' if number >= 0 else '-'}${abs(number):.{digits}f}"


def fmt_int(value: object) -> str:
    number = as_float(value)
    return DASH if number is None else f"{round(number):,}"


def sort_value(value: object) -> str:
    """The data-v attribute: a plain number, or empty so the row sorts last."""
    number = as_float(value)
    return "" if number is None else repr(number)


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def fmt_time(value: object, tz: ZoneInfo) -> str:
    """``Tue Nov 10, 9:57 PM CST``. Built by hand: ``%-d`` is not portable."""
    moment = value if isinstance(value, datetime) else parse_time(value)
    if moment is None:
        return DASH
    local = moment.astimezone(tz)
    hour = local.hour % 12 or 12
    half = "AM" if local.hour < 12 else "PM"
    return f"{local:%a %b} {local.day}, {hour}:{local:%M} {half} {local:%Z}"


def fmt_settles(entry: Mapping[str, object], tz: ZoneInfo) -> str:
    """Settlement time in ``tz``, else the report's Chicago string, else a dash.

    ``settles_at`` is the instant. ``settles_chicago`` is already written for
    one zone, so it is only a fallback when that instant is missing or
    unreadable. The caller still runs the result through ``esc``.
    """
    shown = fmt_time(entry.get("settles_at"), tz)
    if shown != DASH:
        return shown
    fallback = entry.get("settles_chicago")
    if isinstance(fallback, str) and fallback.strip():
        return fallback
    return DASH


def settles_cell(entry: Mapping[str, object], tz: ZoneInfo) -> str:
    """Visible time in ``tz``. ``data-v`` stays the raw instant so the column sorts."""
    return f'<td data-v="{esc(entry.get("settles_at"))}">{esc(fmt_settles(entry, tz))}</td>'


def fmt_age(hours: float) -> str:
    if hours < 1:
        return f"{max(1, round(hours * 60))} min"
    if hours < 48:
        return f"{hours:.0f} h"
    return f"{hours / 24:.0f} days"


def freshness(label: str, stamp: object, now: datetime, limit_hours: float) -> Freshness:
    """How old one input was when the page was built.

    A stamp in the future means two clocks disagree (or a file was edited by
    hand). That is a warning, not "fresh": an age cannot be trusted either way.
    """
    if stamp is None or stamp == "":
        return Freshness(label, "none", "Not supplied", "no data for this panel")
    moment = parse_time(stamp)
    if moment is None:
        return Freshness(label, "warning", "Unreadable time", "the timestamp could not be parsed")
    hours = (now - moment).total_seconds() / 3600
    if hours < -0.05:
        return Freshness(label, "warning", "Clock mismatch", "stamped later than this page was built")
    hours = max(hours, 0.0)
    if hours > limit_hours:
        return Freshness(label, "critical", "Stale", f"{fmt_age(hours)} old when built")
    return Freshness(label, "good", "Fresh", f"{fmt_age(hours)} old when built")


# ---------------------------------------------------------------------------
# Small HTML pieces
# ---------------------------------------------------------------------------


def status_html(status: str, word: str) -> str:
    key = status if status in _ICONS else "none"
    return f'<span class="status st-{key}">{_ICONS[key]}{esc(word)}</span>'


def tags_html(flags: object) -> str:
    return "".join(f'<span class="tag">{esc(flag)}</span>' for flag in as_list(flags))


def edge_bar(value: object, scale: float) -> str:
    """A bar from a center line: blue to the right for positive, red to the left.

    The data end is rounded and the end on the axis is square, so the bar
    reads as growing out of zero. Sizes are SVG attributes because the page's
    policy forbids inline styles.
    """
    number = as_float(value)
    center = BAR_WIDTH / 2
    axis = f'<line class="axis" x1="{center}" y1="0" x2="{center}" y2="12"/>'
    if number is None or number == 0 or scale <= 0:
        return f'<svg class="bar" viewBox="0 0 {BAR_WIDTH} 12" aria-hidden="true">{axis}</svg>'
    length = max(1.0, min(abs(number) / scale, 1.0) * BAR_HALF)
    radius = min(3.0, length)
    if number > 0:
        tip = center + length
        path = (
            f"M{center} 2H{tip - radius:.1f}A{radius:.1f} {radius:.1f} 0 0 1 {tip:.1f} {2 + radius:.1f}"
            f"V{10 - radius:.1f}A{radius:.1f} {radius:.1f} 0 0 1 {tip - radius:.1f} 10H{center}Z"
        )
        css = "pos"
    else:
        tip = center - length
        path = (
            f"M{center} 2H{tip + radius:.1f}A{radius:.1f} {radius:.1f} 0 0 0 {tip:.1f} {2 + radius:.1f}"
            f"V{10 - radius:.1f}A{radius:.1f} {radius:.1f} 0 0 0 {tip + radius:.1f} 10H{center}Z"
        )
        css = "neg"
    return (
        f'<svg class="bar" viewBox="0 0 {BAR_WIDTH} 12" aria-hidden="true">'
        f'<path class="{css}" d="{path}"/>{axis}</svg>'
    )


def bar_scale(values: Iterable[object]) -> float:
    """The edge that fills a bar: the largest in the table, never under 1 pp."""
    numbers = [abs(n) for n in (as_float(v) for v in values) if n is not None]
    return max(0.01, max(numbers, default=0.01))


def edge_cell(value: object, scale: float) -> str:
    return (
        f'<td class="r" data-v="{sort_value(value)}"><span class="edge">'
        f"{edge_bar(value, scale)}<span>{esc(fmt_pp(value))}</span></span></td>"
    )


def num_cell(value: object, text: str) -> str:
    return f'<td class="r" data-v="{sort_value(value)}">{esc(text)}</td>'


def th(label: str, *, right: bool = False, sortable: bool = True, first: str = "asc") -> str:
    classes = " ".join(c for c in ("r" if right else "", "sortable" if sortable else "") if c)
    attrs = f' class="{classes}"' if classes else ""
    if sortable:
        attrs += f' data-first="{first}"'
    return f'<th scope="col"{attrs}>{esc(label)}</th>'


def empty(text: str) -> str:
    return f'<p class="empty">{esc(text)}</p>'


def bullet_details(summary: str, items: object) -> str:
    lines = [str(item) for item in as_list(items) if str(item).strip()]
    if not lines:
        return ""
    body = "".join(f"<li>{esc(line)}</li>" for line in lines)
    return f"<details><summary>{esc(summary)} ({len(lines)})</summary><ul>{body}</ul></details>"


BAR_KEY = (
    '<p class="key"><span><svg viewBox="0 0 10 10" aria-hidden="true"><rect class="pos" width="10" height="10" rx="2"/>'
    "</svg>edge above zero</span>"
    '<span><svg viewBox="0 0 10 10" aria-hidden="true"><rect class="neg" width="10" height="10" rx="2"/></svg>'
    "edge below zero</span>"
    "<span>a full bar is the largest edge in this table</span></p>"
)


# ---------------------------------------------------------------------------
# The non-sports scan
# ---------------------------------------------------------------------------


def scan_kind(series_ticker: object) -> str:
    text = str(series_ticker or "").upper()
    if text.startswith("KXHIGH"):
        return "Weather"
    if text.startswith("KXCPI"):
        return "CPI"
    return "Other"


def _scan_order(row: Mapping[str, object]) -> tuple[int, float]:
    edge = as_float(row.get("edge"))
    return (0 if row.get("clears") is True else 1, -(edge if edge is not None else -math.inf))


def render_modeled(rows: Sequence[Mapping[str, object]], tz: ZoneInfo) -> str:
    if not rows:
        return empty("The scan priced no contract against a model this run.")
    ordered = sorted(rows, key=_scan_order)
    scale = bar_scale(row.get("edge") for row in ordered)
    kinds = sorted({scan_kind(row.get("series_ticker")) for row in ordered})
    body: list[str] = []
    for row in ordered:
        kind = scan_kind(row.get("series_ticker"))
        flags = as_list(row.get("flags"))
        clears = row.get("clears") is True
        body.append(
            f'<tr data-clears="{1 if clears else 0}" data-clean="{0 if flags else 1}" data-kind="{esc(kind)}">'
            f'<td class="title">{esc(row.get("title"))}<br><span class="tick">{esc(row.get("ticker"))}</span>'
            f'<span class="why">{esc(row.get("why"))}</span></td>'
            f"<td>{esc(kind)}</td>"
            f"<td>{esc(row.get('side'))}</td>"
            f"{num_cell(row.get('ask'), fmt_money(row.get('ask')))}"
            f"{num_cell(row.get('model'), fmt_pct(row.get('model')))}"
            f"{num_cell(row.get('break_even'), fmt_pct(row.get('break_even')))}"
            f"{edge_cell(row.get('edge'), scale)}"
            f"{num_cell(row.get('contracts'), fmt_int(row.get('contracts')))}"
            f"{settles_cell(row, tz)}"
            f"<td>{tags_html(flags)}</td></tr>"
        )
    options = "".join(f'<option value="{esc(kind)}">{esc(kind)}</option>' for kind in kinds)
    controls = (
        '<div class="controls">'
        '<label>Search <input type="search" data-for="scan-modeled" data-filter="text" '
        'placeholder="city, ticker, side"></label>'
        '<label><input type="checkbox" data-for="scan-modeled" data-filter="flag" data-name="clears" checked> '
        "Clears costs only</label>"
        '<label><input type="checkbox" data-for="scan-modeled" data-filter="flag" data-name="clean"> '
        "Hide THIN / WIDE / NOBID</label>"
        f'<label>Kind <select data-for="scan-modeled" data-filter="eq" data-name="kind">'
        f'<option value="">All</option>{options}</select></label>'
        '<span class="count" data-count="scan-modeled"></span></div>'
    )
    head = (
        th("Market")
        + th("Kind")
        + th("Side")
        + th("Ask", right=True)
        + th("Model", right=True, first="desc")
        + th("Break-even", right=True)
        + th("Edge", right=True, first="desc")
        + th("Contracts", right=True, first="desc")
        + th("Settles")
        + th("Flags", sortable=False)
    )
    return (
        f'{controls}<div class="scroll tall"><table class="sort" id="scan-modeled">'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>{BAR_KEY}"
    )


def render_econ(events: Sequence[Mapping[str, object]], tz: ZoneInfo) -> str:
    if not events:
        return empty("No open economic-release markets were listed this run.")
    cards: list[str] = []
    for event in events:
        strikes = [as_dict(s) for s in as_list(event.get("strikes"))]
        table = ""
        if strikes:
            rows = "".join(
                f"<tr><td>{esc(s.get('title'))}<br><span class=\"tick\">{esc(s.get('ticker'))}</span></td>"
                f'<td class="r">{esc(fmt_money(s.get("yes_ask")))}</td>'
                f'<td class="r">{esc(fmt_money(s.get("no_ask")))}</td>'
                f"<td>{tags_html(s.get('flags'))}</td></tr>"
                for s in strikes
            )
            table = (
                '<table><thead><tr><th scope="col">Strike</th><th scope="col" class="r">Yes ask</th>'
                '<th scope="col" class="r">No ask</th><th scope="col">Flags</th></tr></thead>'
                f"<tbody>{rows}</tbody></table>"
            )
        horizon = "" if event.get("within_horizon") is True else ' <span class="tag">PAST HORIZON</span>'
        cards.append(
            f'<div class="event"><h4>{esc(event.get("label"))}{horizon}</h4>'
            f'<p class="small muted">{esc(fmt_settles(event, tz))} &middot; '
            f"{esc(fmt_int(event.get('market_count')))} markets "
            f'&middot; <span class="tick">{esc(event.get("event_ticker"))}</span></p>'
            f'<p class="small">{esc(event.get("summary"))}</p>{table}</div>'
        )
    return f'<div class="events">{"".join(cards)}</div>'


def render_short(rows: Sequence[Mapping[str, object]], total: object, omitted: object, tz: ZoneInfo) -> str:
    if not rows:
        return empty("No short-dated markets in the chosen categories this run.")
    body = "".join(
        f'<tr><td class="title">{esc(row.get("title"))}<br><span class="tick">{esc(row.get("ticker"))}</span></td>'
        f"{num_cell(row.get('ask'), fmt_money(row.get('ask')))}"
        f"{num_cell(row.get('no_ask'), fmt_money(row.get('no_ask')))}"
        f"{num_cell(row.get('volume_24h'), fmt_int(row.get('volume_24h')))}"
        f"{num_cell(row.get('open_interest'), fmt_int(row.get('open_interest')))}"
        f"{settles_cell(row, tz)}"
        f"<td>{tags_html(row.get('flags'))}</td></tr>"
        for row in rows
    )
    head = (
        th("Market")
        + th("Yes ask", right=True)
        + th("No ask", right=True)
        + th("24h volume", right=True, first="desc")
        + th("Open interest", right=True, first="desc")
        + th("Settles")
        + th("Flags", sortable=False)
    )
    note = ""
    omitted_count = as_float(omitted)
    if omitted_count:
        note = f" The scan left {fmt_int(omitted)} of {fmt_int(total)} out of its report."
    return (
        f'<p class="small muted">No model behind this list. None of it is an edge.{esc(note)}</p>'
        '<div class="controls"><label>Search <input type="search" data-for="scan-short" data-filter="text" '
        'placeholder="title or ticker"></label><span class="count" data-count="scan-short"></span></div>'
        f'<div class="scroll tall"><table class="sort" id="scan-short"><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def median_clearing_edge(modeled: Sequence[Mapping[str, object]]) -> float | None:
    edges = [as_float(row.get("edge")) for row in modeled if row.get("clears") is True]
    known = [edge for edge in edges if edge is not None]
    return statistics.median(known) if known else None


def large_edge_count(modeled: Sequence[Mapping[str, object]]) -> int:
    edges = (as_float(row.get("edge")) for row in modeled if row.get("clears") is True)
    return sum(1 for edge in edges if edge is not None and edge > LARGE_EDGE)


def render_scan(scan: Mapping[str, object] | None, data: DashboardData) -> str:
    if scan is None:
        return (
            '<section class="card" id="scan"><h2>Non-sports scan</h2>'
            f"{empty('No scan report was supplied. Run: python -m kalshi_edge.scan --json scan.json')}</section>"
        )
    modeled = [as_dict(row) for row in as_list(scan.get("modeled"))]
    clears = sum(1 for row in modeled if row.get("clears") is True)
    verdict = (
        "Nothing clears costs today."
        if clears == 0
        else f"{clears:,} of {len(modeled):,} modeled sides clear the break-even at the ask, fee included."
    )
    caution = ""
    large = large_edge_count(modeled)
    if large:
        caution = (
            f'<p class="banner warn">{large:,} of the {clears:,} clearing sides show an edge over '
            f"{LARGE_EDGE * 100:.0f} pp. A gap that large against a book people are trading usually means the model "
            "is missing something the market knows, not that the market is wrong. Weather edges here rest on an "
            "assumed forecast error that has not been fit to settlements. Read them as disagreements to "
            "investigate, not as money on the table.</p>"
        )
    facts = (
        f'<ul class="facts"><li><b>Run</b> {esc(fmt_time(scan.get("generated_at"), data.tz))}</li>'
        f"<li><b>Order size</b> {esc(fmt_money(scan.get('budget')))}</li>"
        f"<li><b>Horizon</b> {esc(fmt_int(scan.get('horizon_days')))} days</li></ul>"
    )
    short = render_short(
        [as_dict(row) for row in as_list(scan.get("short_dated"))],
        scan.get("short_dated_total"),
        scan.get("short_dated_omitted"),
        data.tz,
    )
    return (
        '<section class="card" id="scan"><h2>Non-sports scan</h2>'
        f"<p>{esc(verdict)}</p>{facts}{caution}"
        f"{bullet_details('Notes from this run', scan.get('notes'))}"
        f"{bullet_details('Read this honestly', scan.get('caveats'))}"
        f"<h3>Modeled contracts: weather and CPI</h3>{render_modeled(modeled, data.tz)}"
        "<h3>Economic releases</h3>"
        f"{render_econ([as_dict(event) for event in as_list(scan.get('econ_events'))], data.tz)}"
        f"<h3>Short-dated markets</h3>{short}"
        "</section>"
    )


# ---------------------------------------------------------------------------
# Sportsbook comparison
# ---------------------------------------------------------------------------


def compare_sides(report: Mapping[str, object]) -> list[tuple[Mapping[str, object], Mapping[str, object]]]:
    pairs = []
    for event in as_list(report.get("events")):
        event_map = as_dict(event)
        for side in as_list(event_map.get("sides")):
            pairs.append((event_map, as_dict(side)))
    return pairs


def render_compare(report: Mapping[str, object], index: int, data: DashboardData) -> str:
    table_id = f"compare-{index}"
    pairs = compare_sides(report)
    stale = report.get("stale") is True
    gap = as_float(report.get("gap_minutes"))
    gap_text = DASH if gap is None else f"{gap:.0f} min"
    gap_status = status_html("critical", "STALE") if stale else status_html("good", "Within limit")
    if gap is None:
        gap_status = status_html("none", "Unknown")
    facts = (
        '<ul class="facts">'
        f"<li><b>Kalshi snapshot</b> {esc(fmt_time(report.get('kalshi_captured_at'), data.tz))}</li>"
        f"<li><b>Odds capture</b> {esc(fmt_time(report.get('odds_captured_at'), data.tz))}</li>"
        f"<li><b>Gap</b> {esc(gap_text)} {gap_status}</li>"
        f"<li><b>Reference</b> {esc(report.get('reference'))}</li>"
        f"<li><b>De-vig</b> {esc(report.get('devig'))}</li>"
        f"<li><b>Fees</b> {esc(report.get('fee_source'))}</li>"
        f"<li><b>Open contracts</b> {esc(fmt_int(report.get('open_contracts')))}</li></ul>"
    )
    warning = ""
    if stale:
        warning = (
            '<p class="banner">The Kalshi snapshot and the odds capture are '
            f"{esc(gap_text)} apart, past the {esc(fmt_int(report.get('max_gap_minutes')))} minute limit. "
            "An edge below may just be line movement between the two captures.</p>"
        )
    if pairs:
        scale = bar_scale(as_dict(side.get("evaluation")).get("edge") for _, side in pairs)
        ordered = sorted(
            pairs,
            key=lambda pair: -(as_float(as_dict(pair[1].get("evaluation")).get("edge")) or -math.inf),
        )
        body: list[str] = []
        for event, side in ordered:
            ev = as_dict(side.get("evaluation"))
            live = ' <span class="tag">IN PLAY</span>' if event.get("in_play") is True else ""
            body.append(
                f'<tr><td class="title">{esc(event.get("away_team"))} at {esc(event.get("home_team"))}{live}<br>'
                f'<span class="tick">{esc(side.get("ticker"))}</span>'
                f'<span class="why">{esc(fmt_time(event.get("kickoff"), data.tz))} &middot; '
                f"ref {esc(event.get('reference'))}, {esc(fmt_int(event.get('books')))} books</span></td>"
                f"<td>{esc(side.get('team'))}</td>"
                f"<td>{esc(side.get('side'))}</td>"
                f"{num_cell(ev.get('price'), fmt_money(ev.get('price')))}"
                f"{num_cell(ev.get('break_even'), fmt_pct(ev.get('break_even')))}"
                f"{num_cell(ev.get('fair_probability'), fmt_pct(ev.get('fair_probability')))}"
                f"{num_cell(side.get('alt_fair'), fmt_pct(side.get('alt_fair')))}"
                f"{edge_cell(ev.get('edge'), scale)}"
                f"{num_cell(ev.get('ev'), fmt_signed_money(ev.get('ev')))}"
                f"{num_cell(ev.get('roi'), fmt_pct(ev.get('roi')))}</tr>"
            )
        head = (
            th("Game")
            + th("Contract")
            + th("Side")
            + th("Ask", right=True)
            + th("Break-even", right=True)
            + th("Fair", right=True, first="desc")
            + th("Alt fair", right=True, first="desc")
            + th("Edge", right=True, first="desc")
            + th("EV / contract", right=True, first="desc")
            + th("ROI", right=True, first="desc")
        )
        table = (
            '<div class="controls"><label>Search <input type="search" '
            f'data-for="{table_id}" data-filter="text" placeholder="team or ticker"></label>'
            f'<span class="count" data-count="{table_id}"></span></div>'
            f'<div class="scroll tall"><table class="sort" id="{table_id}"><thead><tr>{head}</tr></thead>'
            f"<tbody>{''.join(body)}</tbody></table></div>{BAR_KEY}"
        )
    else:
        table = empty("No contract was matched to a sportsbook line in this report.")
    unmatched = [
        f"{as_dict(item).get('event_ticker')}: {as_dict(item).get('reason')}" for item in as_list(report.get("unmatched"))
    ]
    live = [" ".join(str(part) for part in as_list(item)) for item in as_list(report.get("live_skipped"))]
    return (
        f'<section class="card" id="{table_id}-card"><h2>Sportsbook comparison: {esc(report.get("series_ticker"))}</h2>'
        f'<p class="small muted">A positive edge means cheaper than one sportsbook&#x27;s view after fees. '
        f"It does not mean a sure thing.</p>{facts}{warning}{table}"
        f"{bullet_details('Unmatched events', unmatched)}"
        f"{bullet_details('In play, not priced', live)}"
        f"{bullet_details('Read this honestly', report.get('caveats'))}</section>"
    )


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def reliability_svg(buckets: Sequence[Mapping[str, object]]) -> str:
    """Realized YES rate against the mean price, one point per bucket.

    A point on the diagonal is a bucket whose prices were honest. The whisker
    is the Wilson 95% interval, so a point off the line whose whisker still
    crosses it is noise, not a finding. The dot's area grows with the number
    of contracts in the bucket.
    """
    size, left, top, plot = 440, 48, 12, 372
    points = []
    for bucket in buckets:
        predicted = as_float(bucket.get("predicted"))
        actual = as_float(bucket.get("actual"))
        count = as_float(bucket.get("count")) or 0
        if predicted is None or actual is None or count <= 0:
            continue
        points.append((bucket, predicted, actual, count))
    largest = max((count for *_, count in points), default=1)

    def x_of(p: float) -> float:
        return left + min(max(p, 0.0), 1.0) * plot

    def y_of(p: float) -> float:
        return top + (1 - min(max(p, 0.0), 1.0)) * plot

    parts = [
        (
            f'<svg class="chart" viewBox="0 0 {size} {size}" role="img" '
            'aria-label="Reliability diagram: realized YES rate against the pre-event price, by bucket. '
            'The table beside it has the same numbers.">'
        )
    ]
    for step in range(5):
        fraction = step / 4
        parts.append(
            f'<line class="grid" x1="{x_of(fraction):.1f}" y1="{top}" x2="{x_of(fraction):.1f}" y2="{top + plot}"/>'
            f'<line class="grid" x1="{left}" y1="{y_of(fraction):.1f}" x2="{left + plot}" y2="{y_of(fraction):.1f}"/>'
            f'<text x="{x_of(fraction):.1f}" y="{top + plot + 16}" text-anchor="middle">{step * 25}%</text>'
            f'<text x="{left - 8}" y="{y_of(fraction) + 4:.1f}" text-anchor="end">{step * 25}%</text>'
        )
    parts.append(
        f'<rect class="frame" x="{left}" y="{top}" width="{plot}" height="{plot}"/>'
        f'<line class="ideal" x1="{x_of(0):.1f}" y1="{y_of(0):.1f}" x2="{x_of(1):.1f}" y2="{y_of(1):.1f}"/>'
        f'<text class="axis-title" x="{left + plot / 2:.1f}" y="{size - 8}" text-anchor="middle">'
        "Price before the event (bucket mean)</text>"
        f'<text class="axis-title" transform="rotate(-90 12 {top + plot / 2:.1f})" x="12" y="{top + plot / 2:.1f}" '
        'text-anchor="middle">How often YES happened</text>'
    )
    for bucket, predicted, actual, count in points:
        cx, cy = x_of(predicted), y_of(actual)
        radius = 4 + 6 * math.sqrt(count / largest)
        low, high = as_float(bucket.get("actual_low")), as_float(bucket.get("actual_high"))
        whisker = ""
        interval = ""
        if low is not None and high is not None:
            whisker = f'<line class="whisker" x1="{cx:.1f}" y1="{y_of(low):.1f}" x2="{cx:.1f}" y2="{y_of(high):.1f}"/>'
            interval = f"\n95% interval {fmt_pct(low)} to {fmt_pct(high)}"
        tip = (
            f"Priced {fmt_pct(bucket.get('lower'), 0)} to {fmt_pct(bucket.get('upper'), 0)}\n"
            f"{fmt_int(count)} contracts, mean price {fmt_pct(predicted)}\n"
            f"YES happened {fmt_pct(actual)}{interval}"
        )
        parts.append(
            f'<g class="pt" tabindex="0" data-tip="{esc(tip)}">{whisker}'
            f'<circle class="dot" cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}"/>'
            f'<circle class="hit" cx="{cx:.1f}" cy="{cy:.1f}" r="{radius + 8:.1f}"/></g>'
        )
    parts.append("</svg>")
    return "".join(parts)


def render_calibration(report: Mapping[str, object], index: int, data: DashboardData) -> str:
    buckets = [as_dict(b) for b in as_list(report.get("buckets"))]
    brier, base = as_float(report.get("brier")), as_float(report.get("base_rate_brier"))
    verdict = ""
    if brier is not None and base is not None:
        if brier < base:
            verdict = (
                f"Brier {brier:.4f} against {base:.4f} for a constant forecast at the base rate: "
                "the prices carried information about the games."
            )
        else:
            verdict = (
                f"Brier {brier:.4f} against {base:.4f} for a constant forecast at the base rate: "
                "the prices did no better than always guessing the base rate."
            )
    facts = (
        '<ul class="facts">'
        f"<li><b>Run</b> {esc(fmt_time(report.get('generated_at'), data.tz))}</li>"
        f"<li><b>Settled markets</b> {esc(fmt_int(report.get('settled')))}</li>"
        f"<li><b>With a pre-event quote</b> {esc(fmt_int(report.get('with_quote')))}</li>"
        f"<li><b>Observations</b> {esc(fmt_int(report.get('observations')))}"
        f"{' (one contract per event)' if report.get('one_per_event') is True else ''}</li>"
        f"<li><b>Base rate</b> {esc(fmt_pct(report.get('base_rate')))}</li>"
        f"<li><b>Contracts per order</b> {esc(fmt_int(report.get('contracts')))}</li></ul>"
    )
    populated = [b for b in buckets if (as_float(b.get("count")) or 0) > 0]
    if not populated:
        body = empty("No bucket has an observation yet. Collect settled markets, then run calibrate again.")
    else:
        rows = "".join(
            f'<tr><td data-v="{sort_value(b.get("lower"))}">{esc(fmt_pct(b.get("lower"), 0))} to '
            f"{esc(fmt_pct(b.get('upper'), 0))}</td>"
            f"{num_cell(b.get('count'), fmt_int(b.get('count')))}"
            f"{num_cell(b.get('predicted'), fmt_pct(b.get('predicted')))}"
            f"{num_cell(b.get('actual'), fmt_pct(b.get('actual')))}"
            f'<td class="r">{esc(fmt_pct(b.get("actual_low"), 0))} to {esc(fmt_pct(b.get("actual_high"), 0))}</td>'
            f"{num_cell(b.get('yes_roi'), fmt_pct(b.get('yes_roi')))}"
            f"{num_cell(b.get('no_roi'), fmt_pct(b.get('no_roi')))}</tr>"
            for b in buckets
        )
        head = (
            th("Price bucket")
            + th("Contracts", right=True, first="desc")
            + th("Mean price", right=True)
            + th("YES rate", right=True)
            + th("95% interval", right=True, sortable=False)
            + th("Buy YES ROI", right=True, first="desc")
            + th("Buy NO ROI", right=True, first="desc")
        )
        key = (
            '<p class="key"><span>diagonal: prices that were honest probabilities</span>'
            "<span>dot area: contracts in the bucket</span><span>whisker: Wilson 95% interval</span></p>"
        )
        body = (
            f'<div class="grid2"><figure>{reliability_svg(buckets)}{key}'
            "<figcaption>A dot off the line whose whisker still crosses it is noise, not a mispricing.</figcaption>"
            f'</figure><div class="scroll"><table class="sort"><thead><tr>{head}</tr></thead>'
            f"<tbody>{rows}</tbody></table></div></div>"
            '<p class="small muted">ROI is the fee-adjusted return of buying that side at the ask in every market '
            "of the bucket and holding to settlement.</p>"
        )
    skipped = [f"{key}: {fmt_int(value)}" for key, value in as_dict(report.get("skipped")).items()]
    return (
        f'<section class="card" id="calibration-{index}">'
        f"<h2>Calibration: {esc(report.get('series'))}</h2>"
        f"<p>{esc(verdict)}</p>{facts}{body}{bullet_details('Markets left out, by reason', skipped)}</section>"
    )


# ---------------------------------------------------------------------------
# Database health
# ---------------------------------------------------------------------------


def render_health(health: Health | None, data: DashboardData) -> str:
    if health is None:
        return (
            '<section class="card" id="health"><h2>Data on disk</h2>'
            f"{empty('No database was read. Pass --db to show what has been collected.')}</section>"
        )
    if health.series:
        rows = []
        for series in health.series:
            fresh = freshness("", series.last_snapshot, data.built_at, data.stale_hours)
            fee = " ".join(part for part in (series.fee_type, series.fee_multiplier) if part) or DASH
            rows.append(
                f'<tr><td><span class="tick">{esc(series.series_ticker)}</span></td>'
                f"{num_cell(series.markets, fmt_int(series.markets))}"
                f"{num_cell(series.open_markets, fmt_int(series.open_markets))}"
                f"{num_cell(series.settled, fmt_int(series.settled))}"
                f"{num_cell(series.snapshot_runs, fmt_int(series.snapshot_runs))}"
                f'<td data-v="{esc(series.last_snapshot)}">{esc(fmt_time(series.last_snapshot, data.tz))}</td>'
                f"<td>{status_html(fresh.status, fresh.word)}</td>"
                f"<td>{esc(fee)}</td></tr>"
            )
        head = (
            th("Series")
            + th("Markets", right=True, first="desc")
            + th("Open", right=True, first="desc")
            + th("Settled", right=True, first="desc")
            + th("Snapshot runs", right=True, first="desc")
            + th("Last snapshot", first="desc")
            + th("Snapshot age", sortable=False)
            + th("Fee schedule", sortable=False)
        )
        series_table = (
            '<div class="controls"><label>Search <input type="search" data-for="health-series" '
            'data-filter="text" placeholder="series ticker"></label>'
            '<span class="count" data-count="health-series"></span></div>'
            f'<div class="scroll tall"><table class="sort" id="health-series"><thead><tr>{head}</tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
    else:
        series_table = empty("The database has no markets yet. Run collect first.")
    odds_table = ""
    if health.odds:
        odds_rows = "".join(
            f'<tr><td><span class="tick">{esc(o.sport_key)}</span></td>'
            f'<td class="r">{esc(fmt_int(o.captures))}</td>'
            f"<td>{esc(fmt_time(o.last_capture, data.tz))}</td>"
            f'<td class="r">{esc(fmt_int(o.last_events))}</td><td class="r">{esc(fmt_int(o.last_rows))}</td></tr>'
            for o in health.odds
        )
        odds_table = (
            '<h3>Sportsbook captures</h3><div class="scroll"><table><thead><tr><th scope="col">Sport</th>'
            '<th scope="col" class="r">Captures</th><th scope="col">Last capture</th>'
            '<th scope="col" class="r">Games in it</th><th scope="col" class="r">Price rows in it</th>'
            f"</tr></thead><tbody>{odds_rows}</tbody></table></div>"
        )
    facts = (
        '<ul class="facts">'
        f"<li><b>File</b> <code>{esc(health.name)}</code></li>"
        f"<li><b>Schema version</b> {esc(health.schema_version or DASH)}</li>"
        f"<li><b>Markets</b> {esc(fmt_int(health.markets))}</li>"
        f"<li><b>Settled</b> {esc(fmt_int(health.settled))}</li>"
        f"<li><b>Price snapshots</b> {esc(fmt_int(health.snapshots))}</li>"
        f"<li><b>Cached pre-event quotes</b> {esc(fmt_int(health.pre_event_prices))}</li></ul>"
    )
    return (
        '<section class="card" id="health"><h2>Data on disk</h2>'
        '<p class="small muted">Read with a read-only connection. Building this page changes nothing in the file.</p>'
        f"{facts}<h3>By series</h3>{series_table}{odds_table}</section>"
    )


# ---------------------------------------------------------------------------
# Header, tiles, page
# ---------------------------------------------------------------------------


def input_freshness(data: DashboardData) -> list[Freshness]:
    items = [
        freshness(
            "Scan", data.scan.get("generated_at") if data.scan is not None else None, data.built_at, data.stale_hours
        )
    ]
    if not data.compares:
        items.append(freshness("Sportsbook comparison", None, data.built_at, data.stale_hours))
    for report in data.compares:
        items.append(
            freshness(
                f"Comparison {report.get('series_ticker') or ''}".strip(),
                report.get("generated_at"),
                data.built_at,
                data.stale_hours,
            )
        )
    if not data.calibrations:
        items.append(freshness("Calibration", None, data.built_at, data.calibration_stale_days * 24))
    for report in data.calibrations:
        items.append(
            freshness(
                f"Calibration {report.get('series') or ''}".strip(),
                report.get("generated_at"),
                data.built_at,
                data.calibration_stale_days * 24,
            )
        )
    items.append(
        freshness(
            "Price snapshots",
            data.health.last_snapshot if data.health is not None else None,
            data.built_at,
            data.stale_hours,
        )
    )
    return items


def tile(label: str, value: str, sub: str) -> str:
    return (
        f'<div class="tile"><div class="label">{esc(label)}</div><div class="value">{esc(value)}</div>'
        f'<div class="sub">{esc(sub)}</div></div>'
    )


def render_tiles(data: DashboardData) -> str:
    tiles: list[str] = []
    if data.scan is not None:
        modeled = [as_dict(row) for row in as_list(data.scan.get("modeled"))]
        clearing = [row for row in modeled if row.get("clears") is True]
        clean = [row for row in clearing if not as_list(row.get("flags"))]
        tiles.append(tile("Scan sides clearing costs", f"{len(clearing):,}", f"of {len(modeled):,} modeled sides"))
        median = median_clearing_edge(modeled)
        largest = max((as_float(row.get("edge")) or 0.0 for row in clearing), default=None)
        tiles.append(
            tile(
                "Median clearing edge",
                DASH if median is None else fmt_pp(median),
                "nothing clears"
                if largest is None
                else f"largest {fmt_pp(largest)}; {large_edge_count(modeled):,} over {LARGE_EDGE * 100:.0f} pp; "
                f"{len(clean):,} carry no flag",
            )
        )
    for report in data.compares:
        edges = [as_float(as_dict(side.get("evaluation")).get("edge")) for _, side in compare_sides(report)]
        known = [edge for edge in edges if edge is not None]
        positive = sum(1 for edge in known if edge > 0)
        sub = f"of {len(known):,} priced sides"
        if report.get("stale") is True:
            sub += ", captures STALE"
        tiles.append(tile(f"{report.get('series_ticker') or 'Sportsbook'} sides above fair", f"{positive:,}", sub))
    for report in data.calibrations:
        brier, base = as_float(report.get("brier")), as_float(report.get("base_rate_brier"))
        tiles.append(
            tile(
                f"{report.get('series') or 'Calibration'} Brier score",
                DASH if brier is None else f"{brier:.4f}",
                "lower is better" if base is None else f"{base:.4f} for a base-rate guess, lower is better",
            )
        )
    if data.health is not None:
        tiles.append(
            tile("Markets on disk", f"{data.health.markets:,}", f"{data.health.settled:,} settled with a result")
        )
    return f'<div class="tiles">{"".join(tiles)}</div>' if tiles else ""


def csp_hash(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return "sha256-" + base64.b64encode(digest).decode("ascii")


def content_security_policy() -> str:
    """Nothing loads from anywhere; only our two inline blocks may run."""
    return (
        "default-src 'none'; "
        f"style-src '{csp_hash(CSS)}'; "
        f"script-src '{csp_hash(JS)}'; "
        "img-src data:; base-uri 'none'; form-action 'none'"
    )


def render_page(data: DashboardData) -> str:
    built_iso = data.built_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    chips = "".join(
        f'<li class="chip"><span>{esc(item.label)}</span>{status_html(item.status, item.word)}'
        f'<span class="muted">{esc(item.detail)}</span></li>'
        for item in input_freshness(data)
    )
    nav = ['<a href="#scan">Scan</a>']
    nav += [
        f'<a href="#compare-{i}-card">Sportsbook {esc(report.get("series_ticker"))}</a>'
        for i, report in enumerate(data.compares)
    ] or ['<a href="#compare">Sportsbook</a>']
    nav += [
        f'<a href="#calibration-{i}">Calibration {esc(report.get("series"))}</a>'
        for i, report in enumerate(data.calibrations)
    ] or ['<a href="#calibration">Calibration</a>']
    nav.append('<a href="#health">Data on disk</a>')
    compare_html = "".join(render_compare(report, i, data) for i, report in enumerate(data.compares))
    if not data.compares:
        compare_html = (
            '<section class="card" id="compare"><h2>Sportsbook comparison</h2>'
            f"{empty('No comparison report was supplied. Run odds, then: python -m kalshi_edge.compare --series KXNFLGAME --json compare.json')}"
            "</section>"
        )
    calibration_html = "".join(render_calibration(report, i, data) for i, report in enumerate(data.calibrations))
    if not data.calibrations:
        calibration_html = (
            '<section class="card" id="calibration"><h2>Calibration</h2>'
            f"{empty('No calibration report was supplied. Run: python -m kalshi_edge.calibrate --series KXNFLGAME --json calibration.json')}"
            "</section>"
        )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{esc(content_security_policy())}">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>{esc(data.title)}</title>"
        f"<style>{CSS}</style></head><body><div class=\"wrap\">"
        '<header class="top"><div>'
        f"<h1>{esc(data.title)}</h1>"
        f'<p class="muted small" data-built="{built_iso}" data-stale-hours="{data.stale_hours:g}">'
        f'Built {esc(fmt_time(data.built_at, data.tz))}<span id="built-age"></span>. '
        "A snapshot, not a live feed.</p></div>"
        '<p class="muted small">Read-only. Nothing here places an order.</p></header>'
        '<p class="banner" id="old-file" hidden>This file is older than '
        f"{data.stale_hours:g} hours. Prices have moved since it was built. Build a new one before acting on it.</p>"
        f'<ul class="chips" aria-label="How fresh each input was">{chips}</ul>'
        f'<nav class="jump" aria-label="Sections">{"".join(nav)}</nav>'
        f"{render_tiles(data)}"
        f"{render_scan(data.scan, data)}{compare_html}{calibration_html}{render_health(data.health, data)}"
        "<footer>Built by <code>python -m kalshi_edge.dashboard</code>. One file, no network requests, "
        "no cookies. Edges are model or sportsbook comparisons, not guarantees.</footer>"
        '</div><div id="tip" role="tooltip" hidden></div>'
        f"<script>{JS}</script></body></html>\n"
    )
