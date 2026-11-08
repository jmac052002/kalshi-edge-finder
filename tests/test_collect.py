"""Collector orchestration against a fake client and a temp database."""

import json
from pathlib import Path

import pytest

from kalshi_edge.client import KalshiAPIError
from kalshi_edge.collect import main, resolve_series, run
from kalshi_edge.store import MarketStore

OPEN = {
    "ticker": "KXNFLGAME-26NOV16LACBAL-LAC",
    "event_ticker": "KXNFLGAME-26NOV16LACBAL",
    "title": "Los Angeles C wins",
    "yes_sub_title": "Los Angeles C",
    "status": "active",
    "result": "",
    "yes_bid_dollars": "0.4500",
    "yes_ask_dollars": "0.4700",
    "last_price_dollars": "0.4600",
    "volume_fp": "10.00",
    "yes_bid": None,
    "volume": None,
}

SETTLED = {
    "ticker": "KXNFLGAME-26NOV08CINATL-CIN",
    "event_ticker": "KXNFLGAME-26NOV08CINATL",
    "title": "Cincinnati wins",
    "yes_sub_title": "Cincinnati",
    "status": "finalized",
    "result": "yes",
    "settlement_value_dollars": "1.0000",
    "last_price_dollars": "0.9900",
}

HISTORICAL = {
    "ticker": "KXNFLGAME-26AUG29CHITEN-TEN",
    "event_ticker": "KXNFLGAME-26AUG29CHITEN",
    "title": "Tennessee wins",
    "yes_sub_title": "Tennessee",
    "status": "finalized",
    "result": "no",
    "settlement_value_dollars": "0.0000",
    "last_price_dollars": "0.0100",
}


class FakeClient:
    """Records page requests and returns one fixture page per call."""

    def __init__(self, open_bid="0.4500"):
        self.calls = []
        self.open_bid = open_bid
        self.series_calls = []

    def get_series(self, series_ticker):
        self.series_calls.append(series_ticker)
        return {"series": {"ticker": series_ticker, "fee_type": "quadratic", "fee_multiplier": 1}}

    def iter_market_pages(self, *, series_ticker, status=None, historical=False, limit=200, max_pages=500):
        self.calls.append(
            {
                "series_ticker": series_ticker,
                "status": status,
                "historical": historical,
                "limit": limit,
            }
        )
        if historical:
            yield [HISTORICAL, {"title": "missing ticker"}]
        elif status == "settled":
            yield [SETTLED]
        elif status == "open":
            payload = dict(OPEN)
            payload["yes_bid_dollars"] = self.open_bid
            yield [payload]
        else:
            yield []

    def close(self):
        pass


@pytest.fixture
def store(tmp_path):
    db = MarketStore(tmp_path / "kalshi.db")
    try:
        yield db
    finally:
        db.close()


def test_snapshot_mode_writes_markets_and_snapshots(store):
    client = FakeClient()
    stats = run(
        series=["KXNFLGAME"],
        mode="snapshot",
        client=client,
        store=store,
        now="2026-11-08T19:00:00Z",
    )
    assert client.calls == [
        {
            "series_ticker": "KXNFLGAME",
            "status": "open",
            "historical": False,
            "limit": 200,
        }
    ]
    assert stats.markets_written == 1
    assert stats.snapshots_written == 1
    assert stats.snapshots_in_db == 1
    row = store.conn.execute(
        "SELECT yes_bid, yes_bid_dollars, captured_at FROM price_snapshots"
    ).fetchone()
    assert row["yes_bid"] == pytest.approx(0.45)
    assert row["yes_bid_dollars"] == "0.4500"
    assert row["captured_at"] == "2026-11-08T19:00:00Z"


def test_rerun_snapshot_same_second_keeps_one_row(store):
    now = "2026-11-08T19:00:00Z"
    run(series=["KXNFLGAME"], mode="snapshot", client=FakeClient(), store=store, now=now)
    stats = run(
        series=["KXNFLGAME"],
        mode="snapshot",
        client=FakeClient(open_bid="0.5100"),
        store=store,
        now=now,
    )
    assert stats.markets_in_db == 1
    assert stats.snapshots_in_db == 1
    row = store.conn.execute("SELECT yes_bid, yes_bid_dollars FROM price_snapshots").fetchone()
    assert row["yes_bid"] == pytest.approx(0.51)
    assert row["yes_bid_dollars"] == "0.5100"


def test_later_snapshot_appends(store):
    client = FakeClient()
    run(series=["KXNFLGAME"], mode="snapshot", client=client, store=store, now="2026-11-08T19:00:00Z")
    stats = run(
        series=["KXNFLGAME"],
        mode="snapshot",
        client=client,
        store=store,
        now="2026-11-08T19:01:00Z",
    )
    assert stats.markets_in_db == 1
    assert stats.snapshots_in_db == 2


