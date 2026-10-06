# ADR 0003: one strategy contract for backtest and paper, our own event-driven engine

- Status: accepted
- Date: 2026-10-06

## Context

The plan requires a production-faithful backtester whose strategy code runs unchanged in paper
and live trading, risk checks that strategies cannot bypass, fills never on the signal bar, and
results traceable to code, data and configuration. Strategies trade daily bars for now; intraday
and options come later. Writing the engine ourselves is part of the point of the project.

## Decision

- **Contract** (`tp_trading.strategy`): a strategy is a frozen dataclass whose fields are its
  parameters, with `symbols()`, `on_bar(ctx)` and optionally `on_fill(ctx, fill)`. It sees only a
  `Context` (point-in-time history, prices, its own positions, equity, cash) and returns
  `OrderIntent`s via `ctx.order` / `ctx.order_target_weights`. Sizing lives in the shared
  `Context` base class, so backtest and paper size identically.
- **Intents, not orders.** An intent becomes an order only after the `RiskGate` approves it (and,
  in paper trading, a human). The gate is handed to the engine, never to the strategy.
- **Engine** (`tp_trading.engine`): a session loop rather than a general event queue. With daily
  bars the order of events is fixed (fills at the open, mark at the close, decide, review), so a
  loop states it plainly; `on_quote` and `on_timer` will arrive with intraday data, and the
  message bus in Phase 3 will carry the same message types (`OrderIntent`, `Fill`).
- **Simulated execution**: next open by default, slippage and regulatory fees from a `CostModel`,
  whole shares, cash-account constraints. A research mode (next close, size at fill, fractional,
  no costs) exists so the event engine can be checked against vectorised results exactly.
- **Run records**: every saved backtest writes its summary (parameters, engine settings, data
  version, git commit) and its equity, orders and fills. MLflow can ingest these later; JSON and
  Parquet need no server today.

## Alternatives considered

- **NautilusTrader / LEAN**: mature and fast, but the learning is in building it, and both bring
  large dependencies and their own data models. Revisit for intraday or tick work.
- **vectorbt only**: perfect for sweeps, but a vectorised backtest cannot be the code that trades.
- **A generic event queue now**: more moving parts with no benefit while every event is daily.

## Consequences

- Strategies must be stateless between bars (paper runs a fresh process daily).
- Strategy objects are hashable and comparable by parameters, which makes them easy to log and
  key by.
- A second engine to keep honest: every library strategy that also has a vectorised version gets
  a parity test.
