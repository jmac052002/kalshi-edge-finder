# kalshi-edge-finder

Python tools that read [Kalshi](https://kalshi.com) prediction-market data from the public Trade API, store it in a local SQLite file, check whether the prices were honest probabilities, compare them with sportsbook moneylines, and list short-dated non-sports markets next to a weather forecast or a public inflation nowcast. The Kalshi side needs no account and no API key. The sportsbook side needs one free key from [The Odds API](https://the-odds-api.com), kept in an environment variable. Nothing in this repository places an order.

`kalshi_edge.collect` gets clean market data onto disk. `kalshi_edge.calibrate` asks whether contracts priced at 45 cents resolved YES about 45% of the time, and whether buying them would have made money after fees. `kalshi_edge.odds` and `kalshi_edge.compare` put a sportsbook's de-vigged probability next to each open Kalshi game contract. `kalshi_edge.scan` is the daily list for everything that is not a game: city high temperatures, a handful of economic releases, and other markets in chosen categories that settle soon. See [How to run it](#how-to-run-it-the-routine) for the weekly routine, [The non-sports scan](#the-non-sports-scan) for the daily one, [docs/RUNBOOK.md](docs/RUNBOOK.md) for a step-by-step operator's guide, and [Roadmap](#roadmap) for what is still missing.

## What exists now

| Command | What it does |
| --- | --- |
| `python -m kalshi_edge.collect --mode snapshot` | Stores open markets and a timestamped price row |
| `python -m kalshi_edge.collect --mode settled` | Stores finalized markets and how they resolved |
| `python -m kalshi_edge.calibrate` | Fetches each settled market's pre-event price and prints a calibration table with Brier score and fee-adjusted ROI |
| `python -m kalshi_edge.odds` | Captures sportsbook moneylines for one sport from The Odds API (needs `ODDS_API_KEY`) |
| `python -m kalshi_edge.compare` | Matches open Kalshi game contracts to those lines and prints break-even, fair probability, edge and EV per contract. Offline |
| `python -m kalshi_edge.scan` | Ranks short-dated non-sports markets. Models daily high temperatures and CPI when a public nowcast covers that month. Everything else is price and settlement date only. Never trades |

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

Every run also reads `GET /series/{ticker}` once per series and stores its `fee_type` and `fee_multiplier` in `series_fees`. That is the fee schedule the exchange will actually charge, and `compare` reads it rather than trusting a constant that Kalshi may have changed since this was written. If that call fails the run continues with a warning.

### Calibration backtest

`kalshi_edge.calibrate` takes the settled markets of one series and asks, bucket by bucket, whether the price before the event matched how often the event happened.

**Where the pre-event price comes from.** Quotes stored on a finalized market are useless as forecasts: once the game is over the last trade sits at $0.99 or $0.01. The backtest instead reads the public candlestick history (`GET /series/{series}/markets/{ticker}/candlesticks`, hourly bars, no auth) and takes the closing bid and ask of the last bar that **ended at or before the event started**. A bar that ends at kickoff was formed entirely before kickoff, so nothing in it can know the result.

**What "event started" means.** No timestamp Kalshi publishes on a market is the start of the event, and the first version of this backtest learned that the hard way. On `KXNFLGAME`, `occurrence_datetime` is the scheduled **end** of the game (kickoff plus three hours; Sunday 1 pm ET games carry 20:00Z), `expected_expiration_time` is the same instant or three hours later on early-season rows, and `close_time` is when the book actually closed, minutes after that. When a game ran long, Kalshi rewrote `occurrence_datetime` to the real final whistle. A run that trusted `occurrence_datetime` as kickoff scored a Brier of 0.11, with half the "pre-game" prices at 98% or 2%: it was reading the last in-game bar. So the backtest takes the earlier of `occurrence_datetime` and `expected_expiration_time` (then `close_time`), treats it as the end of the event, and subtracts `--event-hours` (default 4, an hour before an NFL kickoff). A series whose `occurrence_datetime` really is the start can pass `--event-hours 0`. The report says which field each observation used. `--lead-hours N` moves every cutoff N hours earlier for a "what did the market say that morning" view.

**One contract per game.** A game-winner event lists both teams, and "A wins" is "B loses". Counting both doubles the sample without adding information, so by default the lowest ticker in each event is kept (ticker order is by team abbreviation, which does not know who was favored). `--all-contracts` keeps both sides.

**What is reported.** For each probability bucket (default ten of 10%): the number of contracts, the mean midpoint, the realized YES rate with a Wilson 95% interval, and the fee-adjusted return of buying each side at the ask and holding to settlement. The interval is the point of the table: a bucket priced at 65% that resolved YES 57% of the time looks like a mispricing, but if the interval runs 49%..64% on 160 games the miss is a couple of standard errors, and on 12 games it is noise. Wilson is used instead of the usual `p ± 1.96·√(p(1−p)/n)` because that formula collapses to zero width when a bucket is all YES or all NO and overshoots 0 or 1 near the edges. The interval assumes independent outcomes, which holds with one contract per game and not with `--all-contracts`. The Brier score (mean squared error of the midpoint against the 0/1 outcome, lower is better) summarizes the series, next to the Brier score of a constant forecast at the base rate as the floor a forecast with no game-specific information would reach.

**Fees.** Kalshi's taker fee is `round_up(0.07 × C × P × (1 − P))` per order, rounded up to the cent ([fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf)). There is no settlement fee. `P(1 − P)` peaks at 50 cents, so the middle of the board is the most expensive place to trade and that is where "calibrated but unprofitable" shows up. The ROI columns pay the fee on every purchase, at `--contracts` contracts per market (default 1; rounding up per order makes single contracts slightly pessimistic, which is the right direction to be wrong in).

**What the numbers do not say.** The ROI is for one contract per market. A multi-leg parlay multiplies its legs: if each leg is fairly priced and costs a fee, the combination's expected return is the product of each leg's (negative) expected return, which is worse, not better, than the legs alone. Nothing in this table turns a long-shot combo into a positive-expectation bet.

Pre-event quotes are cached in the `pre_event_prices` table, one request per market on the first run and none after. A cached row is reused only when it answers the window being asked for: change `--lead-hours`, `--event-hours`, or `--lookback-hours`, or collect a new event time, and the affected markets are fetched again rather than served a quote from the wrong moment. `--refresh` throws the whole cache away. `--max-fetch N` caps the number of candlestick requests, for a quick first look at a big series. `--plot calibration.png` also writes a reliability diagram (realized rate against predicted, one point per bucket with its interval as an error bar, sized by count); it needs matplotlib, which is kept out of the main requirements: `pip install -r requirements-plot.txt`.

### Sportsbook comparison

`kalshi_edge.odds` captures every bookmaker's head-to-head (moneyline) price for one sport, and `kalshi_edge.compare` lines those up with the newest Kalshi snapshot. The design, with the reasoning behind each choice, is in [docs/design/sportsbook-comparison.md](docs/design/sportsbook-comparison.md). The short version:

**Why a sportsbook is the reference.** Books like Pinnacle take sharp money all day and move their lines until they stop losing. Their price, with the margin removed, is the best public estimate of a game's probability. The tool treats it as the yardstick, not as the truth, and says so in every report. The calibration backtest is the only thing that will ever tell us whether Kalshi's own prices are biased.

**Removing the vig.** A book's two moneyline prices imply probabilities that sum to more than one; the excess is the margin. Dividing each by the total (`multiplicative`) is the common fix, but books shade long shots more than favorites, so after that fix the long shot still looks too likely. The default here is the `power` method: raise each raw probability to the power that makes them sum to one. That shrinks small probabilities more, which is the direction the bias calls for. For a `1.80 / 2.10` line the two methods differ by less than half a point; for `1.20 / 5.00` they differ by 1.6 points on the long shot, which is bigger than most "edges" the tool will ever print. So the table shows both (`fair` and `alt`) and names the method.

**Which book.** `--reference pinnacle` (default) uses Pinnacle's line when the capture has it (that needs `--regions us,eu` on the odds run; Pinnacle is listed under `eu`) and falls back to the median of the other books otherwise. `--reference median` always uses the median. A median is robust to one book's stale line.

**The Kalshi side is priced at the ask.** You cannot buy at the midpoint or at the last trade. The fee is Kalshi's published quadratic taker fee, `round_up(multiplier × 0.07 × contracts × P × (1 − P))` to the cent, computed with `Decimal` from the exchange's own `fee_type` and `fee_multiplier`. Rounding is per order, so the tool sizes the order first: `--budget 10` (default) buys as many contracts as $10 covers including the fee; `--contracts N` fixes the count.

**What the columns mean.** `brkeven` is `(ask + fee per contract) / payout`, the win probability you need just to get your money back. `fair` is the de-vigged reference probability. `edge` is `fair − brkeven` in probability points, `ev/ct` is the same thing in dollars per contract, and `roi` is `ev / cost`. A negative number is the normal case: a Kalshi contract that looks a point cheaper than the book's raw price is usually still a little expensive once the vig is removed and the fee is added. The tool prints that without drama.

**Matching games.** A Kalshi event ticker like `KXNFLGAME-26NOV08CINATL` carries the game date (US Eastern) and two team codes run together. The codes are not fixed width (`GB`, `KC`, `LA`, `LAC`, `LAR`...), so the parser tries every split against the checked-in alias table `config/teams/nfl.json` and uses the contract tickers (`...-CIN`, `...-ATL`) to settle anything ambiguous. A sportsbook game matches when it has the same two teams and its kickoff falls on that date in US Eastern time. Exactly one candidate is a match; zero or several are reported as unmatched, never guessed. `close_time` is never used: on an open market it can sit days after kickoff. Checked against 505 real events: all parse, and a Thursday night ticker (`26AUG06`) lines up with a 00:00Z Friday kickoff, which confirms the Eastern-date rule.

**Time gap and live games.** The report prints how far apart the Kalshi snapshot and the odds capture were and flags `STALE` past `--max-gap-minutes` (default 15). An "edge" between a line from noon and a price from 3 pm is just line movement. Games that have already kicked off are listed but not priced: in-play book lines and Kalshi's live market move at different speeds, so a gap between them is lag. `--include-live` prices them anyway, tagged `IN PLAY`.

**Combos.** `kalshi_edge.odds_math.combo` computes the fair multiplier and EV of a parlay under an independence assumption, so the gap between the offered and fair payout is visible. There is no CLI for it and the tool never searches for or ranks combos: at $10 on a typical three-leg parlay the expected loss is about $1.35, and the honest output of a combo evaluator is almost always a negative number.

#### The one secret: `ODDS_API_KEY`

The Odds API's free tier is 500 credits a month, no card. Sign up at [the-odds-api.com](https://the-odds-api.com) and you get a key. It goes in the `ODDS_API_KEY` environment variable and nowhere else:

```bash
# bash/zsh: read -rs does not echo and does not land in shell history
read -rs ODDS_API_KEY && export ODDS_API_KEY
```

```powershell
# PowerShell: Read-Host input is not written to the PSReadLine history file
$env:ODDS_API_KEY = Read-Host "Odds API key"
```

Why not a `--api-key` flag or a config file: a flag is saved in shell history and visible in the process list; a config file next to `series.example.json` is one careless `git add .` away from a public repository, and this repository is public. The code reads the environment in exactly one place (`kalshi_edge.odds.main`) and hands the value to the client. Library code never reads the environment, which is also why the tests need no monkeypatching of globals.

The API wants the key as a query parameter (`apiKey=...`), which means every URL is a leak: `requests` puts the URL in its exception text, and a debug logger would print it. The client builds requests with `params=`, scrubs `apiKey=` from every exception it re-raises and every log record on its logger, and has tests that trigger failures with a known fake key and assert the key is absent from the exception and the captured logs.

If the key ever leaks (a screenshot, a pasted traceback), rotate it in The Odds API dashboard first and work out how it leaked second. `.env` and `.envrc` are gitignored for people who use `direnv`; the GitHub Actions workflow runs `gitleaks` on every push and pull request so a committed key fails CI.

**Credits.** `/odds` costs one credit per region per market, so the default `--regions us,eu` with `h2h` is 2 credits per run; `/sports` is free. Every response carries `x-requests-remaining` and `x-requests-used`, which the client logs and prints. Before spending anything the run reads those counters from the free endpoint and stops if fewer than `--min-remaining` (default 50) are left, so a scheduled job with a bug cannot burn the month's allowance.

## The non-sports scan

`python -m kalshi_edge.scan` is one daily list for an account that bets about $10 and does not want the money tied up for months. It reads the public Kalshi API (no key, `*_dollars` quotes, cursor pages, the same retry and pacing as `collect`), the National Weather Service, and the Cleveland Fed's inflation-nowcast files. It writes the weather and economics markets into the same SQLite tables `collect` uses, including `series_fees` from `GET /series`. The short category sweep is a listing, not a price history, and is not stored.

An **edge** is a model probability above the break-even of buying at the ask. The break-even is `(ask + taker fee per contract) / $1` for as many contracts as $10 covers, fee included, using that series' `fee_type` and `fee_multiplier`. Paying the ask is already the cost of crossing the spread, so the spread is not added a second time. When no side clears that bar the report says `nothing clears costs today`. A `THIN`, `WIDE`, or `NOBID` flag does not remove a row: `THIN` is 24-hour volume under 50 contracts, open interest under 20, or fewer contracts at the ask than the $10 order; `WIDE` is a spread over 5 cents; `NOBID` is a bid of zero. A thin quote can clear the bar and still be untradable at $10. Settlement times are printed in `America/Chicago`.

### Weather

Daily city-high series are discovered from `GET /series`, not from a hardcoded list of cities: category `Climate and Weather`, frequency `daily`, tag `Daily temperature`, ticker starting with `KXHIGH`. That is the set that includes `KXHIGHNY`, `KXHIGHCHI`, `KXHIGHMIA`, `KXHIGHAUS`, `KXHIGHLAX`, `KXHIGHDEN`, and the other cities the exchange has filed the same way. A series with no open markets is skipped.

The settlement station comes from the market rules, not from the series title. The rules name a CLI product, for example `New York City (CLINYC)` or `Louisville (CLISDF)`. `KXHIGHTSDF` is titled like San Antonio; the rules settle Louisville. The NWS station id is `K` plus the last three letters of that CLI code (`CLINYC` -> `KNYC`, Central Park). That mapping was checked against `api.weather.gov` for every open daily-high series when this was written. If a future code 404s, that city is skipped and the report says so.

The forecast is the highest hourly temperature `api.weather.gov` still shows for the market's local calendar date. The 12-hour "daytime" period is one rounded number for 6am to 6pm. The contract settles on the calendar-day maximum, and the hourly grid is that maximum with the hour attached, so the lead time is the hour of the high. Two gaps make the grid unusable, and the city is then not priced. The afternoon has to still be ahead. And if local noon has already passed, every hour from noon through the current hour has to still be in the grid: the hourly product drops hours once they end, so a 3pm fragment that reads 65°F and falling is the temperature after the peak, not the peak, even when a 5pm hour is still listed. The client sends a User-Agent, as that API asks, and uses the same pacing and retries as the Kalshi client.

The probability is a normal distribution centered on that forecast high. Settlement is a whole number of degrees: "65° to 66°" is the integers 65 and 66, a latent temperature in `[64.5, 66.5)`; "greater than 66" is 67 or above. The brackets of a complete ladder sum to 1.

The width is an **assumption**, not a calibration. `api.weather.gov` serves the current forecast only, so there is no public archive here to fit lead-time error to Kalshi's settlements, and this tool does not pretend there is. Mean absolute error is taken from a small table in the neighborhood of published NWS max-temperature errors: 2°F inside 12 hours, 2.5°F inside a day, then 3.2, 3.8, 4.4, and 5°F. A normal distribution has `sd = MAE * sqrt(pi / 2)`. Half a degree is added in quadrature for the gap between the NWS grid and the Weather Company CLI print the contract actually settles on. Every weather line says `assumed sd` and `not fit to settlements`.

Under that standard deviation a two-degree bracket holds about a quarter of the probability even when the forecast sits on it. The live book often prices that same bracket much higher, so NO on the market's favorite bin clears this test over and over. Those lines are one assumption repeating, not a pile of unrelated discoveries. The text prints the 12 largest gaps and the count of how many sides cleared; `--json` has every side. A penny ask on a tail can also clear, and it is flagged `THIN` or `NOBID` when the book will not fill $10.

### Economic releases

The series are the ones the exchange lists for these prints: `KXCPI` and `KXCPICORE` (month-over-month), `KXCPIYOY` and `KXCPICOREYOY` (year-over-year), `KXPAYROLLS`, `KXU3` (unemployment), `KXJOBLESSCLAIMS`, `KXFEDDECISION`, and `KXFED`. Core CPI is included because the same free file publishes it. Open markets are listed with their settlement time. Releases inside `--horizon-days` (default 30) also show the strikes nearest a 50-cent yes ask, which is where the book is actually talking. Later meetings are named with their settlement time and left out of the ranking, because that is money tied up past the horizon.

A model probability is attached only where a free public reference covers that contract's month.

* **CPI.** The Cleveland Fed publishes its daily nowcast as two JSON files linked from [its nowcasting page](https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting): month-over-month and year-over-year, headline and core. Each file is one chart per month. A finished month has an actual; the current month has a nowcast and no actual. The model is a normal distribution on that nowcast. Its standard deviation comes from the same file: actual minus the first nowcast of each finished month, and actual minus the last nowcast before the actual printed. Early in the month the error is the early one; near the release it is the final one; in between the two variances are blended by how long the current path is compared with a typical finished month. The historical mean error is reported and not removed. Kalshi settles these contracts on the one-decimal published print ("more than 0.2%" is a print of 0.3 or higher). The probability is the chance the unrounded change rounds to such a print. A month the file does not cover, and a file that fails to download or has too little history, is price and settlement only. The report says which.

* **Payrolls, unemployment, initial claims, and the Fed.** Price and settlement date only. There is no free consensus feed for the jobs report or for claims, and none is invented. The CME FedWatch API is a paid product. A 30-day fed-funds future settles to the average effective rate over the month, not to the post-meeting target, and backing a meeting probability out of that average is easy to get wrong. This tool does not do it.

### Short-dated markets

Any open market whose series is in `--category` (default `Science and Technology`, `Companies`, and `AI`) or whose series is tagged `AI` outside sports, elections, entertainment, mentions, and politics, and whose expected settlement is within the horizon. Nearest first. The exchange does not filter markets by category, so the scan pages open events to learn which of those series are actually open, then reads each series' open markets and keeps the ones whose expected settlement (or close, when no expected settlement is published) falls inside the horizon. The exchange's `max_close_ts` filter is not used here: it cuts on `close_time`, and a market can be expected to settle this week while its scheduled close is months out. There is no model behind this list. None of it is an edge. Tech and AI show up here because that is where the exchange files them, not because a forecast was built for them.

## How to run it (the routine)

The tool is only as good as the data on disk, and the data has to be collected while it exists. Here is what to run, how often, and why.

**Weekly, after the week's games (Tuesday is good): collect settled markets.**

```bash
python -m kalshi_edge.collect --series KXNFLGAME --mode settled
```

Kalshi archives settled markets, and its candlestick endpoint returns 404 for markets older than the archive cutoff. Markets the collector never saw before that happens are lost to the backtest for good. This run upserts, so running it more often than needed costs nothing but a few requests. It also refreshes the fee schedule in `series_fees`.

**Whenever you want the calibration picture: run the backtest.**

```bash
python -m kalshi_edge.calibrate --series KXNFLGAME
```

Only the newly settled markets are fetched; the rest come from the cache. The sample grows by about 30 games a week, and every bucket needs a few hundred observations before its "actual" column means much, so expect this to be interesting in months, not weeks.

**Before placing a trade, or on a timer on game day: capture both sides within minutes of each other.**

```bash
python -m kalshi_edge.odds --sport americanfootball_nfl          # 2 credits
python -m kalshi_edge.collect --series KXNFLGAME --mode snapshot  # free
python -m kalshi_edge.compare --series KXNFLGAME
```

The order matters only in that the two captures should be close together; `compare` prints the gap and flags it when it is wide. `compare` makes no network calls, so you can rerun it with `--devig multiplicative`, `--reference median` or `--contracts 10` as often as you like against the same captures. Snapshot runs also build the price history the roadmap's second calibration view will use.

At two credits a run, the free tier covers one odds capture a day for the month with 400 credits to spare, or a few per game day.

**Once a day, for anything that is not a game: the non-sports scan.**

```bash
python -m kalshi_edge.scan
```

No key. It takes a few minutes because it walks the open events before it reads the short-dated series, at the same quarter-second pace as `collect`. The first line that matters is either a list of sides whose model beats the ask after fees, or `nothing clears costs today`. A positive line is a question, the same as a positive line in `compare`.

**What to do with a positive edge.** Treat it as a question, not an answer. Check the gap, check that the reference was Pinnacle and not a median of two books, check `alt` to see whether the other de-vig method agrees, and remember that the book depth at that ask is unknown. A +0.3 point edge on one contract is inside the noise of every one of those. Nothing in this repository is a reason to bet more than you already planned to.

## Setup

Python 3.11 or newer. From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt   # pytest and ruff, only for tests
pip install -r requirements-plot.txt  # matplotlib, only for calibrate --plot
```

On Windows, WSL works the same way; make the virtual environment inside the shell you will run from, because a Windows `.venv` cannot be used from WSL or the other way round (`.venv*` is gitignored, so a second one such as `.venv-wsl` is fine).

`requirements.txt` is `requests` plus `tzdata` (time zone data for the game matcher; Windows does not ship it). The database file is created on first run at `data/kalshi.db` (override with `--db`). That directory is gitignored.

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

# Cutoff twelve hours earlier than the default, ten contracts per market, JSON copy of the report.
python -m kalshi_edge.calibrate --series KXNFLGAME --lead-hours 12 --contracts 10 --json report.json
```

Useful collector flags: `--db PATH`, `--min-interval SECONDS`, `--limit N` (page size, 1–1000), `--max-pages N`, `--base-url URL`. Calibration flags: `--buckets N`, `--contracts N`, `--all-contracts`, `--lookback-hours H`, `--event-hours H`, `--lead-hours H`, `--refresh`, `--max-fetch N`, `--json PATH`.

```bash
# Sportsbook moneylines for the NFL from US books plus Pinnacle (2 credits). Needs ODDS_API_KEY.
python -m kalshi_edge.odds --sport americanfootball_nfl --regions us,eu

# Compare the newest Kalshi snapshot with the newest odds capture. No network.
python -m kalshi_edge.compare --series KXNFLGAME

# Same, with the other de-vig method, a median of the books, a fixed order size, and a JSON copy.
python -m kalshi_edge.compare --series KXNFLGAME --devig multiplicative --reference median --contracts 10 --json compare.json
```

Odds flags: `--sport KEY`, `--regions us,eu`, `--min-remaining N`, `--db PATH`, `--base-url URL`. Compare flags: `--devig power|multiplicative`, `--reference pinnacle|median`, `--budget DOLLARS` or `--contracts N`, `--max-gap-minutes M`, `--include-live`, `--teams PATH`, `--json PATH`.

```bash
# Daily non-sports list. No key. Weather and CPI are modeled; jobs, claims, and the Fed are price only.
python -m kalshi_edge.scan

# A shorter horizon, and a JSON copy of the same report.
python -m kalshi_edge.scan --horizon-days 14 --json scan.json

# Weather and the economic series only, skipping the category sweep.
python -m kalshi_edge.scan --skip-short
```

Scan flags: `--budget DOLLARS` (default 10), `--horizon-days N` (default 30), `--category NAME` (repeatable; default Science and Technology, Companies, and AI), `--skip-short`, `--short-limit N`, `--db PATH`, `--min-interval SECONDS`, `--json PATH`.

### Sample scan output

Real output from a live run. The block below is the command's own text, not a mock.

```text
scan  generated=2026-11-11T03:19:23Z  budget=$10.00  horizon=30d  zone=America/Chicago
modeled sides=368  clear=180  econ events=31  short-dated=940
edges that clear costs (180); showing the best 12. The rest are in --json.
  KXHIGHLAX-26NOV11-T83  NO  Los Angeles: 82° or below  ask 0.3800  model 0.865  brkeven 0.397  edge +46.8pp  settles 2026-11-12 14:00 CST  -
    NWS hourly high 86F at KLAX (Los Angeles, Los Angeles International Airport), lead 17h, assumed sd 3.2F (not fit to settlements)
  KXHIGHTMIN-26NOV11-T55  NO  Minneapolis: 56° or above  ask 0.3900  model 0.865  brkeven 0.407  edge +45.8pp  settles 2026-11-12 14:00 CST  -
    NWS hourly high 52F at KMSP (Minneapolis, Minneapolis-St. Paul International Airport), lead 17h, assumed sd 3.2F (not fit to settlements)
  KXHIGHTOKC-26NOV11-B72.5  NO  Oklahoma City: 72° to 73°  ask 0.3500  model 0.817  brkeven 0.366  edge +45.1pp  settles 2026-11-12 14:00 CST  -
    NWS hourly high 70F at KOKC (Oklahoma City, Will Rogers World Airport), lead 18h, assumed sd 3.2F (not fit to settlements)
  KXHIGHTSDF-26NOV11-B71.5  NO  Louisville: 71° to 72°  ask 0.4100  model 0.863  brkeven 0.427  edge +43.6pp  settles 2026-11-12 14:00 CST  -
    NWS hourly high 68F at KSDF (Louisville Muhammad Ali International Airport), lead 17h, assumed sd 3.2F (not fit to settlements)
  KXHIGHTDAL-26NOV11-B80.5  NO  Dallas: 80° to 81°  ask 0.3100  model 0.756  brkeven 0.325  edge +43.1pp  settles 2026-11-12 14:00 CST  -
    NWS hourly high 80F at KDFW (Dallas/Fort Worth International Airport), lead 18h, assumed sd 3.2F (not fit to settlements)
  KXHIGHTLV-26NOV11-B76.5  NO  Las Vegas: 76° to 77°  ask 0.3100  model 0.756  brkeven 0.325  edge +43.1pp  settles 2026-11-12 14:00 CST  -
    NWS hourly high 76F at KLAS (Harry Reid International Airport), lead 19h, assumed sd 3.2F (not fit to settlements)
  KXHIGHTPHX-26NOV11-B83.5  NO  Phoenix: 83° to 84°  ask 0.3200  model 0.756  brkeven 0.336  edge +42.0pp  settles 2026-11-12 14:00 CST  -
    NWS hourly high 84F at KPHX (Phoenix, Phoenix Sky Harbor International Airport), lead 19h, assumed sd 3.2F (not fit to settlements)
  KXHIGHTHOU-26NOV11-T76  NO  Houston: 75° or below  ask 0.1400  model 0.563  brkeven 0.149  edge +41.4pp  settles 2026-11-12 14:00 CST  -
    NWS hourly high 76F at KHOU (Houston, Houston Hobby Airport), lead 18h, assumed sd 3.2F (not fit to settlements)
  KXHIGHMIA-26NOV11-B85.5  NO  Miami: 85° to 86°  ask 0.3500  model 0.778  brkeven 0.366  edge +41.2pp  settles 2026-11-12 14:00 CST  -
    NWS hourly high 84F at KMIA (Miami, Miami International Airport), lead 14h, assumed sd 3.2F (not fit to settlements)
  KXHIGHDEN-26NOV11-B65.5  NO  Denver: 65° to 66°  ask 0.4100  model 0.817  brkeven 0.427  edge +39.0pp  settles 2026-11-12 14:00 CST  -
    NWS hourly high 63F at KDEN (Denver, Denver International Airport), lead 18h, assumed sd 3.2F (not fit to settlements)
  KXHIGHTTTN-26NOV11-B64.5  NO  Trenton: 64° to 65°  ask 0.4300  model 0.817  brkeven 0.447  edge +37.0pp  settles 2026-11-12 14:00 CST  WIDE
    NWS hourly high 62F at KTTN (Trenton-Mercer Airport), lead 15h, assumed sd 3.2F (not fit to settlements)
  KXHIGHTSEA-26NOV11-B66.5  NO  Seattle: 66° to 67°  ask 0.4700  model 0.817  brkeven 0.487  edge +32.9pp  settles 2026-11-12 14:00 CST  -
    NWS hourly high 64F at KSEA (Seattle, Seattle-Tacoma International Airport), lead 18h, assumed sd 3.2F (not fit to settlements)
economic releases (price only except where a nowcast month matches):
  KXJOBLESSCLAIMS-26NOV12  initial jobless claims  settles 2026-11-12 08:59 CST  11 strikes  inside horizon
    price only: no free public consensus; price and settlement only
    KXJOBLESSCLAIMS-26NOV12-190000  At least 190,000  YES ask 0.4100  NO ask 0.6000  vol24h 208  -
    KXJOBLESSCLAIMS-26NOV12-185000  At least 185,000  YES ask 0.6200  NO ask 0.3900  vol24h 1085  -
    KXJOBLESSCLAIMS-26NOV12-180000  At least 180,000  YES ask 0.9000  NO ask 0.1400  vol24h 30  THIN
    KXJOBLESSCLAIMS-26NOV12-195000  At least 195,000  YES ask 0.1000  NO ask 0.9100  vol24h 2041  -
    KXJOBLESSCLAIMS-26NOV12-200000  At least 200,000  YES ask 0.0500  NO ask 0.9900  vol24h 137  -
    KXJOBLESSCLAIMS-26NOV12-205000  At least 205,000  YES ask 0.0300  NO ask 0.9900  vol24h 36  THIN
  KXPAYROLLS-26NOV  nonfarm payrolls  settles 2026-12-04 09:00 CST  13 strikes  inside horizon
    price only: no free public consensus; price and settlement only
    KXPAYROLLS-26NOV-T90000  Above 90,000  YES ask 0.5100  NO ask 0.5800  vol24h 15  WIDE,THIN
    KXPAYROLLS-26NOV-T100000  Above 100,000  YES ask 0.4500  NO ask 0.6200  vol24h 5  WIDE,THIN
    KXPAYROLLS-26NOV-T80000  Above 80,000  YES ask 0.5800  NO ask 0.4700  vol24h 169  -
    KXPAYROLLS-26NOV-T70000  Above 70,000  YES ask 0.6400  NO ask 0.4500  vol24h 108  WIDE
    KXPAYROLLS-26NOV-T125000  Above 125,000  YES ask 0.3200  NO ask 0.7200  vol24h 0  THIN
    KXPAYROLLS-26NOV-T60000  Above 60,000  YES ask 0.6900  NO ask 0.3800  vol24h 11  WIDE,THIN
  KXU3-26NOV  unemployment rate  settles 2026-12-04 09:00 CST  14 strikes  inside horizon
    price only: no free public consensus; price and settlement only
    KXU3-26NOV-T4.1  Above 4.1%  YES ask 0.5000  NO ask 0.5700  vol24h 68  WIDE
    KXU3-26NOV-T4.0  Above 4.0%  YES ask 0.8400  NO ask 0.1900  vol24h 250  -
    KXU3-26NOV-T4.2  Above 4.2%  YES ask 0.0700  NO ask 0.9500  vol24h 482  -
    KXU3-26NOV-T4.3  Above 4.3%  YES ask 0.0400  NO ask 0.9900  vol24h 37  THIN
    KXU3-26NOV-T4.6  Above 4.6%  YES ask 0.0400  NO ask 1.0000  vol24h 0  NOBID,THIN
    KXU3-26NOV-T4.7  Above 4.7%  YES ask 0.0400  NO ask 1.0000  vol24h 21  NOBID,THIN
  KXFED-26DEC  Fed funds upper bound  settles 2026-12-09 13:05 CST  11 strikes  inside horizon
    price only: no free probability source (same reason as the meeting decision); price and settlement only
    KXFED-26DEC-T4.25  Above 4.25%  YES ask 0.1000  NO ask 0.9100  vol24h 175785  -
    KXFED-26DEC-T4.00  Above 4.00%  YES ask 0.9600  NO ask 0.0500  vol24h 63603  -
    KXFED-26DEC-T4.50  Above 4.50%  YES ask 0.0200  NO ask 0.9900  vol24h 666  -
    KXFED-26DEC-T4.75  Above 4.75%  YES ask 0.0100  NO ask 1.0000  vol24h 0  NOBID,THIN
    KXFED-26DEC-T5.00  Above 5.00%  YES ask 0.0100  NO ask 1.0000  vol24h 0  NOBID,THIN
    KXFED-26DEC-T5.25  Above 5.25%  YES ask 0.0100  NO ask 1.0000  vol24h 0  NOBID,THIN
  KXFEDDECISION-26DEC  Fed meeting decision  settles 2026-12-09 13:05 CST  5 strikes  inside horizon
    price only: CME FedWatch is a paid API; a fed-funds future is a month average, not the post-meeting rate
    KXFEDDECISION-26DEC-H25  Hike 25bps  YES ask 0.8600  NO ask 0.1500  vol24h 697768  -
    KXFEDDECISION-26DEC-H26  Hike >25bps  YES ask 0.1200  NO ask 0.8900  vol24h 1766784  -
    KXFEDDECISION-26DEC-H0  Fed maintains rate  YES ask 0.0500  NO ask 0.9600  vol24h 654300  -
    KXFEDDECISION-26DEC-C25  Cut 25bps  YES ask 0.0100  NO ask 1.0000  vol24h 23756  NOBID
    KXFEDDECISION-26DEC-C26  Cut >25bps  YES ask 0.0100  NO ask 1.0000  vol24h 4100  NOBID
  KXCPI-26NOV  CPI, month over month  settles 2026-12-10 08:56 CST  9 strikes  inside horizon
    modeled from Cleveland Fed month-over-month nowcast (CPI Inflation) 0.14 (sd 0.22, 158 months)
    KXCPI-26NOV-T0.0  Above 0.0%  YES ask 0.4300  NO ask 0.5900  vol24h 7343  -
    KXCPI-26NOV-T-0.1  Above -0.1%  YES ask 0.7800  NO ask 0.2700  vol24h 946  -
    KXCPI-26NOV-T0.1  Above 0.1%  YES ask 0.1800  NO ask 0.8300  vol24h 11045  -
    KXCPI-26NOV-T-0.2  Above -0.2%  YES ask 0.9200  NO ask 0.0900  vol24h 1701  -
    KXCPI-26NOV-T0.2  Above 0.2%  YES ask 0.0800  NO ask 0.9400  vol24h 4829  -
    KXCPI-26NOV-T-0.3  Above -0.3%  YES ask 0.9500  NO ask 0.1000  vol24h 1688  -
  KXCPICORE-26NOV  core CPI, month over month  settles 2026-12-10 09:00 CST  11 strikes  inside horizon
    modeled from Cleveland Fed month-over-month nowcast (Core CPI Inflation) 0.21 (sd 0.16, 158 months)
    KXCPICORE-26NOV-T0.2  Above 0.2%  YES ask 0.4400  NO ask 0.5800  vol24h 188  -
    KXCPICORE-26NOV-T0.3  Above 0.3%  YES ask 0.1400  NO ask 0.8800  vol24h 3107  -
    KXCPICORE-26NOV-T0.1  Above 0.1%  YES ask 0.9400  NO ask 0.1800  vol24h 2483  WIDE
    KXCPICORE-26NOV-T0.4  Above 0.4%  YES ask 0.0400  NO ask 0.9700  vol24h 0  THIN
    KXCPICORE-26NOV-T0.0  Above 0.0%  YES ask 0.9700  NO ask 0.0400  vol24h 15  THIN
    KXCPICORE-26NOV-T0.5  Above 0.5%  YES ask 0.0200  NO ask 0.9900  vol24h 1  THIN
  KXCPICOREYOY-26NOV  core CPI, year over year  settles 2026-12-10 09:00 CST  16 strikes  inside horizon
    modeled from Cleveland Fed year-over-year nowcast (Core CPI Inflation) 2.87 (sd 0.27, 158 months)
    KXCPICOREYOY-26NOV-T2.8  Above 2.8%  YES ask 0.6400  NO ask 0.3700  vol24h 2993  -
    KXCPICOREYOY-26NOV-T3.0  Above 3.0%  YES ask 0.2800  NO ask 0.8200  vol24h 230  WIDE
    KXCPICOREYOY-26NOV-T2.9  Above 2.9%  YES ask 0.2700  NO ask 0.8000  vol24h 115  WIDE
    KXCPICOREYOY-26NOV-T2.7  Above 2.7%  YES ask 0.8400  NO ask 0.2200  vol24h 1470  WIDE
    KXCPICOREYOY-26NOV-T2.6  Above 2.6%  YES ask 0.9200  NO ask 0.1700  vol24h 1  WIDE,THIN
    KXCPICOREYOY-26NOV-T3.1  Above 3.1%  YES ask 0.0400  NO ask 0.9800  vol24h 0  THIN
  KXCPIYOY-26NOV  CPI, year over year  settles 2026-12-10 09:00 CST  21 strikes  inside horizon
    modeled from Cleveland Fed year-over-year nowcast (CPI Inflation) 3.94 (sd 0.31, 158 months)
    KXCPIYOY-26NOV-T3.7  Above 3.7%  YES ask 0.6700  NO ask 0.3700  vol24h 10966  -
    KXCPIYOY-26NOV-T3.8  Above 3.8%  YES ask 0.3100  NO ask 0.7000  vol24h 9690  -
    KXCPIYOY-26NOV-T3.9  Above 3.9%  YES ask 0.1800  NO ask 0.8400  vol24h 12291  -
    KXCPIYOY-26NOV-T4.0  Above 4.0%  YES ask 0.1200  NO ask 0.9000  vol24h 7967  -
    KXCPIYOY-26NOV-T3.6  Above 3.6%  YES ask 0.9000  NO ask 0.2500  vol24h 6548  WIDE
    KXCPIYOY-26NOV-T3.5  Above 3.5%  YES ask 0.9200  NO ask 0.1000  vol24h 9754  -
  KXCPI-26DEC  CPI, month over month  settles 2027-01-13 08:56 CST  11 strikes  outside horizon, not ranked
    price only: the nowcast file covers 2026-11, not 2026-12
  KXCPICORE-26DEC  core CPI, month over month  settles 2027-01-13 09:00 CST  11 strikes  outside horizon, not ranked
    price only: the nowcast file covers 2026-11, not 2026-12
  KXCPICOREYOY-26DEC  core CPI, year over year  settles 2027-01-13 09:00 CST  15 strikes  outside horizon, not ranked
    price only: the nowcast file covers 2026-11, not 2026-12
  KXCPIYOY-26DEC  CPI, year over year  settles 2027-01-13 09:00 CST  21 strikes  outside horizon, not ranked
    price only: the nowcast file covers 2026-11, not 2026-12
  KXFED-27JAN  Fed funds upper bound  settles 2027-01-27 13:05 CST  25 strikes  outside horizon, not ranked
    price only: no free probability source (same reason as the meeting decision); price and settlement only
  KXFEDDECISION-27JAN  Fed meeting decision  settles 2027-01-27 13:05 CST  5 strikes  outside horizon, not ranked
    price only: CME FedWatch is a paid API; a fed-funds future is a month average, not the post-meeting rate
  KXFED-27MAR  Fed funds upper bound  settles 2027-03-17 13:05 CDT  25 strikes  outside horizon, not ranked
    price only: no free probability source (same reason as the meeting decision); price and settlement only
  KXFEDDECISION-27MAR  Fed meeting decision  settles 2027-03-17 13:05 CDT  5 strikes  outside horizon, not ranked
    price only: CME FedWatch is a paid API; a fed-funds future is a month average, not the post-meeting rate
  KXFED-27APR  Fed funds upper bound  settles 2027-04-28 13:05 CDT  25 strikes  outside horizon, not ranked
    price only: no free probability source (same reason as the meeting decision); price and settlement only
  KXFEDDECISION-27APR  Fed meeting decision  settles 2027-04-28 13:05 CDT  5 strikes  outside horizon, not ranked
    price only: CME FedWatch is a paid API; a fed-funds future is a month average, not the post-meeting rate
  KXFED-27JUN  Fed funds upper bound  settles 2027-06-09 13:05 CDT  25 strikes  outside horizon, not ranked
    price only: no free probability source (same reason as the meeting decision); price and settlement only
  KXFEDDECISION-27JUN  Fed meeting decision  settles 2027-06-09 13:05 CDT  5 strikes  outside horizon, not ranked
    price only: CME FedWatch is a paid API; a fed-funds future is a month average, not the post-meeting rate
  KXFED-27JUL  Fed funds upper bound  settles 2027-07-28 13:05 CDT  25 strikes  outside horizon, not ranked
    price only: no free probability source (same reason as the meeting decision); price and settlement only
  KXFEDDECISION-27JUL  Fed meeting decision  settles 2027-07-28 13:05 CDT  5 strikes  outside horizon, not ranked
    price only: CME FedWatch is a paid API; a fed-funds future is a month average, not the post-meeting rate
  KXFED-27SEP  Fed funds upper bound  settles 2027-09-15 13:05 CDT  25 strikes  outside horizon, not ranked
    price only: no free probability source (same reason as the meeting decision); price and settlement only
  KXFEDDECISION-27SEP  Fed meeting decision  settles 2027-09-15 13:05 CDT  5 strikes  outside horizon, not ranked
    price only: CME FedWatch is a paid API; a fed-funds future is a month average, not the post-meeting rate
  KXFED-27OCT  Fed funds upper bound  settles 2027-10-27 13:05 CDT  25 strikes  outside horizon, not ranked
    price only: no free probability source (same reason as the meeting decision); price and settlement only
  KXFEDDECISION-27OCT  Fed meeting decision  settles 2027-10-27 13:05 CDT  5 strikes  outside horizon, not ranked
    price only: CME FedWatch is a paid API; a fed-funds future is a month average, not the post-meeting rate
  KXFED-27DEC  Fed funds upper bound  settles 2027-12-08 13:05 CST  25 strikes  outside horizon, not ranked
    price only: no free probability source (same reason as the meeting decision); price and settlement only
  KXFEDDECISION-27DEC  Fed meeting decision  settles 2027-12-08 13:05 CST  5 strikes  outside horizon, not ranked
    price only: CME FedWatch is a paid API; a fed-funds future is a month average, not the post-meeting rate
  KXFED-28JAN  Fed funds upper bound  settles 2028-01-26 13:05 CST  25 strikes  outside horizon, not ranked
    price only: no free probability source (same reason as the meeting decision); price and settlement only
  KXFEDDECISION-28JAN  Fed meeting decision  settles 2028-01-26 13:05 CST  5 strikes  outside horizon, not ranked
    price only: CME FedWatch is a paid API; a fed-funds future is a month average, not the post-meeting rate
short-dated Science and Technology, Companies, AI, nearest first, one strike per event (the yes ask closest to 50 cents). price only, not edges. shown 12 events of 21 (940 markets).
  740 further markets were past the 200 kept in this report
  KXANTHVREQ-10NOV26-T7P2  settles 2026-11-11 09:00 CST  YES ask 0.9900  NO ask 0.0900  vol24h 224  WIDE
    Anthropic Vercel share of AI Gateway requests across the top labs above 7.2%
  KXANTHVSPEND-10NOV26-T46P6  settles 2026-11-11 09:00 CST  YES ask 0.4900  NO ask 1.0000  vol24h 0  NOBID,WIDE,THIN
    Anthropic Vercel share of AI Gateway spend across the top labs above 46.6%
  KXDEEPVREQ-10NOV26-T15P4  settles 2026-11-11 09:00 CST  YES ask 0.1100  NO ask 1.0000  vol24h 127  NOBID,WIDE
    DeepSeek Vercel share of AI Gateway requests across the top labs above 15.4%
  KXGOOGVREQ-10NOV26-T10P5  settles 2026-11-11 09:00 CST  YES ask 0.0600  NO ask 0.9800  vol24h 252  -
    Google Vercel share of AI Gateway requests across the top labs above 10.5%
  KXGOOGVSPEND-10NOV26-T8P1  settles 2026-11-11 09:00 CST  YES ask 0.4900  NO ask 1.0000  vol24h 0  NOBID,WIDE,THIN
    Google Vercel share of AI Gateway spend across the top labs above 8.1%
  KXMOONVSPEND-10NOV26-T13P6  settles 2026-11-11 09:00 CST  YES ask 0.0900  NO ask 1.0000  vol24h 8  NOBID,WIDE,THIN
    Moonshot AI Vercel share of AI Gateway spend across the top labs above 13.6%
  KXOPENVREQ-10NOV26-T22P8  settles 2026-11-11 09:00 CST  YES ask 0.1100  NO ask 1.0000  vol24h 4  NOBID,WIDE,THIN
    OpenAI Vercel share of AI Gateway requests across the top labs above 22.8%
  KXOPENVSPEND-10NOV26-T15P8  settles 2026-11-11 09:00 CST  YES ask 0.1100  NO ask 1.0000  vol24h 145  NOBID,WIDE
    OpenAI Vercel share of AI Gateway spend across the top labs above 15.8%
  KXOSSPEND-10NOV26-T22P0  settles 2026-11-11 09:00 CST  YES ask 0.9900  NO ask 0.9100  vol24h 49  WIDE,THIN
    Open source share of spend above 22.0%
  KXZAIVREQ-10NOV26-T10P8  settles 2026-11-11 09:00 CST  YES ask 0.0900  NO ask 1.0000  vol24h 0  NOBID,WIDE,THIN
    Z.AI Vercel share of AI Gateway requests across the top labs above 10.8%
  KXZAIVSPEND-10NOV26-T17P5  settles 2026-11-11 09:00 CST  YES ask 0.0900  NO ask 1.0000  vol24h 3350  NOBID,WIDE
    Z.AI Vercel share of AI Gateway spend across the top labs above 17.5%
  KXANTHVREQ-12NOV26-T8P2  settles 2026-11-13 09:00 CST  YES ask 0.5500  NO ask 0.6400  vol24h 0  WIDE,THIN
    Anthropic Vercel share of AI Gateway requests across the top labs above 8.2%
notes:
  - weather probabilities use an assumed lead-time sd, not a fit to Kalshi settlements
  - 24 events had no remaining afternoon hour in the NWS forecast (the high is already outside it), including KXHIGHAUS-26NOV10
read this honestly:
  - an edge means the model probability beats the break-even of a $10 taker order at the ask
  - the spread is already in that break-even: you pay the ask, and the spread is not added again
  - weather sd is an assumption about NWS high-temperature error by lead time, not a fit to settlements
  - CPI uses the Cleveland Fed nowcast and that file's own errors; payrolls, claims, and the Fed are price only
  - THIN: 24h volume under 50, open interest under 20, or ask size below the order; WIDE: spread over 5 cents
  - NOBID: the bid is missing or zero. A thin quote can clear this bar and still be untradable at $10
  - this tool never places an order
```

How to read this run (03:19 UTC on Nov 11, which is about 9:19pm Central on Nov 10, so the Nov 11 highs are still ahead and the Nov 10 highs are not):

- **180 sides cleared the fee.** 136 of them are weather and 44 are November CPI (headline and core, month-over-month and year-over-year). Payrolls, unemployment, claims, and both Fed series cleared nothing, because they have no model. The text shows the 12 largest gaps. The other 168 are in the JSON from the same run.
- **The 12 are one assumption, repeated.** Dallas is the plain case: the NWS hourly high at DFW is 80°F and the 80–81° bracket is offered at a NO ask of 0.31. A normal with a 3.2°F standard deviation only puts about a quarter of the probability on a two-degree bin, so NO is scored at 0.76 and the edge is +43 points. Los Angeles is a different gap: the forecast high is 86°F and NO on "82° or below" is offered at 0.38, which the same distribution scores at 0.86. Neither number has been checked against settled highs.
- **The best CPI side did not make the printed 12.** In the JSON it is `KXCPIYOY-26NOV-T3.9` YES at 0.18, model 0.481, break-even 0.190, edge +29.1 points, no thin-book flag. The nowcast behind it is 3.94% year-over-year for November as of the Nov 10 file, with a standard deviation of 0.31 from 158 finished months. December CPI is listed and not modeled: the file's current month is November.
- **Price only, on purpose.** Initial claims settle the morning of Nov 12 (the book's center is near 190,000). Payrolls and unemployment settle Dec 4. The Dec 9 Fed meeting is priced as a hike: "Hike 25bps" is a YES ask of 0.86 on heavy volume, and "Fed maintains rate" is a YES ask of 0.05. There is no probability next to either.
- **The short list is not a bet.** The soonest technology markets are same-morning brackets on AI-gateway share. Most of those books are one-sided (`NOBID`, `WIDE`). They are dates and prices.
- **Nov 10 was not priced.** Twenty-four city events were still open and were skipped, because the afternoon had already left the NWS hourly forecast.


### Sample comparison output

Real output from the first live run on 2026-11-08, about 20 minutes into the 4:25 pm Eastern games. The odds capture cost 2 credits (498 left), came back with 29 bookmakers and 8 games, and the Kalshi snapshot followed one second later. The report shown is from rerunning `compare` against that database with the current code.

```text
compare  series=KXNFLGAME  sport=americanfootball_nfl  generated=2026-11-08T21:44:38Z
devig=power  reference=pinnacle  budget=$10
kalshi snapshot=2026-11-08T21:40:01Z  odds capture=2026-11-08T21:40:00Z  gap=0m  (max 15m)
fees: quadratic_with_maker_fees x1 from GET /series (fetched 2026-11-08T21:40:01Z)
open contracts=40  matched events=2  unmatched=14

KXNFLGAME-26NOV08TBCHI  Tampa Bay Buccaneers at Chicago Bears  kickoff 2026-11-09T01:20:00Z  ref=pinnacle (21:39Z)  books=28
 contract side    ask    n  fee/ct brkeven   fair    alt    edge    ev/ct     roi
 CHI      YES    0.72   13  0.0146  0.7346 0.7027 0.6943  -3.2pp  -0.0319   -4.3%
 CHI      NO     0.29   32  0.0147  0.3047 0.2973 0.3057  -0.7pp  -0.0074   -2.4%
 TB       YES    0.29   32  0.0147  0.3047 0.2973 0.3057  -0.7pp  -0.0074   -2.4%
 TB       NO     0.72   13  0.0146  0.7346 0.7027 0.6943  -3.2pp  -0.0319   -4.3%

KXNFLGAME-26NOV09BUFMIN  Buffalo Bills at Minnesota Vikings  kickoff 2026-11-10T01:15:00Z  ref=pinnacle (21:39Z)  books=28
 contract side    ask    n  fee/ct brkeven   fair    alt    edge    ev/ct     roi
 BUF      YES    0.62   15  0.0167  0.6367 0.6213 0.6152  -1.5pp  -0.0154   -2.4%
 BUF      NO     0.39   24  0.0167  0.4067 0.3787 0.3848  -2.8pp  -0.0280   -6.9%
 MIN      YES    0.39   24  0.0167  0.4067 0.3787 0.3848  -2.8pp  -0.0280   -6.9%
 MIN      NO     0.62   15  0.0167  0.6367 0.6213 0.6152  -1.5pp  -0.0154   -2.4%

in play, not priced (4; live book lines move faster than Kalshi's, so any gap is lag; --include-live shows them anyway):
  KXNFLGAME-26NOV08ARISEA  kickoff 2026-11-08T21:25:42Z
  KXNFLGAME-26NOV08GBNE  kickoff 2026-11-08T21:25:34Z
  KXNFLGAME-26NOV08HOULAC  kickoff 2026-11-08T21:05:49Z
  KXNFLGAME-26NOV08LVSF  kickoff 2026-11-08T21:25:35Z

unmatched (14; the odds feed covered kickoffs from 2026-11-08T18:02:45Z to 2026-11-10T01:15:00Z):
  KXNFLGAME-26NOV12WASNYG: New York Giants vs Washington Commanders: no sportsbook game with these teams
  KXNFLGAME-26NOV15BUFNYJ: Buffalo Bills vs New York Jets: no sportsbook game with these teams
  KXNFLGAME-26NOV15CARNO: Carolina Panthers vs New Orleans Saints: no sportsbook game with these teams
  KXNFLGAME-26NOV15HOUCLE: Cleveland Browns vs Houston Texans: no sportsbook game with these teams
  KXNFLGAME-26NOV15JACTEN: Jacksonville Jaguars vs Tennessee Titans: no sportsbook game with these teams
  KXNFLGAME-26NOV15KCATL: Atlanta Falcons vs Kansas City Chiefs: no sportsbook game with these teams
  KXNFLGAME-26NOV15LARARI: Arizona Cardinals vs Los Angeles Rams: no sportsbook game with these teams
  KXNFLGAME-26NOV15MIAIND: Indianapolis Colts vs Miami Dolphins: no sportsbook game with these teams
  KXNFLGAME-26NOV15MINGB: Green Bay Packers vs Minnesota Vikings: no sportsbook game with these teams
  KXNFLGAME-26NOV15NEDET: Detroit Lions vs New England Patriots: no sportsbook game with these teams
  KXNFLGAME-26NOV15PITCIN: Cincinnati Bengals vs Pittsburgh Steelers: no sportsbook game with these teams
  KXNFLGAME-26NOV15SEALV: Las Vegas Raiders vs Seattle Seahawks: no sportsbook game with these teams
  KXNFLGAME-26NOV15SFDAL: Dallas Cowboys vs San Francisco 49ers: no sportsbook game with these teams
  KXNFLGAME-26NOV16LACBAL: Baltimore Ravens vs Los Angeles Chargers: no sportsbook game with these teams

columns: ask = Kalshi ask; n = contracts; fee/ct = taker fee per contract; brkeven = win probability needed to break even; fair = de-vigged reference probability (power); alt = same with multiplicative; edge = fair - brkeven; ev/ct = expected profit per contract in dollars; roi = ev / cost.
read this honestly:
  - fair probabilities come from one reference (a sportsbook), not from the truth
  - edge is per contract at the current ask; book depth at that price is unknown
  - NFL ties are ignored: a book pushes, Kalshi pays 0.5 (about 0.2% of games)
  - a wide time gap between captures means the 'edge' may just be line movement
```

How to read it:

- **Both pre-game games are negative on every side**, by 0.7 to 3.2 points against Pinnacle. That is the expected result: Kalshi's ask plus the fee is a little above the book's fair price. There is nothing to buy here, and the tool says so plainly.
- **Four games were already in play and are listed, not priced.** Live book lines move on every snap, and Kalshi's live market lags them by a different amount each minute. In the first run, before this rule existed, those games showed "edges" of up to +2 points. They were latency, not mispricing. `--include-live` prices them anyway, tagged `IN PLAY`.
- **The 14 unmatched games are next week's.** The free odds feed lists the next few days only (the window is printed), so there was nothing to match them to yet. They match once the books post lines.
- Four rows per game because buying NO on one team and YES on the other are the same bet at possibly different asks; the table shows both so the cheaper one is visible.
- `fees:` names the schedule the exchange reported for this series. `KXNFLGAME` charges `quadratic_with_maker_fees`, which for a taker is the same quadratic formula.

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

SQLite, schema version 3 in `schema_meta`. Older files are upgraded in place on open: a version 1 file gains two columns on `markets` (they stay `NULL` until the next `settled` run rewrites each row), and the version 3 tables are created when missing.

`markets` — one row per ticker. Reruns update the row. `occurrence_datetime` and `expected_expiration_time` are the event timing fields the backtest reads. `source` is `live` or `historical`. A live row that already has a `result` is kept when the same ticker also shows up in the archive. A snapshot of a market that had not resolved yet does not block a later historical settlement: after Kalshi archives the contract, that archive row is the only copy of the payoff. The open-market price snapshot stays either way.

`price_snapshots` — primary key `(ticker, captured_at)`. Float columns are the parsed dollar prices. `yes_bid_dollars` and the other `*_dollars` columns keep the API's original strings.

`pre_event_prices` — one row per ticker: `cutoff_ts` (Unix second treated as the event start), `cutoff_source` (which market field it came from), `candle_end_ts` and the closing `yes_bid` / `yes_ask` / `last_price` of the chosen bar. A market with no two-sided bar before the cutoff is stored with those `NULL` so the miss is remembered.

`odds_captures` — one row per `odds` run (sport, regions, event and row counts), written even when the provider returned nothing. The newest row here defines the latest capture, so an empty run supersedes older odds instead of leaving `compare` on stale lines.

`sportsbook_odds` — one row per bookmaker outcome per capture, primary key `(source, event_id, bookmaker, market, outcome, captured_at)`. `price_decimal` is the decimal-odds text exactly as the provider sent it; nothing is de-vigged on the way in, so the method can change without re-collecting. `captured_at` is our run time, `book_updated` the bookmaker's own `last_update`.

`series_fees` — `fee_type` and `fee_multiplier` per series from `GET /series/{ticker}`, refreshed by every `collect` run.

`line_matches` — which sportsbook `event_id` each Kalshi `event_ticker` was tied to, the matching `method`, and `kickoff_delta_s` (kickoff minus the ticker date at midnight US Eastern). Rewritten by every `compare` run so a wrong match can be traced.

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

The tests parse fixture payloads (a candlestick history and an Odds API response in `tests/fixtures/`) and talk to fake HTTP sessions. They call neither Kalshi, nor The Odds API, nor the National Weather Service, nor the Cleveland Fed. The fee tests check the formula against the worked examples in Kalshi's fee schedule; the de-vig and EV tests check the worked examples in the design doc; the client tests trigger failures with a fake key and assert it never appears in an exception or a log line; the compare test runs the whole pipeline on a fixture database and checks the printed table. The scan tests check the temperature brackets, the assumed lead-time error, the nowcast parser on a small chart, and a full report built from fake pages, including the case where nothing clears the fee.

`ruff check .` and `pytest` run in GitHub Actions on every push and pull request, along with `gitleaks` for committed secrets. `.pre-commit-config.yaml` runs the same secret scan locally for anyone who installs [pre-commit](https://pre-commit.com).

## Roadmap

Not built. Nothing below is in this repository yet.

* **Calibration from the snapshot history.** The backtest uses candlesticks; a second view built from `price_snapshots` (prices at the exact moments the collector ran) would cross-check it once enough snapshot runs exist.
* **Calibration chart.** A PNG of predicted against realized rates per bucket. The `--json` report has everything a plot needs.
* **Order book depth.** `compare` reports edge per contract at the ask. `GET /markets/{ticker}/orderbook` is public and would say how many contracts are actually there.
* **Combo CLI.** The parlay math exists in `kalshi_edge.odds_math.combo`; there is no command around it, by design until there is a reason to evaluate a specific combo.
* **Kalshi-side sportsbook history.** `sportsbook_odds` keeps every capture, so a line-movement view (how the book and Kalshi moved toward kickoff) is a query away once there are captures.

## License

MIT. See [LICENSE](LICENSE).
