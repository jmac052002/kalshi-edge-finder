"""The dashboard: reports in, one safe self-contained page out.

The reports are built with the real dataclasses and each command's own
``report_to_json``, not hand-written dictionaries. If ``scan``, ``compare``,
or ``calibrate`` renames a field the dashboard reads, these tests are where
that shows up.
"""

import base64
import hashlib
import json
import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from kalshi_edge import calibrate, compare, dashboard, scan
from kalshi_edge.dashboard import DashboardError, build, load_report, main, read_health, write_atomic
from kalshi_edge.dashboard_render import (
    LARGE_EDGE,
    DashboardData,
    as_float,
    bar_scale,
    edge_bar,
    fmt_time,
    freshness,
    reliability_svg,
    render_page,
)
from kalshi_edge.parse import SeriesFee, parse_market, parse_snapshot
from kalshi_edge.sportsbook.parse import SOURCE, OddsCapture, parse_odds_payload
from kalshi_edge.sportsbook.teams import DEFAULT_TABLE_PATH, TeamTable
from kalshi_edge.store import MarketStore

FIXTURE = Path(__file__).parent / "fixtures" / "odds_api_nfl.json"
KALSHI_AT = "2026-11-08T17:35:30Z"
ODDS_AT = "2026-11-08T17:31:02Z"
BUILT = datetime(2026, 11, 8, 18, 0, tzinfo=UTC)
CHICAGO = ZoneInfo("America/Chicago")
ATTACK = '<script>alert("x")</script>'
ATTR_ATTACK = '" onmouseover="alert(1)'


def _scan_row(ticker, title, *, edge, clears, flags=(), why="NWS hourly high 52F, assumed sd 3.2F"):
    return scan.ScanRow(
        ticker=ticker,
        title=title,
        side="NO",
        ask="0.3800",
        model=0.50 + edge,
        break_even=0.50,
        edge=edge,
        contracts=25,
        settles_at="2026-11-09T20:00:00Z",
        settles_chicago="2026-11-09 14:00 CST",
        flags=flags,
        why=why,
        clears=clears,
        series_ticker="KXHIGHTMIN",
        event_ticker="KXHIGHTMIN-26NOV08",
    )


def _scan_report(edges=(0.031, 0.012, -0.04)):
    report = scan.Report(generated_at="2026-11-08T17:40:00Z", budget="10.00", horizon_days=30, categories=("AI",))
    report.modeled = [
        _scan_row("KXHIGHTMIN-26NOV08-T55", "Minneapolis: 56° or above", edge=edges[0], clears=edges[0] > 0),
        _scan_row("KXHIGHTMIN-26NOV08-T57", "Minneapolis: 58° or above", edge=edges[1], clears=edges[1] > 0, flags=("THIN",)),
        _scan_row("KXHIGHTMIN-26NOV08-T59", "Minneapolis: 60° or above", edge=edges[2], clears=False),
    ]
    report.econ_events = [
        scan.EconEventView(
            series_ticker="KXCPI",
            event_ticker="KXCPI-26NOV",
            label="CPI month over month",
            settles_at="2026-11-12T13:30:00Z",
            settles_chicago="2026-11-12 07:30 CST",
            within_horizon=True,
            market_count=9,
            summary="nowcast 0.24, sd 0.08",
            strikes=(scan.StrikeQuote("KXCPI-26NOV-T0.2", "Above 0.2%", "0.5500", "0.4700", 310.0, 900.0, ("WIDE",)),),
        )
    ]
    report.short_dated = [
        scan.ScanRow(
            ticker="KXAIX-26NOV10",
            title="A short-dated listing",
            side="",
            ask="0.6000",
            model=None,
            break_even=None,
            edge=None,
            contracts=None,
            settles_at="2026-11-10T15:00:00Z",
            settles_chicago="2026-11-10 09:00 CST",
            flags=(),
            why="price only",
            clears=False,
            series_ticker="KXAIX",
            event_ticker="KXAIX-26NOV10",
            no_ask="0.4200",
            volume_24h=120.0,
            open_interest=400.0,
        )
    ]
    report.short_dated_total = 5
    report.short_dated_omitted = 4
    report.notes = ["weather: 1 city priced"]
    return report


