# Design: sportsbook comparison, break-even, and expected value

Status: proposal, not implemented. Covers README roadmap items 3 and 4
("Sportsbook comparison" and "Parlays, break-even, and expected value").
The calibration backtest (roadmap item 1) is being built separately and is
out of scope here, except where the two need to share one fee module.

Date: 2026-11-08.

## 1. Decisions up front

| Question | Decision | Why in one line |
| --- | --- | --- |
| Odds data source | The Odds API v4, free tier (500 credits/month) | Legal, documented, JSON, covers DraftKings/FanDuel/BetMGM/Caesars and Pinnacle; a moneyline pull for one sport and one region costs 1 credit |
| Reference "fair" price | Pinnacle de-vigged when present, else the median of de-vigged US books | Pinnacle is the lowest-hold book that takes sharp money; a median is robust to one book's stale line |
| Vig removal | Power method by default, multiplicative kept as an option and shown side by side | Multiplicative normalization overstates long-shot probabilities; the power method corrects for that, which matters because long shots are what joseph bets |
| Kalshi price used for EV | `yes_ask` (or `no_ask`) at the time of comparison, never the midpoint or `last_price` | You cannot buy at the midpoint. EV computed on a price you cannot get is fiction |
| Fee model | Kalshi's quadratic formula with per-series `fee_type` and `fee_multiplier` read from `GET /series/{ticker}` and cached in the DB; the 0.07 default is a fallback, not the truth | Kalshi changes the schedule (latest effective 2026-07-07). Hard-coding it is how the tool silently lies six months from now |
| Matching Kalshi to a sportsbook game | Deterministic: parse the event ticker (`KXNFLGAME-26NOV08CINATL`) for date and team codes, map codes through a checked-in alias table, require exactly one candidate within a kickoff window | No fuzzy string matching; an ambiguous match is reported as unmatched instead of guessed |
| API key handling | One environment variable, `ODDS_API_KEY`, loaded at the CLI boundary; URL redaction in logs and exceptions; no key for Kalshi at all | The Odds API puts the key in the query string, so every log line that prints a URL is a leak unless redacted |
| Money arithmetic | `decimal.Decimal` for prices, fees and payouts; `float` only for probabilities | Fees round up to the cent. `0.07 * 0.45 * 0.55` in binary floating point is not what Kalshi's ledger computes |
| Parlay/combo stance | Report the EV of a combo honestly and show how the hold compounds per leg. Never rank or recommend combos | At $10 on a typical three-leg parlay the expected loss is on the order of $1 to $1.50. The tool should make that visible, not hide it behind a "best bets" list |

## 2. What this feature answers

For every open Kalshi game-winner contract (one per team), the tool should answer:

1. What does the sportsbook market think the fair probability of this outcome is, after removing the book's margin?
2. What would it cost to buy this Kalshi contract right now, including the fee?
3. What probability do I need to be right at just to break even?
4. Is the sportsbook fair probability above or below that break-even, and by how much, in dollars per contract and as a percentage return?

Everything else (combos, cross-book arbitrage, line movement) is built from those four numbers.

What it does not answer: whether the sportsbook is right. The sportsbook is the better-resourced forecaster, so the design treats it as the reference. The calibration backtest is what eventually tells us whether Kalshi prices themselves are biased.

## 3. Odds data source

### 3.1 Options considered

| Option | Cost | Legality / stability | Verdict |
| --- | --- | --- | --- |
| The Odds API (the-odds-api.com) | Free: 500 credits/month, no card. Paid from $30/month for 20k credits and historical snapshots | Documented v4 JSON API, terms allow personal use, bookmakers by region (`us`, `us2`, `eu`, `uk`) | **Use.** Free tier is enough for one sport snapshotted daily plus a pre-kickoff pull |
| Scraping DraftKings/FanDuel sites | Free | Violates their terms, breaks on every frontend change, bot detection. Not something to put in a portfolio | Reject |
| Pinnacle directly | Pinnacle's API needs a funded account and is not available in most US states | | Reject; get Pinnacle lines through The Odds API `eu` region instead |
| Other aggregators (odds-api.io, OddsPapi, SportsGameOdds) | Free tiers exist, vary month to month | Smaller track records, less documentation | Keep the client behind an interface so one of these can be swapped in later, but do not build for it now |

