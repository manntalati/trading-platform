# Strategy 7: pairs trading on cointegration (the plan's strategy 6)

- Family: statistical arbitrage, relative value (daily)
- Sources: Engle and Granger, "Co-integration and Error Correction" (Econometrica, 1987) for
  the test; Gatev, Goetzmann and Rouwenhorst, "Pairs Trading: Performance of a Relative-Value
  Arbitrage Rule" (Review of Financial Studies, 2006) for the trading rule. Citations from
  general knowledge; check the details against the papers.
- Code: [`library/pairs.py`](../../strategies/src/tp_strategies/library/pairs.py)
  (`tp-backtest run pairs`; the long/short version: `tp-backtest research pairs`)
- Stage: 1 (backtest)

## Economic rationale

Two companies exposed to the same business (two oil majors, two card networks, two banks) are
priced off the same news, so their prices drift apart only temporarily: a large order, a
headline about one of them, a rebalancing flow. When the gap is a temporary dislocation rather
than news about one company's fortunes, it closes, and the trade is paid for providing liquidity
to whoever pushed the price apart. Cointegration is the test that the gap has been mean
reverting rather than wandering, which is the property the trade needs.

## Rules

| | |
|---|---|
| Universe | The 38 large caps; pairs only within the same GICS sector |
| Formation | At each month end, on the last 252 sessions of log prices: regress A on B (hedge ratio and intercept), then an augmented Dickey-Fuller test (one lag) on the residual spread. Keep pairs with a t-statistic below the Engle-Granger 5% critical value (−3.34) and a half-life of mean reversion between 5 and 60 sessions; at most 10 pairs, most significant first, each stock in one pair |
| Signal | Each session, the spread's z-score: today's residual against the formation mean and standard deviation |
| Entry | z below −2: A is cheap against B. z above +2: B is cheap against A |
| Exit | z back inside ±0.5, or beyond ±4 (the relationship broke), or after 3 half-lives (at most 60 sessions), or the pair drops out at the next formation |
| Engine version (long-only) | Buys the cheap leg only, 10% of the sleeve per open pair. The risk service blocks short sales, so the engine can't hold the hedge: this version keeps the market exposure of the leg it buys |
| Research version (long/short) | Long the cheap leg and short the hedge-ratio-weighted rich leg, equal gross dollars per pair; vectorized, outside the engine, 5 bps per leg per trade |
| Execution | Next session's open, 5 bps slippage |

## What the sources report (to check against)

Gatev, Goetzmann and Rouwenhorst find that self-financing portfolios of the closest pairs
(minimum distance between normalized prices, 1962 to 2002) earned excess returns of up to about
11% a year, falling over time as the trade became known. Their pairs come from the minimum
distance rule; cointegration is a common variant, not their method.

| Sample | Annual excess return | Sharpe | Trades per pair per year |
|---|---|---|---|
| Top 20 pairs, 1962-2002 | _fill in from the paper_ | | |

## Known failure regimes

- **Structural breaks**: a merger, a spinoff, a guidance cut or a regulatory change gives one
  company a new value; the spread doesn't come back. The ±4 stop and the monthly re-formation
  limit how long a broken pair is held.
- **Crowded unwinds**: August 2007, when statistical-arbitrage funds deleveraged together and
  spreads widened for days before reverting.
- **The long-only version is mostly market beta**: in a selloff the cheap leg falls with the
  market; without the short hedge, a drawdown in the index shows up in full.
- **A small universe**: 38 stocks give few same-sector candidates, so few pairs pass the test
  and the sleeve often sits in cash. Survivorship (today's large caps) flatters the results.
- **Multiple testing**: testing ~120 pairs a month at 5% finds ~6 "cointegrated" pairs by chance;
  the out-of-sample half of the validation is what tells whether they revert.

## Promotion notes

The long/short research version is the real test of the idea. The engine version can paper
trade, but its result mixes the pair signal with market exposure; compare the two before reading
anything into it.

## A note on the test's power

A year of daily data is short for a cointegration test. A spread that reverts with a half-life
of two weeks (an AR(1) coefficient of 0.95) has an expected ADF statistic around −2.5, which
fails the 5% bar most of the time; one with a half-life of about a week (0.9) passes about as
often as not. So the formation finds the fast-reverting pairs and misses slower real ones: a
longer window or a looser bar finds more, and more false ones.
