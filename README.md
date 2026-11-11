# kalshi-edge-finder

Python tools that read [Kalshi](https://kalshi.com) prediction-market data from the public Trade API, store it in a local SQLite file, check whether the prices were honest probabilities, compare them with sportsbook moneylines, and list short-dated non-sports markets next to a weather forecast or a public inflation nowcast. The Kalshi side needs no account and no API key. The sportsbook side needs one free key from [The Odds API](https://the-odds-api.com), kept in an environment variable. Nothing in this repository places an order.

`kalshi_edge.collect` gets clean market data onto disk. `kalshi_edge.calibrate` asks whether contracts priced at 45 cents resolved YES about 45% of the time, and whether buying them would have made money after fees. `kalshi_edge.odds` and `kalshi_edge.compare` put a sportsbook's de-vigged probability next to each open Kalshi game contract. `kalshi_edge.scan` is the daily list for everything that is not a game: city high temperatures, a handful of economic releases, and other markets in chosen categories that settle soon. `kalshi_edge.weather_calibrate` fits the daily-high error to settled city markets. See [How to run it](#how-to-run-it-the-routine) for the weekly routine, [The non-sports scan](#the-non-sports-scan) for the daily one, [docs/RUNBOOK.md](docs/RUNBOOK.md) for a step-by-step operator's guide, and [Roadmap](#roadmap) for what is still missing.

## What exists now

| Command | What it does |
| --- | --- |
| `python -m kalshi_edge.collect --mode snapshot` | Stores open markets and a timestamped price row |
| `python -m kalshi_edge.collect --mode settled` | Stores finalized markets and how they resolved |
| `python -m kalshi_edge.calibrate` | Fetches each settled market's pre-event price and prints a calibration table with Brier score and fee-adjusted ROI |
| `python -m kalshi_edge.odds` | Captures sportsbook moneylines for one sport from The Odds API (needs `ODDS_API_KEY`) |
| `python -m kalshi_edge.compare` | Matches open Kalshi game contracts to those lines and prints break-even, fair probability, edge and EV per contract. Offline |
| `python -m kalshi_edge.scan` | Ranks short-dated non-sports markets. Models daily high temperatures and CPI when a public nowcast covers that month. Everything else is price and settlement date only. Never trades |
| `python -m kalshi_edge.weather_calibrate` | Fits the daily-high bias and width to settled `KXHIGH` markets and an archived forecast. Public data only. Writes the file `scan` loads |

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

The probability is a distribution centered on that forecast high plus a fitted bias. Settlement is a whole number of degrees: "65° to 66°" is the integers 65 and 66, a latent temperature in `[64.5, 66.5)`; "greater than 66" is 67 or above. The brackets of a complete ladder sum to 1.

The bias and the width are a fit. `api.weather.gov` keeps no forecast archive, so the errors are not NWS errors and the file says so. `python -m kalshi_edge.weather_calibrate` reads settled `KXHIGH*` markets from the live list and from `GET /historical/markets`. The daily high is `expiration_value` when Kalshi sends it. Older rows leave that blank; the high is then the midpoint of the winning two-degree bin, and a winning tail with no print is dropped because it does not name a temperature. Each high is paired with the [Open-Meteo Previous Runs API](https://open-meteo.com/en/docs/previous-runs-api), model `gfs_seamless`. `temperature_2m_previous_day1` is the temperature that run predicted 24 hours before the valid hour, and `previous_day2` is 48 hours before. The forecast high is the max of those hours on the station's local date, the same reduction this scan applies to the NWS grid. The day-0 series is a short lead and is not used. That archive, for this model, is used here from January 2024.

Bias is the mean of settlement minus that GFS high. The scan adds it to the live NWS high. That transfers a GFS bias onto the NWS. It is the right correction when the two forecasts share the bias (a station the model is always cold at), and the wrong one when the NWS forecaster already took it out. Cities with at least 8 settled days get their own bias and width, shrunk toward the pool. Everyone else uses the pool. Leads up to 36 hours use the 24-hour fit, because that is the lead a same-day scan actually has and the archive has nothing shorter, so those hours are not given a tighter width than the data measured. Leads out to 72 hours use the 48-hour fit. Past that, the width grows with the old lead-time table. A Student-t replaces the normal when the tails are heavy enough that the likelihood gain is real. The fitted file is `kalshi_edge/weather_calibration.json`. The fit command prints the bias, the standard deviation, and the Brier and log loss against the Kalshi price.

The price in that comparison is the last two-sided hourly candle at or before local noon on the market date. Noon is after the 24-hour forecast existed and before the usual afternoon high. The score is fit on days before Kalshi's historical cutoff and evaluated on quotes after it, so the test days are not the days that set the width. Brackets in one event are one forecast, not independent samples. The market's own noon price is on the same table. If the market's Brier is better, a weather "edge" is the model disagreeing with a price that has been the better probability, and it should be read that way.

Two brakes sit on top of the fee break-even, for weather only. The model has to beat that break-even by an extra margin (3 points in the shipped file), so a one-point gap is not a trade. A model-minus-ask gap at least as large as the disagree guard is printed under `model disagrees strongly, verify` and is not ranked. The guard is the 95th percentile of the historical absolute gap between the model and the noon midpoint, clamped to between 10 and 20 points. That is the line that used to show up as dozens of +40 point NO bets on the market's favorite bin. The fee and the spread are unchanged: you pay the ask, and the spread is not added a second time. A penny ask on a tail can still clear, and it is flagged `THIN` or `NOBID` when the book will not fill $10.

Every scan writes the NWS hourly high it used into `nws_forecasts`. A later fit prefers those stored forecasts once 30 or more city-days at the 24-hour lead have settled, and does not mix them with the GFS sample. Until that day the scan is using the GFS transfer. A missing calibration file falls back to the old assumed width and does not rank weather at all.

The fit shipped with this repo, run on 2026-11-11, used 10,155 city-days at 24 hours and 10,152 at 48 hours, across 24 stations, from January 2024 through the settlements then on the books. One pair was dropped as an outlier (more than 25°F off). The 24-hour error is a Student-t with 6 degrees of freedom: pooled bias +1.10°F (the GFS hourly max runs cold versus the CLI print), standard deviation 3.10°F. At 48 hours the bias is +0.12°F and the standard deviation is 3.83°F. City biases at 24 hours, after shrinkage, run from −0.34°F in Las Vegas to +2.13°F in Austin. `KXHIGHHOU` and `KXHIGHOU` did not name one CLI station in the rules, so they are not in the fit; Houston in the file is Hobby (`KHOU`), from the series that did.

Scored out of sample (fit on days before 2026-09-11, the exchange's historical cutoff that day; quotes after it; 8,634 contracts, 1,439 winning bins):

| | Brier | log loss | winning-bin log loss |
| --- | ---: | ---: | ---: |
| Kalshi noon midpoint | 0.077 | 0.241 | 0.810 |
| old assumed normal, same GFS high | 0.141 | 0.446 | 1.725 |
| this fit | 0.132 | 0.424 | 1.635 |

Lower is better. The fit beats the old assumed width and loses to the noon price, by a lot, on the same contracts. A weather line is the model disagreeing with a price that has been the better probability. The 95th percentile of that absolute gap was 0.49, so the shipped guard is the 20-point cap.

`weather_calibrate` prints that report and writes `kalshi_edge/weather_calibration.json`, which is the parameter file `scan` loads. It does not write a dashboard report. The dashboard's calibration panel is still `python -m kalshi_edge.calibrate --json` (the pre-event price check). There is no panel for this weather fit yet.

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

No key. It takes a few minutes because it walks the open events before it reads the short-dated series, at the same quarter-second pace as `collect`. The first line that matters is either a list of sides whose model beats the ask after fees, or `nothing clears costs today`. A weather line that says `model disagrees strongly, verify` is not in that list. A positive line is a question, the same as a positive line in `compare`.

Refit the weather error when the stored NWS sample is worth using, or when a new city has settled for a while. The first run is slow because it pages the historical markets and then one candlestick history per recent bracket. A rerun is `--fit-only` and reads the cache.

```bash
python -m kalshi_edge.weather_calibrate
python -m kalshi_edge.weather_calibrate --fit-only
```

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

Real output from `python -m kalshi_edge.scan` at 2026-11-11T04:52:32Z (10:52pm Central on Nov 10). The Nov 11 highs were still ahead. The economic-release block and the short-dated list from this same run are left out below; they are price-only, 31 release events and 941 short-dated markets, same role as before. The lines here are the command's own text.

```text
scan  generated=2026-11-11T04:52:32Z  budget=$10.00  horizon=30d  zone=America/Chicago
modeled sides=365  clear=128  verify=30  econ events=31  short-dated=941
edges that clear costs (128); showing the best 12. The rest are in --json.
  KXCPIYOY-26NOV-T3.9  YES  CPI, year over year: Above 3.9%  ask 0.1800  model 0.481  brkeven 0.190  edge +29.1pp  settles 2026-12-10 09:00 CST  -
    Cleveland Fed year-over-year nowcast (CPI Inflation) 3.94 for 2026-11 as of 11/10, sd 0.31 from 158 finished months (mean error +0.04 left in); P(one-decimal print > 3.9)
  KXCPI-26NOV-T0.1  YES  CPI, month over month: Above 0.1%  ask 0.1800  model 0.473  brkeven 0.190  edge +28.3pp  settles 2026-12-10 08:56 CST  -
    Cleveland Fed month-over-month nowcast (CPI Inflation) 0.14 for 2026-11 as of 11/10, sd 0.22 from 158 finished months (mean error +0.03 left in); P(one-decimal print > 0.1)
  KXCPIYOY-26NOV-T3.8  YES  CPI, year over year: Above 3.8%  ask 0.3100  model 0.608  brkeven 0.325  edge +28.3pp  settles 2026-12-10 09:00 CST  -
    Cleveland Fed year-over-year nowcast (CPI Inflation) 3.94 for 2026-11 as of 11/10, sd 0.31 from 158 finished months (mean error +0.04 left in); P(one-decimal print > 3.8)
  KXCPIYOY-26NOV-T4.0  YES  CPI, year over year: Above 4.0%  ask 0.1300  model 0.357  brkeven 0.138  edge +21.9pp  settles 2026-12-10 09:00 CST  THIN
    Cleveland Fed year-over-year nowcast (CPI Inflation) 3.94 for 2026-11 as of 11/10, sd 0.31 from 158 finished months (mean error +0.04 left in); P(one-decimal print > 4)
  KXCPI-26NOV-T0.2  YES  CPI, month over month: Above 0.2%  ask 0.0900  model 0.304  brkeven 0.096  edge +20.8pp  settles 2026-12-10 08:56 CST  THIN
    Cleveland Fed month-over-month nowcast (CPI Inflation) 0.14 for 2026-11 as of 11/10, sd 0.22 from 158 finished months (mean error +0.03 left in); P(one-decimal print > 0.2)
  KXCPI-26NOV-T0.0  YES  CPI, month over month: Above 0.0%  ask 0.4300  model 0.648  brkeven 0.447  edge +20.1pp  settles 2026-12-10 08:56 CST  -
    Cleveland Fed month-over-month nowcast (CPI Inflation) 0.14 for 2026-11 as of 11/10, sd 0.22 from 158 finished months (mean error +0.03 left in); P(one-decimal print > 0)
  KXHIGHDEN-26NOV11-B61.5  YES  Denver: 61° to 62°  ask 0.0200  model 0.218  brkeven 0.021  edge +19.7pp  settles 2026-11-12 14:00 CST  THIN
    NWS hourly high 63F at KDEN (Denver, Denver International Airport), lead 16h, bias +0.2F, sd 3.5F, student-t df 6 (fit n=695 (KDEN) / 10155 pooled at 24h, Open-Meteo GFS seamless previous-day hourly maximum)
  KXCPIYOY-26NOV-T4.1  YES  CPI, year over year: Above 4.1%  ask 0.0500  model 0.246  brkeven 0.053  edge +19.2pp  settles 2026-12-10 09:00 CST  -
    Cleveland Fed year-over-year nowcast (CPI Inflation) 3.94 for 2026-11 as of 11/10, sd 0.31 from 158 finished months (mean error +0.04 left in); P(one-decimal print > 4.1)
  KXCPICORE-26NOV-T0.1  NO  core CPI, month over month: Above 0.1%  ask 0.0500  model 0.244  brkeven 0.053  edge +19.0pp  settles 2026-12-10 09:00 CST  -
    Cleveland Fed month-over-month nowcast (Core CPI Inflation) 0.21 for 2026-11 as of 11/10, sd 0.16 from 158 finished months (mean error +0.00 left in); P(unrounded change > 0.1); the rules do not say the print is one-decimal
  KXHIGHTOKC-26NOV11-B68.5  YES  Oklahoma City: 68° to 69°  ask 0.0400  model 0.231  brkeven 0.043  edge +18.8pp  settles 2026-11-12 14:00 CST  THIN
    NWS hourly high 70F at KOKC (Oklahoma City, Will Rogers World Airport), lead 16h, bias +0.3F, sd 3.0F, student-t df 6 (fit n=272 (KOKC) / 10155 pooled at 24h, Open-Meteo GFS seamless previous-day hourly maximum)
  KXHIGHLAX-26NOV11-B89.5  YES  Los Angeles: 89° to 90°  ask 0.0100  model 0.197  brkeven 0.011  edge +18.6pp  settles 2026-11-12 14:00 CST  NOBID
    NWS hourly high 86F at KLAX (Los Angeles, Los Angeles International Airport), lead 15h, bias +1.2F, sd 2.8F, student-t df 6 (fit n=660 (KLAX) / 10155 pooled at 24h, Open-Meteo GFS seamless previous-day hourly maximum)
  KXHIGHDEN-26NOV11-T61  YES  Denver: 60° or below  ask 0.0100  model 0.196  brkeven 0.011  edge +18.5pp  settles 2026-11-12 14:00 CST  NOBID,THIN
    NWS hourly high 63F at KDEN (Denver, Denver International Airport), lead 16h, bias +0.2F, sd 3.5F, student-t df 6 (fit n=695 (KDEN) / 10155 pooled at 24h, Open-Meteo GFS seamless previous-day hourly maximum)
model disagrees strongly, verify (30), not ranked as edges:
  showing 8; the rest are in --json.
  KXHIGHLAX-26NOV11-T83  NO  Los Angeles: 82° or below  ask 0.4400  model 0.956  brkeven 0.458  edge +49.9pp  settles 2026-11-12 14:00 CST  VERIFY
    NWS hourly high 86F at KLAX (Los Angeles, Los Angeles International Airport), lead 15h, bias +1.2F, sd 2.8F, student-t df 6 (fit n=660 (KLAX) / 10155 pooled at 24h, Open-Meteo GFS seamless previous-day hourly maximum) model disagrees strongly, verify
  KXHIGHTHOU-26NOV11-T76  NO  Houston: 75° or below  ask 0.1400  model 0.627  brkeven 0.149  edge +47.8pp  settles 2026-11-12 14:00 CST  VERIFY
    NWS hourly high 76F at KHOU (Houston, Houston Hobby Airport), lead 16h, bias +0.3F, sd 2.9F, student-t df 6 (fit n=272 (KHOU) / 10155 pooled at 24h, Open-Meteo GFS seamless previous-day hourly maximum) model disagrees strongly, verify
  KXHIGHTSDF-26NOV11-B71.5  NO  Louisville: 71° to 72°  ask 0.4100  model 0.874  brkeven 0.427  edge +44.7pp  settles 2026-11-12 14:00 CST  VERIFY
    NWS hourly high 68F at KSDF (Louisville Muhammad Ali International Airport), lead 15h, bias +0.1F, sd 3.1F, student-t df 6 (fit n=74 (KSDF) / 10155 pooled at 24h, Open-Meteo GFS seamless previous-day hourly maximum) model disagrees strongly, verify
  KXHIGHTOKC-26NOV11-B72.5  NO  Oklahoma City: 72° to 73°  ask 0.3800  model 0.802  brkeven 0.397  edge +40.5pp  settles 2026-11-12 14:00 CST  VERIFY
    NWS hourly high 70F at KOKC (Oklahoma City, Will Rogers World Airport), lead 16h, bias +0.3F, sd 3.0F, student-t df 6 (fit n=272 (KOKC) / 10155 pooled at 24h, Open-Meteo GFS seamless previous-day hourly maximum) model disagrees strongly, verify
  KXHIGHDEN-26NOV11-B65.5  NO  Denver: 65° to 66°  ask 0.4000  model 0.816  brkeven 0.417  edge +39.9pp  settles 2026-11-12 14:00 CST  VERIFY
    NWS hourly high 63F at KDEN (Denver, Denver International Airport), lead 16h, bias +0.2F, sd 3.5F, student-t df 6 (fit n=695 (KDEN) / 10155 pooled at 24h, Open-Meteo GFS seamless previous-day hourly maximum) model disagrees strongly, verify
  KXHIGHTDAL-26NOV11-B80.5  NO  Dallas: 80° to 81°  ask 0.3100  model 0.706  brkeven 0.325  edge +38.1pp  settles 2026-11-12 14:00 CST  VERIFY
    NWS hourly high 80F at KDFW (Dallas/Fort Worth International Airport), lead 16h, bias +1.3F, sd 2.9F, student-t df 6 (fit n=272 (KDFW) / 10155 pooled at 24h, Open-Meteo GFS seamless previous-day hourly maximum) model disagrees strongly, verify
  KXHIGHTLV-26NOV11-B76.5  NO  Las Vegas: 76° to 77°  ask 0.2800  model 0.648  brkeven 0.294  edge +35.4pp  settles 2026-11-12 14:00 CST  VERIFY
    NWS hourly high 76F at KLAS (Harry Reid International Airport), lead 17h, bias -0.3F, sd 2.3F, student-t df 6 (fit n=300 (KLAS) / 10155 pooled at 24h, Open-Meteo GFS seamless previous-day hourly maximum) model disagrees strongly, verify
  KXHIGHTPHX-26NOV11-B83.5  NO  Phoenix: 83° to 84°  ask 0.3200  model 0.686  brkeven 0.336  edge +35.0pp  settles 2026-11-12 14:00 CST  VERIFY
    NWS hourly high 84F at KPHX (Phoenix, Phoenix Sky Harbor International Airport), lead 17h, bias +0.7F, sd 2.3F, student-t df 6 (fit n=279 (KPHX) / 10155 pooled at 24h, Open-Meteo GFS seamless previous-day hourly maximum) model disagrees strongly, verify
```

The economic releases and the short-dated list from this same run are omitted here.

```text
notes:
  - weather error fit to Open-Meteo GFS seamless previous-day hourly maximum: 24h pooled bias +1.10F, sd 3.10F, student-t df 6, n=10155 city-days. Applied bias is 100% of that fit. A model-minus-ask gap of 20% or more is flagged verify and not ranked; weather also needs 3% of extra margin over the fee break-even
  - 24 events were not priced because the NWS hourly grid no longer contains the calendar-day high, including KXHIGHAUS-26NOV10
read this honestly:
  - an edge means the model probability beats the break-even of a $10 taker order at the ask
  - the spread is already in that break-even: you pay the ask, and the spread is not added again
  - weather bias and sd come from weather_calibration.json; a missing fit is not ranked as an edge
  - a weather gap at least as large as the disagree guard is flagged VERIFY and is not an edge
  - weather also has to beat the fee break-even by the fit's extra margin
  - CPI uses the Cleveland Fed nowcast and that file's own errors; payrolls, claims, and the Fed are price only
  - THIN: 24h volume under 50, open interest under 20, or ask size below the order; WIDE: spread over 5 cents
  - NOBID: the bid is missing or zero. A thin quote can clear this bar and still be untradable at $10
  - this tool never places an order
```

How to read this run:

- **128 sides cleared the fee. 86 are weather and 42 are November CPI.** The previous run, with the assumed width, had 136 weather sides in that list, and the top of it was NO on the market's favorite bin at about +40 points. Those NO lines are now under `model disagrees strongly, verify` (30 sides, not ranked). The printed top 12 is CPI, then weather YES at 1 to 4 cents.
- **The weather sides that still clear are mostly a cheap tail.** 52 of the 86 are YES asks of 5 cents or less. 16 have no liquidity flag. The largest of those are NO on a neighboring bin at +12 to +17 points (Philadelphia, Washington, Oklahoma City), under the 20-point guard. The noon-price Brier above says the market has been the better probability, so these are questions, not a buy list.
- **Verify is the old failure mode, labeled.** Los Angeles NO on "82° or below" is model 0.956 against an ask of 0.44, edge +49.9 points, and it is not in the ranked list. The why line names the fit: NWS 86°F, bias +1.2°F, sd 2.8°F, Student-t with 6 degrees of freedom, from the GFS archive, not from a historical NWS grid.
- **CPI is unchanged.** The best CPI side is still `KXCPIYOY-26NOV-T3.9` YES at 0.18, model 0.481, edge +29.1 points. Payrolls, unemployment, claims, and the Fed are price only.
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

SQLite, schema version 4 in `schema_meta`. Older files are upgraded in place on open: a version 1 file gains two columns on `markets` (they stay `NULL` until the next `settled` run rewrites each row), and later tables are created when missing. Version 4 adds `nws_forecasts` (the hourly max each scan priced), `weather_days` (settled daily highs), `archive_forecasts` (the Open-Meteo highs they were paired with), and `weather_brackets` (the ladder the noon-price score uses).

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

The tests parse fixture payloads (a candlestick history, an Open-Meteo previous-run payload, and an Odds API response in `tests/fixtures/`) and talk to fake HTTP sessions. They call neither Kalshi, nor The Odds API, nor the National Weather Service, nor Open-Meteo, nor the Cleveland Fed. The fee tests check the formula against the worked examples in Kalshi's fee schedule; the de-vig and EV tests check the worked examples in the design doc; the client tests trigger failures with a fake key and assert it never appears in an exception or a log line; the compare test runs the whole pipeline on a fixture database and checks the printed table. The scan tests check the temperature brackets, the fitted error and the guard that keeps a huge weather gap out of the ranked list, the nowcast parser on a small chart, and a full report built from fake pages, including the case where nothing clears the fee.

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
