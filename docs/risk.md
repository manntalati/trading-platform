# Risk

Every order from every strategy passes `tp_risk.RiskManager` before it can trade, in backtests
and in paper trading alike. Strategies never get a reference to it, so they cannot skip it or
change a limit. Limits live in [`config/risk.toml`](../config/risk.toml) (the plan's starting
values); change them in a reviewed commit, never to push one trade through.

## Pre-trade checks

Equity means the book the check is measured against: the strategy's capital in a backtest, the
whole paper account in paper trading.

| Check | Limit | Applies to |
|---|---|---|
| `kill_switch` | engaged = nothing trades | every order |
| `instrument` | options rejected until the options module adds defined-risk structures and the 5% premium cap | option symbols |
| `no_short` | can't sell more than the book holds | sells |
| `strategy_enabled` | disabled strategies can't open risk | buys |
| `daily_loss` | equity down 2% since the prior close: no new risk that day | buys |
| `fresh_data` | the symbol's newest bar must be the signal session | buys |
| `gross_exposure` | sum of positions ≤ 100% of equity (no leverage) | buys |
| `max_position` | one single name ≤ 10% of equity | buys of stocks/ADRs (funds exempt) |
| `max_sector` | single names in one GICS sector ≤ 30% | buys of stocks/ADRs |
| `risk_per_trade` | (price − stop) × shares ≤ 1% of equity | buys that carry a stop |
| `price_sanity` | limit price within 5% of the last close | limit orders |
| `liquidity` | shares ≤ 2% of 20-session average volume | every order |

Selling shares the book holds is risk-reducing: it skips the checks that exist to stop new risk,
but not the kill switch, the no-short rule, price sanity or liquidity. A disabled strategy can
therefore still exit; it can't buy.

Checks run on the batch in order (a rebalance lists sells first), and each approval counts
against the next order, so ten buys that are fine alone can't jointly breach a cap. Every check
is recorded on the order, passed or failed, so the record shows why something traded as well as
why something didn't.

Funds versus single names, and sectors, come from `config/classifications.toml` (plus your
git-ignored `classifications.local.toml`). A symbol classified nowhere is treated as a single
name in a sector of its own.

## Drawdown limit and switches

After every close the manager tracks each strategy's peak equity. A drawdown past the limit
(10% by default; per-strategy overrides in `[strategy_drawdown.overrides]`, each with its
reason) is recorded and, in paper trading, **disables the strategy** until you look at it and
run `tp-risk enable <name>`, which also restarts the peak from its next equity.

Backtests record breaches but keep trading, so you can see how often a strategy would have been
stopped. `tp-backtest run ... --enforce-drawdown` shows the effect of stopping instead.

```bash
uv run tp-risk status                          # kill switch, strategies, peaks, limits in force
uv run tp-risk kill --reason "broker outage"   # block every new order
uv run tp-risk resume
uv run tp-risk disable ma-timing --reason "reviewing fills"
uv run tp-risk enable ma-timing
```

State lives in `<data root>/state/risk.json`. `tp-risk kill` only blocks new orders; paper
trading's `tp-paper kill` also cancels the ones already at the broker.

## In backtests

`tp-backtest run` applies the limits by default (`--no-risk` to compare against an unconstrained
run). Rejections appear in the output with their reasons and in `orders.parquet`; the run's
`summary.json` records the limits in force and any drawdown breaches.