def _contract(ticker, event, yes_ask, no_ask):
    payload = {
        "ticker": ticker,
        "event_ticker": event,
        "status": "active",
        "result": "",
        "yes_bid_dollars": str(Decimal(yes_ask) - Decimal("0.02")),
        "yes_ask_dollars": yes_ask,
        "no_bid_dollars": str(Decimal(no_ask) - Decimal("0.02")),
        "no_ask_dollars": no_ask,
        "notional_value_dollars": "1.0000",
    }
    market = parse_market(payload, series_ticker="KXNFLGAME", source="live", fetched_at=KALSHI_AT)
    snapshot = parse_snapshot(payload, series_ticker="KXNFLGAME", captured_at=KALSHI_AT)
    return market, snapshot


def _calibration_report():
    buckets = [
        calibrate.Bucket(0.0, 0.5, 40, 0.31, 0.25, 0.14, 0.40, -0.12, 0.03),
        calibrate.Bucket(0.5, 1.0, 60, 0.68, 0.72, 0.59, 0.82, 0.01, -0.09),
    ]
    return calibrate.Report(
        series="KXNFLGAME",
        settled=220,
        with_quote=200,
        observations=100,
        one_per_event=True,
        contracts=1,
        cutoff_sources={"occurrence_datetime": 100},
        skipped={"no_quote": 20},
        fetched=0,
        cached=200,
        buckets=buckets,
        brier=0.2104,
        base_rate=0.53,
        base_rate_brier=0.2491,
        generated_at="2026-11-04T12:00:00Z",
    )


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "kalshi.db"
    rows = [
        _contract("KXNFLGAME-26NOV08CINATL-CIN", "KXNFLGAME-26NOV08CINATL", "0.4500", "0.5600"),
        _contract("KXNFLGAME-26NOV08CINATL-ATL", "KXNFLGAME-26NOV08CINATL", "0.5600", "0.4500"),
        _contract("KXNFLGAME-26NOV08KCBUF-KC", "KXNFLGAME-26NOV08KCBUF", "0.5000", "0.5200"),
    ]
    with MarketStore(path) as store:
        store.save_page([m for m, _ in rows], [s for _, s in rows])
        odds_rows = parse_odds_payload(json.loads(FIXTURE.read_text()), captured_at=ODDS_AT)
        store.save_odds(OddsCapture(SOURCE, "americanfootball_nfl", ODDS_AT, "us,eu", 2, len(odds_rows)), odds_rows)
        store.save_series_fee(SeriesFee("KXNFLGAME", "quadratic", "1", KALSHI_AT))
    return path


@pytest.fixture
def paths(tmp_path, db_path):
    """The three report files, written the way the commands write them."""
    with MarketStore(db_path) as store:
        compared = compare.run(
            series_ticker="KXNFLGAME",
            store=store,
            table=TeamTable.load(DEFAULT_TABLE_PATH),
            now="2026-11-08T17:36:00Z",
            contracts=10,
            budget=None,
        )
    files = {
        "scan": tmp_path / "scan.json",
        "compare": tmp_path / "compare.json",
        "calibration": tmp_path / "calibration.json",
        "db": db_path,
    }
    files["scan"].write_text(scan.report_to_json(_scan_report()), encoding="utf-8")
    files["compare"].write_text(compare.report_to_json(compared), encoding="utf-8")
    files["calibration"].write_text(calibrate.report_to_json(_calibration_report()), encoding="utf-8")
    return files


def _build(paths, **overrides):
    options = {
        "built_at": BUILT,
        "tz": CHICAGO,
        "scan_path": paths["scan"],
        "compare_paths": [paths["compare"]],
        "calibration_paths": [paths["calibration"]],
        "db_path": paths["db"],
    }
    options.update(overrides)
    return build(**options)