### 3.2 Credit budget

The Odds API charges `markets × regions` credits per `/odds` call. Calls to `/sports` and `/events` are free. Historical endpoints cost 10× and are paid-only.

For NFL moneylines:

| Call | Credits |
| --- | --- |
| `GET /v4/sports/americanfootball_nfl/odds?regions=us&markets=h2h` | 1 |
| Same with `regions=us,eu` (adds Pinnacle) | 2 |
| Twice a day for a month | about 120 |

That leaves headroom for a second sport or an extra pre-kickoff pull on game days. The client must log the `x-requests-remaining` and `x-requests-used` response headers after every call and refuse to run when remaining is below a configurable floor (default 50), so a cron job cannot burn the month's allowance on a bug.

### 3.3 Response shape we depend on

One element per game:

```json
{
  "id": "e912a1...",
  "sport_key": "americanfootball_nfl",
  "commence_time": "2026-11-08T18:00:00Z",
  "home_team": "Atlanta Falcons",
  "away_team": "Cincinnati Bengals",
  "bookmakers": [
    {"key": "pinnacle", "last_update": "...", "markets": [
      {"key": "h2h", "outcomes": [
        {"name": "Atlanta Falcons", "price": 1.80},
        {"name": "Cincinnati Bengals", "price": 2.10}
      ]}
    ]}
  ]
}
```

Request `oddsFormat=decimal`. Decimal odds convert to implied probability as `1 / price` with no sign handling. American odds are kept out of the pipeline entirely except as a display option.

## 4. Storage

Schema version goes from 1 to 2. `MarketStore` already writes `schema_meta.version`; the upgrade is a `CREATE TABLE IF NOT EXISTS` for the new tables plus a version bump, done inside one transaction. Nothing in the existing tables changes.

```sql
-- One row per bookmaker outcome per capture. Prices stay as the API sent
-- them. Nothing here is de-vigged; that is a query-time calculation so the
-- method can change without re-collecting.
CREATE TABLE sportsbook_odds (
    source        TEXT NOT NULL,          -- 'the_odds_api'
    event_id      TEXT NOT NULL,          -- provider's game id
    sport_key     TEXT NOT NULL,
    commence_time TEXT NOT NULL,          -- UTC ISO-8601
    home_team     TEXT NOT NULL,
    away_team     TEXT NOT NULL,
    bookmaker     TEXT NOT NULL,          -- 'pinnacle', 'draftkings', ...
    market        TEXT NOT NULL,          -- 'h2h'
    outcome       TEXT NOT NULL,          -- team name as the book printed it
    price_decimal TEXT NOT NULL,          -- raw string, parsed with Decimal
    book_updated  TEXT,                   -- bookmaker.last_update
    captured_at   TEXT NOT NULL,          -- our run timestamp, UTC second
    PRIMARY KEY (source, event_id, bookmaker, market, outcome, captured_at)
);

-- One row per Kalshi event that we could tie to a sportsbook game.
-- 'method' and 'confidence' are stored so a wrong match is debuggable.
CREATE TABLE line_matches (
    event_ticker   TEXT NOT NULL,         -- KXNFLGAME-26NOV08CINATL
    source         TEXT NOT NULL,
    event_id       TEXT NOT NULL,
    method         TEXT NOT NULL,         -- 'ticker_codes_v1'
    kickoff_delta_s INTEGER,              -- odds commence_time minus Kalshi close_time
    matched_at     TEXT NOT NULL,
    PRIMARY KEY (event_ticker, source)
);

-- Per-series fee parameters from GET /series/{ticker}, refreshed each run.
CREATE TABLE series_fees (
    series_ticker  TEXT PRIMARY KEY,
    fee_type       TEXT NOT NULL,         -- e.g. 'quadratic', 'quadratic_with_maker_fees'
    fee_multiplier TEXT,                  -- raw string from the API
    fetched_at     TEXT NOT NULL
);
```

Why raw strings for prices: it mirrors the existing `*_dollars` columns. The parsed `Decimal` is derived at read time, so a parsing bug never corrupts stored data.

