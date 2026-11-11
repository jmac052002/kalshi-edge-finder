"""Build one HTML page from the reports the other commands already write.

``scan``, ``compare``, and ``calibrate`` print text for a terminal and can
also write ``--json``. This command reads those JSON files and the SQLite
database and writes a single self-contained ``dashboard.html``: the day's
scan, the sportsbook comparison, the calibration table with its reliability
diagram, and how fresh each of them was.

    python -m kalshi_edge.scan --json scan.json
    python -m kalshi_edge.dashboard --scan scan.json --out dashboard.html

Design choices, and the reason for each:

* **A file, not a server.** The data is produced by batch runs a few times a
  day. A page that is rebuilt after each run shows everything a live server
  would, and it has no listening port, no login, and nothing to keep
  patched. It can be opened from disk or sent as an attachment.
* **No network, enforced.** The page embeds its own styles and script and
  carries a Content-Security-Policy that allows only those two blocks, by
  hash. It cannot load a script from a CDN, and neither can anything that
  ends up in it by accident.
* **Offline and read-only.** This command makes no HTTP request. It opens the
  database with SQLite's read-only mode, so it cannot create the file,
  migrate it, or take a write lock while a collector is running.
* **It never recomputes an edge.** Break-evens, fees, and model
  probabilities come from the report that calculated them. A second
  implementation of that math here would be a second place to be wrong.
* **Every input is optional.** A missing report becomes a panel that says
  which command produces it. A report that is the wrong kind of file is an
  error, because silently rendering the wrong numbers is worse than stopping.
* **The output is written to a temporary file and renamed into place**, so a
  reader (or a bot about to send the file) never sees half a page.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from kalshi_edge.dashboard_render import DashboardData, Health, OddsHealth, SeriesHealth, render_page

logger = logging.getLogger(__name__)

DEFAULT_TZ = "America/Chicago"

# A key that only that report has. Enough to catch "--scan compare.json".
REPORT_MARKERS = {
    "scan": ("modeled", "short_dated"),
    "compare": ("events", "devig"),
    "calibration": ("buckets", "brier"),
}


class DashboardError(Exception):
    """An input that cannot be used. The message is safe to show as is."""


def load_report(path: Path, kind: str) -> dict[str, object]:
    """Read one ``--json`` report and check it is the kind it was passed as."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DashboardError(f"cannot read {kind} report {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise DashboardError(f"{kind} report {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise DashboardError(f"{kind} report {path} is not a JSON object")
    missing = [key for key in REPORT_MARKERS[kind] if key not in data]
    if missing:
        raise DashboardError(
            f"{path} does not look like a {kind} report (no {', '.join(repr(key) for key in missing)}); "
            f"was it written by the {kind} command's --json?"
        )
    return data


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _one(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return int(row[0] or 0) if row else 0


def read_health(path: Path) -> Health:
    """Summarize the database without writing to it.

    ``MarketStore`` is deliberately not used: opening one runs migrations and
    writes the schema version. A viewer has no business changing the file it
    is looking at. ``mode=ro`` also means a missing file is an error here
    instead of a new empty database. Tables are checked for first, so a file
    from an older schema shows what it has.
    """
    if not path.is_file():
        raise DashboardError(f"{path} does not exist; run collect first, or leave --db off")
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise DashboardError(f"cannot open {path} read-only: {exc}") from exc
    try:
        tables = _tables(conn)
        if "markets" not in tables:
            raise DashboardError(f"{path} has no markets table; is it a kalshi-edge-finder database?")
        version = None
        if "schema_meta" in tables:
            row = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
            version = str(row[0]) if row else None
        snapshots: dict[str, tuple[int, int, str | None]] = {}
        if "price_snapshots" in tables:
            for series, count, runs, last in conn.execute(
                "SELECT series_ticker, COUNT(*), COUNT(DISTINCT captured_at), MAX(captured_at) "
                "FROM price_snapshots GROUP BY series_ticker"
            ):
                snapshots[series] = (int(count), int(runs), last)
        fees: dict[str, tuple[str | None, str | None]] = {}
        if "series_fees" in tables:
            for series, fee_type, multiplier in conn.execute(
                "SELECT series_ticker, fee_type, fee_multiplier FROM series_fees"
            ):
                fees[series] = (fee_type, multiplier)
        series_rows = []
        # An unresolved market has result NULL; older rows may hold ''.
        for series, markets, settled in conn.execute(
            "SELECT series_ticker, COUNT(*), "
            "SUM(CASE WHEN result IS NOT NULL AND result != '' THEN 1 ELSE 0 END) "
            "FROM markets GROUP BY series_ticker ORDER BY series_ticker"
        ):
            count, runs, last = snapshots.get(series, (0, 0, None))
            fee_type, multiplier = fees.get(series, (None, None))
            settled_count = int(settled or 0)
            series_rows.append(
                SeriesHealth(
                    series_ticker=str(series),
                    markets=int(markets),
                    open_markets=int(markets) - settled_count,
                    settled=settled_count,
                    snapshots=count,
                    snapshot_runs=runs,
                    last_snapshot=last,
                    fee_type=fee_type,
                    fee_multiplier=multiplier,
                )
            )
        odds_rows = []
        if "odds_captures" in tables:
            for sport, captures, last in conn.execute(
                "SELECT sport_key, COUNT(*), MAX(captured_at) FROM odds_captures GROUP BY sport_key ORDER BY sport_key"
            ):
                detail = conn.execute(
                    "SELECT events, rows FROM odds_captures WHERE sport_key = ? AND captured_at = ? "
                    "ORDER BY rows DESC LIMIT 1",
                    (sport, last),
                ).fetchone()
                odds_rows.append(
                    OddsHealth(
                        sport_key=str(sport),
                        captures=int(captures),
                        last_capture=last,
                        last_events=int(detail[0]) if detail else None,
                        last_rows=int(detail[1]) if detail else None,
                    )
                )
        return Health(
            name=path.name,
            schema_version=version,
            markets=sum(s.markets for s in series_rows),
            settled=sum(s.settled for s in series_rows),
            snapshots=sum(s.snapshots for s in series_rows),
            pre_event_prices=_one(conn, "SELECT COUNT(*) FROM pre_event_prices") if "pre_event_prices" in tables else 0,
            series=tuple(series_rows),
            odds=tuple(odds_rows),
        )
    except sqlite3.Error as exc:
        raise DashboardError(f"cannot read {path}: {exc}") from exc
    finally:
        conn.close()


def write_atomic(path: Path, text: str) -> None:
    """Write next to the target, then rename over it.

    ``os.replace`` is atomic on one file system, so whoever opens ``path``
    gets the old page or the new one, never a partial write. The temporary
    file is created in the target's directory for that reason: a rename
    across file systems is a copy.
    """
    directory = path.parent if str(path.parent) else Path(".")
    directory.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def build(
    *,
    built_at: datetime,
    tz: ZoneInfo,
    scan_path: Path | None = None,
    compare_paths: Sequence[Path] = (),
    calibration_paths: Sequence[Path] = (),
    db_path: Path | None = None,
    stale_hours: float = 26.0,
    calibration_stale_days: float = 8.0,
    title: str = "Kalshi Edge Finder",
) -> str:
    """Load every input that was given and return the page."""
    scan: Mapping[str, object] | None = load_report(scan_path, "scan") if scan_path is not None else None
    data = DashboardData(
        built_at=built_at,
        tz=tz,
        scan=scan,
        compares=[load_report(path, "compare") for path in compare_paths],
        calibrations=[load_report(path, "calibration") for path in calibration_paths],
        health=read_health(db_path) if db_path is not None else None,
        stale_hours=stale_hours,
        calibration_stale_days=calibration_stale_days,
        title=title,
    )
    return render_page(data)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m kalshi_edge.dashboard",
        description=(
            "Write one self-contained HTML dashboard from the --json reports of scan, compare, and calibrate "
            "and a read-only look at the database. Makes no network request and never trades."
        ),
    )
    parser.add_argument("--scan", type=Path, help="JSON written by: python -m kalshi_edge.scan --json PATH")
    parser.add_argument(
        "--compare",
        type=Path,
        action="append",
        default=[],
        help="JSON written by compare --json. Repeat for more than one series.",
    )
    parser.add_argument(
        "--calibration",
        type=Path,
        action="append",
        default=[],
        help="JSON written by calibrate --json. Repeat for more than one series.",
    )
    parser.add_argument("--db", type=Path, help="SQLite file to summarize, opened read-only (e.g. data/kalshi.db).")
    parser.add_argument("--out", type=Path, default=Path("dashboard.html"), help="Output file (default: dashboard.html).")
    parser.add_argument(
        "--stale-hours",
        type=float,
        default=26.0,
        help="Flag the scan, comparison, and price snapshots as stale past this age (default: 26).",
    )
    parser.add_argument(
        "--calibration-stale-days",
        type=float,
        default=8.0,
        help="Flag a calibration report as stale past this age (default: 8; it is a weekly run).",
    )
    parser.add_argument("--tz", default=DEFAULT_TZ, help=f"IANA time zone for displayed times (default: {DEFAULT_TZ}).")
    parser.add_argument("--title", default="Kalshi Edge Finder", help="Page title.")
    args = parser.parse_args(argv)
    if args.stale_hours <= 0:
        parser.error("--stale-hours must be > 0")
    if args.calibration_stale_days <= 0:
        parser.error("--calibration-stale-days must be > 0")
    if args.scan is None and not args.compare and not args.calibration and args.db is None:
        parser.error("nothing to show: pass at least one of --scan, --compare, --calibration, --db")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 2
    try:
        tz = ZoneInfo(args.tz)
    except (ZoneInfoNotFoundError, ValueError):
        print(f"error: unknown time zone {args.tz!r}", file=sys.stderr)
        return 2
    try:
        page = build(
            built_at=datetime.now(UTC),
            tz=tz,
            scan_path=args.scan,
            compare_paths=args.compare,
            calibration_paths=args.calibration,
            db_path=args.db,
            stale_hours=args.stale_hours,
            calibration_stale_days=args.calibration_stale_days,
            title=args.title,
        )
    except DashboardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        write_atomic(args.out, page)
    except OSError as exc:
        print(f"error: cannot write {args.out}: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {args.out} ({len(page.encode('utf-8')) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
