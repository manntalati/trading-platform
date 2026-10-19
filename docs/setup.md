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
| `ANTHROPIC_API_KEY` | Ask Claude on positions and ideas; scoring news headlines | optional |
| `TP_NEWS_SCORING=on` | the scheduler has Claude score each evening's headlines for the news-sentiment feature (costs API tokens) | optional |
| `TP_JOBS_OFF` | scheduled jobs to turn off, e.g. `options-midday,validation` | optional |
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

`tp-api` runs a job scheduler while it's up, so keeping the dashboard's server running keeps the
data and the recommendations current. On trading days (New York time):

| Job | When | Command |
|---|---|---|
| VIX and Treasury yields | from 07:30 | `tp-data macro daily` |
| Fidelity sync | hourly, 07:30 to 22:30 (once linked) | `tp-broker sync --source snaptrade` |
| Option chains, midday | from 12:30 | `tp-data options snapshot` |
| Option chains, close | 15 minutes before the close | `tp-data options snapshot` |
| Daily bars | from 18:30 until the next open | `tp-data bars daily` |
| News headlines | from 18:45 | `tp-data news fetch` |
| News sentiment | from 18:45, with `TP_NEWS_SCORING=on` | `tp-data news score` |
| Model monitoring | from 19:30 (after the bars) | `tp-models monitor` |
| Fundamentals | from 20:00 (with `TP_SEC_USER_AGENT`) | `tp-data fundamentals` |
| Backtest gate | Saturdays from 10:00 | `tp-backtest validate --all` |
| **Recommendations** | whenever any job above brings new data | `tp-recs run` |
| Paper bot | always on, separately | `tp-paper bot` (or `tp-paper bot --once` every 5 minutes from cron) |

A job runs once in each window, as soon as it opens or the computer is next awake inside it: a
laptop asleep at 18:30 gets its bars when it wakes at 21:00 (cron would skip the day). Option
chains are only taken while the market is open. Failures retry 15 minutes later (two when the
network was down). **System → Scheduled jobs** shows each job's last run and next window, with a
button to run one now; `uv run tp-jobs status` prints the same, and the logs are in
`data/logs/<job>.log`. `TP_JOBS_OFF=options-midday,validation` in `.env` turns jobs off.

- **macOS**: `make mac-agent` starts `tp-api` at login and restarts it if it stops (a launchd
  agent; `make mac-agent-remove` undoes it). The Mac still has to be awake for a window; the
  scheduler catches up on wake. To be awake for the closing option snapshot, System Settings →
  Battery → Options → "Wake for network access", or `sudo pmset repeat wakeorpoweron MTWRF
  15:35:00`.
- **Linux**: the `tp-api` user service runs it ([infra/README.md](../infra/README.md)); the
  per-job systemd timers there are an alternative with failure alerts.
- **Without the dashboard**: `uv run tp-jobs run` runs the scheduler alone, or `tp-jobs once`
  every five minutes from cron. Cron or systemd entries can stay: a job whose data is already
  fresh isn't run twice, and the scheduler waits a while into a window for a job cron also runs.
  [infra/cron/crontab.example](../infra/cron/crontab.example) has fixed-time cron lines; on macOS,
  if the repo is under Documents or Desktop, give `cron` Full Disk Access (System Settings →
  Privacy & Security) or it can't read it.

## 7. Check

```bash
uv run tp-doctor
```

Each line is ✓ (done), ✗ (to set up, with the command), ! (set up but stale or worth a look) or
· (optional). The System page of the dashboard shows the same freshness while it runs.

Trades from Fidelity missing? `uv run tp-broker check` compares what SnapTrade has with what's
stored here and says why ([portfolio.md](portfolio.md#when-trades-are-missing)).
