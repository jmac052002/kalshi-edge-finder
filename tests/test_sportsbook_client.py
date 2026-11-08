"""Odds API client: credit headers, the spending floor, and key redaction."""

import logging

import pytest
import requests

from kalshi_edge.sportsbook.client import (
    CreditFloorError,
    OddsApiClient,
    OddsApiError,
    redact,
)

KEY = "sk-test-0123456789abcdef"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


def _headers(remaining, used, last=1):
    return {"x-requests-remaining": str(remaining), "x-requests-used": str(used), "x-requests-last": str(last)}


def test_redact_scrubs_query_param_and_literal_key():
    text = f"https://api.the-odds-api.com/v4/sports?apiKey={KEY}&regions=us and again {KEY}"
    scrubbed = redact(text, KEY)
    assert KEY not in scrubbed
    assert "apiKey=REDACTED" in scrubbed
    assert redact("apikey=abc&x=1") == "apikey=REDACTED&x=1"


def test_key_is_sent_as_a_param_not_formatted_into_the_url():
    session = FakeSession([FakeResponse(200, [], _headers(480, 20))])
    client = OddsApiClient(KEY, session=session, sleep=lambda s: None)
    client.check_credits()
    call = session.calls[0]
    assert KEY not in call["url"]
    assert call["params"]["apiKey"] == KEY
    assert client.credits.remaining == 480 and client.credits.used == 20
    assert "REDACTED" in repr(client) and KEY not in repr(client)


def test_refuses_to_spend_below_the_floor():
    session = FakeSession([FakeResponse(200, [], _headers(10, 490))])
    client = OddsApiClient(KEY, session=session, min_remaining=50, sleep=lambda s: None)
    client.check_credits()
    with pytest.raises(CreditFloorError):
        client.get_odds("americanfootball_nfl", regions="us")
    assert len(session.calls) == 1  # the free call only


def test_get_odds_requests_decimal_iso_and_returns_list():
    session = FakeSession([FakeResponse(200, [{"id": "e1"}], _headers(479, 21, 2))])
    client = OddsApiClient(KEY, session=session, sleep=lambda s: None)
    events = client.get_odds("americanfootball_nfl", regions="us,eu", markets="h2h")
    assert events == [{"id": "e1"}]
    params = session.calls[0]["params"]
    assert params["oddsFormat"] == "decimal" and params["dateFormat"] == "iso"
    assert session.calls[0]["url"].endswith("/sports/americanfootball_nfl/odds")
    assert client.credits.last_cost == 2
    with pytest.raises(ValueError):
        client.get_odds("nfl; drop", regions="us")


def test_connection_error_text_and_logs_never_contain_the_key(caplog):
    url = f"https://api.the-odds-api.com/v4/sports?apiKey={KEY}"
    session = FakeSession(
        [requests.ConnectionError(f"failed {url}"), requests.ConnectionError(f"failed again {url}")]
    )
    client = OddsApiClient(KEY, session=session, max_attempts=2, sleep=lambda s: None)
    with (
        caplog.at_level(logging.WARNING, logger="kalshi_edge.sportsbook.client"),
        pytest.raises(OddsApiError) as info,
    ):
        client.check_credits()
    assert KEY not in str(info.value)
    assert "REDACTED" in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert KEY not in caplog.text
    assert "retrying" in caplog.text
    client.close()


def test_http_error_body_is_redacted_and_401_is_explained():
    session = FakeSession([FakeResponse(422, None, _headers(1, 1), text=f"bad request for apiKey={KEY}")])
    client = OddsApiClient(KEY, session=session, sleep=lambda s: None)
    with pytest.raises(OddsApiError) as info:
        client.check_credits()
    assert KEY not in str(info.value) and info.value.status_code == 422

    session = FakeSession([FakeResponse(401, None, {}, text="Unauthorized")])
    client = OddsApiClient(KEY, session=session, sleep=lambda s: None)
    with pytest.raises(OddsApiError, match="key was rejected"):
        client.check_credits()


def test_retries_transient_status_then_succeeds():
    waits = []
    session = FakeSession([FakeResponse(503, None, {}), FakeResponse(200, [], _headers(400, 100))])
    client = OddsApiClient(KEY, session=session, max_attempts=3, backoff_base=1.0, sleep=waits.append)
    client.check_credits()
    assert waits == [1.0]
    assert client.credits.remaining == 400


def test_rejects_bad_keys_and_settings():
    with pytest.raises(ValueError):
        OddsApiClient("")
    with pytest.raises(ValueError):
        OddsApiClient(" padded ")
    with pytest.raises(ValueError):
        OddsApiClient(KEY, min_remaining=-1)
