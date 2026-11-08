"""Flattening the Odds API payload into rows."""

import json
from pathlib import Path

from kalshi_edge.sportsbook.parse import parse_odds_payload

FIXTURE = Path(__file__).parent / "fixtures" / "odds_api_nfl.json"


def test_fixture_flattens_to_h2h_rows_only():
    rows = parse_odds_payload(json.loads(FIXTURE.read_text()), captured_at="2026-11-08T17:31:02Z")
    # 3 books x 2 outcomes for CIN@ATL, 3 x 2 for LAC@BAL; the DraftKings spread market is skipped.
    assert len(rows) == 12
    assert {row.market for row in rows} == {"h2h"}
    pinnacle = [row for row in rows if row.bookmaker == "pinnacle"]
    assert {(row.outcome, row.price_decimal) for row in pinnacle} == {
        ("Atlanta Falcons", "1.8"),
        ("Cincinnati Bengals", "2.1"),
    }
    assert pinnacle[0].book_updated == "2026-11-08T17:30:00Z"
    assert pinnacle[0].captured_at == "2026-11-08T17:31:02Z"
    assert pinnacle[0].home_team == "Atlanta Falcons" and pinnacle[0].away_team == "Cincinnati Bengals"


def test_malformed_pieces_are_skipped_not_guessed(caplog):
    events = [
        "not an object",
        {"id": "no-teams", "sport_key": "x", "commence_time": "t"},
        {
            "id": "e1",
            "sport_key": "americanfootball_nfl",
            "commence_time": "2026-11-08T18:00:00Z",
            "home_team": "A",
            "away_team": "B",
            "bookmakers": [
                {"markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": 1.5}]}]},
                {
                    "key": "book",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "A", "price": 1.5},
                                {"name": "B", "price": "0.9"},
                                {"name": "", "price": 2},
                                {"name": "C", "price": True},
                                {"name": "D"},
                            ],
                        }
                    ],
                },
            ],
        },
    ]
    rows = parse_odds_payload(events, captured_at="2026-11-08T17:31:02Z")
    assert [(r.bookmaker, r.outcome, r.price_decimal) for r in rows] == [("book", "A", "1.5")]
    assert "not decimal odds" in caplog.text
