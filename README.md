# Fundamentals + News Trader

An investing bot that screens the full US-equity universe on **fundamentals**, reacts to
**real-time news**, and produces trades on **Robinhood** — built as a funnel so cheap
deterministic code does the heavy lifting and the LLM only analyzes a handful of
high-signal candidates per day.

```
[1] Universe sync (weekly)      SEC ticker/exchange file -> ~4-5k listed US stocks
[2] Fundamentals ETL (nightly)  EDGAR XBRL companyfacts -> cleaned quarterly rows
[3] Quant screen (nightly)      composite score (quality/growth/health/value)
                                -> top-200 candidate pool + red-flag list
[4] News poller (90s, mkt hrs)  Stock Titan JSON feed -> dedupe -> event classification
[5] Trigger engine              news x fundamentals rules -> analysis queue
[6] LLM analyst (Claude)        cleaned data + news + quote -> structured Thesis
[7] Risk gate (pure code)       position/daily/concentration/cash limits
[8] Execution                   dry_run | recommend | auto (Robinhood MCP limit orders)
[9] Monitor (daily)             open theses vs. positions -> markdown report
```

### Data sources

- **Fundamentals — SEC EDGAR** (free, official): full XBRL financial statements for every
  US filer. The cleaning layer (`src/bot/cleaning.py`) handles tag fallbacks, restatements,
  Q4 derivation from annual filings, and unit/outlier sanity checks. This replicates a
  Morningstar-style quality/health/valuation analysis on data we can legally pull at scale
  (Morningstar itself has no self-serve API).
- **News — Stock Titan**: polls the JSON endpoint their own web app uses. Unofficial, so it
  is isolated behind a `NewsProvider` interface (`src/bot/news/base.py`) and can be swapped.
- **Market data + orders — Robinhood MCP**: quotes, portfolio, positions, and the
  `review_equity_order -> place_equity_order` flow.

### Going live: the toggle

Trading mode is a single runtime toggle — no redeploy, takes effect on the next cycle:

```bash
bot mode              # show the effective mode + kill switch
bot mode recommend    # reports + notifications, no orders
bot mode auto         # LIVE trading (asks for confirmation)
bot mode dry_run      # back to paper
bot killswitch on     # emergency stop: blocks ALL orders in every mode
```

The toggle is stored in the database and overrides the `TRADE_MODE` env var, so on Render
you can flip it from a shell (`Service -> Shell -> bot mode auto`) while the worker runs.

### Bankroll awareness

The bot never assumes the account is funded. Position sizing is
`min(requested, MAX_POSITION_USD, 10% of portfolio, daily budget headroom, cash above the
reserve floor)` — so a small bankroll automatically produces small orders, an unfunded
account blocks buys cleanly *before* they reach the broker (with "insufficient funds"
recorded on the order), and in AUTO mode an empty account skips LLM analysis of new buys
entirely (held positions are still reviewed so sells can fire). Anything below
`MIN_ORDER_USD` is skipped. If the broker still rejects an order, the failure is recorded
and notified — the worker never crashes on it.

### Safety model

Real money demands layered controls, all in plain code the LLM can't override:

1. `TRADE_MODE=dry_run` (default) only logs hypothetical orders; `recommend` writes reports
   and notifies you; `auto` actually places limit orders.
2. The risk gate (`src/bot/risk.py`) enforces max $/position, max daily deployment, max open
   positions, per-name concentration, a cash reserve floor, and a minimum conviction score.
3. `KILL_SWITCH=true` blocks all orders regardless of mode.
4. Red-flagged stocks (dilution, distress, cash burn) are excluded from the candidate pool
   and bullish news on them is ignored.

