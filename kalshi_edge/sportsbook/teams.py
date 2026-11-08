"""Team alias table and Kalshi event-ticker parsing.

A game-winner event ticker looks like ``KXNFLGAME-26NOV08CINATL``: the
series, a dash, the game date as ``YYMMMDD``, then the two team codes run
together with nothing between them. The codes are not fixed width. Kalshi
uses ``GB``, ``KC``, ``LA``, ``LV``, ``NE``, ``NO``, ``SF`` and ``TB`` beside
three-letter codes, and the Rams have appeared as both ``LA`` and ``LAR`` in
the same season. So ``LACHI`` could be ``LA``+``CHI`` or ``LAC``+``HI``, and
the only way to split it is to know which codes exist.

That knowledge is the checked-in alias table ``config/teams/nfl.json``,
keyed by the full names The Odds API prints. The parser tries every split
and keeps the ones where both halves are known codes. When the caller also
passes the codes from the event's market tickers (``...-CIN``, ``...-ATL``),
those settle any split that is still ambiguous. Anything that does not
resolve to exactly one pair is an error, not a guess.

The ticker date is the game's date in US Eastern time. That was checked
against a Thursday night game (``KXNFLGAME-26AUG06CARARI``, kickoff 8 pm
Eastern on 6 August, which is already 7 August in UTC).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DEFAULT_TABLE_PATH = Path(__file__).resolve().parents[2] / "config" / "teams" / "nfl.json"

_EVENT_TICKER_RE = re.compile(
    r"(?P<series>[A-Z0-9]+)-(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})(?P<codes>[A-Z]{4,8})"
)
_CODE_RE = re.compile(r"[A-Z]{2,4}")
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}  # fmt: skip


class TickerParseError(ValueError):
    """The event ticker does not name one game in a known way."""


class UnknownTeamCodeError(TickerParseError):
    """A code in the ticker is not in the alias table."""


class AmbiguousTickerError(TickerParseError):
    """More than one pair of known codes fits the ticker."""


@dataclass(frozen=True)
class TeamTable:
    """Two-way mapping between Kalshi codes and sportsbook team names."""

    sport_key: str
    code_to_name: dict[str, str]
    name_to_codes: dict[str, tuple[str, ...]]

    @classmethod
    def from_mapping(cls, data: dict) -> TeamTable:
        sport_key = data.get("sport_key")
        teams = data.get("teams")
        if not isinstance(sport_key, str) or not sport_key:
            raise ValueError("team table needs a sport_key string")
        if not isinstance(teams, dict) or not teams:
            raise ValueError("team table needs a non-empty 'teams' object")
        code_to_name: dict[str, str] = {}
        name_to_codes: dict[str, tuple[str, ...]] = {}
        for name, codes in teams.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("team names must be non-empty strings")
            if not isinstance(codes, list) or not codes:
                raise ValueError(f"{name}: codes must be a non-empty list")
            cleaned = []
            for code in codes:
                if not isinstance(code, str) or _CODE_RE.fullmatch(code) is None:
                    raise ValueError(f"{name}: code {code!r} must be 2-4 uppercase letters")
                if code in code_to_name:
                    raise ValueError(f"code {code} maps to both {code_to_name[code]} and {name}")
                code_to_name[code] = name
                cleaned.append(code)
            name_to_codes[name] = tuple(cleaned)
        return cls(sport_key=sport_key, code_to_name=code_to_name, name_to_codes=name_to_codes)

    @classmethod
    def load(cls, path: Path = DEFAULT_TABLE_PATH) -> TeamTable:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"team table not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"team table is not JSON: {path}") from exc
        if not isinstance(data, dict):
            # Bad config data, not a programming error, so ValueError like the rest.
            raise ValueError("team table must be a JSON object")  # noqa: TRY004
        return cls.from_mapping(data)

    def name(self, code: str) -> str:
        try:
            return self.code_to_name[code]
        except KeyError:
            raise UnknownTeamCodeError(f"unknown team code {code!r}") from None


@dataclass(frozen=True)
class ParsedEventTicker:
    event_ticker: str
    series_ticker: str
    game_date: date
    codes: tuple[str, str]
    teams: frozenset[str]


def parse_event_ticker(
    event_ticker: str, table: TeamTable, *, side_codes: frozenset[str] = frozenset()
) -> ParsedEventTicker:
    """Split ``KXNFLGAME-26NOV08CINATL`` into its date and two known teams.

    ``side_codes`` are the final segments of the event's market tickers,
    one per contract. They are not required, but they are what resolves a
    ticker whose code string has two valid splits.
    """
    match = _EVENT_TICKER_RE.fullmatch(event_ticker.strip())
    if match is None:
        raise TickerParseError(f"not a game event ticker: {event_ticker!r}")
    month = _MONTHS.get(match["mon"])
    if month is None:
        raise TickerParseError(f"{event_ticker!r}: unknown month {match['mon']!r}")
    try:
        game_date = date(2000 + int(match["yy"]), month, int(match["dd"]))
    except ValueError as exc:
        raise TickerParseError(f"{event_ticker!r}: {exc}") from None

    codes = match["codes"]
    splits = [
        (codes[:i], codes[i:])
        for i in range(2, len(codes) - 1)
        if codes[:i] in table.code_to_name and codes[i:] in table.code_to_name
    ]
    if side_codes:
        narrowed = [pair for pair in splits if side_codes <= set(pair)]
        if narrowed:
            splits = narrowed
    if not splits:
        raise UnknownTeamCodeError(f"{event_ticker!r}: no known pair of team codes in {codes!r}")
    if len(splits) > 1:
        raise AmbiguousTickerError(f"{event_ticker!r}: {codes!r} splits as {splits}")
    (first, second) = splits[0]
    if table.name(first) == table.name(second):
        raise TickerParseError(f"{event_ticker!r}: both codes name {table.name(first)}")
    return ParsedEventTicker(
        event_ticker=event_ticker.strip(),
        series_ticker=match["series"],
        game_date=game_date,
        codes=(first, second),
        teams=frozenset({table.name(first), table.name(second)}),
    )
