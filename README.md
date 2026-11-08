# kalshi-edge-finder

A Python collector that reads [Kalshi](https://kalshi.com) prediction-market data from the public Trade API and stores it in a local SQLite file. No account and no API key.

This repository is step 1 of a learning project: get clean market data onto disk so later code can ask whether a contract priced at 45 cents actually resolves YES about 45% of the time, and how that price compares with a sportsbook. Those later steps are not built yet. See [Roadmap](#roadmap).

## What exists now

`kalshi_edge.collect` does two things, for whatever series tickers you pass it. Nothing in the code is NFL-specific; `KXNFLGAME` is just an example.

| Mode | What it calls | What it stores |
| --- | --- | --- |
| `snapshot` | `GET /markets?status=open` | The market, plus a price row stamped with the time the run started |
| `settled` | `GET /markets?status=settled` and `GET /historical/markets` | The market and how it resolved (`result`, settlement payoff). No price snapshot |

Run `snapshot` more than once and you get a price history. The same contract at the same timestamp is updated in place, so a retry does not insert a duplicate. A later run (a new timestamp) appends a row.

`settled` reads both endpoints because Kalshi archives old settlements. `GET /historical/cutoff` publishes `market_settled_ts`; markets settled before that time leave the live list and are served from `/historical/markets`. The cutoff moves forward, so the collector always reads both lists and upserts on ticker instead of hard-coding a date. When this was written the cutoff was `2026-09-09T00:00:00Z`, and both lists answered with no `Authorization` header.

The public base URL is `https://api.elections.kalshi.com/trade-api/v2`. The host name says elections; Kalshi documents that this host serves every category.

Quotes are read from the fixed-point dollar strings (`yes_bid_dollars`, `last_price_dollars`, …) and the contract-count strings (`volume_fp`, `open_interest_fp`). The older integer cent fields came back null and are ignored. A null or garbage field becomes SQL `NULL`; it does not drop the market.

Requests are spaced (default 0.25s) and retried with exponential backoff on connection errors and HTTP 429/5xx. Pages follow the response `cursor` until it is empty. A repeated cursor stops the loop. Hitting `--max-pages` is an error, so a stuck cursor cannot quietly cut a sample short.

## Setup

Python 3.11 or newer. From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt   # pytest, only for tests
```

`requirements.txt` is just `requests`. The database file is created on first run at `data/kalshi.db` (override with `--db`). That directory is gitignored.

## Usage

```bash
# Current prices for open NFL game-winner markets.
python -m kalshi_edge.collect --series KXNFLGAME --mode snapshot

# Same idea, more than one series. Tickers can be repeated flags or commas.
python -m kalshi_edge.collect --series KXNFLGAME KXNFLANYTD --mode snapshot

# Finalized markets and their results, including the historical archive.
python -m kalshi_edge.collect --series KXNFLGAME --mode settled

# Series list from a JSON file: {"series": ["KXNFLGAME", "KXNFLANYTD"]}
python -m kalshi_edge.collect --config config/series.example.json --mode snapshot
```

Useful flags: `--db PATH`, `--min-interval SECONDS`, `--limit N` (page size, 1–1000), `--max-pages N`, `--base-url URL`.

`KXNFLGAME` is the game-winner series (one contract per team). `KXNFLANYTD` is anytime touchdown. Any other series ticker works the same way.

A practical way to build history is to run `snapshot` on a timer while the markets are open, and run `settled` after games finish. Two snapshot runs in the same UTC second collapse to one row per contract; a run a second later adds the next observation.

## How a price becomes a probability

A binary Kalshi contract pays its notional, usually **$1**, if the event happens and **$0** if it does not. A YES price of `0.4500` is 45 cents for a $1 payoff, so the market is pricing that side at 45% before fees.

The book is two-sided:

* `yes_bid` is the highest price someone will pay for YES.
* `yes_ask` is the lowest price someone will sell YES for.
* The gap is the spread. Crossing it is the cost of trading, and a wide spread means the midpoint is a soft estimate.
* `last_price` is the last trade, which can sit outside the current quote if the book moved.

Buying NO at 0.53 is economically close to selling YES at 0.47, because the two sides pay out $1 between them. The collector stores both sides as Kalshi sent them rather than forcing them to add up.

`status=open` on the query matches markets whose own `status` field is `active`. `status=settled` matches `finalized`. The row stores that lifecycle status, plus `result` (`yes`, `no`, or `scalar`) once the exchange has determined the market. `settlement_value` is the dollars a YES holder is paid: 1 or 0 on a binary contract, a fraction on a scalar. A tie is neither team's win. On `KXNFLGAME` those contracts have come back as `scalar` with `settlement_value` 0.5, so each side paid half.

**Do not treat `markets.last_price` on a finalized row as the forecast.** After the outcome is known the last trade sits near 0 or 1, which would make every market look perfectly calibrated. `price_snapshots` is the table of quotes taken while a snapshot run was pointed at open markets. Comparing those rows to `markets.result` is the calibration question, and it is not computed here.

A game-winner event has two contracts (one per team) under the same `event_ticker`. They are one game, not two independent samples.

Candlesticks (`GET /series/{series_ticker}/markets/{ticker}/candlesticks`) and trades (`GET /markets/trades`) also answer without auth. They are the way to recover prices from before a market resolved. This collector does not store them yet. Pulling a settled market's last trade and calling it the pre-game price would be the wrong dataset.

## Database

SQLite, schema version 1 in `schema_meta`.

`markets` — one row per ticker. Reruns update the row. `source` is `live` or `historical`. If a ticker appears in both during an archive handoff, the live row is kept.

`price_snapshots` — primary key `(ticker, captured_at)`. Float columns are the parsed dollar prices. `yes_bid_dollars` and the other `*_dollars` columns keep the API's original strings.

```sql
-- Book from the newest snapshot run.
SELECT m.ticker, m.yes_sub_title, s.yes_bid, s.yes_ask, s.last_price, s.captured_at
FROM price_snapshots AS s
JOIN markets AS m ON m.ticker = s.ticker
WHERE s.series_ticker = 'KXNFLGAME'
  AND s.captured_at = (
      SELECT MAX(captured_at)
      FROM price_snapshots
      WHERE series_ticker = 'KXNFLGAME'
  );

-- What already resolved. settlement_value is the YES payoff, not a forecast.
SELECT ticker, yes_sub_title, result, settlement_value, close_time, source
FROM markets
WHERE series_ticker = 'KXNFLGAME'
  AND result IS NOT NULL;
```

## Tests

```bash
pytest
```

The tests parse fixture payloads and talk to a fake HTTP session. They do not call Kalshi.

## Roadmap

Not built. Nothing below is in this repository yet.

* **Calibration.** Bucket open-market snapshots by price and compare each bucket to the share that later settled YES. Needs the snapshot history this collector is starting to record, and the settled results it already stores. Counting both sides of one game as independent samples would double-count the game.
* **Pre-resolution history for markets that already settled.** Public candlestick and trade endpoints can supply the price from before the outcome was known. Not collected yet.
* **Sportsbook comparison.** Match a Kalshi contract to a sportsbook line, remove the book's vig, and compare implied probabilities. No sportsbook client and no matching.
* **Parlays, break-even, and expected value.** Not implemented.

## License

MIT. See [LICENSE](LICENSE).
