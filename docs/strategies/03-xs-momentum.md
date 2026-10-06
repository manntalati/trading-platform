# Strategy 3: Cross-sectional momentum (12-1)

- Family: momentum (cross-sectional, monthly)
- Source: Narasimhan Jegadeesh and Sheridan Titman, *Returns to Buying Winners and Selling
  Losers: Implications for Stock Market Efficiency*, Journal of Finance (1993); crash risk in
  Kent Daniel and Tobias Moskowitz, *Momentum Crashes* (2016). Citations from general knowledge;
  check the details against the papers.
- Code: [`library/xs_momentum.py`](../../strategies/src/tp_strategies/library/xs_momentum.py)
  (`tp-backtest run xs-momentum`)
- Stage: 1 (backtest)

## Economic rationale

Stocks that did best over the past year (excluding the last month) tend to keep outperforming
the worst ones for the next several months. Explanations: investors under-react to firm news
(earnings, guidance), analysts revise slowly, and the disposition effect makes holders sell
winners too early and hold losers too long, which slows price discovery. The most recent month
is skipped because one-month returns tend to *reverse* (bid-ask bounce and liquidity effects).

Who is on the other side: value-oriented and contrarian investors buying last year's losers,
and holders selling winners too early.

## Rules

| | |
|---|---|
| Universe | The 38 large-cap stocks in `config/universes.toml` |
| Signal | Last session of the month: return from 252 sessions ago to 21 sessions ago ("12-1") |
| Selection | The top 10 by that score, at most 3 per GICS sector (from `config/classifications.toml`) |
| Sizing | Equal weight, 10% each, the rest cash (so it stays inside the 10% position and 30% sector caps) |
| Rebalance | Full monthly rebalance back to 10% each (trimming drift is what keeps sectors under 30%); a new paper sleeve takes its first positions at once |
| Options | `require_positive=true` adds an absolute filter: only names with a positive score |
| Execution | Next session's open, 5 bps slippage, whole shares |

## What the paper reports (to check against)

Headline claim: portfolios buying past 3–12-month winners and selling past losers earned
significant positive returns over the following 3–12 months, in US stocks from 1965 to 1989,
and the effect is not explained by systematic risk. Later work found it in most equity markets
and periods, and documented occasional severe crashes.

| Paper sample | Monthly winner-minus-loser return | t-stat |
|---|---|---|
| 12-month formation (J = 12), each holding period K | _fill in from the paper_ | |

Differences that matter: the paper is long winners and **short** losers across thousands of
stocks; this is long-only on 38 mega caps, so most of its return is market beta, and the
universe is chosen with hindsight (survivorship bias). Judge it against an equal-weighted
basket of the same 38 names, not just SPY.

## Known failure regimes

- **Momentum crashes**: sharp market rebounds after a bear market (1932, April–May 2009) when
  beaten-down, high-beta losers rally hardest. Long-only avoids the short-leg blow-up, but the
  long leg still lags badly in the rebound.
- **Factor rotations**: abrupt switches between growth and value leadership (late 2000, Nov
  2020, 2022) turn last year's winners into this month's losers.
- **Concentration**: in tech-led markets the top names cluster in one sector; the 3-per-sector
  rule caps the damage but also the upside.

## Promotion notes

Turnover is the highest of the monthly strategies, which gives the paper gate its 30 trades in a
few months. Check slippage carefully: the gate compares live slippage with the 5 bps assumed
here.
