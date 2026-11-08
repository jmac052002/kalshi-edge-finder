"""The odds capture command against a fake client. The key never touches the network here."""

import json
from pathlib import Path

from kalshi_edge.odds import ENV_VAR, main, run
from kalshi_edge.sportsbook.client import CreditFloorError, Credits
from kalshi_edge.store import MarketStore

FIXTURE = Path(__file__).parent / "fixtures" / "odds_api_nfl.json"


class FakeOddsClient:
    def __init__(self, api_key="", **kwargs):
        self.api_key = api_key
        self.kwargs = kwargs
        self.calls = []
        self.credits = Credits(None, None, None)
        self.closed = False

    def check_credits(self):
        self.calls.append("check")
        self.credits = Credits(remaining=480, used=20, last_cost=0)
        return self.credits

    def get_odds(self, sport_key, *, regions, markets="h2h"):
        self.calls.append(("odds", sport_key, regions, markets))
        self.credits = Credits(remaining=478, used=22, last_cost=2)
        return json.loads(FIXTURE.read_text())

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_run_stores_one_capture(tmp_path):
    client = FakeOddsClient()
    with MarketStore(tmp_path / "k.db") as store:
        stats = run(sport_key="americanfootball_nfl", regions="us,eu", client=client, store=store, now="2026-11-08T17:31:02Z")
        captured_at, rows = store.latest_odds_capture("americanfootball_nfl")
    assert client.calls == ["check", ("odds", "americanfootball_nfl", "us,eu", "h2h")]
    assert stats.events == 2 and stats.rows == 12
    assert stats.bookmakers == ("betmgm", "draftkings", "fanduel", "pinnacle")
    assert stats.credits.remaining == 478
    assert captured_at == "2026-11-08T17:31:02Z" and len(rows) == 12


def test_main_requires_the_env_var_and_never_takes_a_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert main(["--db", str(tmp_path / "k.db")]) == 2
    assert ENV_VAR in capsys.readouterr().err
    assert main(["--db", str(tmp_path / "k.db"), "--api-key", "x"]) == 2


def test_main_reads_key_from_env_and_passes_floor(tmp_path, monkeypatch, capsys):
    created = []

    def factory(api_key, **kwargs):
        client = FakeOddsClient(api_key, **kwargs)
        created.append(client)
        return client

    monkeypatch.setenv(ENV_VAR, "not-a-real-key-aaaaaaaaaaaaaaaaaaaa")
    monkeypatch.setattr("kalshi_edge.odds.OddsApiClient", factory)
    code = main(["--db", str(tmp_path / "k.db"), "--min-remaining", "75", "--regions", "us"])
    out = capsys.readouterr().out
    assert code == 0
    assert created[0].api_key == "not-a-real-key-aaaaaaaaaaaaaaaaaaaa"
    assert created[0].kwargs["min_remaining"] == 75
    assert created[0].closed
    assert "remaining=478" in out
    assert "not-a-real-key" not in out


def test_main_reports_credit_floor_as_failure(tmp_path, monkeypatch):
    class Floor(FakeOddsClient):
        def get_odds(self, *args, **kwargs):
            raise CreditFloorError("12 credits remaining is below the floor of 50")

    monkeypatch.setenv(ENV_VAR, "not-a-real-key-aaaaaaaaaaaaaaaaaaaa")
    monkeypatch.setattr("kalshi_edge.odds.OddsApiClient", lambda key, **kw: Floor(key, **kw))
    assert main(["--db", str(tmp_path / "k.db")]) == 1
    with MarketStore(tmp_path / "k.db") as store:
        assert store.latest_odds_capture("americanfootball_nfl") == (None, [])


def test_run_records_an_empty_capture(tmp_path, caplog):
    class Empty(FakeOddsClient):
        def get_odds(self, sport_key, *, regions, markets="h2h"):
            self.credits = Credits(remaining=478, used=22, last_cost=2)
            return []

    with MarketStore(tmp_path / "k.db") as store:
        stats = run(sport_key="americanfootball_nfl", regions="us", client=Empty(), store=store, now="2026-11-08T17:31:02Z")
        captured_at, rows = store.latest_odds_capture("americanfootball_nfl")
    assert (stats.events, stats.rows) == (0, 0)
    assert captured_at == "2026-11-08T17:31:02Z" and rows == []
    assert "recorded an empty capture" in caplog.text