def test_settled_mode_reads_live_and_historical_without_snapshots(store):
    client = FakeClient()
    stats = run(
        series=["KXNFLGAME", "KXNFLANYTD"],
        mode="settled",
        client=client,
        store=store,
        now="2026-11-08T19:00:00Z",
    )
    # Archive page, then live settled page, for each series. No open-market snapshots.
    assert [call["historical"] for call in client.calls] == [True, False, True, False]
    assert [call["status"] for call in client.calls] == [None, "settled", None, "settled"]
    assert stats.snapshots_written == 0
    assert stats.snapshots_in_db == 0
    # One payload had no ticker and was skipped, per series (two historical pages).
    assert stats.skipped == 2
    assert stats.with_result_in_db == 2
    rows = store.conn.execute(
        "SELECT ticker, result, settlement_value, source FROM markets ORDER BY ticker"
    ).fetchall()
    by_ticker = {row["ticker"]: row for row in rows}
    assert by_ticker["KXNFLGAME-26AUG29CHITEN-TEN"]["result"] == "no"
    assert by_ticker["KXNFLGAME-26AUG29CHITEN-TEN"]["settlement_value"] == pytest.approx(0.0)
    assert by_ticker["KXNFLGAME-26AUG29CHITEN-TEN"]["source"] == "historical"
    assert by_ticker["KXNFLGAME-26NOV08CINATL-CIN"]["result"] == "yes"
    assert by_ticker["KXNFLGAME-26NOV08CINATL-CIN"]["source"] == "live"


def test_settled_rerun_does_not_duplicate(store):
    client = FakeClient()
    run(series=["KXNFLGAME"], mode="settled", client=client, store=store, now="t1")
    stats = run(series=["KXNFLGAME"], mode="settled", client=client, store=store, now="t2")
    assert stats.markets_in_db == 2
    assert stats.with_result_in_db == 2


def test_normalize_series_dedupes_and_rejects_junk():
    assert resolve_series(["kxnflgame", "KXNFLGAME,KXNFLANYTD"], None) == [
        "KXNFLGAME",
        "KXNFLANYTD",
    ]
    with pytest.raises(ValueError):
        resolve_series(["not a ticker"], None)
    with pytest.raises(ValueError):
        resolve_series(None, None)


def test_config_file_and_cli_series_combine(tmp_path):
    path = tmp_path / "series.json"
    path.write_text(json.dumps({"series": ["kxnflgame"]}), encoding="utf-8")
    assert resolve_series(["KXNFLANYTD"], path) == ["KXNFLGAME", "KXNFLANYTD"]


def test_example_config_loads():
    path = Path("config/series.example.json")
    assert resolve_series(None, path) == ["KXNFLGAME", "KXNFLANYTD"]


def test_main_snapshot_uses_db_path(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "out.db"
    created = []

    class RecordingClient(FakeClient):
        def __init__(self, **kwargs):
            super().__init__()
            created.append(kwargs)
            self.closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr("kalshi_edge.collect.KalshiClient", RecordingClient)
    monkeypatch.setattr("kalshi_edge.collect.utc_now", lambda: "2026-11-08T19:00:00Z")
    code = main(["--series", "KXNFLGAME", "--mode", "snapshot", "--db", str(db_path)])
    assert code == 0
    captured = capsys.readouterr()
    assert "markets_written=1" in captured.out
    assert "snapshots=1" in captured.out
    assert created[0]["min_interval"] == 0.25
    assert created[0]["base_url"].startswith("https://api.elections.kalshi.com")
    with MarketStore(db_path) as store:
        assert store.counts()["snapshots"] == 1


def test_main_reports_api_failure(tmp_path, monkeypatch):
    class Boom:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def get_series(self, series_ticker):
            raise KalshiAPIError("down")

        def iter_market_pages(self, **kwargs):
            raise KalshiAPIError("down")

        def close(self):
            pass

    monkeypatch.setattr("kalshi_edge.collect.KalshiClient", Boom)
    code = main(["--series", "KXNFLGAME", "--mode", "snapshot", "--db", str(tmp_path / "x.db")])
    assert code == 1


def test_main_rejects_missing_series(tmp_path):
    code = main(["--mode", "snapshot", "--db", str(tmp_path / "x.db")])
    assert code == 2


def test_collect_refreshes_series_fee_schedule(tmp_path):
    """Each run stores fee_type/fee_multiplier from GET /series so compare charges the real fee."""
    client = FakeClient()
    with MarketStore(tmp_path / "k.db") as store:
        run(series=["KXNFLGAME"], mode="snapshot", client=client, store=store, now="2026-11-08T19:00:00Z")
        fee = store.series_fee("KXNFLGAME")
    assert client.series_calls == ["KXNFLGAME"]
    assert fee is not None
    assert (fee.fee_type, fee.fee_multiplier, fee.fetched_at) == ("quadratic", "1", "2026-11-08T19:00:00Z")


def test_series_fee_failure_does_not_stop_collection(tmp_path, caplog):
    class NoSeries(FakeClient):
        def get_series(self, series_ticker):
            raise KalshiAPIError("503 from /series", status_code=503)

    with MarketStore(tmp_path / "k.db") as store:
        stats = run(series=["KXNFLGAME"], mode="snapshot", client=NoSeries(), store=store, now="2026-11-08T19:00:00Z")
        assert store.series_fee("KXNFLGAME") is None
    assert stats.markets_written == 1
    assert "could not refresh fee schedule" in caplog.text
