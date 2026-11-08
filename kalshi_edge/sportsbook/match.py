"""Tie each Kalshi game event to exactly one sportsbook game.

The rule is deterministic and strict on purpose. A Kalshi event ticker gives
a game date (US Eastern) and two team codes; the alias table turns the codes
into the names the sportsbook prints. A sportsbook event is a candidate when
it has the same two teams, in either order, and its kickoff falls on that
date in US Eastern time. Exactly one candidate is a match. Zero is reported
as unmatched, two or more as ambiguous, and neither is guessed at.

Why the date and not just the teams: two teams meet twice a season, and the
odds feed can carry next week's game beside this week's.

Why US Eastern and not UTC: a Sunday night kickoff at 8:20 pm Eastern is
already Monday in UTC. The ticker says Sunday. Comparing UTC dates would
miss every prime-time game. ``close_time`` is never consulted: on an open
market it can sit days after kickoff (``KXNFLGAME-26NOV16LACBAL`` closed on
the 19th), so it is not a kickoff proxy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from kalshi_edge.sportsbook.teams import TeamTable, TickerParseError, parse_event_ticker

METHOD = "ticker_codes_v1"
EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class KalshiEvent:
    """One game on Kalshi: the event ticker and the code on each of its contracts."""

    event_ticker: str
    side_codes: frozenset[str]


@dataclass(frozen=True)
class SportsbookEvent:
    event_id: str
    sport_key: str
    commence_time: str
    home_team: str
    away_team: str

    @property
    def teams(self) -> frozenset[str]:
        return frozenset({self.home_team, self.away_team})

    def kickoff(self) -> datetime:
        return parse_iso_utc(self.commence_time)

    def local_date(self) -> date:
        return self.kickoff().astimezone(EASTERN).date()


@dataclass(frozen=True)
class LineMatch:
    event_ticker: str
    event: SportsbookEvent
    game_date: date
    codes: tuple[str, str]
    kickoff_delta_s: int
    method: str = METHOD


@dataclass(frozen=True)
class Unmatched:
    event_ticker: str
    reason: str


@dataclass
class MatchResult:
    matches: list[LineMatch] = field(default_factory=list)
    unmatched: list[Unmatched] = field(default_factory=list)


def parse_iso_utc(text: str) -> datetime:
    """Parse the provider's ISO-8601 UTC timestamp (``2026-11-08T18:00:00Z``)."""
    cleaned = text.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    parsed = datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def match_events(
    kalshi_events: list[KalshiEvent],
    sportsbook_events: list[SportsbookEvent],
    table: TeamTable,
) -> MatchResult:
    result = MatchResult()
    by_teams: dict[frozenset[str], list[SportsbookEvent]] = {}
    for event in sportsbook_events:
        if event.sport_key != table.sport_key:
            continue
        by_teams.setdefault(event.teams, []).append(event)

    for kalshi in sorted(kalshi_events, key=lambda e: e.event_ticker):
        try:
            parsed = parse_event_ticker(kalshi.event_ticker, table, side_codes=kalshi.side_codes)
        except TickerParseError as exc:
            result.unmatched.append(Unmatched(kalshi.event_ticker, str(exc)))
            continue
        same_teams = by_teams.get(parsed.teams, [])
        candidates = [e for e in same_teams if e.local_date() == parsed.game_date]
        if len(candidates) == 1:
            event = candidates[0]
            midnight = datetime.combine(parsed.game_date, time(), tzinfo=EASTERN)
            delta = int((event.kickoff() - midnight).total_seconds())
            result.matches.append(
                LineMatch(
                    event_ticker=kalshi.event_ticker,
                    event=event,
                    game_date=parsed.game_date,
                    codes=parsed.codes,
                    kickoff_delta_s=delta,
                )
            )
        elif not candidates:
            names = " vs ".join(sorted(parsed.teams))
            if same_teams:
                dates = ", ".join(sorted(e.local_date().isoformat() for e in same_teams))
                reason = f"{names}: sportsbook has that game on {dates}, ticker says {parsed.game_date}"
            else:
                reason = f"{names}: no sportsbook game with these teams"
            result.unmatched.append(Unmatched(kalshi.event_ticker, reason))
        else:
            ids = ", ".join(sorted(e.event_id for e in candidates))
            result.unmatched.append(
                Unmatched(kalshi.event_ticker, f"ambiguous: {len(candidates)} sportsbook games match ({ids})")
            )
    return result
