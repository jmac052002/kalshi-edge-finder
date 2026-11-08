"""End to end: a fixture database with one Kalshi snapshot and one odds capture."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from kalshi_edge.compare import main, report_to_json, run
from kalshi_edge.fees import UnknownFeeTypeError
from kalshi_edge.parse import SeriesFee, parse_market, parse_snapshot
from kalshi_edge.sportsbook.parse import parse_odds_payload
from kalshi_edge.sportsbook.teams import DEFAULT_TABLE_PATH, TeamTable
from kalshi_edge.store import MarketStore

FIXTURE = Path(__file__).parent / "fixtures" / "odds_api_nfl.json"
KALSHI_AT = "2026-11-08T17:35:30Z"
ODDS_AT = "2026-11-08T17:31:02Z"
TABLE = TeamTable.load(DEFAULT_TABLE_PATH)


def _contract(ticker, event, yes_ask, no_ask, result=""):
    payload = {
        "ticker": ticker,
        "event_ticker": event,
        "status": "finalized" if result else "active",
        "result": result,
        "yes_bid_dollars": str(Decimal(yes_ask) - Decimal("0.02")) if yes_ask else None,
        "yes_ask_dollars": yes_ask,
        "no_bid_dollars": str(Decimal(no_ask) - Decimal("0.02")) if no_ask else None,
        "no_ask_dollars": no_ask,
        "notional_value_dollars": "1.0000",
    }
    market = parse_market(payload, series_ticker="KXNFLGAME", source="live", fetched_at=KALSHI_AT)
    snapshot = parse_snapshot(payload, series_ticker="KXNFLGAME", captured_at=KALSHI_AT)
    return market, snapshot


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "kalshi.db"
    rows = [
        _contract("KXNFLGAME-26NOV08CINATL-CIN", "KXNFLGAME-26NOV08CINATL", "0.4500", "0.5600"),
        _contract("KXNFLGAME-26NOV08CINATL-ATL", "KXNFLGAME-26NOV08CINATL", "0.5600", "0.4500"),
        # Night game: ticker date Nov 16, kickoff 01:15Z Nov 17. No Pinnacle line -> median.
        _contract("KXNFLGAME-26NOV16LACBAL-LAC", "KXNFLGAME-26NOV16LACBAL", "0.3300", "0.6900"),
        _contract("KXNFLGAME-26NOV16LACBAL-BAL", "KXNFLGAME-26NOV16LACBAL", "0.6800", "0.3400"),
        # No sportsbook game for this one.
        _contract("KXNFLGAME-26NOV08KCBUF-KC", "KXNFLGAME-26NOV08KCBUF", "0.5000", "0.5200"),
        # One-sided book: no ask to buy.
        _contract("KXNFLGAME-26NOV08KCBUF-BUF", "KXNFLGAME-26NOV08KCBUF", None, "0.5300"),
        # Already settled: must be ignored even though it is in the snapshot.
        _contract("KXNFLGAME-26NOV01DALNYG-DAL", "KXNFLGAME-26NOV01DALNYG", "0.9900", "0.0200", result="yes"),
    ]
    with MarketStore(path) as store:
        store.save_page([m for m, _ in rows], [s for _, s in rows])
        # An older snapshot that must not be used.
        old = parse_snapshot(
            {"ticker": "KXNFLGAME-26NOV08CINATL-CIN", "yes_ask_dollars": "0.9000"},
            series_ticker="KXNFLGAME",
            captured_at="2026-11-07T12:00:00Z",
        )
        store.save_page([], [old])
        store.save_odds(parse_odds_payload(json.loads(FIXTURE.read_text()), captured_at=ODDS_AT))
        store.save_series_fee(SeriesFee("KXNFLGAME", "quadratic", "1", KALSHI_AT))
    return path


def _side(report, ticker, side):
    for event in report.events:
        for row in event.sides:
            if row.ticker == ticker and row.side == side:
                return row
    raise AssertionError(f"{ticker} {side} not in report")


def test_report_matches_design_doc_worked_example(db_path):
    with MarketStore(db_path) as store:
        report = run(series_ticker="KXNFLGAME", store=store, table=TABLE, now="2026-11-08T17:36:00Z", contracts=10, budget=None)
        matches = store.conn.execute("SELECT event_ticker, event_id, kickoff_delta_s FROM line_matches ORDER BY 1").fetchall()

    assert report.kalshi_captured_at == KALSHI_AT and report.odds_captured_at == ODDS_AT
    assert report.gap_minutes == pytest.approx(4.47, abs=0.01)
    assert not report.stale
    assert report.open_contracts == 6  # the settled contract is not counted
    assert "quadratic x1" in report.fee_source
    assert [e.event_ticker for e in report.events] == ["KXNFLGAME-26NOV08CINATL", "KXNFLGAME-26NOV16LACBAL"]
    assert [(u.event_ticker, u.reason[:12]) for u in report.unmatched] == [("KXNFLGAME-26NOV08KCBUF", "Buffalo Bill")]

    cin = _side(report, "KXNFLGAME-26NOV08CINATL-CIN", "YES")
    assert cin.team == "Cincinnati Bengals"
    e = cin.evaluation
    # Design doc section 6.3: fee $0.18 on ten, cost 0.468, fair 0.4597 (power), edge -0.8pp, EV -$0.0083.
    assert e.fee_total == Decimal("0.18")
    assert e.cost == Decimal("0.468")
    assert e.fair_probability == pytest.approx(0.4597, abs=5e-5)
    assert cin.alt_fair == pytest.approx(0.4615, abs=5e-5)
    assert e.edge == pytest.approx(-0.0083, abs=1e-4)
    assert e.ev == pytest.approx(-0.0083, abs=1e-4)
    # NO on CIN is the Falcons at 0.56 with fair 1 - 0.4597.
    cin_no = _side(report, "KXNFLGAME-26NOV08CINATL-CIN", "NO")
    assert cin_no.evaluation.price == Decimal("0.56")
    assert cin_no.evaluation.fair_probability == pytest.approx(1 - 0.4597, abs=5e-5)
    assert report.events[0].reference == "pinnacle" and report.events[0].books == 3
    assert report.events[0].reference_updated == "2026-11-08T17:30:00Z"

    # Night game had no Pinnacle: median of DraftKings, FanDuel, BetMGM.
    night = report.events[1]
    assert night.reference == "median of 3 books" and night.reference_updated is None
    lac = _side(report, "KXNFLGAME-26NOV16LACBAL-LAC", "YES")
    assert 0.33 < lac.evaluation.fair_probability < 0.37

    assert [tuple(m) for m in matches] == [
        ("KXNFLGAME-26NOV08CINATL", "evt-cinatl", 13 * 3600),
        ("KXNFLGAME-26NOV16LACBAL", "evt-lacbal", 20 * 3600 + 15 * 60),
    ]


def test_budget_sizing_and_median_reference(db_path):
    with MarketStore(db_path) as store:
        report = run(
            series_ticker="KXNFLGAME", store=store, table=TABLE, now="2026-11-08T17:36:00Z",
            reference="median", devig_method="multiplicative", budget=Decimal(10), contracts=None,
        )
    cin = _side(report, "KXNFLGAME-26NOV08CINATL-CIN", "YES")
    assert cin.evaluation.contracts == 21  # $10 at 0.45 with the fee
    assert cin.evaluation.stake <= Decimal(10)
    assert report.events[0].reference == "median of 2 books"  # Pinnacle excluded from the median
    # Multiplicative primary, power alternate: the long shot looks better under multiplicative.
    assert cin.evaluation.fair_probability > cin.alt_fair


def test_main_prints_table_and_json_and_flags_stale(db_path, tmp_path, capsys):
    out_json = tmp_path / "report.json"
    code = main(["--series", "kxnflgame", "--db", str(db_path), "--contracts", "10", "--json", str(out_json), "--max-gap-minutes", "2"])
    out = capsys.readouterr().out
    assert code == 0
    assert "STALE" in out
    assert "KXNFLGAME-26NOV08CINATL  Cincinnati Bengals at Atlanta Falcons" in out
    assert " CIN      YES    0.45   10  0.0180  0.4680 0.4597 0.4615  -0.8pp  -0.0083   -1.8%" in out
    assert "skipped KXNFLGAME-26NOV08KCBUF-BUF YES: no ask to buy at" not in out  # unmatched events have no rows
    assert "unmatched (1):" in out
    assert "read this honestly:" in out
    data = json.loads(out_json.read_text())
    assert data["stale"] is True
    # Sides are ordered by ticker, so ATL comes first: 0.5600 + 0.018 fee.
    assert data["events"][0]["sides"][0]["evaluation"]["cost"] == "0.5780"
    assert len(data["caveats"]) == 4


def test_missing_data_is_explained(tmp_path, capsys):
    path = tmp_path / "empty.db"
    with MarketStore(path):
        pass
    assert main(["--series", "KXNFLGAME", "--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "no open-market snapshot" in out and "no sportsbook capture" in out
    assert "published default" in out
    assert main(["--series", "KXNFLGAME", "--db", str(tmp_path / "nope.db")]) == 2


def test_unknown_fee_type_fails_loudly(db_path):
    with MarketStore(db_path) as store:
        store.save_series_fee(SeriesFee("KXNFLGAME", "flat", "1", KALSHI_AT))
        with pytest.raises(UnknownFeeTypeError):
            run(series_ticker="KXNFLGAME", store=store, table=TABLE, now="x", contracts=1, budget=None)
    assert main(["--series", "KXNFLGAME", "--db", str(db_path)]) == 1


def test_report_json_roundtrip_has_strings_for_money(db_path):
    with MarketStore(db_path) as store:
        report = run(series_ticker="KXNFLGAME", store=store, table=TABLE, now="x", contracts=10, budget=None)
    data = json.loads(report_to_json(report))
    side = data["events"][0]["sides"][0]
    assert side["evaluation"]["fee_total"] == "0.18"  # ATL YES at 0.56 x 10: 0.07*10*0.2464 = 0.17248 -> $0.18
    assert isinstance(side["evaluation"]["ev"], float)
