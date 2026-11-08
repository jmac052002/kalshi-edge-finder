"""Turn an Odds API ``/odds`` payload into flat rows.

One event in the payload carries every bookmaker and every market for one
game. The store wants one row per (bookmaker, market, outcome) so that a
query can ask "what did Pinnacle say about the Bengals at 17:31Z" without
unpacking JSON. Prices are kept as the text the API sent (``"1.80"``) and
parsed with ``Decimal`` when they are used, so a parsing change can never
corrupt what was captured.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

SOURCE = "the_odds_api"


@dataclass(frozen=True)
class OddsCapture:
    """One run of the odds command, whether or not it found any lines."""

    source: str
    sport_key: str
    captured_at: str
    regions: str
    events: int
    rows: int

    def as_tuple(self) -> tuple:
        return (self.source, self.sport_key, self.captured_at, self.regions, self.events, self.rows)


@dataclass(frozen=True)
class OddsRow:
    source: str
    event_id: str
    sport_key: str
    commence_time: str
    home_team: str
    away_team: str
    bookmaker: str
    market: str
    outcome: str
    price_decimal: str
    book_updated: str | None
    captured_at: str

    def as_tuple(self) -> tuple:
        return (
            self.source,
            self.event_id,
            self.sport_key,
            self.commence_time,
            self.home_team,
            self.away_team,
            self.bookmaker,
            self.market,
            self.outcome,
            self.price_decimal,
            self.book_updated,
            self.captured_at,
        )


def parse_odds_payload(
    events: list, *, captured_at: str, markets: frozenset[str] = frozenset({"h2h"})
) -> list[OddsRow]:
    """Flatten the payload. Malformed pieces are logged and skipped, never guessed."""
    rows: list[OddsRow] = []
    for event in events:
        if not isinstance(event, dict):
            logger.warning("skipping non-object event in odds payload")
            continue
        header = _event_header(event)
        if header is None:
            continue
        event_id, sport_key, commence_time, home_team, away_team = header
        for book in event.get("bookmakers") or []:
            if not isinstance(book, dict) or not isinstance(book.get("key"), str):
                logger.warning("%s: skipping bookmaker with no key", event_id)
                continue
            book_key = book["key"]
            book_updated = book.get("last_update") if isinstance(book.get("last_update"), str) else None
            for market in book.get("markets") or []:
                if not isinstance(market, dict) or market.get("key") not in markets:
                    continue
                for outcome in market.get("outcomes") or []:
                    row = _outcome_row(
                        outcome,
                        event_id=event_id,
                        sport_key=sport_key,
                        commence_time=commence_time,
                        home_team=home_team,
                        away_team=away_team,
                        bookmaker=book_key,
                        market=str(market["key"]),
                        book_updated=book_updated,
                        captured_at=captured_at,
                    )
                    if row is not None:
                        rows.append(row)
    return rows


def _event_header(event: dict) -> tuple[str, str, str, str, str] | None:
    values = []
    for key in ("id", "sport_key", "commence_time", "home_team", "away_team"):
        value = event.get(key)
        if not isinstance(value, str) or not value.strip():
            logger.warning("skipping event %r: missing %s", event.get("id"), key)
            return None
        values.append(value.strip())
    return tuple(values)  # type: ignore[return-value]


def _outcome_row(outcome: object, **fields: str | None) -> OddsRow | None:
    if not isinstance(outcome, dict):
        return None
    name = outcome.get("name")
    price = outcome.get("price")
    if not isinstance(name, str) or not name.strip():
        logger.warning("%s/%s: outcome with no name", fields["event_id"], fields["bookmaker"])
        return None
    if isinstance(price, bool) or not isinstance(price, (int, float, str)):
        logger.warning("%s/%s/%s: no price", fields["event_id"], fields["bookmaker"], name)
        return None
    text = str(price).strip()
    try:
        value = Decimal(text)
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite() or value <= 1:
        logger.warning(
            "%s/%s/%s: %r is not decimal odds", fields["event_id"], fields["bookmaker"], name, price
        )
        return None
    return OddsRow(
        source=SOURCE,
        event_id=fields["event_id"],  # type: ignore[arg-type]
        sport_key=fields["sport_key"],  # type: ignore[arg-type]
        commence_time=fields["commence_time"],  # type: ignore[arg-type]
        home_team=fields["home_team"],  # type: ignore[arg-type]
        away_team=fields["away_team"],  # type: ignore[arg-type]
        bookmaker=fields["bookmaker"],  # type: ignore[arg-type]
        market=fields["market"],  # type: ignore[arg-type]
        outcome=name.strip(),
        price_decimal=text,
        book_updated=fields["book_updated"],
        captured_at=fields["captured_at"],  # type: ignore[arg-type]
    )
