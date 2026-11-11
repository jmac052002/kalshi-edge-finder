"""Unauthenticated Kalshi REST client.

Public market data lives at ``https://api.elections.kalshi.com/trade-api/v2``.
The host name says "elections"; Kalshi documents that this same base serves
every category. Market list, historical markets, trades, and candlesticks
all answer with no API key. This client never sets an Authorization header.

List endpoints are cursor-paginated. A response carries ``cursor``; pass it
back as the ``cursor`` query parameter. An empty cursor means the last page.

Authenticated tiers document token buckets and ``429`` responses with no
``Retry-After`` header. Public reads are not in that table, but a collector
that pages a whole series should still be a polite guest: wait between
requests, and back off if the server says it is busy or errors.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import quote, urlparse

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
USER_AGENT = "kalshi-edge-finder/0.1 (public market-data research; no api key)"

# Busy or transient. A 400/401/404 will not start working if we retry it.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class KalshiAPIError(Exception):
    """A public endpoint did not return a usable JSON object."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class KalshiClient:
    """GET JSON from the public Trade API, with pacing and retries.

    ``min_interval`` is the gap between requests (default a quarter second,
    about four requests per second). ``max_attempts`` includes the first try.
    Backoff starts at ``backoff_base`` seconds and doubles, capped, with
    jitter so a few collectors do not retry in lockstep. A ``Retry-After``
    header, when the server sends one, is a lower bound on that wait.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        session: requests.Session | None = None,
        min_interval: float = 0.25,
        timeout: float = 30.0,
        max_attempts: int = 5,
        backoff_base: float = 0.5,
        backoff_cap: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
        user_agent: str = USER_AGENT,
        accept: str = "application/json",
    ) -> None:
        if min_interval < 0:
            raise ValueError("min_interval must be >= 0")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.min_interval = min_interval
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._sleep = sleep
        self._clock = clock
        self._rng = rng
        self._last_request_at: float | None = None
        self._owns_session = session is None
        self.session = session if session is not None else requests.Session()
        # Overwrite rather than setdefault: requests installs its own
        # User-Agent, and a reused session must not keep an Authorization key.
        self.session.headers["Accept"] = accept
        self.session.headers["User-Agent"] = user_agent
        self.session.headers.pop("Authorization", None)

    def close(self) -> None:
        if self._owns_session:
            self.session.close()

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """GET ``path`` and return a JSON object, retrying transient failures."""
        payload = self._read_json(path, params)
        if not isinstance(payload, dict):
            raise KalshiAPIError(f"{path} returned {type(payload).__name__}, expected object")
        return payload

    def get_json_list(self, path: str, params: dict[str, Any] | None = None) -> list:
        """GET ``path`` and return a JSON array. Same pacing and retries as ``get_json``."""
        payload = self._read_json(path, params)
        if not isinstance(payload, list):
            raise KalshiAPIError(f"{path} returned {type(payload).__name__}, expected list")
        return payload

    def _url(self, path: str) -> str:
        """Resolve ``path`` against this client's host.

        An absolute URL is accepted only when its host is the client's host,
        so a forecast link returned by that host can be followed and a link
        to somewhere else cannot.
        """
        if path.startswith(("http://", "https://")):
            base_host = urlparse(self.base_url).netloc
            other = urlparse(path).netloc
            if other != base_host:
                raise KalshiAPIError(f"refusing URL on {other}; this client talks to {base_host}")
            return path
        return self.base_url + path

    def _read_json(self, path: str, params: dict[str, Any] | None) -> Any:
        url = self._url(path)
        query = {key: value for key, value in (params or {}).items() if value is not None}
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            self._pace()
            try:
                response = self.session.get(url, params=query, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = exc
                if attempt + 1 >= self.max_attempts:
                    break
                self._backoff(attempt, None, reason=str(exc))
                continue
            status = response.status_code
            if status in RETRYABLE_STATUS:
                last_error = KalshiAPIError(
                    f"{status} from {path}",
                    status_code=status,
                )
                if attempt + 1 >= self.max_attempts:
                    break
                self._backoff(attempt, _retry_after(response.headers), reason=f"HTTP {status}")
                continue
            if status >= 400:
                snippet = (response.text or "")[:200]
                raise KalshiAPIError(
                    f"{status} from {path}: {snippet}",
                    status_code=status,
                )
            try:
                return response.json()
            except ValueError as exc:
                raise KalshiAPIError(f"{path} did not return JSON") from exc
        raise KalshiAPIError(
            f"giving up on {path} after {self.max_attempts} attempts: {last_error}"
        )

    def iter_market_pages(
        self,
        *,
        series_ticker: str,
        status: str | None = None,
        historical: bool = False,
        limit: int = 200,
        max_pages: int = 500,
        event_ticker: str | None = None,
        min_close_ts: int | None = None,
        max_close_ts: int | None = None,
    ) -> Iterator[list[dict]]:
        """Yield each page of markets for one series.

        ``historical=True`` reads ``GET /historical/markets``. Kalshi moves
        markets there after they settle and age past the cutoff published by
        ``GET /historical/cutoff`` (``market_settled_ts``). The live
        ``GET /markets?status=settled`` list only has the newer settled
        markets. Callers that want a complete settled set read both.

        The status filter is a query convenience, not the row's lifecycle
        status. ``status=open`` matches markets whose ``status`` field is
        ``active``. ``status=settled`` matches ``finalized``. Historical
        markets are already settled, and that endpoint's filters do not take
        ``status``.

        ``min_close_ts`` and ``max_close_ts`` are Unix seconds. They are a
        server-side filter on ``close_time`` and are only sent to the live
        list. ``event_ticker`` narrows the page to one event.

        Stops when the cursor is empty or repeats. Repeating means the server
        handed us a page we already consumed; continuing would loop. Raises
        if ``max_pages`` is hit before the cursor ends, so a stuck cursor
        cannot silently truncate a backtest sample.
        """
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        path = "/historical/markets" if historical else "/markets"
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for page_number in range(1, max_pages + 1):
            params: dict[str, Any] = {"series_ticker": series_ticker, "limit": limit}
            # Historical filters are mutually exclusive and there is no status
            # parameter; these rows are already off the live book.
            if status and not historical:
                params["status"] = status
            if event_ticker and not historical:
                params["event_ticker"] = event_ticker
            if not historical:
                if min_close_ts is not None:
                    params["min_close_ts"] = int(min_close_ts)
                if max_close_ts is not None:
                    params["max_close_ts"] = int(max_close_ts)
            if cursor:
                params["cursor"] = cursor
            payload = self.get_json(path, params)
            markets = payload.get("markets") or []
            if not isinstance(markets, list):
                raise KalshiAPIError(f"{path} 'markets' was not a list")
            logger.info(
                "%s page %d: %d markets (historical=%s status=%s)",
                series_ticker,
                page_number,
                len(markets),
                historical,
                status,
            )
            yield markets
            next_cursor = _cursor(payload)
            if not next_cursor or next_cursor in seen_cursors:
                return
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise KalshiAPIError(
            f"{path} for {series_ticker} did not end after {max_pages} pages"
        )

    def list_series(self) -> list[dict]:
        """Every series from ``GET /series``, following ``cursor`` if present.

        The unpaginated response is one object whose ``series`` value is the
        whole catalog (category, tags, frequency, and the fee schedule). A
        cursor, if the exchange starts sending one, is followed the same way
        as a market list. A repeated cursor stops the loop.
        """
        collected: list[dict] = []
        cursor: str | None = None
        seen: set[str] = set()
        for page_number in range(1, 51):
            params: dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            payload = self.get_json("/series", params or None)
            batch = payload.get("series") or []
            if not isinstance(batch, list):
                raise KalshiAPIError("/series 'series' was not a list")
            collected.extend(item for item in batch if isinstance(item, dict))
            logger.info("series page %d: %d series", page_number, len(batch))
            next_cursor = _cursor(payload)
            if not next_cursor or next_cursor in seen:
                return collected
            seen.add(next_cursor)
            cursor = next_cursor
        raise KalshiAPIError("/series did not end after 50 pages")

    def iter_event_pages(
        self,
        *,
        status: str | None = "open",
        limit: int = 200,
        max_pages: int = 500,
    ) -> Iterator[list[dict]]:
        """Yield pages of events.

        Nested markets are not requested. An event carries ``category`` and
        ``series_ticker`` but not a close time; callers that need quotes
        fetch the series' markets separately. The cursor rules match
        ``iter_market_pages``.
        """
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        if not 1 <= limit <= 200:
            # 200 is the page size this scan was written against. The markets
            # list accepts 1000; this endpoint is kept at the size that was
            # actually used, so a larger guess cannot 400 the run.
            raise ValueError("limit must be between 1 and 200")
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for page_number in range(1, max_pages + 1):
            params: dict[str, Any] = {"limit": limit, "with_nested_markets": "false"}
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            payload = self.get_json("/events", params)
            events = payload.get("events") or []
            if not isinstance(events, list):
                raise KalshiAPIError("/events 'events' was not a list")
            logger.info("events page %d: %d events (status=%s)", page_number, len(events), status)
            yield events
            next_cursor = _cursor(payload)
            if not next_cursor or next_cursor in seen_cursors:
                return
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise KalshiAPIError(f"/events did not end after {max_pages} pages")

    def get_candlesticks(
        self,
        *,
        series_ticker: str,
        ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int = 60,
    ) -> list[dict]:
        """Return the raw candlesticks for one market between two Unix times.

        ``GET /series/{series}/markets/{ticker}/candlesticks`` is the public
        price history. Each candle carries the open/high/low/close of the
        yes bid, the yes ask, and the trade price for one period, keyed by
        the Unix second the period ended. ``period_interval`` is in minutes
        and the server only accepts 1, 60, or 1440.

        The server includes candles ending on or after ``start_ts`` and on or
        before ``end_ts``, so a candle whose ``end_period_ts`` is at or before
        the cutoff was entirely formed before it. That is what makes this a
        pre-event price and not a lookahead.
        """
        if period_interval not in (1, 60, 1440):
            raise ValueError("period_interval must be 1, 60, or 1440 minutes")
        if end_ts < start_ts:
            raise ValueError("end_ts must be >= start_ts")
        # Tickers come from our own database. Quoting keeps a malformed one
        # from turning into extra path segments instead of a clean 404.
        path = f"/series/{quote(series_ticker, safe='')}/markets/{quote(ticker, safe='')}/candlesticks"
        payload = self.get_json(
            path,
            {"start_ts": int(start_ts), "end_ts": int(end_ts), "period_interval": period_interval},
        )
        candles = payload.get("candlesticks") or []
        if not isinstance(candles, list):
            raise KalshiAPIError(f"{path} 'candlesticks' was not a list")
        return candles

    def get_series(self, series_ticker: str) -> dict:
        """``GET /series/{ticker}``: the series metadata, including its fee schedule.

        ``fee_type`` and ``fee_multiplier`` are what the exchange will
        actually charge on this series. Reading them each run is how the
        tool notices a schedule change instead of carrying a stale constant.
        """
        return self.get_json(f"/series/{quote(series_ticker, safe='')}")

    def _pace(self) -> None:
        if self.min_interval <= 0:
            return
        now = self._clock()
        if self._last_request_at is not None:
            remaining = self.min_interval - (now - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        self._last_request_at = now

    def _backoff(self, attempt: int, retry_after: float | None, *, reason: str) -> None:
        growth = self.backoff_base * (2**attempt)
        # rng() in [0, 1) spreads the wait across [50%, 100%] of the backoff.
        wait = min(self.backoff_cap, growth) * (0.5 + 0.5 * self._rng())
        if retry_after is not None:
            wait = max(wait, retry_after)
        wait = min(wait, self.backoff_cap)
        logger.warning("retrying after %s; sleeping %.2fs", reason, wait)
        if wait > 0:
            self._sleep(wait)
        self._last_request_at = self._clock()


def _cursor(payload: dict) -> str:
    raw = payload.get("cursor") or ""
    if isinstance(raw, str):
        return raw.strip()
    return str(raw).strip()


def _retry_after(headers: Any) -> float | None:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    raw = getter("Retry-After")
    if raw is None:
        raw = getter("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return seconds
