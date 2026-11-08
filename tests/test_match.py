"""Kalshi event to sportsbook game matching."""

from datetime import date

from kalshi_edge.sportsbook.match import KalshiEvent, SportsbookEvent, match_events
from kalshi_edge.sportsbook.teams import DEFAULT_TABLE_PATH, TeamTable

TABLE = TeamTable.load(DEFAULT_TABLE_PATH)


def _game(event_id, commence, home, away, sport="americanfootball_nfl"):
    return SportsbookEvent(event_id=event_id, sport_key=sport, commence_time=commence, home_team=home, away_team=away)


def test_one_candidate_matches_in_either_home_away_order():
    kalshi = [KalshiEvent("KXNFLGAME-26NOV08CINATL", frozenset({"CIN", "ATL"}))]
    for home, away in (("Atlanta Falcons", "Cincinnati Bengals"), ("Cincinnati Bengals", "Atlanta Falcons")):
        result = match_events(kalshi, [_game("g1", "2026-11-08T18:00:00Z", home, away)], TABLE)
        assert [m.event.event_id for m in result.matches] == ["g1"]
        assert result.matches[0].game_date == date(2026, 11, 8)
        assert result.matches[0].kickoff_delta_s == 13 * 3600  # 1 pm Eastern (EST)
        assert result.unmatched == []


def test_night_game_matches_previous_eastern_date_without_close_time():
    # KXNFLGAME-26NOV16LACBAL closed 2026-11-19 on Kalshi; the matcher never sees close_time.
    kalshi = [KalshiEvent("KXNFLGAME-26NOV16LACBAL", frozenset({"LAC", "BAL"}))]
    game = _game("g2", "2026-11-17T01:15:00Z", "Baltimore Ravens", "Los Angeles Chargers")
    result = match_events(kalshi, [game], TABLE)
    assert [m.event.event_id for m in result.matches] == ["g2"]
    assert result.matches[0].kickoff_delta_s == 20 * 3600 + 15 * 60  # 8:15 pm Eastern on the 16th


def test_same_teams_two_weeks_apart_resolve_by_ticker_date():
    kalshi = [KalshiEvent("KXNFLGAME-26NOV08CINATL", frozenset({"CIN", "ATL"}))]
    games = [
        _game("this-week", "2026-11-08T18:00:00Z", "Atlanta Falcons", "Cincinnati Bengals"),
        _game("rematch", "2026-11-22T18:00:00Z", "Cincinnati Bengals", "Atlanta Falcons"),
    ]
    result = match_events(kalshi, games, TABLE)
    assert [m.event.event_id for m in result.matches] == ["this-week"]


def test_zero_candidates_and_ambiguous_candidates_are_reported_not_guessed():
    kalshi = [
        KalshiEvent("KXNFLGAME-26NOV08CINATL", frozenset({"CIN", "ATL"})),
        KalshiEvent("KXNFLGAME-26NOV08DALNYG", frozenset({"DAL", "NYG"})),
        KalshiEvent("KXNFLGAME-26NOV08KCBUF", frozenset({"KC", "BUF"})),
        KalshiEvent("KXNFLGAME-26NOV08XXXYYY", frozenset({"XXX", "YYY"})),
        KalshiEvent("KXNFLGAME-26NOV08SEASF", frozenset({"SEA", "SF"})),
    ]
    games = [
        _game("wrong-day", "2026-11-09T18:00:00Z", "Atlanta Falcons", "Cincinnati Bengals"),
        _game("dup-a", "2026-11-08T18:00:00Z", "Dallas Cowboys", "New York Giants"),
        _game("dup-b", "2026-11-08T21:25:00Z", "New York Giants", "Dallas Cowboys"),
        _game("other-sport", "2026-11-08T18:00:00Z", "Seattle Seahawks", "San Francisco 49ers", sport="americanfootball_ncaaf"),
    ]
    result = match_events(kalshi, games, TABLE)
    assert result.matches == []
    reasons = {u.event_ticker: u.reason for u in result.unmatched}
    assert "ticker says 2026-11-08" in reasons["KXNFLGAME-26NOV08CINATL"]
    assert reasons["KXNFLGAME-26NOV08DALNYG"].startswith("ambiguous: 2")
    assert "no sportsbook game" in reasons["KXNFLGAME-26NOV08KCBUF"]
    assert "no known pair" in reasons["KXNFLGAME-26NOV08XXXYYY"]
    assert "no sportsbook game" in reasons["KXNFLGAME-26NOV08SEASF"]