## 5. Matching a Kalshi contract to a sportsbook line

### 5.1 What the Kalshi side gives us

Observed in the collector's fixtures:

```
event_ticker:  KXNFLGAME-26NOV08CINATL
market ticker: KXNFLGAME-26NOV08CINATL-CIN
yes_sub_title: "Cincinnati"
close_time:    2026-11-08T17:35:30Z
```

The event ticker suffix is `YYMMMDD` plus two three-letter team codes. The market ticker's final segment names the team that contract pays on. `yes_sub_title` is truncated by Kalshi ("Los Angeles C" for the Chargers), so it is for display only and is never a match key.

Which code is home and which is away is not stated in the ticker. The design does not assume an order; it matches the unordered pair of codes against the unordered pair of the game's two teams.

### 5.2 Algorithm

```
for each open Kalshi event (grouped by event_ticker, series in scope):
    date, code_a, code_b = parse_event_ticker(event_ticker)   # strict regex, else skip + log
    teams = {alias[code_a], alias[code_b]}                     # KeyError -> skip + log "unknown code"
    candidates = odds events where
        sport_key matches the series' configured sport
        and {home_team, away_team} == teams
        and |commence_time - kalshi close_time| <= window (default 6 h)
    if len(candidates) == 1: record match
    elif len(candidates) == 0: report unmatched
    else: report ambiguous, do not pick
```

The alias table is a checked-in JSON file, `config/teams/nfl.json`, mapping Kalshi codes to the full names The Odds API prints:

```json
{"CIN": "Cincinnati Bengals", "ATL": "Atlanta Falcons", "LAC": "Los Angeles Chargers"}
```

Thirty-two entries, reviewed by a human once, covered by a test that asserts every code is three uppercase letters and every name is unique. A relocation or rename is a one-line PR, which is better than a fuzzy matcher that quietly pairs "LA" with the Rams.

Why a time window at all when the pair of teams is already unique: two teams can meet twice in a season, and the odds feed can carry next week's game alongside this week's. The window is the tiebreak. The `26AUG29CHITEN` fixture has a preseason game whose date may be the local date rather than the UTC close date, so the window is measured against Kalshi's `close_time`, not against the date string in the ticker.

### 5.3 Side alignment

Once an event is matched, each Kalshi market ticker's final segment gives the team the YES contract pays on. The sportsbook outcome for that team is the comparable. The other team's outcome is the comparable for the NO side.

Ties: NFL moneylines at most books are two-way with a tie voiding the bet (push), while Kalshi settles a tie at 0.5. A push returns the stake; Kalshi returns half the notional. These differ by roughly the price of the contract times the tie probability (about 0.2% of NFL games). The design notes it and ignores it, with the assumption stated in the output.

## 6. The math

Notation: `o_i` is decimal odds for outcome `i`, `r_i = 1/o_i` is the raw implied probability, `V = Σ r_i` is the overround (for a two-way NFL moneyline, typically 1.04 to 1.05 at retail books, about 1.02 at Pinnacle), `q_i` is the de-vigged "fair" probability.

### 6.1 Vig removal

**Multiplicative (normalize):** `q_i = r_i / V`. Simple, and the industry default. Its flaw is that it spreads the margin in proportion to the raw probability, but bookmakers do not price margin that way: they shade long shots more (the favorite-longshot bias). So after normalizing, the long shot still looks more likely than it is. For someone betting long shots, that error is in the dangerous direction.

**Power:** find `k` such that `Σ r_i^k = 1`, then `q_i = r_i^k`. Since `V > 1`, `k > 1`, and raising to a power greater than one shrinks small probabilities proportionally more than large ones. That is the correction the bias calls for. Solve for `k` with a bracketed root finder (bisection on `k ∈ [1, 10]` is fine, it is monotone), no SciPy dependency.

**Shin:** models the margin as insider trading share `z`. Better theoretical grounding for racing fields; for two-way markets it behaves much like the power method. Listed as a future option, not built.

Worked two-way example, Pinnacle-style `1.80 / 2.10`:

| | Favorite | Underdog |
| --- | --- | --- |
| raw `r` | 0.5556 | 0.4762 (V = 1.0317) |
| multiplicative `q` | 0.5385 | 0.4615 |
| power `q` (k ≈ 1.047) | 0.5403 | 0.4597 |

