# Dashboard

A web dashboard over everything the platform collects, live while the market is open:

- **Overview**: market clock, live watchlist, portfolio value and today's P&L ticking in real
  time, data-pipeline health.
- **Portfolio**: Fidelity holdings (read-only), allocation by sector and asset class, concentration,
  and performance against S&P 500, Nasdaq 100 and 60/40.
- **Ideas**: the rules-based research ideas ([ideas.md](ideas.md)) with the numbers behind each.
- **Strategies**: strategy 1 (10-month MA timing) on SPY and on Faber's GTAA, against buy and hold.
- **Market / Options**: price history per symbol; IV term structure, expected move and smile from
  the daily chain snapshots.
- **System**: last ingest, validation report, option snapshots, broker syncs, live-feed status.

## Run it

```bash
make web-install web-build           # once, and after UI changes: bundle apps/dashboard (Node 22+)
uv run tp-api --quotes fake          # http://127.0.0.1:8000 with simulated live prices
uv run tp-api --quotes alpaca        # real-time IEX trades from Alpaca (needs ALPACA_* keys)
```

`tp-api` serves the built app at `/`. For UI work, `npm run dev` in `apps/dashboard` gives hot
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
`config/universes.toml`. Today's P&L is measured against the last close in the lake. Holdings
without a live price (mutual funds, options, cash) are held at the broker's last valuation.
Outside market hours the stream is quiet and the dashboard shows last closes.

## API

All JSON, read-only (`GET`):

| Endpoint | What |
|---|---|
| `/api/health` | liveness |
| `/api/status` | market clock, bars freshness, latest validation and options reports, broker syncs, live feed |
| `/api/bars/{symbol}?days=365` | daily bars (raw and adjusted close) |
| `/api/options/{underlying}` | latest chain snapshot: ATM IV and expected move per expiry, smile |
| `/api/portfolio` | accounts (masked), holdings with weights and classification, exposures |
| `/api/portfolio/performance?days=365` | current-holdings backtest and account TWR vs benchmarks |
| `/api/ideas` | research ideas |
| `/api/strategies/ma-timing?universe=spy\|gtaa` | strategy 1 vs buy and hold |
| `/ws/live` | WebSocket: `snapshot`, then `update` messages with quotes and portfolio value |

Interactive docs: `http://127.0.0.1:8000/docs`.

## Security

The dashboard shows your brokerage holdings, so:

- `tp-api` binds to `127.0.0.1` and refuses any other `--host` unless `TP_DASHBOARD_TOKEN` is set.
- With a token set, every `/api/*` call needs `Authorization: Bearer <token>` and the WebSocket
  needs `?token=<token>`. `/api/health` stays open for monitoring.
- Reach it remotely through an SSH tunnel or a private network (Tailscale/WireGuard), not by
  opening a port to the internet.
