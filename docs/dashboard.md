# Dashboard

A web dashboard over everything the platform collects, live while the market is open.

The layout is a workspace: the page you are on fills the middle, and a column of panels on the
right keeps the rest in view. The top bar names the section you are in and what it is for; click
it (or press **1**–**6**) to switch. Each panel on the right (**Account**, **Your trading**,
**Ideas**, **Market**, **Positions**, **Paper bot**, **System**) shows a one-line summary in its header,
expands for the detail, and has a **›** that opens its full page in the middle. **Ideas** shows the
top idea of each list and how many are new since the previous session. Which panels are
open is remembered on this browser; **◫** hides the column, **◐** cycles dark (the default),
light and the system theme. Below 1100 px wide the panels move under the page.

Six sections, each answering one question:

| Section | The question | Inside |
|---|---|---|
| **Home** | What's going on today, and what needs me? | Account value and today's P&L (live), total P&L, options time decay, the paper bot, the market (trend, VIX, breadth) and rates (10-year and 3-month yields, the curve); **Needs your attention** (options about to be exercised or expire, actions the rules flag on positions, paper orders waiting, stale data), each linking to where you act; the top research ideas; live prices; recent trades |
| **Portfolio** | What do I own, and what should I do about it? | The account's numbers on top, then three views. **Positions**: every holding with its statistics, options first by expiry; open one for live bid/ask, greeks, odds, scenarios, plain-language notes, **What you could do** (rolls, taking profits, covered calls, LEAPS in place of shares, protective puts, priced from the latest chain) and a Claude chat ([positions.md](positions.md)). **Allocation**: by sector and asset class, concentration, exposure per underlying (shares plus option delta). **Performance**: your account's time-weighted return and today's mix against the S&P 500, Nasdaq 100 and 60/40. **Risk**: the whole book's beta, volatility, Value at Risk and expected shortfall, option Greeks in dollars, a scenario grid (S&P 500 ±10% with implied volatility ±5 points, options repriced) and how correlated the largest positions are ([risk.md](risk.md#portfolio-risk)) |
| **Research** | What should I look at next? | **Options ideas**: ranked swing calls, LEAPS, bearish puts and premium selling with the contract each would use, from the latest stored run (made whenever new data lands; **Update now** for a fresh one), new ones marked ([research.md](research.md#kept-refreshed-and-scored)). **Track record**: how every past idea has done since it was listed. **Look up a symbol**: any ticker in depth (trend, option pricing and skew, every structure's contract including cash-secured puts, credit spreads, iron condors and calendars, scenarios, price chart, term structure and the smile with its SVI fit, Claude); a ticker the platform doesn't have yet can be fetched from there. **Portfolio checks**: the rules-based checks on your portfolio ([ideas.md](ideas.md)) |
| **Trades** | What did I (and the bot) trade, and how did it go? | **How you trade**: your own closed trades as statistics (below). **All trades**: every trade, yours and the paper bot's, newest first, with the P&L each realized; today's trades as pending until the transaction posts |
| **Paper trading** | How are the automated strategies doing? | **Bot and orders**: the bot's state, queued orders with their reasons and a veto, approvals, every sleeve's equity and progress, the kill switch ([paper-trading.md](paper-trading.md)). **Strategy backtests** |
| **System** | Is the data fresh, and where does it live? | Each job's last run; **Scheduled jobs**: the scheduler's jobs, when each runs, how its last run went, and a button to run one now ([setup.md](setup.md#6-schedules)); **Where your data lives**: every dataset's folder, size, last update, what writes it and what reads it, and how data flows from Fidelity, Alpaca and the paper bot to each section |

Addresses are `#/section/view/detail` (for example `#/portfolio/positions/<symbol>` or
`#/research/lookup/<symbol>`), so any view can be bookmarked. Old addresses (`#/overview`,
`#/positions`, `#/ideas`, `#/market`, `#/options`, `#/strategies`) still land in the right place.

### How you trade

`#/trades/stats` turns your brokerage history into the numbers behind your habits:

- **Round trips.** Each sale is matched to the buys before it, first in first out, per account
  and symbol. Options are opened and closed by their action (buy to open, sell to close, and so
  on); expired, assigned and exercised contracts close at zero. Options you sold to open are
  tracked as their own kind, where the premium kept is the profit.
- **By kind** (calls, puts, shares, options sold): how many trades, the win rate, the typical
  (median) return per trade, the average winner and loser, how long you held (median, and
  separately for winners and losers), how many expired, and the realized P&L. The return on all
  the money put in (cost-weighted) sits next to the median, since a few large trades can differ
  from the typical one.
- **How long you hold**: trades per holding period (same day, a week, a month, three months, a
  year, longer), for all trades or one kind, and how long the positions still open have been
  held.
- **Realized by month**, and the **best and worst trades** (winners and losers only).
- **What the numbers say**: plain sentences when a pattern stands out, such as holding losers
  much longer than winners, calls left to expire worthless, or a win rate below what the average
  win and loss need to break even.

Sales of positions bought before the synced history have no known cost; they are counted and
left out rather than guessed. The numbers cover only what the brokerage sync has pulled, so
sync first (`uv run tp-broker sync`) if recent trades are missing.

## Run it

```bash
make web-install web-build           # once, and after UI changes: bundle apps/dashboard (Node 22+)
uv run tp-api --quotes fake          # http://127.0.0.1:8000 with simulated live prices
uv run tp-api --quotes alpaca        # real-time IEX trades from Alpaca (needs ALPACA_* keys)
```

`tp-api` serves the built app at `/`. When the dashboard's source is newer than the build (you
pulled new code), it rebuilds it on start if Node and the packages are installed, and otherwise
logs how to (`make web-install web-build`); the System tab shows when it was built. Without
that, new pages such as Paper and Trades don't appear. For UI work, `npm run dev` in `apps/dashboard` gives hot
reload on http://localhost:5173 and proxies `/api` and `/ws` to `tp-api`
([apps/dashboard/README.md](../apps/dashboard/README.md)).

A complete demo with synthetic data needs no keys:

```bash
uv run tp-data bars backfill --source fake
uv run tp-data options snapshot --source fake
uv run tp-broker sync --source fake
```

On the Linux box, `infra/systemd/tp-api.service` keeps it running (user unit, `Restart=on-failure`).

## How live updates work

```
Alpaca IEX trade stream (1 connection, <= 30 symbols)
        -> LiveHub (latest price per symbol, live portfolio valuation)
            -> /ws/live -> every open browser tab (batched once a second)
```

Streamed symbols are your market-priced holdings plus the `[dashboard] watchlist` in
`config/universes.toml`, and the underlyings of options you hold. Today's P&L is measured against
the last close in the lake. Holdings without a live price (mutual funds, options, cash) are held
at the broker's last valuation; an option's underlying is live, so its moneyness updates.
Outside market hours the stream is quiet and the dashboard shows last closes.

## API

All JSON. Reads (`GET`):

| Endpoint | What |
|---|---|
| `/api/health` | liveness |
| `/api/status` | market clock, bars freshness, latest validation, options and fundamentals reports, VIX and Treasury yields with the last macro run, broker syncs, live feed, dashboard build |
| `/api/market/regime` | today's trend, volatility (VIX), breadth, rates and yield-curve labels with the numbers behind them, and the last year of labels |
| `/api/bars/{symbol}?days=365` | daily bars (raw and adjusted close) |
| `/api/options/{underlying}` | latest chain snapshot: ATM IV, expected move and SVI fit per expiry, the smile with its fit, 25-delta skew, term-structure slope, inconsistent quotes |
| `/api/portfolio` | accounts (masked), holdings with weights, classification and P&L, option terms (premium per share, breakeven, moneyness, days to expiry), exposures, and `pnl`: unrealized, realized, income, total |
| `/api/positions` | every position with statistics, notes and actions; options with live quotes, greeks, odds and scenarios ([positions.md](positions.md)) |
| `/api/positions/risk` | the whole book: exposure per underlying, beta, volatility, VaR and expected shortfall, correlation, option Greeks, scenario grid ([risk.md](risk.md#portfolio-risk)) |
| `/api/research?refresh=false` | ranked swing, LEAPS, bearish and premium-selling ideas with contract picks, from the latest stored run, with `run` (when it was made, what dropped off) and `new` on each idea ([research.md](research.md#kept-refreshed-and-scored)); `refresh=true` makes a run first |
| `/api/research/track-record` | how every past recommendation has done: the stock's move since (5 and 21 sessions, to date, against the S&P 500) and the position marked now or at expiry; a summary per list |
| `/api/jobs` | the job scheduler: whether it runs, and each job's schedule, last run and next window |
| `/api/research/symbol/{symbol}` | one symbol in depth: trend, option pricing (IV, rank, skew, events), each structure's contract, earnings dates and valuation from SEC filings |
| `/api/research/fetch` | the last ticker fetch: running, result |
| `/api/system/data` | every dataset: folder, files, size, last update, what writes and reads it |
| `/api/trades?source=all\|mine\|paper` | your trades (and today's, pending, from position changes) and the paper bot's fills, newest first, with realized P&L; totals; how fresh the brokerage data is |
| `/api/trades/stats` | your closed round trips as statistics: by kind, holding periods, monthly realized P&L, best and worst trades, open positions' age, observations |
| `/api/broker/sync` | the last dashboard-started brokerage sync: running, result |
| `/api/portfolio/performance?days=365` | current-holdings backtest and account TWR vs benchmarks |
| `/api/ideas` | research ideas |
| `/api/strategies/ma-timing?universe=spy\|gtaa` | strategy 1 vs buy and hold |
| `/api/paper` | paper account, bot heartbeat and next task, kill switch, reconciliation, every sleeve's progress |
| `/api/paper/proposals?scope=pending\|queued\|recent` | proposals with reasons and risk checks |
| `/api/paper/history` | each sleeve's equity at every close |
| `/api/paper/events` | paper-trading activity log |
| `/ws/live` | WebSocket: `snapshot`, then `update` messages with quotes and portfolio value |

Writes (`POST`; paper trading, and pulling brokerage data; nothing can trade the Fidelity
account):

| Endpoint | What |
|---|---|
| `/api/paper/proposals/{id}/approve` | `{"quantity": n?, "note": "..."}`: approve, optionally fewer shares |
| `/api/paper/proposals/{id}/reject` | `{"note": "..."}`: reject, or veto a queued order before it is sent |
| `/api/paper/approve-all` | `{"strategy": "..."?}`: every pending proposal (of one strategy) |
| `/api/paper/kill` | `{"reason": "..."}`: engage the kill switch and cancel open paper orders |
| `/api/research/fetch` | `{"symbol": "..."}`: pull two years of bars and today's option chain for a ticker (background) |
| `/api/research/run` | make and store a run of the recommendations now, and return it |
| `/api/jobs/{name}/run` | run one scheduled job now (`bars`, `options-close`, `recommendations`, ...) |
| `/api/research/ask` | `{"question": "...", "symbol": "...", "history": [...]}`: ask Claude about a research idea (needs `ANTHROPIC_API_KEY`) |
| `/api/positions/ask` | `{"question": "...", "symbol": "..."?, "history": [...]}`: ask Claude about a position (needs `ANTHROPIC_API_KEY`; sends the figures to Anthropic's API) |
| `/api/broker/sync` | `{"refresh": true?}`: sync the brokerage now (read-only), optionally having SnapTrade re-pull from Fidelity first; runs in the background |

Interactive docs: `http://127.0.0.1:8000/docs`.

## Security

The dashboard shows your brokerage holdings, so:

- `tp-api` binds to `127.0.0.1` and refuses any other `--host` unless `TP_DASHBOARD_TOKEN` is set.
- With a token set, every `/api/*` call needs `Authorization: Bearer <token>` and the WebSocket
  needs `?token=<token>`. `/api/health` stays open for monitoring.
- Reach it remotely through an SSH tunnel or a private network (Tailscale/WireGuard), not by
  opening a port to the internet.
- It only answers to the host names in `TP_DASHBOARD_HOSTS` (default `localhost,127.0.0.1`), so
  a web page whose domain points at your machine (DNS rebinding) gets nothing. Add the name you
  use to reach it remotely.
- The writes (paper trading, brokerage sync, asking Claude) also need the dashboard's
  `X-TP-Client: dashboard` header and, when the browser sends one, an `Origin` matching the
  host. A page you happen to visit can't send that header to another site without a CORS
  preflight, which this server never grants, so it can't approve trades, trip the kill switch
  or run up your Claude bill on your behalf.
