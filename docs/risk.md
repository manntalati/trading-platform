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
| `gross_exposure` | sum of positions ≤ 100% of equity (no borrowing; leveraged funds count at market value) | buys |
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
run `tp-risk enable <name>`, which also restarts the peak from its next equity. The one override
so far is the high-risk sleeve, strategy 6, at 50%: it holds 3x funds, and its spec explains why
it is the deliberate exception to the "no leverage" rule, for paper only.

Changing a sleeve's capital in `config/paper.toml` moves its peak in proportion, so taking money
out of a sleeve can't trip its limit.

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

`tp-backtest run --sizing METHOD` changes how much of each position the strategy's signal turns
into (below).

## Position sizing

A strategy says what to hold and the most it wants in each name (its target weights). A sizing
policy (`tp_trading.sizing`) then decides how much, from trailing prices only. It applies at
`order_target_weights`, the one place every order is sized, so a backtest and the paper bot size
the same way. The plan's three methods:

| Method | Weight per position | What it does |
|---|---|---|
| `strategy` (default) | the strategy's own | nothing |
| `fixed-fractional` | `risk / stop distance`: 1% of equity lost if a stop 2 ATR (14 days) below the price is hit | volatile names get less, quiet ones more |
| `vol-target` | inverse to each name's 60-day volatility, then the whole book scaled down if its estimated volatility (with correlations) is above 12% a year | each position carries about the same risk; the book's risk is capped |
| `kelly` | a quarter of the Kelly bet `mu / sigma^2` from the last year's returns; nothing if the trailing edge is negative | bets more on what has been working |

Every method only reallocates: no name gets more than the strategy's largest single weight, the
total never exceeds the strategy's total, and there's never leverage. The risk limits still
apply afterwards.

```bash
uv run tp-backtest sizing ts-momentum            # all four side by side
uv run tp-backtest run ts-momentum --sizing vol-target
```

`tp-backtest sizing` runs the same signals, costs and risk limits under each method and prints
CAGR, volatility, Sharpe, maximum drawdown, exposure and turnover. Kelly with a year of trailing
returns is noisy: that it is a quarter Kelly at most is the point. To paper-trade a sleeve with
a method, add `sizing = "vol-target"` (or another) to its block in `config/paper.toml`.

## Portfolio risk

`tp_core.portfolio_risk`, shown on **Portfolio → Risk** and served by `/api/positions/risk`,
looks at the whole Fidelity book at once:

- **Exposure per underlying** in dollars: shares at market value plus options by delta, so a
  call and the stock on one name add up.
- **Beta to the S&P 500** (one year of daily returns): how many dollars the book moves for a 1%
  move in SPY; and the book's annualized **volatility**.
- **Value at Risk**: the one-day loss exceeded on 5% (and 1%) of days, from replaying the last
  year's daily returns on today's exposures (historical) and from the book's volatility
  (parametric, normal); and the **expected shortfall**, the average loss on the days beyond the
  95% VaR. The worst five replayed days are listed.
- **Correlation** between the ten largest exposures, their average pairwise correlation and the
  most correlated pairs: diversification that isn't there.
- **Option Greeks** across the book in dollars: delta, gamma per 1% move, theta per day, vega per
  volatility point.
- **Scenario grid**: the book's P&L if the S&P 500 moved -10% to +10% (each name by its beta)
  with implied volatility 5 points lower, unchanged or higher. Stocks move linearly; options are
  fully repriced (Black-Scholes), so the grid shows gamma and vega that delta alone misses.

VaR uses option deltas (first order); the grid is exact at today's implied volatility. Figures
built from history cover only what has daily bars in the lake: funds without bars and cash are
left out, and the page says how much of the exposure is covered. VaR measures ordinary days: it
says nothing about a gap through a stop or an earnings move, which is what the grid is for.