The methods differ by less than half a point here. The gap grows for lopsided games: at `1.20 / 5.00` multiplicative gives the underdog 0.1935, power gives 0.1777. That difference is larger than most "edges" the tool will ever report, which is why the output shows both and names the method.

### 6.2 Kalshi fees

Kalshi's published schedule (effective 2026-07-07) is quadratic in price:

```
taker_fee  = ceil_to_cent( M_taker × 0.07   × C × P × (1 − P) )
maker_fee  = ceil_to_cent( M_maker × 0.0175 × C × P × (1 − P) )
```

`C` is the contract count, `P` the price in dollars, `M` a per-series multiplier (default 1 for takers, 0 for makers; some sports series charge makers at multiplier 1; combo markets use 2). Which applies is in `fee_type` and `fee_multiplier` on `GET /series/{series_ticker}`.

Rounding is per order, not per contract: 10 contracts at 0.45 cost `ceil(0.07 × 10 × 0.2475) = ceil(0.17325) = $0.18`, not `10 × $0.02 = $0.20`. The design computes fees for the actual order size, with `C` as a CLI flag defaulting to the number of $1 contracts $10 buys at the ask.

Implementation notes that matter:

- Use `Decimal` with `ROUND_CEILING` at two places. The coefficient, price and count are all exact decimals; the product is exact; the ceiling is then unambiguous.
- Keep the 0.07 and 0.0175 as named constants in one module, `kalshi_edge/fees.py`, with the schedule's effective date in a comment, and let `fee_type` plus `fee_multiplier` from the API override them. Log a warning when the series row is missing and the fallback is used.
- `fees.py` is also what the calibration thread needs for fee-adjusted ROI. Whichever PR lands first owns it; the other imports it rather than re-deriving the formula.

A spot check for the tests: one contract at P = 0.45 gives `0.07 × 0.2475 = 0.017325`, which rounds up to $0.02. At P = 0.99, `0.07 × 0.0099 = 0.000693`, which rounds up to $0.01, so a 99-cent contract carries a one-cent fee, which is about 1% of the stake and matters for heavy favorites.

### 6.3 Cost, break-even, EV

For buying one YES contract at ask `P` with per-contract fee `f = fee(C, P) / C`:

```
cost        = P + f
break_even  = cost / payout          (payout is 1.00 for a binary contract; use notional_value from the DB)
EV          = q × payout − cost
edge        = q − break_even
ROI         = EV / cost
```

The NO side is identical with `P = no_ask` and `q = 1 − q_yes_fair` (the de-vigged probability of the other team).

Worked example, Bengals YES, `yes_ask = 0.45`, fair `q = 0.4597` (power method, from the table above), ten contracts:

| | |
| --- | --- |
| fee for 10 contracts | $0.18, so f = $0.018 |
| cost per contract | $0.468 |
| break-even probability | 46.8% |
| edge | 45.97% − 46.8% = −0.8 point |
| EV per contract | 0.4597 − 0.468 = −$0.0083 |
| EV on $4.68 (ten contracts) | about −$0.08 |

So a Kalshi contract that looks about a point cheaper than the sportsbook's raw implied probability is still slightly negative once the vig is removed and the fee is added. That is the normal case, and the tool should say so without drama.

### 6.4 Combos and parlays

For `n` legs with fair probabilities `q_1 … q_n` that are independent:

```
q_combo         = Π q_i
fair_payout     = stake / q_combo
EV              = stake × (q_combo × offered_multiplier − 1)
```

Two things make combos negative by default:

1. **The hold compounds.** A sportsbook parlay is priced by multiplying each leg's vigged odds. If each leg carries a 4.8% hold, a three-leg parlay carries roughly `1 − (1/1.048)^3 ≈ 13%`. A Kalshi combo market charges its own fee (combo series have their own `fee_multiplier`), and the expected fee as a share of stake is highest near P = 0.5 per leg.
2. **Correlation.** If legs are positively correlated, the true joint probability is higher than the product and a parlay can be underpriced; books void or reprice those. Negative correlation makes the product an overestimate and the combo worse than it looks. The tool assumes independence and prints that assumption.

