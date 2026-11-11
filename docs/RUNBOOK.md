# Runbook: running kalshi-edge-finder on WSL

Every command you need, in the order you need it, with the reason behind each one. Written for Ubuntu under WSL with zsh; the same commands work in bash and on any Linux or macOS machine. The [README](../README.md) explains what each tool computes and why; this document is about operating it.

## What you are running

The tools share one SQLite file, `data/kalshi.db`. Nothing runs in the cloud and nothing places a bet.

| Tool | Question it answers | Network |
| --- | --- | --- |
| `kalshi_edge.collect` | What is Kalshi quoting right now (snapshot), and how did past contracts resolve (settled)? | Kalshi public API, no key |
| `kalshi_edge.calibrate` | When Kalshi said 45%, did the event happen about 45% of the time? Would buying have made money after fees? | Kalshi candlesticks, no key |
| `kalshi_edge.odds` | What are the sportsbooks (including Pinnacle) quoting for the same games? | The Odds API, needs your key, costs credits |
| `kalshi_edge.compare` | After removing the book's margin and adding Kalshi's fee, is any contract cheaper than the book thinks it should be? | None; reads the database only |
| `kalshi_edge.scan` | For a small short-dated account: which non-sports contracts, if any, beat the ask after fees? | Kalshi public API, api.weather.gov, Cleveland Fed nowcast files. No key |

**Why one database file.** Every tool writes to or reads from the same SQLite file, so order matters: `calibrate` needs settled rows from `collect`, and `compare` needs both a Kalshi snapshot and an odds capture. The path `data/kalshi.db` is relative to where you run the command, so always run from the repository root, or you will create a second, empty database somewhere else and wonder where your data went.

## Open WSL and find the clone

Open Windows Terminal and pick the Ubuntu profile, or type `wsl` in any prompt. Keep the clone in your Linux home, for example `~/projects`:

```bash
cd ~/projects/kalshi-edge-finder
git branch --show-current     # should print: main
git log --oneline -3
```

Why the Linux home and not a Windows folder under `/mnt/c`: files there are slower for WSL than files in `~`, which shows up as slow `pytest` and `pip`. And if the folder is inside OneDrive, the sync client reads `data/kalshi.db` while SQLite is writing it, which is how you get "database is locked" and conflict copies. To browse the Linux clone from Windows Explorer, type `\\wsl$\Ubuntu\home\<user>\projects` in the address bar.

Moving an existing clone: the only thing worth carrying over is `data/kalshi.db`. Do not copy a venv; it hard-codes its own path and has to be rebuilt where it lives. After the fresh setup below, copy the database and run `python -m kalshi_edge.compare --series KXNFLGAME` offline; the same report as before means the move is complete.

## Fresh setup

1. **Clone the repository.**

   ```bash
   mkdir -p ~/projects && cd ~/projects
   git clone https://github.com/jmac052002/kalshi-edge-finder.git
   cd kalshi-edge-finder
   ```

2. **Check Python.** The project requires 3.11 or newer; Ubuntu 24.04 ships 3.12.

   ```bash
   python3 --version
   ```

   If `python3 -m venv` fails, or produces a venv without `pip`, install Ubuntu's venv package once:

   ```bash
   sudo apt update && sudo apt install -y python3-venv
   ```

3. **Create a virtual environment named `.venv-wsl`.**

   ```bash
   python3 -m venv .venv-wsl
   source .venv-wsl/bin/activate
   python -m pip install --upgrade pip
   ```

   A venv is a private copy of Python's package directory for this one project, so the project's `requests` version never fights with anything else on the machine and `pip install` never needs `sudo`. The `-wsl` suffix matters when the same folder is also used from Windows: a venv hard-codes the interpreter path and binary format of the OS that created it, so a Windows `.venv` is unusable from Linux and the other way round. `.gitignore` covers `.venv*`, so both can coexist.

4. **Install the dependencies.**

   ```bash
   pip install -r requirements.txt -r requirements-dev.txt
   pip install -r requirements-plot.txt   # optional: matplotlib, only for calibrate --plot
   ```

   `requirements.txt` is what the tools need to run (`requests`, `tzdata`). `requirements-dev.txt` adds `pytest` and `ruff`, which only a developer needs. `requirements-plot.txt` is `matplotlib`, a heavy package that only the optional calibration chart uses. Splitting them is a habit worth keeping: a production image installs only the first file, so it is smaller and has a smaller attack surface.

