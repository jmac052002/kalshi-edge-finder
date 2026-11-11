"""HTTP client tests. The session is fake, so nothing here touches the network."""

import requests

from kalshi_edge.client import USER_AGENT, KalshiAPIError, KalshiClient


class FakeResponse:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = "" if payload is None else str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return self.handler(url, params, timeout)

    def close(self):
        pass


def _client(handler, **kwargs):
    defaults = {"min_interval": 0, "max_attempts": 4, "sleep": lambda _seconds: None, "rng": lambda: 1.0}
    defaults.update(kwargs)
    return KalshiClient(session=FakeSession(handler), **defaults)


def test_paginates_until_cursor_is_empty():
    def handler(url, params, timeout):
        cursor = (params or {}).get("cursor")
        if not cursor:
            return FakeResponse(200, {"markets": [{"ticker": "A"}], "cursor": "page-2"})
        assert cursor == "page-2"
        return FakeResponse(200, {"markets": [{"ticker": "B"}], "cursor": ""})

    client = _client(handler)
    pages = list(client.iter_market_pages(series_ticker="KXNFLGAME", status="open", limit=200))
    assert pages == [[{"ticker": "A"}], [{"ticker": "B"}]]
    assert client.session.calls[0]["params"]["status"] == "open"
    assert client.session.calls[0]["params"]["limit"] == 200
    assert "Authorization" not in client.session.headers


def test_stops_when_cursor_repeats():
    def handler(url, params, timeout):
        return FakeResponse(200, {"markets": [{"ticker": "A"}], "cursor": "again"})

    client = _client(handler)
    pages = list(client.iter_market_pages(series_ticker="KXNFLGAME", status="open"))
    # First page, follow "again", second page returns the same cursor, stop.
    assert len(pages) == 2
    assert len(client.session.calls) == 2


def test_historical_path_does_not_send_status():
    def handler(url, params, timeout):
        return FakeResponse(200, {"markets": [], "cursor": ""})

    client = _client(handler)
    list(client.iter_market_pages(series_ticker="KXNFLGAME", status="settled", historical=True))
    call = client.session.calls[0]
    assert call["url"].endswith("/historical/markets")
    assert "status" not in call["params"]
    assert call["params"]["series_ticker"] == "KXNFLGAME"


def test_retries_429_then_returns():
    sleeps = []
    state = {"n": 0}

    def handler(url, params, timeout):
        state["n"] += 1
        if state["n"] == 1:
            return FakeResponse(429, {"error": "too many requests"}, headers={"Retry-After": "0.2"})
        return FakeResponse(200, {"markets": [], "cursor": ""})

    client = _client(handler, sleep=sleeps.append, backoff_base=0.5, backoff_cap=30)
    payload = client.get_json("/markets", {"limit": 1})
    assert payload == {"markets": [], "cursor": ""}
    assert state["n"] == 2
    assert sleeps  # backed off instead of hammering
    assert sleeps[0] >= 0.2


def test_retries_connection_error():
    state = {"n": 0}

    def handler(url, params, timeout):
        state["n"] += 1
        if state["n"] == 1:
            raise requests.ConnectionError("connection reset")
        return FakeResponse(200, {"ok": True})

    client = _client(handler)
    assert client.get_json("/markets") == {"ok": True}
    assert state["n"] == 2


def test_400_does_not_retry():
    state = {"n": 0}

    def handler(url, params, timeout):
        state["n"] += 1
        return FakeResponse(400, {"error": "bad series"})

    client = _client(handler)
    try:
        client.get_json("/markets")
    except KalshiAPIError as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("expected KalshiAPIError")
    assert state["n"] == 1


def test_gives_up_after_max_attempts():
    state = {"n": 0}

    def handler(url, params, timeout):
        state["n"] += 1
        return FakeResponse(503, {"error": "unavailable"})

    client = _client(handler, max_attempts=3)
    try:
        client.get_json("/markets")
    except KalshiAPIError as exc:
        assert "3 attempts" in str(exc)
    else:
        raise AssertionError("expected KalshiAPIError")
    assert state["n"] == 3