A three-leg combo of three roughly 55% favorites at a book: `q_combo ≈ 0.166`, fair payout about 6.0×, typical offered multiplier about 5.2×, EV on $10 ≈ 10 × (0.166 × 5.2 − 1) ≈ −$1.35. The design has the tool print the fair multiplier next to the offered one so the gap is visible. It does not search for combos, rank them, or suggest stake sizes. The honest output of a combo evaluator is almost always a negative number, and that is the useful information.

### 6.5 What counts as an edge

A reported positive EV needs these qualifiers in the output, or it is misleading:

- It is relative to one reference (Pinnacle or a book median), not to the truth.
- Kalshi and sportsbook timestamps must be within a configurable gap (default 15 minutes); otherwise one side's line is stale and the "edge" is movement.
- The Kalshi book depth at the ask is not in the public market payload used so far; a one-cent edge on a contract with 3 contracts at the ask is not actionable. The order book endpoint (`GET /markets/{ticker}/orderbook`) is public and can fill this in later.
- The calibration backtest, once it exists, is the only evidence about whether Kalshi's own prices are systematically off. Until then "edge versus sportsbook" is the best available proxy and should be labelled that way.

## 7. Secure key handling

The feature needs exactly one secret: the Odds API key. Kalshi stays keyless. Everything below keeps that true and keeps the one key out of the repo, the logs, and the chat.

**Where the key lives.** `ODDS_API_KEY` in the environment. Locally that means a `.env` file loaded by the shell or `direnv`, and `.env` and `.envrc` are already gitignored. The code reads `os.environ` in one place, the CLI entry point, and passes the value into the client constructor. Library code never reads the environment, so tests construct clients with a dummy key and no monkeypatching of globals.

**Why not a config file or a CLI flag.** A flag lands in shell history and `ps` output. A config file next to `series.example.json` is one careless `git add .` from being public. The repo is public, so the failure mode is permanent.

**Redaction.** The Odds API expects the key as a query parameter, `apiKey=`. That means `requests` exceptions, debug logging of URLs, and tracebacks all contain it. The client must:

- build the URL with `params=` and never format the key into a string that could be logged;
- wrap `requests` exceptions and re-raise with the URL scrubbed (`re.sub(r"apiKey=[^&]+", "apiKey=REDACTED", ...)`);
- install a `logging.Filter` on the client's logger that applies the same scrub to every record;
- have a test that triggers an HTTP error with a known fake key and asserts the key string is absent from the exception text and captured logs.

**Scope and rotation.** The Odds API key is read-only by nature, which is the least privilege available. Rotation is a dashboard action plus a new environment value. Document in the README how to rotate and that a leaked key should be rotated first and investigated second.

**Repository guards.** Add a `gitleaks` or `detect-secrets` pre-commit hook and a GitHub Actions job that runs it on pull requests. Turn on GitHub secret scanning and push protection in the repo settings. These are five minutes each and are what a reviewer looks for in a public portfolio repo.

**If it ever runs in AWS.** Store the key in SSM Parameter Store as a `SecureString` (free at this scale) or Secrets Manager, grant the Lambda or ECS task role `ssm:GetParameter` on that one parameter ARN with the matching `kms:Decrypt`, and read it at startup. Never bake it into a container image or a Terraform variable file that is committed. Terraform should reference the parameter's ARN as data, not its value.

**If Kalshi authentication is ever added** (for order placement or private order book depth): Kalshi keys are an Ed25519 or RSA key pair where Kalshi holds only the public key. The private PEM is the secret, stored with `chmod 600` locally or in Secrets Manager, and loaded by path from an environment variable, never by value. This design does not need it and should not add it.

## 8. CLI and module layout

```
kalshi_edge/
  fees.py            pure: fee(count, price, fee_type, multiplier) -> Decimal
  odds_math.py       pure: implied(), devig_multiplicative(), devig_power(), ev(), break_even(), combo()
  sportsbook/
    client.py        OddsApiClient: get_odds(sport, regions, markets), credit tracking, redaction
    parse.py         payload -> OddsRow dataclasses
    teams.py         load alias table, parse_event_ticker()
    match.py         match_events() -> matches, unmatched, ambiguous
  odds.py            CLI: python -m kalshi_edge.odds --sport americanfootball_nfl --regions us,eu
  compare.py         CLI: python -m kalshi_edge.compare --series KXNFLGAME [--devig power|multiplicative] [--reference pinnacle|median] [--contracts N] [--max-gap-minutes 15]
```