def _page(**parts):
    return render_page(DashboardData(built_at=BUILT, tz=CHICAGO, **parts))


def _inline(page, tag):
    blocks = re.findall(rf"<{tag}>(.*?)</{tag}>", page, flags=re.DOTALL)
    assert len(blocks) == 1, f"expected exactly one <{tag}> block"
    return blocks[0]


# --- the whole page --------------------------------------------------------


def test_page_shows_every_report(paths):
    page = _build(paths)
    assert page.startswith("<!DOCTYPE html>")
    assert "Minneapolis: 56° or above" in page and "KXHIGHTMIN-26NOV08-T55" in page
    assert "2 of 3 modeled sides clear the break-even" in page
    assert "CPI month over month" in page and "Above 0.2%" in page
    assert "A short-dated listing" in page and "left 4 of 5 out" in page
    assert "Sportsbook comparison: KXNFLGAME" in page and "Cincinnati Bengals" in page
    assert "Calibration: KXNFLGAME" in page and "Brier 0.2104 against 0.2491" in page
    assert "the prices carried information" in page
    assert 'id="health-series"' in page and "quadratic 1" in page
    # The comparison's own numbers, not a recomputation: section 6.3 of the design doc.
    assert "-0.8 pp" in page and "46.0%" in page


def test_missing_inputs_become_panels_that_name_the_command():
    page = _page()
    assert "python -m kalshi_edge.scan --json scan.json" in page
    assert "python -m kalshi_edge.compare" in page
    assert "python -m kalshi_edge.calibrate" in page
    assert "Pass --db" in page
    assert page.count("Not supplied") == 4
    for anchor in ("#scan", "#compare", "#calibration", "#health"):
        assert f'href="{anchor}"' in page
        assert f'id="{anchor[1:]}"' in page


def test_a_report_with_missing_or_garbage_fields_still_renders():
    page = _page(
        scan={"modeled": ["not a row", 3, None, {"title": "only a title"}], "short_dated": None, "notes": "x"},
        compares=[{"events": [{"sides": [{"evaluation": "nope"}, 7]}, "junk"], "devig": None}],
        calibrations=[{"buckets": [{"count": "many"}, None], "brier": "n/a"}],
    )
    assert "only a title" in page
    assert "Nothing clears costs today." in page
    assert "No bucket has an observation yet" in page


# --- escaping and the content security policy ------------------------------


def test_text_from_reports_is_escaped_everywhere(paths):
    report = _scan_report()
    report.modeled = [_scan_row(ATTR_ATTACK, ATTACK, edge=0.03, clears=True, flags=(ATTACK,), why=ATTACK)]
    report.notes = [ATTACK]
    paths["scan"].write_text(scan.report_to_json(report), encoding="utf-8")
    compared = json.loads(paths["compare"].read_text())
    compared["series_ticker"] = ATTACK
    compared["events"][0]["home_team"] = ATTACK
    compared["events"][0]["sides"][0]["ticker"] = ATTR_ATTACK
    compared["unmatched"] = [{"event_ticker": ATTACK, "reason": ATTACK}]
    paths["compare"].write_text(json.dumps(compared), encoding="utf-8")
    calibrated = json.loads(paths["calibration"].read_text())
    calibrated["series"] = ATTACK
    calibrated["skipped"] = {ATTACK: 1}
    paths["calibration"].write_text(json.dumps(calibrated), encoding="utf-8")

    page = _build(paths, title=ATTACK)

    assert "<script>alert" not in page
    assert page.count("<script") == 1  # ours
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in page
    assert 'onmouseover="alert' not in page
    assert "&quot; onmouseover=&quot;alert(1)" in page


