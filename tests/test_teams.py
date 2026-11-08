"""Alias table integrity and event-ticker parsing."""

from datetime import date

import pytest

from kalshi_edge.sportsbook.teams import (
    DEFAULT_TABLE_PATH,
    AmbiguousTickerError,
    TeamTable,
    TickerParseError,
    UnknownTeamCodeError,
    parse_event_ticker,
)


@pytest.fixture(scope="module")
def table():
    return TeamTable.load(DEFAULT_TABLE_PATH)


def test_checked_in_table_has_32_teams_and_no_code_collisions(table):
    assert table.sport_key == "americanfootball_nfl"
    assert len(table.name_to_codes) == 32
    assert len(set(table.name_to_codes)) == 32
    codes = [code for codes in table.name_to_codes.values() for code in codes]
    assert len(codes) == len(set(codes))
    assert all(code.isupper() and 2 <= len(code) <= 4 for code in codes)
    assert table.name("LA") == table.name("LAR") == "Los Angeles Rams"


@pytest.mark.parametrize(
    ("ticker", "expected_codes", "expected_date"),
    [
        ("KXNFLGAME-26NOV08CINATL", ("CIN", "ATL"), date(2026, 11, 8)),
        ("KXNFLGAME-26NOV16LACBAL", ("LAC", "BAL"), date(2026, 11, 16)),
        ("KXNFLGAME-26OCT05LACHI", ("LA", "CHI"), date(2026, 10, 5)),  # LA+CHI, not LAC+HI
        ("KXNFLGAME-26OCT05LACLA", ("LAC", "LA"), date(2026, 10, 5)),
        ("KXNFLGAME-26OCT05NOLA", ("NO", "LA"), date(2026, 10, 5)),
        ("KXNFLGAME-26AUG06CARARI", ("CAR", "ARI"), date(2026, 8, 6)),
    ],
)
def test_real_tickers_parse(table, ticker, expected_codes, expected_date):
    parsed = parse_event_ticker(ticker, table)
    assert parsed.codes == expected_codes
    assert parsed.game_date == expected_date
    assert parsed.series_ticker == "KXNFLGAME"
    assert parsed.teams == {table.name(expected_codes[0]), table.name(expected_codes[1])}


def test_side_codes_resolve_an_otherwise_ambiguous_split():
    table = TeamTable.from_mapping(
        {"sport_key": "x", "teams": {"Alpha": ["AB"], "Beta": ["CDE"], "Gamma": ["ABC"], "Delta": ["DE"]}}
    )
    with pytest.raises(AmbiguousTickerError):
        parse_event_ticker("S-26NOV08ABCDE", table)
    assert parse_event_ticker("S-26NOV08ABCDE", table, side_codes=frozenset({"AB", "CDE"})).codes == ("AB", "CDE")
    assert parse_event_ticker("S-26NOV08ABCDE", table, side_codes=frozenset({"ABC"})).codes == ("ABC", "DE")


@pytest.mark.parametrize(
    ("ticker", "error"),
    [
        ("KXNFLGAME-26NOV08CINXXX", UnknownTeamCodeError),
        ("KXNFLGAME-26NOV08CIN", TickerParseError),
        ("KXNFLGAME-26XYZ08CINATL", TickerParseError),
        ("KXNFLGAME-26NOV31CINATL", TickerParseError),
        ("KXNFLGAME-26NOV08CINCIN", TickerParseError),
        ("KXNFLANYTD-26NOV08CINATL-J-SMITH", TickerParseError),
        ("", TickerParseError),
    ],
)
def test_malformed_tickers_are_rejected(table, ticker, error):
    with pytest.raises(error):
        parse_event_ticker(ticker, table)


def test_table_validation():
    with pytest.raises(ValueError, match="maps to both"):
        TeamTable.from_mapping({"sport_key": "x", "teams": {"A": ["AA"], "B": ["AA"]}})
    with pytest.raises(ValueError, match="2-4 uppercase"):
        TeamTable.from_mapping({"sport_key": "x", "teams": {"A": ["a"]}})
    with pytest.raises(ValueError):
        TeamTable.from_mapping({"teams": {"A": ["AA"]}})
    with pytest.raises(ValueError, match="not found"):
        TeamTable.load(DEFAULT_TABLE_PATH.with_name("missing.json"))