5. **Run the tests and the linter.** The tests talk to fake HTTP sessions and never touch Kalshi or The Odds API.

   ```bash
   python -m pytest
   ruff check .
   ```

## Every session: the first four lines

```bash
cd ~/projects/kalshi-edge-finder
source .venv-wsl/bin/activate
git status
git pull origin main
```

`source` puts the venv's `python` first on your `PATH` for this shell only; the prompt gains a `(.venv-wsl)` prefix. Without activation the same commands work as `.venv-wsl/bin/python -m kalshi_edge...`. `git status` tells you which branch you are on and whether you have uncommitted edits, which you need to know before pulling. `git pull origin main` brings in whatever merged since your last session. A clean status looks like:

```text
On branch main
Your branch is up to date with 'origin/main'.

nothing to commit, working tree clean
```

If it says anything else, see [Troubleshooting](#troubleshooting) before pulling.

## The one secret: `ODDS_API_KEY`

Only the `odds` command needs it. Kalshi's public endpoints need nothing.

**Getting a key.** Sign up at [the-odds-api.com](https://the-odds-api.com). The free tier is 500 credits a month with no card. The dashboard shows your key and remaining credits and has a regenerate button, which is how you rotate the key if it ever leaks.

**Loading it into the shell.**

```bash
read -rs ODDS_API_KEY && export ODDS_API_KEY
# paste the key, press Enter. Nothing is echoed. Then confirm it landed:
[ -n "$ODDS_API_KEY" ] && echo "key is set (${#ODDS_API_KEY} chars)"
```

Why this and not `export ODDS_API_KEY=abc123`: anything typed as a command lands in `~/.zsh_history` (or `~/.bash_history`) in plain text, and `export KEY=value` also shows in `ps` output for child processes. `read -rs` reads from the keyboard with echo off (`-s`) and without interpreting backslashes (`-r`), so the key never appears on screen, in history, or in a screenshot. `export` marks the variable for inheritance by the Python process. The variable dies with the shell; a new terminal tab needs the `read` again. That is a feature.

**Never** put the key in `~/.zshrc` (plain text on disk), in any file inside the repo (the repo is public; `gitleaks` in CI would catch a commit, but do not test it), as a `--api-key` flag (the code deliberately has none), or in a chat message or screenshot. The length check above prints only the character count. If the key leaks, regenerate it in the dashboard first and investigate second.

The code reads the variable in exactly one place, hands it to the HTTP client, and that client scrubs `apiKey=` from every exception and log line it emits, so a pasted traceback is safe. The tests prove that with a fake key.

## The commands

All commands run from the repository root with the venv active. Every one accepts `--help`, which is the source of truth when this document and the code disagree.

### `collect --mode settled` (free, weekly)

```bash
python -m kalshi_edge.collect --series KXNFLGAME --mode settled
```

Reads every finalized `KXNFLGAME` contract from the live settled list and the historical archive, and upserts one row per ticker with its result (`yes`, `no`, or `scalar` for a tie) and payoff. Also refreshes the series fee schedule in `series_fees`.

Why weekly: Kalshi moves old settlements into an archive, and the candlestick endpoint the backtest depends on returns 404 for archived markets. A market the collector never saw before archiving is gone from your backtest for good. Upserting means running it more often costs a few HTTP requests and nothing else.

Output is one summary line (`settled series=KXNFLGAME pages=... markets_written=...`) plus database totals. `with_result` is your backtest sample in contracts, two per game.

### `collect --mode snapshot` (free, game day)

```bash
python -m kalshi_edge.collect --series KXNFLGAME --mode snapshot
```

Reads the currently open markets and stores each contract's bid, ask, last price, volume and open interest, stamped with the run's start time. Rerunning within the same second updates in place; a later run appends, so over weeks this builds a price history.

`compare` prices the Kalshi side at the ask from the newest snapshot, so the snapshot has to be taken within minutes of the odds capture or the "edge" is just line movement.

Flags for both modes: `--series A B` or `--series A,B`; `--config config/series.example.json`; `--db PATH`; `--min-interval 0.25` seconds between requests; `--limit 200` page size; `--max-pages 500` safety stop.

### `calibrate` (free, whenever)

```bash
python -m kalshi_edge.calibrate --series KXNFLGAME
```

For each settled game, fetches the hourly candlestick history once, takes the last bar that closed before the game started, caches that pre-game bid and ask in `pre_event_prices`, then buckets games by pre-game probability and reports how often each bucket resolved YES (with a 95% interval) and the fee-adjusted return of buying each side.

"Before the game started" is the hard part: no field Kalshi publishes is kickoff. On `KXNFLGAME`, `occurrence_datetime` is the scheduled end of the game. The first version of this backtest trusted it, read in-game prices, and scored a flattering Brier of 0.11 that was pure lookahead. The current code takes the earliest end-of-event timestamp and subtracts `--event-hours` (default 4), about an hour before an NFL kickoff.

| Flag | Default | What it changes |
| --- | --- | --- |
| `--event-hours H` | 4 | Hours subtracted from the event's end timestamp to find the pre-game cutoff. A series whose timestamp really is the start would use 0. |
| `--lead-hours H` | 0 | Moves the cutoff another H hours earlier. `--lead-hours 12` asks "what did the market say that morning". |
| `--lookback-hours H` | 72 | How far back from the cutoff to search for the last two-sided bar. |
| `--buckets N` | 10 | Equal-width probability buckets. Fewer buckets means more games per bucket and steadier "actual" numbers. |
| `--contracts N` | 1 | Contracts per market for the ROI columns. The fee rounds up per order, so 1 is slightly pessimistic. |
| `--all-contracts` | off | Count both teams' contracts. Off because "A wins" and "B loses" are one coin, not two; it also makes the intervals narrower than the truth. |
| `--max-fetch N` | none | Stop after N candlestick requests, for a quick first look. |
| `--refresh` | off | Throw the pre-event cache away and refetch everything. |
| `--json PATH` | none | Also write the report as JSON. |
| `--plot PATH.png` | none | Also write a reliability diagram (realized against predicted, one dot per bucket sized by count, 95% interval as an error bar). Needs `requirements-plot.txt`. The PNG is not gitignored; do not `git add` it. |

The first run makes one request per settled market and takes a few minutes. Later runs only fetch newly settled games. To look at the chart from WSL, `explorer.exe .` opens the current folder in Windows Explorer.

### `odds` (2 credits, game day)

```bash
python -m kalshi_edge.odds --sport americanfootball_nfl
```

Calls The Odds API once for every bookmaker's moneyline on upcoming NFL games and stores every book's raw decimal odds in `sportsbook_odds`, plus one row in `odds_captures` recording the run. Nothing is de-vigged on the way in, so the method can change later without spending credits again.

One credit per region per market: the default `--regions us,eu` is 2 credits, and `eu` is what brings in Pinnacle. Before spending anything the run reads the remaining-credit counter from a free endpoint and refuses to run if fewer than `--min-remaining` (default 50) are left, so a bug can never drain the month. Every run prints `remaining=` and `used=`.

Run it before the snapshot: it is the slow one and the one that can fail (no key, no credits), so a failure costs nothing and you do not take a snapshot you cannot use.

Flags: `--sport KEY`, `--regions us,eu`, `--min-remaining N`, `--db PATH`.

### `compare` (free, offline, game day)

```bash
python -m kalshi_edge.compare --series KXNFLGAME
```

Takes the newest Kalshi snapshot and the newest odds capture, matches games by team codes and the US-Eastern game date in the ticker, removes each book's margin, uses Pinnacle (or a median of the books) as the fair probability, computes Kalshi's exact taker fee for a $10 order, and prints the edge per contract. No network calls, so rerun it with different flags as often as you like.

| Flag | Default | What it changes |
| --- | --- | --- |
| `--devig power\|multiplicative` | `power` | How the book's margin is removed. Power shrinks long shots more, which matches how books shade. The other method always shows in the `alt` column. |
| `--reference pinnacle\|median` | `pinnacle` | Whose line is "fair". Pinnacle falls back to the median when missing. |
| `--budget DOLLARS` | 10 | Order size: as many contracts as the budget covers at the ask including fee. |
| `--contracts N` | none | Fixed order size instead of a budget. Mutually exclusive with `--budget`. |
| `--max-gap-minutes M` | 15 | Flag the report `STALE` when the snapshot and the odds capture are further apart than this. |
| `--include-live` | off | Also price games that already kicked off, tagged `IN PLAY`. Off because live book lines and Kalshi's live market move at different speeds. |
| `--teams PATH` | `config/teams/nfl.json` | Team alias table; also names the sport. |
| `--json PATH` | none | Also write the report as JSON. |

### `scan` (free, daily)

```bash
python -m kalshi_edge.scan
```

One list for markets that are not NFL games. It needs no key. It calls the public Kalshi API, `api.weather.gov` (with a User-Agent), and the Cleveland Fed nowcast files. Weather contracts and the economic series are written into `data/kalshi.db` the same way `collect --mode snapshot` writes them, and each NWS hourly high the scan prices is written to `nws_forecasts` so a later `weather_calibrate` can fit the forecast the scan actually used. The short category list is printed and not stored.

Read the first verdict before anything else. Either some sides have a model probability above the break-even of a $10 order at the ask, or the report says `nothing clears costs today`. Jobs, unemployment, initial claims, and the Fed decision are listed with price and settlement time only: there is no free consensus for them, and this command will not invent one. CPI is modeled only for the month the Cleveland Fed file is currently nowcasting.

The run takes a few minutes. It pages every open event so it can tell which technology and AI series are actually listed, then reads those series. `--skip-short` drops that sweep and keeps weather and the economic releases. `--horizon-days 14` drops anything expected to settle further out than two weeks. Settlement times are in Central time.

| Flag | Default | What it changes |
| --- | --- | --- |
| `--budget DOLLARS` | 10 | Order size the break-even is computed for, fee included. |
| `--horizon-days N` | 30 | How soon a market has to settle to be ranked or listed. |
| `--category NAME` | Science and Technology, Companies, AI | Repeat the flag to replace the short-dated categories. Series tagged AI are included either way, except sports and politics. |
| `--skip-short` | off | Skip the category sweep. |
| `--short-limit N` | 12 | How many short-dated events to print, one strike each. The JSON copy keeps the markets. |
| `--json PATH` | none | Also write the report as JSON. |

## The routine

**Every Tuesday, after Monday night's game settles:**

```bash
cd ~/projects/kalshi-edge-finder
source .venv-wsl/bin/activate
git status && git pull origin main
python -m kalshi_edge.collect --series KXNFLGAME --mode settled
python -m kalshi_edge.calibrate --series KXNFLGAME
```

The settled run must not be skipped, for the archive reason above. The calibrate run is cheap once the cache is warm. Expect the sample to grow by about 30 games a week; each bucket needs a few hundred games before its "actual" column means much, so this is a months-long picture.

**Game day (Sunday, Monday night, Thursday night).** Run the three captures within a few minutes of each other, in one shell, before the games you care about kick off:

```bash
cd ~/projects/kalshi-edge-finder
source .venv-wsl/bin/activate
git status && git pull origin main
read -rs ODDS_API_KEY && export ODDS_API_KEY

python -m kalshi_edge.odds --sport americanfootball_nfl          # 2 credits
python -m kalshi_edge.collect --series KXNFLGAME --mode snapshot  # free
python -m kalshi_edge.compare --series KXNFLGAME                  # free, offline
```

One shell, because the exported key lives only where you ran `read`. At 2 credits a run, 500 credits is one capture a day for the month with 400 to spare. The free feed lists games only a few days ahead, so next week's Kalshi markets show as "unmatched" until the books post lines, usually midweek.

**Any morning, for the non-sports list:**

```bash
cd ~/projects/kalshi-edge-finder
source .venv-wsl/bin/activate
python -m kalshi_edge.scan
```

No key, and it does not have to share a shell with the odds capture. A positive line is still a question: check the flag (`THIN` and `WIDE` mean the ask may not fill $10; `VERIFY` means the model and the ask disagree by more than the weather guard and the side was not ranked), check that the "why" names a forecast or a nowcast rather than a guess, and read a weather edge as the fitted error in the README, which is a GFS archive transferred onto the NWS high until enough stored NWS forecasts have settled.

Refit that error with `python -m kalshi_edge.weather_calibrate` when you want the stored NWS sample in the fit. `--fit-only` uses the cache and does not call the network. The daily scan already records the forecast it used.

## Reading the output

### The calibration report

```text
calibration  series=KXNFLGAME  generated=...
settled=1010  with_pre_event_quote=246  observations=122 (one contract per event)
candles fetched=0  cached=246  skipped: not_found=764
cutoff source: occurrence_datetime=122
brier=0.2220  base_rate=0.5574  base_rate_brier=0.2467

   bucket      n  predicted    actual           95% CI   yes ROI    no ROI   (buy at ask, taker fee, 1 contract(s))
    ...
```

- `skipped: not_found=764` is normal: archived markets whose candlesticks Kalshi no longer serves. The rest only grows if the Tuesday collect keeps running.
- `observations` is games, not contracts, because one contract per event is kept by default.
- `brier` is the mean squared error between the pre-game midpoint and the 0/1 outcome. Lower is better; 0.25 is a coin flip. `base_rate_brier` is the score of predicting the overall YES rate for every game. The gap between them is how much game-specific information the market carries. A market that cannot beat the base rate knows nothing.
- `predicted` vs `actual` per bucket is the calibration question itself. If the 40-50% bucket resolves YES 45% of the time, the prices were honest there.
- `95% CI` is the range the true YES rate plausibly lies in given how few games the bucket holds. If `predicted` sits inside it, the bucket is consistent with honest pricing and you have learned nothing yet. If `predicted` sits outside it, you have a candidate mispricing worth watching as the sample grows. A bucket of 9 games has an interval 40 points wide, which is why nothing in a first season is a finding. It is a Wilson score interval: the textbook `p ± 1.96√(p(1−p)/n)` gives zero width when a bucket is all YES or all NO and can run past 0 or 1, while Wilson stays inside 0..1 (two of two YES reads `34%..100%`, not `100%..100%`). It assumes independent outcomes, which holds with one contract per game and not with `--all-contracts`.
- `yes ROI` / `no ROI` is what buying every contract in the bucket at the ask, paying the fee, and holding to settlement would have returned. A bucket can be perfectly calibrated and still lose money for a taker, because the fee and the spread are real costs. That is the usual finding.

### The compare report

See the real sample in the [README](../README.md#sample-comparison-output). The columns:

| Column | Meaning |
| --- | --- |
| `ask` | Kalshi's ask for that side. You cannot buy at the midpoint or the last trade. |
| `n` | Contracts the budget buys at that ask including the fee. |
| `fee/ct` | Kalshi's taker fee per contract, `0.07 × P × (1 − P)`, rounded up to the cent per order. Highest near 50 cents. |
| `brkeven` | `(ask + fee) / payout`: the win probability needed just to get your money back. |
| `fair` | The reference book's probability with its margin removed (power method). |
| `alt` | The same with the multiplicative method. If `fair` and `alt` disagree by more than the edge, the edge is a method artefact. |
| `edge` | `fair − brkeven` in probability points. Positive means the book thinks the contract is underpriced after fees. |
| `ev/ct`, `roi` | The same edge in dollars per contract and as a fraction of what you would spend. |

- **Negative edges everywhere are the normal result.** Kalshi's ask plus the fee is usually a point or three above the sharp book's fair price. That is the market being roughly efficient and charging you to trade, not a bug and not bad luck.
- `gap=0m` on the third header line is why the trio runs together. Past `--max-gap-minutes` the header says `STALE` and any edge is suspect.
- **In play, not priced.** Before this rule existed, the first live run showed "+2 point edges" on games already underway. They were latency between a book updating every snap and Kalshi catching up. The games are listed so you know they were seen.
- **Unmatched** lists Kalshi events with no book line yet, usually next week's games. The printed feed window says how far ahead the books have posted.
- Four rows per game, because NO on one team and YES on the other are the same bet at possibly different asks.
- Ties: a book pushes, Kalshi pays 0.5. The tool ignores this; it is about 0.2% of games.

## Troubleshooting

**The clone is on the wrong branch.** `git status` says `On branch something/else`. `main` is the branch that receives merged pull requests, so it is the only one that always has the current code; feature branches are frozen at the moment they were created and deleted after merging.

```bash
git branch --show-current
git switch main          # older git: git checkout main
git pull origin main
```

**The venv has no `pip`.** `pip install` says `command not found` or `No module named pip` inside the activated venv. Debian and Ubuntu ship `ensurepip`, the module that seeds a new venv with `pip`, in a separate package, so `python3 -m venv` built the directory but left it empty of tools.

```bash
.venv-wsl/bin/python -m ensurepip --upgrade
.venv-wsl/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
```

Installing `python3-venv` prevents it next time.

**Local changes block the pull.** `error: Your local changes to the following files would be overwritten by merge`. Git refuses to overwrite edits it has not been told to keep, which is the safe default. Look at the diff before deciding, every time.

```bash
git status               # which files, and did you mean to change them?
git diff                 # what changed (press q to leave the pager)
git stash                # park the changes; working tree becomes clean
git pull origin main
git stash pop            # bring them back, or: git stash drop  to discard
```

**The pager trap.** `git log`, `git diff` or `git branch` shows a screen of text with `:` or `(END)` at the bottom and the terminal appears stuck. You are inside `less`. Press `q` to quit; space scrolls, `/word` searches. To stop git opening a pager when output fits on one screen:

```bash
git config --global core.pager "less -F -X"
```

**A Windows venv does not work in WSL.** `source .venv/bin/activate` says `No such file or directory` (a Windows venv has `Scripts/`, not `bin/`), or `python` inside it is a `.exe`. Use a venv created from inside WSL, as in [Fresh setup](#fresh-setup). The Windows one is gitignored and harmless.

**"set ODDS_API_KEY in the environment".** The key is not in this shell: you opened a new tab, or `read` was interrupted. Run the `read -rs ... && export` line again in the tab you will run `odds` from.

**"data/kalshi.db does not exist; run collect ... first".** Either no collect has run yet, or you are not in the repository root (`pwd`). The database path is relative to the current directory.

**The odds run refuses to spend.** Fewer than `--min-remaining` (50) credits are left for the month. That is the safety stop doing its job. Credits reset monthly; do not lower the threshold to zero for one more run.

**"database is locked".** Another process has the SQLite file open: a second terminal running a collect, or, if the clone is under OneDrive, the sync client reading it mid-write. Wait and retry. If the clone is under OneDrive, that is the reason to move it to `~/projects`.

## Honest framing

- **A positive edge is a question, not a bet.** Check the gap. Check that `ref=pinnacle` and not a median of two books. Check that `alt` agrees with `fair`. Remember that depth at that ask is unknown, and that a +0.3 point edge on one contract is inside the noise of every one of those checks.
- **The reference is a sportsbook, not the truth.** Pinnacle is the best public estimate and can still be wrong. Only the calibration backtest, over months, will say whether Kalshi's own prices are biased.
- **Combos are negative expectation.** A parlay multiplies its legs; if each leg is fairly priced and costs a fee, the combination's expected return is worse than any leg alone. At $10 on a typical three-leg parlay the expected loss is about $1.35. The math is in `kalshi_edge.odds_math.combo` and there is deliberately no command to search for combos.
- **Stakes stay small.** Nothing this tool prints is a reason to bet more than you already planned to.

## Quick reference

```bash
# ---- every session ----
cd ~/projects/kalshi-edge-finder
source .venv-wsl/bin/activate
git status && git pull origin main

# ---- Tuesday ----
python -m kalshi_edge.collect --series KXNFLGAME --mode settled
python -m kalshi_edge.calibrate --series KXNFLGAME

# ---- game day, one shell, within minutes ----
read -rs ODDS_API_KEY && export ODDS_API_KEY
python -m kalshi_edge.odds --sport americanfootball_nfl          # 2 credits
python -m kalshi_edge.collect --series KXNFLGAME --mode snapshot
python -m kalshi_edge.compare --series KXNFLGAME

# ---- any morning, no key ----
python -m kalshi_edge.scan
python -m kalshi_edge.scan --horizon-days 14 --skip-short

# ---- variations, no network ----
python -m kalshi_edge.compare --series KXNFLGAME --reference median --devig multiplicative
python -m kalshi_edge.compare --series KXNFLGAME --include-live
python -m kalshi_edge.calibrate --series KXNFLGAME --lead-hours 12 --buckets 5
python -m kalshi_edge.calibrate --series KXNFLGAME --plot calibration.png   # needs requirements-plot.txt

# ---- before pushing any code ----
ruff check . && python -m pytest

# ---- escape hatches ----
q                                                   # leave the git pager
git stash && git pull origin main && git stash pop
.venv-wsl/bin/python -m ensurepip --upgrade
```
