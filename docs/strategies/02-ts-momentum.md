# Strategy 2: Time-series momentum

- Family: trend following (time-series momentum, monthly)
- Source: Tobias Moskowitz, Yao Hua Ooi and Lasse Heje Pedersen, *Time Series Momentum*,
  Journal of Financial Economics (2012). Citation from general knowledge; check the details
  against the paper.
- Code: [`library/ts_momentum.py`](../../strategies/src/tp_strategies/library/ts_momentum.py)
  (`tp-backtest run ts-momentum`)
- Stage: 1 (backtest)

## Economic rationale

An asset's own past 12-month excess return predicts its next month's return, across asset
classes. The usual explanation is the same under-reaction then over-reaction story as strategy
1: news is priced in slowly, then trends get extended by investors chasing them, and hedgers and
risk-limited institutions trade at the wrong times. Being long what is trending and out of what
isn't harvests that, and diversifying across many asset classes means one asset's whipsaw is
diluted by the others.

Who is on the other side: slow-moving and contrarian capital, hedgers who pay for liquidity, and
buy-and-hold investors sitting through long declines.

## Rules

| | |
|---|---|
| Universe | 9 liquid ETFs across asset classes: US stocks (SPY), developed and emerging stocks (EFA, EEM), Treasuries (IEF, TLT), high yield (HYG), REITs (VNQ), commodities (DBC), gold (GLD) |
| Signal | Last session of the month: trailing 12-month (252-session) total return of each asset vs T-bills (BIL) over the same window; long if it beats T-bills, else flat |
| Sizing | Inverse-volatility slices across the whole universe (63-session realized volatility), so each asset carries a similar share of risk; a flat asset's slice stays in cash |
| Leverage and shorts | None. The paper is long/short and scales each futures position to a fixed volatility, which needs leverage; this is the unlevered long/flat translation |
| Rebalance | Monthly; drift trims under 1% of the sleeve are skipped; a new paper sleeve takes its first positions at once |
| Execution | Next session's open (market-on-open), 5 bps slippage |

## What the paper reports (to check against)

Headline claims: 12-month time-series momentum is positive and significant in nearly every one
of the 58 futures and forward markets studied (equity indexes, currencies, commodities, bonds);
a diversified portfolio of these signals earned strong risk-adjusted returns with little
exposure to standard factors, and did best in extreme up and down markets (a "smile").

| Paper sample | Annual return | Volatility | Sharpe |
|---|---|---|---|
| Diversified TSMOM, all asset classes | _fill in from the paper_ | | |

Expect less here: 9 ETFs instead of 58 markets, no shorts, no leverage, so lower volatility,
lower return and a lower Sharpe than the paper's diversified portfolio.

## Known failure regimes

- **Trend reversals and choppy markets**: 2009's sharp rebound, 2011, 2015–16, 2018: signals
  flip at the wrong time and the monthly lag pays twice.
- **V-shaped crashes**: as with strategy 1, a crash and recovery inside a month gets the worst of
  both.
- **Correlated sell-offs** (March 2020, 2022): when stocks and bonds fall together, every
  slice turns flat at once and the strategy sits in cash through the rebound.
- **Long-only cost**: without shorts, it can only avoid falling assets, not profit from them;
  much of the paper's crisis-period performance came from the short side.

## Promotion notes

Trades a few times a month across the nine slices, so it reaches the paper gate's 30 trades
faster than strategy 1 while staying in liquid ETFs. Watch the drift-trim threshold: lowering it
raises turnover without changing the idea.