Run in `dry_run`/`recommend` long enough to evaluate the hit rate before enabling `auto`.
**This is not financial advice; you are responsible for trades placed by your account.**

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # then fill in EDGAR_USER_AGENT and ANTHROPIC_API_KEY
```

## Usage

```bash
bot sync                  # refresh the universe (~4-5k listed stocks)
bot etl --limit 100       # fundamentals ETL (full universe takes ~10 min at 8 req/s)
bot screen                # compute scores + candidate pool
bot top --n 25            # inspect the top of the pool
bot news                  # one news ingest + analyze cycle
bot news --loop           # keep polling
bot analyze NVDA          # force a full analysis of one ticker
bot monitor               # daily portfolio review report
bot run                   # start the full scheduler (production entrypoint)
pytest                    # unit tests (cleaning, screener, risk gate)
```

## Deploying on Render

The repo ships a `render.yaml` blueprint: a Docker **background worker** running `bot run`
(APScheduler drives all nine pipeline stages) with a persistent disk for SQLite + reports.

1. Create a new Blueprint on Render pointing at this repo.
2. Set the secret env vars in the dashboard: `EDGAR_USER_AGENT`, `ANTHROPIC_API_KEY`, and —
   for trading — `ROBINHOOD_ACCOUNT_NUMBER` (the agentic account; see below).
3. Connect Robinhood (one-time OAuth; see the next section). Until then the bot runs
   data-only: screening, news, analysis, and dry-run sizing all work without a broker.
4. Leave `TRADE_MODE=dry_run` for the first weeks; review `reports/` and the `orders` table;
   then graduate to `recommend`, and finally `auto`.

### Connecting Robinhood (agentic trading)

Robinhood's official Trading MCP lives at a fixed endpoint —
`https://agent.robinhood.com/mcp/trading` (the same URL you'd paste as a connector in
Claude or Cursor) — and it has **no API keys or static tokens**. Auth is an OAuth flow you
approve once in a desktop browser; the server then issues short-lived access tokens plus a
refresh token, which the bot stores and rotates automatically. Trading is confined to a
dedicated **Agentic investing account**, separate from your main brokerage.

One-time setup:

1. In the Robinhood app, enable Agentic Trading: open the agentic account and fund it with
   only what you want the bot to trade. Note its account number.
2. On your **desktop** (Robinhood only allows `localhost` OAuth redirects):
   ```bash
   pip install -e . && bot broker login   # browser opens -> approve in Robinhood
   bot broker status                      # verifies with a live quote call
   ```
   Tokens land in `data/robinhood_tokens.json` (chmod 600) and auto-refresh from then on.
   No browser on the machine? `bot broker login --manual` prints the URL and lets you
   paste the redirect back.
3. To run on Render, move the credentials to the service's persistent disk:
   ```bash
   bot broker export                      # locally: prints a base64 blob
   # Render dashboard -> Service -> Shell:
   bot broker import <BLOB> && bot broker status
   ```
4. Set `ROBINHOOD_ACCOUNT_NUMBER` to the agentic account number.

`ROBINHOOD_MCP_URL` / `ROBINHOOD_MCP_TOKEN` remain available as overrides for community or
self-hosted MCP wrappers only; the official MCP needs neither.

## Layout

```
src/bot/
├── config.py      settings + risk limits (env-driven)
├── db.py          SQLite schema/helpers
├── universe.py    [1] SEC ticker/exchange sync
├── edgar.py       [2] rate-limited EDGAR client
├── cleaning.py    [2] XBRL normalization (tag fallbacks, Q4 derivation, restatements)
├── etl.py         [2] nightly fundamentals refresh
├── screener.py    [3] TTM metrics, percentile scores, red flags, candidate pool
├── news/          [4] provider interface + Stock Titan implementation
├── triggers.py    [5] news x fundamentals trigger rules
├── analyst.py     [6] Claude analyst -> structured Thesis (pydantic)
├── risk.py        [7] hard limits
├── broker.py      [8] Robinhood MCP wrapper
├── broker_auth.py [8] Robinhood OAuth (login flow + token storage/refresh)
├── engine.py      [5-8] orchestration + reports + notifications
├── monitor.py     [9] daily position review
├── scheduler.py   APScheduler wiring (production entrypoint)
└── cli.py         typer CLI
```
