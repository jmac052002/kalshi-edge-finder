# kalshi-edge-finder

Python tools that read [Kalshi](https://kalshi.com) prediction-market data from the public Trade API (no account, no API key) and check whether a contract priced at X% actually resolved YES about X% of the time. Comparing those prices to a sportsbook is not built yet. See [Roadmap](#roadmap).

## What exists now

Two commands, both series-agnostic. `KXNFLGAME` is the example, not a special case.

* `kalshi_edge.collect` stores open quotes and settled results in SQLite.
* `kalshi_edge.calibrate` scores those results against a **pre-event** price and prints a calibration table. It does not use the bid, ask, or last trade stored on a settled market row. Those are the book after the outcome is known.

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
pip install -r requirements-plot.txt  # matplotlib, only for --plot
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

**Do not treat `markets.last_price` on a finalized row as the forecast.** After the outcome is known the last trade sits near 0 or 1. `kalshi_edge.calibrate` ignores those columns.

A game-winner event has two contracts (one per team) under the same `event_ticker`. They are one game, not two independent samples.

## Calibration

Collect settled markets first, then:

```bash
python -m kalshi_edge.calibrate --series KXNFLGAME
python -m kalshi_edge.calibrate --series KXNFLGAME --plot data/calibration.png
```

The reference price is the last **tight mid** (bid and ask within `--max-spread`, default 10 cents) at or before `close_time` minus `--lead-minutes`. If the book is wider than that, the last trade in the same window is used instead. An empty book of $0 bid / $1 ask is not treated as 50%.

Default lead is **180 minutes**. `close_time` on these game markets is about when the game ends, because the market stays open through the game. A quote at the close already knows the score, so a calibration study that used it would look perfect for a reason that is not a forecast. Three hours before the close is around kickoff for a three-hour game. If a series closes *before* the outcome is knowable, pass `--lead-minutes 0` to use the last quote before trading stopped. Nothing with a timestamp after the cutoff is read: not a candlestick whose bar ends later, not a trade, not a snapshot.

Where the price comes from, in order:

1. A `price_snapshots` row inside the lookback window (default 6 hours before the cutoff). Running the collector on a timer is what fills this in.
2. Otherwise public candlesticks, live or historical, one bar size of `--period` minutes (default 60). The bar has to end at or before the cutoff, so its close does not contain later trades.
3. Otherwise the newest public trade in that same window. A trade has no ask, so it counts toward the YES rate and the Brier score but not toward ROI.

The chosen quote is cached in `reference_quotes`. A rerun does not call the API again unless you pass `--refresh`. `--no-fetch` scores only what is already stored.

The table is one row per price bucket (default 10 percentage points):

| Column | Meaning |
| --- | --- |
| `avg_price` | Average pre-event probability in the bucket |
| `yes_rate` | Share of those contracts that resolved YES |
| `diff` | `yes_rate - avg_price`, in percentage points |
| `se` | Binomial standard error of `yes_rate`. Too small when both sides of a game are included |
| `ci95` | Wilson 95% interval. A bucket of one win is not "100%" |
| `avg_cost` | Ask plus the taker fee, for a one-contract buy |
| `avg_roi` | Equal-weighted return of that buy. YES pays $1, NO pays $0 |
| `flag` | `yes+ev` or `yes-ev` only when the whole Wilson interval sits on one side of `avg_cost` |

The fee is Kalshi's published taker formula, `round up to the next cent of multiplier × 0.07 × P × (1−P)`, for one contract. The July 7, 2026 schedule's worked table charges $0.02 on one 50-cent contract (`0.07 × 0.5 × 0.5 = 0.0175`), not the unrounded 1.75 cents. The multiplier is `fee_multiplier` from `GET /series/{ticker}` (1 for `KXNFLGAME`). Buying the ask is a taker fill. There is no settlement fee. Maker fees are not what this column measures.

The Brier score is the mean squared error of those pre-event prices against 1 for YES and 0 for NO. Lower is better. A constant 50% forecast scores 0.25 on a balanced sample. The baseline printed next to it is the Brier score of guessing the sample's own base rate for every contract.

Scalar results are excluded, including ties that paid $0.50. They are neither YES nor NO.

### How to read a result

This is a description of the past, not a signal that the same bucket will pay the next time you look. A flag can be a small sample, both sides of one game counted as two observations, or a lead time that is a poor match for the series. A bucket can sit on the diagonal and still lose money after the spread and the fee. An edge that was real can also disappear once it is visible.

## Database

SQLite, schema version 1 in `schema_meta`.

`markets` — one row per ticker. Reruns update the row. `source` is `live` or `historical`. A live row that already has a `result` is kept when the same ticker also shows up in the archive. A snapshot of a market that had not resolved yet does not block a later historical settlement: after Kalshi archives the contract, that archive row is the only copy of the payoff. The open-market price snapshot stays either way.

`price_snapshots` — primary key `(ticker, captured_at)`. Float columns are the parsed dollar prices. `yes_bid_dollars` and the other `*_dollars` columns keep the API's original strings.

`reference_quotes` — the pre-event price the calibration actually scored, keyed by ticker and the rule (lead, lookback, bar size, max spread). `quote_source` is `snapshot`, `candlestick`, `trade`, or `missing`.

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

Not built.

* **Sportsbook comparison.** Match a Kalshi contract to a sportsbook line, remove the book's vig, and compare implied probabilities. No sportsbook client and no matching.
* **Parlays.** Not implemented. The calibration table's ROI is a single YES contract, not a combo.

## License

MIT. See [LICENSE](LICENSE).
