# Dashboard (web)

React + TypeScript + Vite front end for `tp-api`. What each page shows and how live updates work:
[docs/dashboard.md](../../docs/dashboard.md).

Needs Node 22+.

## Develop

```bash
uv run tp-api --quotes fake        # terminal 1: API on 127.0.0.1:8000
cd apps/dashboard
npm ci
npm run dev                        # terminal 2: http://localhost:5173, proxies /api and /ws
```

Point the dev server at another API with `TP_API_URL=http://127.0.0.1:8100 npm run dev`.

For a full demo with synthetic data and no keys:

```bash
uv run tp-data bars backfill --source fake
uv run tp-data options snapshot --source fake
uv run tp-broker sync --source fake
```

## Build

```bash
npm run build       # type-check, then bundle into dist/
```

`tp-api` serves `dist/` at `/`, so after a build the whole thing runs from one process:
`uv run tp-api`, then open http://127.0.0.1:8000.

## Check

```bash
npm run typecheck
npm test            # vitest + Testing Library (jsdom)
```

CI runs both plus the build.

## Actions

The Paper trading section is the only place the dashboard changes anything: approving or rejecting
paper-trading proposals and the kill switch. Those requests go through `postJson` in `api.ts`,
which adds the `X-TP-Client: dashboard` header the API requires for writes.

## Token

If `TP_DASHBOARD_TOKEN` is set on the API, open the dashboard once as
`http://127.0.0.1:8000/?token=<token>`. The page keeps it for the browser tab (sessionStorage),
removes it from the address bar, and sends it on every API call and on the WebSocket.

## Layout

```
src/
  App.tsx           the workspace: top bar with the section menu, the page, the right column
  api.ts            fetch + useApi hook (token, refresh; one request shared by every panel)
  live.ts           /ws/live client: reducer + reconnecting hook
  format.ts         money, percent, time formatting
  theme.css         design tokens (dark and light), layout, components
  components/       Card, Stat, Delta, BarList, StatsTable; rail.tsx (the right column's
                    panels); charts (Recharts)
  pages/            one file per view: Home, Portfolio, Positions, Research, Trades,
                    TradeStats, Paper, System, ...
```

Colours come from CSS variables in `theme.css`; charts read them at runtime so they follow the
light/dark switch. Status colours are always paired with an icon or a label.
