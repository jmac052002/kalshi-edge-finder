"""Client for The Odds API v4 (https://the-odds-api.com).

Why the key handling is as careful as it is: The Odds API authenticates with
an ``apiKey`` **query parameter**. That puts the secret in the URL, and a URL
shows up everywhere: ``requests`` exception messages, debug logs, tracebacks,
proxy logs. A client that lets any of those print unredacted has leaked the
key into whatever captures its output. So this module

* builds the request with ``params=`` and never formats the key into a
  string of its own;
* re-raises every ``requests`` failure as ``OddsApiError`` with the URL
  scrubbed and the original exception's context suppressed;
* installs a ``logging.Filter`` on this module's logger that scrubs every
  record, including ``%s`` arguments;
* exposes ``redact`` so callers that catch and print can scrub too.

Credits: a call to ``/sports`` is free; ``/odds`` costs ``markets x regions``
credits and the free tier has 500 a month. Every response carries
``x-requests-remaining`` and ``x-requests-used``. The client records them,
logs them, and refuses to spend when the remaining count is already below
``min_remaining``, so a scheduled run with a bug cannot burn the month.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Self

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.the-odds-api.com/v4"
USER_AGENT = "kalshi-edge-finder/0.1 (sportsbook comparison research)"
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

_KEY_PARAM_RE = re.compile(r"(apiKey=)[^&\s\"']+", re.IGNORECASE)
_PLACEHOLDER = "REDACTED"


def redact(text: str, api_key: str | None = None) -> str:
    """Scrub an API key out of free text.

    Two passes: the ``apiKey=...`` query form, which covers URLs we did not
    build, and the literal key value if the caller knows it, which covers a
    key that ended up in a message some other way. Keys shorter than eight
    characters are not substituted literally because the match would hit
    unrelated text.
    """
    scrubbed = _KEY_PARAM_RE.sub(rf"\1{_PLACEHOLDER}", text)
    if api_key and len(api_key) >= 8:
        scrubbed = scrubbed.replace(api_key, _PLACEHOLDER)
    return scrubbed


class RedactingFilter(logging.Filter):
    """Scrub the key from every record that passes through a logger."""

    def __init__(self, api_key: str | None = None) -> None:
        super().__init__()
        self.api_key = api_key

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(str(record.msg), self.api_key)
        if record.args:
            if isinstance(record.args, Mapping):
                record.args = {k: self._scrub(v) for k, v in record.args.items()}
            else:
                record.args = tuple(self._scrub(a) for a in record.args)
        return True

    def _scrub(self, value: Any) -> Any:
        return redact(value, self.api_key) if isinstance(value, str) else value


class OddsApiError(Exception):
    """A request failed. The message never contains the API key."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class CreditFloorError(OddsApiError):
    """Remaining credits are below the floor; the call was not made."""


@dataclass(frozen=True)
class Credits:
    """Usage counters from the last response, as the API reported them."""

    remaining: int | None
    used: int | None
    last_cost: int | None

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> Credits:
        return cls(
            remaining=_int_header(headers, "x-requests-remaining"),
            used=_int_header(headers, "x-requests-used"),
            last_cost=_int_header(headers, "x-requests-last"),
        )