def _poison(value):
    """The same report with every string value replaced by an attack string."""
    if isinstance(value, dict):
        return {key: _poison(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_poison(item) for item in value]
    if isinstance(value, str):
        return ATTACK + ATTR_ATTACK
    return value


def test_no_string_field_anywhere_in_any_report_reaches_the_page_raw(paths, db_path):
    """A sweep, so a field added later is covered without anyone remembering to."""
    reports = {kind: json.loads(paths[kind].read_text()) for kind in ("scan", "compare", "calibration")}
    reports["calibration"]["skipped"] = {ATTACK + ATTR_ATTACK: 3}  # these keys are data too
    for kind, report in reports.items():
        paths[kind].write_text(json.dumps(_poison(report)), encoding="utf-8")
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE series_fees SET fee_type = ?, fee_multiplier = ?", (ATTACK, ATTR_ATTACK))
        conn.execute("UPDATE odds_captures SET sport_key = ?", (ATTACK,))
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("UPDATE markets SET series_ticker = ?", (ATTACK,))
        conn.execute("UPDATE price_snapshots SET series_ticker = ?, captured_at = ?", (ATTACK, ATTR_ATTACK))
        conn.execute("UPDATE series_fees SET series_ticker = ?", (ATTACK,))

    page = _build(paths)

    assert page.count("<script") == 1 and "<script>alert" not in page
    assert 'onmouseover="alert' not in page
    assert page.count("&lt;script&gt;") > 20  # the poison did reach the page, as text


def test_a_string_in_a_number_field_cannot_carry_markup():
    row = {"title": "t", "clears": True, "edge": "<b>9</b>", "ask": "<i>", "model": True, "contracts": "1e999"}
    page = _page(scan={"modeled": [row], "short_dated": []})
    assert "<b>9</b>" not in page and "<i>" not in page
    assert "inf" not in page.lower().replace("information", "")


def test_csp_allows_exactly_the_two_inline_blocks(paths):
    page = _build(paths)
    policy = re.search(r'http-equiv="Content-Security-Policy" content="([^"]*)"', page).group(1)
    policy = policy.replace("&#x27;", "'")
    assert "default-src 'none'" in policy
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
    for tag, directive in (("style", "style-src"), ("script", "script-src")):
        digest = base64.b64encode(hashlib.sha256(_inline(page, tag).encode("utf-8")).digest()).decode("ascii")
        assert f"{directive} 'sha256-{digest}'" in policy


def test_page_has_nothing_the_policy_would_block_or_that_reaches_the_network(paths):
    page = _build(paths)
    body = page.replace(_inline(page, "script"), "").replace(_inline(page, "style"), "")
    assert " style=" not in body  # inline styles are blocked by the policy
    assert not re.search(r"\son[a-z]+=", body)  # no inline event handlers
    assert " src=" not in body and "<link" not in body and "<iframe" not in body and "<form" not in body
    assert all(target.startswith("#") for target in re.findall(r'href="([^"]*)"', body))
    assert "http://" not in body and "https://" not in body
    script = _inline(page, "script")
    for forbidden in ("innerHTML", "eval(", "fetch(", "XMLHttpRequest", "document.write"):
        assert forbidden not in script


def test_the_script_and_style_never_change_with_the_data(paths):
    full = _build(paths)
    bare = _page()
    assert _inline(full, "script") == _inline(bare, "script")
    assert _inline(full, "style") == _inline(bare, "style")


# --- freshness --------------------------------------------------------------


def test_freshness_states():
    assert freshness("x", "2026-11-08T17:30:00Z", BUILT, 26).status == "good"
    stale = freshness("x", "2026-11-06T17:30:00Z", BUILT, 26)
    assert (stale.status, stale.word) == ("critical", "Stale") and "2 days" in stale.detail
    assert freshness("x", None, BUILT, 26).word == "Not supplied"
    assert freshness("x", "last tuesday", BUILT, 26).status == "warning"
    # A stamp from the future is two clocks disagreeing. It must not read as fresh.
    assert freshness("x", "2026-11-08T19:00:00Z", BUILT, 26).word == "Clock mismatch"


def test_stale_inputs_are_called_out_with_a_word_not_only_a_color(paths):
    page = _build(paths, built_at=BUILT + timedelta(days=3))
    assert page.count(">Stale<") >= 3  # scan, comparison, snapshots (chip and table)
    assert "Calibration KXNFLGAME" in page
    fresh_calibration = freshness("c", "2026-11-04T12:00:00Z", BUILT + timedelta(days=3), 8 * 24)
    assert fresh_calibration.status == "good"  # weekly run: 7 days is still inside 8


def test_times_are_shown_in_the_chosen_zone():
    assert fmt_time("2026-11-08T17:35:30Z", CHICAGO) == "Sun Nov 8, 11:35 AM CST"
    assert fmt_time("2026-07-04T05:05:00Z", ZoneInfo("America/New_York")) == "Sat Jul 4, 1:05 AM EDT"
    assert fmt_time("garbage", CHICAGO) == "–"


def test_scan_settlement_times_follow_the_selected_zone():
    """A non-Chicago --tz must not leave the report's preformatted Chicago string on screen."""
    report = json.loads(scan.report_to_json(_scan_report()))
    utc = render_page(DashboardData(built_at=BUILT, tz=ZoneInfo("UTC"), scan=report))
    assert "Mon Nov 9, 8:00 PM UTC" in utc
    assert "Thu Nov 12, 1:30 PM UTC" in utc
    assert "Tue Nov 10, 3:00 PM UTC" in utc
    assert "CST" not in utc
    # The column sorts on the raw instant, not the formatted text.
    assert 'data-v="2026-11-09T20:00:00Z"' in utc
    assert 'data-v="2026-11-10T15:00:00Z"' in utc

    chicago = render_page(DashboardData(built_at=BUILT, tz=CHICAGO, scan=report))
    assert "Mon Nov 9, 2:00 PM CST" in chicago
    assert "Thu Nov 12, 7:30 AM CST" in chicago
    assert "Tue Nov 10, 9:00 AM CST" in chicago
    assert "2026-11-09 14:00 CST" not in chicago


def test_scan_settlement_time_falls_back_when_the_instant_is_unusable():
    page = _page(
        scan={
            "modeled": [
                {
                    "title": "kept chicago",
                    "clears": True,
                    "settles_at": "not a time",
                    "settles_chicago": "2026-11-09 14:00 CST",
                }
            ],
            "econ_events": [{"label": "release", "settles_chicago": "2026-11-12 07:30 CST"}],
            "short_dated": [{"title": "no time at all", "ticker": "NONE"}],
        }
    )
    assert 'data-v="not a time">2026-11-09 14:00 CST' in page
    assert "2026-11-12 07:30 CST" in page
    assert '<td data-v="">–</td>' in page


def test_a_stale_gap_between_captures_is_flagged(paths):
    compared = json.loads(paths["compare"].read_text())
    compared["stale"] = True
    compared["gap_minutes"] = 190.0
    paths["compare"].write_text(json.dumps(compared), encoding="utf-8")
    page = _build(paths)
    assert "190 min apart" in page and ">STALE<" in page and "captures STALE" in page


# --- the large-edge caution -------------------------------------------------


def test_large_edges_get_a_caution_and_ordinary_ones_do_not(paths):
    assert "clearing sides show an edge over" not in _build(paths)
    assert 0.012 < LARGE_EDGE < 0.40
    paths["scan"].write_text(scan.report_to_json(_scan_report(edges=(0.40, 0.012, -0.1))), encoding="utf-8")
    page = _build(paths)
    assert "1 of the 2 clearing sides show an edge over 10 pp" in page
    assert "not an NWS error" in page
    assert "Student-t" in page
    # A large edge on a side that does not clear is not what the caution counts.
    paths["scan"].write_text(scan.report_to_json(_scan_report(edges=(0.02, 0.012, 0.9))), encoding="utf-8")
    assert "clearing sides show an edge over" not in _build(paths)


def test_the_caution_names_the_model_the_large_edges_came_from(paths):
    report = _scan_report(edges=(0.40, 0.012, -0.1))
    paths["scan"].write_text(scan.report_to_json(report), encoding="utf-8")
    weather_only = _build(paths)
    assert "not an NWS error" in weather_only and "The CPI ones are" not in weather_only
    payload = json.loads(scan.report_to_json(report))
    for row in payload["modeled"]:
        row["series_ticker"] = "KXCPIYOY"
    paths["scan"].write_text(json.dumps(payload), encoding="utf-8")
    cpi_only = _build(paths)
    assert "The CPI ones are the Cleveland Fed nowcast" in cpi_only and "not an NWS error" not in cpi_only


def test_bars_are_scaled_to_the_sides_that_clear_not_to_a_verify_gap(paths):
    """A 50-point VERIFY gap that does not clear must not shrink every ranked bar."""
    payload = json.loads(scan.report_to_json(_scan_report(edges=(0.04, 0.02, 0.50))))
    paths["scan"].write_text(json.dumps(payload), encoding="utf-8")
    page = _build(paths)
    table = page[page.index('id="scan-modeled"'):page.index("</table>", page.index('id="scan-modeled"'))]
    bars = re.findall(r'<path class="pos" d="M48\.0 2H([0-9.]+)A', table)
    # The +4.0 pp side is the largest that clears, so its bar is full: tip at 48 + 46, less the 3-unit corner.
    assert bars[0] == "91.0"
    assert bars[2] == "91.0"  # the 50-point side is clipped to a full bar, not drawn off the canvas


# --- small pieces -----------------------------------------------------------


def test_as_float_takes_numbers_and_numeric_strings_only():
    assert as_float("0.4500") == 0.45 and as_float(3) == 3.0
    for bad in (None, True, "abc", "", float("nan"), float("inf"), "1e999", [1], {"a": 1}):
        assert as_float(bad) is None


def test_edge_bar_direction_and_scale():
    assert 'class="pos"' in edge_bar(0.05, 0.10) and 'class="neg"' in edge_bar(-0.05, 0.10)
    assert "<path" not in edge_bar(None, 0.10) and "<path" not in edge_bar(0, 0.10)
    # Twice the scale is clipped to a full bar, not drawn off the canvas.
    assert edge_bar(0.50, 0.10) == edge_bar(0.10, 0.10)
    assert bar_scale([0.02, -0.07, None, "x"]) == pytest.approx(0.07)
    assert bar_scale([]) == 0.01 and bar_scale([0.001]) == 0.01


def test_reliability_diagram_plots_populated_buckets_inside_the_frame():
    buckets = [
        {"lower": 0.0, "upper": 0.5, "count": 0, "predicted": None, "actual": None},
        {"lower": 0.5, "upper": 1.0, "count": 9, "predicted": 1.7, "actual": -0.2, "actual_low": 0.0, "actual_high": 0.3},
    ]
    svg = reliability_svg(buckets)
    assert svg.count('class="pt"') == 1
    for value in re.findall(r'c[xy]="([0-9.]+)"', svg):
        assert 0 <= float(value) <= 440
    assert "9 contracts" in svg


# --- the database is read, never written ------------------------------------


def test_read_health_summarizes_without_touching_the_file(db_path):
    before = (db_path.read_bytes(), os.stat(db_path).st_mtime_ns)
    health = read_health(db_path)
    assert (db_path.read_bytes(), os.stat(db_path).st_mtime_ns) == before
    assert not list(db_path.parent.glob("kalshi.db-*"))  # no journal or WAL left behind
    assert (health.markets, health.settled, health.snapshots) == (3, 0, 3)
    (series,) = health.series
    assert (series.series_ticker, series.open_markets, series.snapshot_runs) == ("KXNFLGAME", 3, 1)
    assert (series.last_snapshot, series.fee_type) == (KALSHI_AT, "quadratic")
    (odds,) = health.odds
    assert (odds.sport_key, odds.captures, odds.last_capture) == ("americanfootball_nfl", 1, ODDS_AT)
    assert health.last_snapshot == KALSHI_AT


def test_read_health_counts_settled_markets(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE markets SET result = 'yes' WHERE ticker LIKE '%-CIN'")
    health = read_health(db_path)
    assert (health.settled, health.series[0].open_markets) == (1, 2)


def test_read_health_does_not_create_a_missing_database(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(DashboardError, match="does not exist"):
        read_health(missing)
    assert not missing.exists()


def test_read_health_rejects_files_that_are_not_ours(tmp_path):
    text = tmp_path / "notes.db"
    text.write_text("not a database", encoding="utf-8")
    with pytest.raises(DashboardError):
        read_health(text)
    other = tmp_path / "other.db"
    with sqlite3.connect(other) as conn:
        conn.execute("CREATE TABLE t (x)")
    with pytest.raises(DashboardError, match="no markets table"):
        read_health(other)


def test_read_health_copes_with_an_older_schema(tmp_path):
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as conn:
        conn.execute("CREATE TABLE markets (ticker TEXT, series_ticker TEXT, result TEXT)")
        conn.execute("INSERT INTO markets VALUES ('A-1', 'A', NULL), ('A-2', 'A', '')")
    health = read_health(old)
    assert (health.markets, health.settled, health.odds, health.schema_version) == (2, 0, (), None)


# --- loading reports --------------------------------------------------------


def test_load_report_rejects_the_wrong_kind_of_file(paths, tmp_path):
    with pytest.raises(DashboardError, match="does not look like a scan report"):
        load_report(paths["compare"], "scan")
    with pytest.raises(DashboardError, match="does not look like a compare report"):
        load_report(paths["calibration"], "compare")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(DashboardError, match="not valid JSON"):
        load_report(bad, "scan")
    bad.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(DashboardError, match="not a JSON object"):
        load_report(bad, "scan")
    with pytest.raises(DashboardError, match="cannot read"):
        load_report(tmp_path / "absent.json", "scan")


# --- writing the file -------------------------------------------------------


def test_write_atomic_replaces_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "out" / "dashboard.html"
    write_atomic(target, "one")
    write_atomic(target, "two")
    assert target.read_text(encoding="utf-8") == "two"
    assert [p.name for p in target.parent.iterdir()] == ["dashboard.html"]


def test_a_failed_write_keeps_the_old_page(tmp_path, monkeypatch):
    target = tmp_path / "dashboard.html"
    write_atomic(target, "old page")

    def boom(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(dashboard.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        write_atomic(target, "half a page")
    assert target.read_text(encoding="utf-8") == "old page"
    assert [p.name for p in tmp_path.iterdir()] == ["dashboard.html"]


# --- the command ------------------------------------------------------------


def test_main_writes_the_page(paths, tmp_path, capsys):
    out = tmp_path / "site" / "dashboard.html"
    code = main(
        [
            "--scan", str(paths["scan"]),
            "--compare", str(paths["compare"]),
            "--calibration", str(paths["calibration"]),
            "--db", str(paths["db"]),
            "--out", str(out),
            "--title", "Edge board",
        ]
    )  # fmt: skip
    assert code == 0
    assert "wrote" in capsys.readouterr().out
    page = out.read_text(encoding="utf-8")
    assert "<title>Edge board</title>" in page and "Minneapolis" in page


def test_main_needs_at_least_one_input_and_sane_options(paths, tmp_path, capsys):
    assert main(["--out", str(tmp_path / "x.html")]) == 2
    assert main(["--scan", str(paths["scan"]), "--stale-hours", "0"]) == 2
    assert main(["--scan", str(paths["scan"]), "--tz", "Mars/Olympus"]) == 2
    assert "unknown time zone" in capsys.readouterr().err
    assert not (tmp_path / "x.html").exists()


def test_main_with_a_bad_input_exits_2_and_leaves_the_old_page(paths, tmp_path, capsys):
    out = tmp_path / "dashboard.html"
    out.write_text("yesterday", encoding="utf-8")
    assert main(["--scan", str(paths["compare"]), "--out", str(out)]) == 2
    assert "does not look like a scan report" in capsys.readouterr().err
    assert main(["--db", str(tmp_path / "missing.db"), "--out", str(out)]) == 2
    assert out.read_text(encoding="utf-8") == "yesterday"
