# Dashboard

A web dashboard over everything the platform collects, live while the market is open:

- **Overview**: portfolio value and today's P&L ticking in real time, your total P&L, the paper
  bot's P&L and whether it's running, the latest trades (yours and the bot's), market clock,
  live watchlist.
- **Portfolio**: Fidelity holdings (read-only) with what you're up or down: total P&L, unrealized
  (open positions against their cost) and realized (closed trades), per holding in $ and %;
  options in their own table, quoted like the broker does (contracts, premium per share), with
  the underlying's live price, breakeven and days to expiry; allocation by sector and asset
  class, concentration, and performance against S&P 500, Nasdaq 100 and 60/40.
- **Trades**: every trade in one list, yours (from the brokerage sync) and the paper bot's
  fills, newest first, each with the profit or loss it realized; filter All / Mine / Paper bot.
- **Ideas**: the rules-based research ideas ([ideas.md](ideas.md)) with the numbers behind each.
- **Strategies**: strategy 1 (10-month MA timing) on SPY and on Faber's GTAA, against buy and hold.
- **Paper**: the bot's state and next step; the orders it has queued for the next open, each
  with its reason and risk checks and a **Don't trade** veto; proposals waiting for approval
  (strategies on manual approval only); every sleeve's equity, drawdown, slippage and progress
  toward the paper gate; recent orders, activity, and the kill switch
  ([paper-trading.md](paper-trading.md)).
- **Market / Options**: price history per symbol; IV term structure, expected move and smile from
  the daily chain snapshots.
- **System**: last ingest, validation report, option snapshots, broker syncs, live-feed status.

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
| `/api/status` | market clock, bars freshness, latest validation and options reports, broker syncs, live feed, dashboard build |
| `/api/bars/{symbol}?days=365` | daily bars (raw and adjusted close) |
| `/api/options/{underlying}` | latest chain snapshot: ATM IV and expected move per expiry, smile |
| `/api/portfolio` | accounts (masked), holdings with weights, classification and P&L, option terms (premium per share, breakeven, moneyness, days to expiry), exposures, and `pnl`: unrealized, realized, income, total |
| `/api/trades?source=all\|mine\|paper` | your trades (and today's, pending, from position changes) and the paper bot's fills, newest first, with realized P&L; totals; how fresh the brokerage data is |
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
- The paper-trading writes also need the dashboard's `X-TP-Client: dashboard` header and, when
  the browser sends one, an `Origin` matching the host. A page you happen to visit can't send
  that header to another site without a CORS preflight, which this server never grants, so it
  can't approve trades or trip the kill switch on your behalf.