class OddsApiClient:
    """GET JSON from The Odds API with pacing, retries, and key redaction.

    ``api_key`` is the only secret. The caller (the CLI) reads it from the
    environment; library code never does, so tests build clients with a
    dummy key and no monkeypatching.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        min_remaining: int = 50,
        max_attempts: int = 3,
        backoff_base: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip() or api_key != api_key.strip():
            raise ValueError("api_key must be a non-empty string with no surrounding whitespace")
        if min_remaining < 0:
            raise ValueError("min_remaining must be >= 0")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.min_remaining = min_remaining
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self._sleep = sleep
        self.credits = Credits(None, None, None)
        self._owns_session = session is None
        self.session = session if session is not None else requests.Session()
        self.session.headers["Accept"] = "application/json"
        self.session.headers["User-Agent"] = USER_AGENT
        self._filter = RedactingFilter(api_key)
        logger.addFilter(self._filter)

    def close(self) -> None:
        logger.removeFilter(self._filter)
        if self._owns_session:
            self.session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __repr__(self) -> str:
        # A repr in a traceback or a debugger must not print the key.
        return f"OddsApiClient(base_url={self.base_url!r}, api_key={_PLACEHOLDER})"

    def check_credits(self) -> Credits:
        """Read the usage counters with a free call to ``/sports``.

        Run this before anything that costs credits: the counters only
        arrive on a response, and this is the response that costs nothing.
        """
        self._get_json("/sports", {})
        return self.credits

    def get_odds(
        self,
        sport_key: str,
        *,
        regions: str,
        markets: str = "h2h",
    ) -> list[dict]:
        """``GET /sports/{sport}/odds`` as a list of event objects.

        Costs ``len(markets) x len(regions)`` credits. Decimal odds and ISO
        timestamps are requested so nothing downstream has to parse American
        odds or Unix seconds. Refuses to call when the last known remaining
        count is below the floor; call ``check_credits`` first so that count
        is known.
        """
        _validate_token(sport_key, "sport_key")
        _validate_token(regions, "regions")
        _validate_token(markets, "markets")
        if self.credits.remaining is not None and self.credits.remaining < self.min_remaining:
            raise CreditFloorError(
                f"{self.credits.remaining} credits remaining is below the floor of "
                f"{self.min_remaining}; not spending any"
            )
        payload = self._get_json(
            f"/sports/{sport_key}/odds",
            {
                "regions": regions,
                "markets": markets,
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
        )
        if not isinstance(payload, list):
            raise OddsApiError(f"/odds returned {type(payload).__name__}, expected a list")
        return payload

    def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        url = self.base_url + path
        query = dict(params)
        query["apiKey"] = self._api_key
        last_error: str | None = None
        for attempt in range(self.max_attempts):
            try:
                response = self.session.get(url, params=query, timeout=self.timeout)
            except requests.RequestException as exc:
                # The exception text carries the full URL, key included.
                last_error = redact(str(exc), self._api_key)
                if attempt + 1 < self.max_attempts:
                    self._backoff(attempt, last_error)
                    continue
                break
            self.credits = Credits.from_headers(response.headers)
            logger.info(
                "%s %s: credits used=%s remaining=%s last_cost=%s",
                path,
                response.status_code,
                self.credits.used,
                self.credits.remaining,
                self.credits.last_cost,
            )
            status = response.status_code
            if status in RETRYABLE_STATUS:
                last_error = f"HTTP {status} from {path}"
                if attempt + 1 < self.max_attempts:
                    self._backoff(attempt, last_error)
                    continue
                break
            if status == 401:
                raise OddsApiError("401 from the Odds API: the key was rejected", status_code=401)
            if status >= 400:
                snippet = redact((response.text or "")[:200], self._api_key)
                raise OddsApiError(f"{status} from {path}: {snippet}", status_code=status)
            try:
                return response.json()
            except ValueError:
                # ``from None``: the JSON error's context would otherwise be
                # printed with it, and that context can include the URL.
                raise OddsApiError(f"{path} did not return JSON") from None
        raise OddsApiError(
            f"giving up on {path} after {self.max_attempts} attempts: {last_error}"
        ) from None

    def _backoff(self, attempt: int, reason: str) -> None:
        wait = self.backoff_base * (2**attempt)
        logger.warning("retrying after %s; sleeping %.1fs", reason, wait)
        self._sleep(wait)


_TOKEN_RE = re.compile(r"[a-z0-9_]+(,[a-z0-9_]+)*")


def _validate_token(value: str, name: str) -> None:
    """Query values are lowercase keys like ``americanfootball_nfl`` or ``us,eu``."""
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be comma-separated lowercase keys, got {value!r}")


def _int_header(headers: Mapping[str, str], name: str) -> int | None:
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None