def test_max_pages_raises_instead_of_truncating():
    def handler(url, params, timeout):
        return FakeResponse(200, {"markets": [{"ticker": "A"}], "cursor": "next"})

    client = _client(handler)
    try:
        list(client.iter_market_pages(series_ticker="KXNFLGAME", status="open", max_pages=1))
    except KalshiAPIError as exc:
        assert "did not end" in str(exc)
    else:
        raise AssertionError("expected KalshiAPIError")


def test_paces_between_requests():
    sleeps = []

    def handler(url, params, timeout):
        cursor = (params or {}).get("cursor")
        if not cursor:
            return FakeResponse(200, {"markets": [], "cursor": "c2"})
        return FakeResponse(200, {"markets": [], "cursor": ""})

    client = _client(handler, min_interval=0.25, sleep=sleeps.append, clock=lambda: 0.0)
    list(client.iter_market_pages(series_ticker="KXNFLGAME", status="open"))
    assert 0.25 in sleeps


def test_session_sends_no_credentials():
    client = KalshiClient(min_interval=0)
    try:
        assert "Authorization" not in client.session.headers
        assert client.session.headers["User-Agent"] == USER_AGENT
        assert client.session.headers["Accept"] == "application/json"
    finally:
        client.close()


def test_custom_user_agent_still_sends_no_credentials():
    client = KalshiClient(
        session=FakeSession(lambda url, params, timeout: FakeResponse(200, {})),
        min_interval=0,
        sleep=lambda _seconds: None,
        user_agent="custom-agent",
        accept="application/geo+json",
    )
    assert client.session.headers["User-Agent"] == "custom-agent"
    assert client.session.headers["Accept"] == "application/geo+json"
    assert "Authorization" not in client.session.headers


def test_get_json_list_and_refuses_another_host():
    client = _client(lambda url, params, timeout: FakeResponse(200, [{"a": 1}]))
    assert client.get_json_list("/nowcast.json") == [{"a": 1}]
    try:
        client.get_json("https://evil.example/secret")
    except KalshiAPIError as exc:
        assert "evil.example" in str(exc)
    else:
        raise AssertionError("expected KalshiAPIError")
    assert all("evil.example" not in call["url"] for call in client.session.calls)


def test_list_series_follows_cursor():
    def handler(url, params, timeout):
        assert url.endswith("/series")
        if not (params or {}).get("cursor"):
            return FakeResponse(200, {"series": [{"ticker": "KXHIGHNY"}], "cursor": "more"})
        return FakeResponse(200, {"series": [{"ticker": "KXCPI"}], "cursor": ""})

    client = _client(handler)
    assert [row["ticker"] for row in client.list_series()] == ["KXHIGHNY", "KXCPI"]


def test_market_page_sends_close_window():
    def handler(url, params, timeout):
        return FakeResponse(200, {"markets": [], "cursor": ""})

    client = _client(handler)
    list(
        client.iter_market_pages(
            series_ticker="KXHIGHNY",
            status="open",
            min_close_ts=100,
            max_close_ts=200,
        )
    )
    params = client.session.calls[0]["params"]
    assert params["min_close_ts"] == 100
    assert params["max_close_ts"] == 200
    assert params["series_ticker"] == "KXHIGHNY"
    assert params["status"] == "open"


def test_event_pages_do_not_nest_markets():
    def handler(url, params, timeout):
        assert url.endswith("/events")
        return FakeResponse(200, {"events": [{"series_ticker": "KXAI"}], "cursor": ""})

    client = _client(handler)
    pages = list(client.iter_event_pages(status="open", limit=50))
    assert pages == [[{"series_ticker": "KXAI"}]]
    assert client.session.calls[0]["params"]["with_nested_markets"] == "false"
    assert client.session.calls[0]["params"]["status"] == "open"