`compare` reads only from SQLite. It makes no network calls, so it is testable from fixtures end to end and can be run against any pair of snapshots.

Example output (format, numbers illustrative):

```
KXNFLGAME-26NOV08CINATL   Kalshi 17:35Z   Pinnacle 17:31Z   gap 4m   devig=power
 side   kalshi_ask  fee/ct  break_even  fair_q   edge    ev/ct   roi
 CIN    0.45        0.018   0.468       0.460   -0.8pp  -0.008  -1.8%
 ATL    0.56        0.018   0.578       0.540   -3.8pp  -0.038  -6.6%
unmatched: KXNFLGAME-26NOV09xxxyyy (unknown team code 'xxx')
```

## 9. Testing

All offline, fixtures only, as with the collector.

- `fees.py`: the spot checks in 6.2, both sides of a cent boundary, count rounding (10 × 0.45 = $0.18 not $0.20), `fee_type` with maker multiplier 0 returning $0.00, `Decimal` inputs only (a `float` argument raises `TypeError`).
- `odds_math.py`: two-way and three-way de-vig sums to 1 within 1e-12; power method `k > 1` whenever `V > 1`; multiplicative and power agree when `V == 1`; EV sign flips exactly at break-even; combo of one leg equals the single-leg result.
- `teams.py`: regex rejects malformed tickers; alias table has 32 unique names; unknown code raises a typed error.
- `match.py`: one candidate matches; two candidates (same teams, two weeks apart) resolve by window; zero candidates reported; swapped home/away still matches.
- `client.py`: fake session; credit headers parsed; refuses to call below the floor; key absent from exception text and logs.
- `compare`: an end-to-end fixture DB with two Kalshi snapshots and one odds capture, asserting the printed table.

## 10. Delivery plan

Three pull requests, each reviewable alone:

1. **Math and fees.** `fees.py`, `odds_math.py`, tests. No network, no schema change. The calibration thread can import `fees.py` as soon as this merges.
2. **Odds collector.** `sportsbook/client.py`, `parse.py`, schema v2, `odds` CLI, `series_fees` refresh, secret handling, pre-commit secret scan, README section on getting a free key and setting `ODDS_API_KEY`.
3. **Matching and compare.** `teams.py`, alias table, `match.py`, `compare` CLI, README with real output from one live run (key never shown, credit headers quoted to prove the budget).

Each PR updates the README roadmap so it stays accurate about what is and is not built.

## 11. Open questions and risks

- **Ticker home/away order.** Unknown; the matcher is order-agnostic so it does not matter for matching, but the display should not label a side "home" until confirmed from a live payload.
- **Fee schedule drift.** Mitigated by reading `fee_type`/`fee_multiplier` per series and dating the constants. Still worth a test that fails loudly if the API returns a `fee_type` the code does not know.
- **Kalshi API unreachable from the cloud environment.** Observed in this project's cloud threads. Live runs for README output will need to happen on joseph's machine, same as PR #1.
- **Odds API free tier changes.** The credit floor and `x-requests-remaining` logging keep a change from becoming a silent failure.
- **Ties and pushes.** Documented, not modelled. Error is about 0.1 point on NFL moneylines.
- **Depth.** Public order book endpoint exists but is not in scope; without it, "edge" is per contract, not per dollar you could actually get filled.

## Sources

- Kalshi API key documentation: https://docs.kalshi.com/getting_started/api_keys
- Kalshi fee breakdown with per-series multipliers (secondary source, checked 2026-11-06; verify against kalshi.com/docs/fee-schedule before implementing): https://chudi.dev/blog/kalshi-fees
- The Odds API pricing and bookmaker coverage: https://www.the-odds-api.com
- The Odds API credit accounting (competitor write-up, cross-check with the API's own docs): https://oddspapi.io/blog/the-odds-api-free-tier-limits
