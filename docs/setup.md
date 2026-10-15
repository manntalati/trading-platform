# Setting it up

Everything the platform needs, in order. `uv run tp-doctor` checks all of it and prints the
command for whatever is missing; run it first, and again at the end.

## 1. Code and tools

```bash
git pull                                # the latest main
uv sync --all-packages                  # Python packages (uv installs Python 3.12 itself)
```

The dashboard also needs Node 22 or newer (`node --version`).

## 2. Keys and settings: `.env`

```bash
cp .env.example .env && chmod 600 .env  # git-ignored; readable by you only
```

| Setting | For | Required? |
|---|---|---|
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | prices, option chains, live quotes, the paper bot. **Paper** keys: https://app.alpaca.markets/paper/dashboard/overview | yes |
| `SNAPTRADE_CLIENT_ID`, `SNAPTRADE_CONSUMER_KEY` | your Fidelity positions and trades (read-only). A personal key: https://dashboard.snaptrade.com | yes, for the portfolio |
| `TP_SEC_USER_AGENT` | fundamentals and earnings dates from the SEC: a name and an email, e.g. `"trading-platform you@example.com"` | yes, for fundamentals |
| `ANTHROPIC_API_KEY` | Ask Claude on positions and ideas | optional |
| `FRED_API_KEY` | makes FRED the first source for VIX and yields (Cboe and the Treasury work without it); free: https://fred.stlouisfed.org/docs/api/api_key.html | optional |
| `TP_DASHBOARD_TOKEN` | opening the dashboard from another machine | optional |
| `TP_DISCORD_WEBHOOK_URL` | a Discord message when a scheduled job fails (systemd only) | optional |

Then `uv run tp-data check` confirms the Alpaca keys work.

## 3. Load the data once

```bash
uv run tp-data bars backfill --years 5          # daily prices (a few minutes)
uv run tp-broker link                           # prints a link: open it, connect Fidelity
uv run tp-broker sync                           # your positions and trades
uv run tp-data macro backfill                   # VIX and Treasury yields since 1990
uv run tp-data fundamentals                     # SEC filings for the universe and your holdings
uv run tp-data options snapshot                 # today's option chains: only while the market is open
```

Optional: your own tickers in `config/universes.local.toml` (git-ignored; see
[research.md](research.md#adding-your-own-tickers)), and 5-minute bars for the opening-range
breakout research (`uv run tp-data bars intraday --symbols SPY,QQQ --days 250`).

## 4. The dashboard

```bash
make web-install web-build                      # once, and after dashboard changes
uv run tp-api --quotes alpaca                   # http://127.0.0.1:8000
```

## 5. Paper trading

Check the book in [`config/paper.toml`](../config/paper.toml) (which strategies, how much each),
run the backtest gate (`uv run tp-backtest validate --all`; the reports appear under Paper
trading → Strategy backtests), then start the bot: `uv run tp-paper bot`
([paper-trading.md](paper-trading.md)).

Optional, for the machine-learning work ([ml.md](ml.md)): `uv run tp-ml meta` evaluates the
RSI(2) meta-labeling filter on your bars and records the run in MLflow (`data/mlflow/`, no
server); `uv run tp-ml ui` browses the runs. Nothing to schedule.

## 6. Schedules

Nothing above refreshes itself. These keep it current; the machine has to be on (and awake) at
those times, which is why the plan puts it on an always-on box:

| Job | When (New York) | Command |
|---|---|---|
| VIX and Treasury yields | weekdays 07:30 | `tp-data macro daily` |
| Fidelity sync | weekdays hourly, 07:30 to 21:30 | `tp-broker sync --source snaptrade` |
| Option chains | weekdays 15:45 (must be during market hours) | `tp-data options snapshot` |
| Daily bars | weekdays 18:30 | `tp-data bars daily` |
| Fundamentals | weekdays 20:00 | `tp-data fundamentals` |
| Backtest gate | Saturdays | `tp-backtest validate --all` |
| Paper bot | always on | `tp-paper bot` (or `tp-paper bot --once` every 5 minutes from cron) |
| Dashboard | always on | `tp-api --quotes alpaca` |

- **Linux**: systemd user timers and services do all of it, with failure alerts:
  [infra/README.md](../infra/README.md).
- **macOS or Linux without systemd**: `mkdir -p data/logs`, then paste
  [infra/cron/crontab.example](../infra/cron/crontab.example) into `crontab -e` and fix the path
  on its `REPO=` line. Its times are in UTC, chosen for both daylight and standard time; the
  logs go to `data/logs/`. On macOS, if the repo is under Documents or Desktop, give `cron` Full
  Disk Access (System Settings → Privacy & Security) or it can't read it.

## 7. Check

```bash
uv run tp-doctor
```

Each line is ✓ (done), ✗ (to set up, with the command), ! (set up but stale or worth a look) or
· (optional). The System page of the dashboard shows the same freshness while it runs.
