# kalshi-edge-finder

Python tools that read [Kalshi](https://kalshi.com) prediction-market data from the public Trade API, store it in a local SQLite file, and check whether the prices were honest probabilities. No account and no API key.

This is a learning project in two parts so far. `kalshi_edge.collect` gets clean market data onto disk. `kalshi_edge.calibrate` asks the question that data is for: did contracts priced at 45 cents resolve YES about 45% of the time, and would buying them have made money after fees? Comparing those prices with a sportsbook is not built yet. See [Roadmap](#roadmap).

## What exists now

| Command | What it does |
| --- | --- |
| `python -m kalshi_edge.collect --mode snapshot` | Stores open markets and a timestamped price row |
| `python -m kalshi_edge.collect --mode settled` | Stores finalized markets and how they resolved |
| `python -m kalshi_edge.calibrate` | Fetches each settled market's pre-event price and prints a calibration table with Brier score and fee-adjusted ROI |

### Collector

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

### Calibration backtest

`kalshi_edge.calibrate` takes the settled markets of one series and asks, bucket by bucket, whether the price before the event matched how often the event happened.

**Where the pre-event price comes from.** Quotes stored on a finalized market are useless as forecasts: once the game is over the last trade sits at $0.99 or $0.01. The backtest instead reads the public candlestick history (`GET /series/{series}/markets/{ticker}/candlesticks`, hourly bars, no auth) and takes the closing bid and ask of the last bar that **ended at or before the event started**. A bar that ends at kickoff was formed entirely before kickoff, so nothing in it can know the result.

**What "event started" means.** No timestamp Kalshi publishes on a market is the start of the event, and the first version of this backtest learned that the hard way. On `KXNFLGAME`, `occurrence_datetime` is the scheduled **end** of the game (kickoff plus three hours; Sunday 1 pm ET games carry 20:00Z), `expected_expiration_time` is the same instant or three hours later on early-season rows, and `close_time` is when the book actually closed, minutes after that. When a game ran long, Kalshi rewrote `occurrence_datetime` to the real final whistle. A run that trusted `occurrence_datetime` as kickoff scored a Brier of 0.11, with half the "pre-game" prices at 98% or 2%: it was reading the last in-game bar. So the backtest takes the earlier of `occurrence_datetime` and `expected_expiration_time` (then `close_time`), treats it as the end of the event, and subtracts `--event-hours` (default 4, an hour before an NFL kickoff). A series whose `occurrence_datetime` really is the start can pass `--event-hours 0`. The report says which field each observation used. `--lead-hours N` moves every cutoff N hours earlier for a "what did the market say that morning" view.

**One contract per game.** A game-winner event lists both teams, and "A wins" is "B loses". Counting both doubles the sample without adding information, so by default the lowest ticker in each event is kept (ticker order is by team abbreviation, which does not know who was favored). `--all-contracts` keeps both sides.

**What is reported.** For each probability bucket (default ten of 10%): the number of contracts, the mean midpoint, the realized YES rate, and the fee-adjusted return of buying each side at the ask and holding to settlement. The Brier score (mean squared error of the midpoint against the 0/1 outcome, lower is better) summarizes the series, next to the Brier score of a constant forecast at the base rate as the floor a forecast with no game-specific information would reach.

**Fees.** Kalshi's taker fee is `round_up(0.07 × C × P × (1 − P))` per order, rounded up to the cent ([fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf)). There is no settlement fee. `P(1 − P)` peaks at 50 cents, so the middle of the board is the most expensive place to trade and that is where "calibrated but unprofitable" shows up. The ROI columns pay the fee on every purchase, at `--contracts` contracts per market (default 1; rounding up per order makes single contracts slightly pessimistic, which is the right direction to be wrong in).

**What the numbers do not say.** The ROI is for one contract per market. A multi-leg parlay multiplies its legs: if each leg is fairly priced and costs a fee, the combination's expected return is the product of each leg's (negative) expected return, which is worse, not better, than the legs alone. Nothing in this table turns a long-shot combo into a positive-expectation bet.

Pre-event quotes are cached in the `pre_event_prices` table, one request per market on the first run and none after. A cached row is reused only when it answers the window being asked for: change `--lead-hours`, `--event-hours`, or `--lookback-hours`, or collect a new event time, and the affected markets are fetched again rather than served a quote from the wrong moment. `--refresh` throws the whole cache away. `--max-fetch N` caps the number of candlestick requests, for a quick first look at a big series.

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

# Calibration of the settled markets collected above. The first run fetches
# one candlestick history per market; later runs read the cache.
python -m kalshi_edge.calibrate --series KXNFLGAME

# Cutoff twelve hours before kickoff, ten contracts per market, JSON copy of the report.
python -m kalshi_edge.calibrate --series KXNFLGAME --lead-hours 12 --contracts 10 --json report.json
```

Useful collector flags: `--db PATH`, `--min-interval SECONDS`, `--limit N` (page size, 1–1000), `--max-pages N`, `--base-url URL`. Calibration flags: `--buckets N`, `--contracts N`, `--all-contracts`, `--lookback-hours H`, `--event-hours H`, `--lead-hours H`, `--refresh`, `--max-fetch N`, `--json PATH`.

### Sample calibration output

Not yet captured. This section will be filled with the output of a real `calibrate --series KXNFLGAME` run before the calibration PR is merged; nothing here is made up.

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

**Do not treat `markets.last_price` on a finalized row as the forecast.** After the outcome is known the last trade sits near 0 or 1, which would make every market look perfectly calibrated. `price_snapshots` holds quotes taken while a snapshot run was pointed at open markets, and `pre_event_prices` holds the quote the backtest recovered from candlesticks as of the event's start. Comparing either to `markets.result` is the calibration question; `kalshi_edge.calibrate` does it with the second.

A game-winner event has two contracts (one per team) under the same `event_ticker`. They are one game, not two independent samples.

Candlesticks (`GET /series/{series_ticker}/markets/{ticker}/candlesticks`) and trades (`GET /markets/trades`) also answer without auth. They are the way to recover prices from before a market resolved; the backtest uses the candlesticks. Pulling a settled market's last trade and calling it the pre-game price would be the wrong dataset.

## Database

SQLite, schema version 2 in `schema_meta`. A version 1 file is upgraded in place on open (two columns are added to `markets`; they stay `NULL` until the next `settled` run rewrites each row).

`markets` — one row per ticker. Reruns update the row. `occurrence_datetime` and `expected_expiration_time` are the event timing fields the backtest reads. `source` is `live` or `historical`. A live row that already has a `result` is kept when the same ticker also shows up in the archive. A snapshot of a market that had not resolved yet does not block a later historical settlement: after Kalshi archives the contract, that archive row is the only copy of the payoff. The open-market price snapshot stays either way.

`price_snapshots` — primary key `(ticker, captured_at)`. Float columns are the parsed dollar prices. `yes_bid_dollars` and the other `*_dollars` columns keep the API's original strings.

`pre_event_prices` — one row per ticker: `cutoff_ts` (Unix second treated as the event start), `cutoff_source` (which market field it came from), `candle_end_ts` and the closing `yes_bid` / `yes_ask` / `last_price` of the chosen bar. A market with no two-sided bar before the cutoff is stored with those `NULL` so the miss is remembered.

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

The tests parse fixture payloads (including a candlestick history in `tests/fixtures/`) and talk to a fake HTTP session. They do not call Kalshi. The fee tests check the formula against the worked examples in Kalshi's fee schedule.

## Roadmap

Not built. Nothing below is in this repository yet.

* **Calibration from the snapshot history.** The backtest uses candlesticks; a second view built from `price_snapshots` (prices at the exact moments the collector ran) would cross-check it once enough snapshot runs exist.
* **Calibration chart.** A PNG of predicted against realized rates per bucket. The `--json` report has everything a plot needs.
* **Sportsbook comparison.** Match a Kalshi contract to a sportsbook line, remove the book's vig, and compare implied probabilities. No sportsbook client and no matching.
* **Parlays, break-even, and expected value.** Not implemented.

## License

MIT. See [LICENSE](LICENSE).
