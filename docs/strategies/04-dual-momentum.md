# Strategy 4: Dual momentum (global equities momentum)

- Family: momentum (absolute + relative, monthly)
- Source: Gary Antonacci, *Dual Momentum Investing* (2014) and *Risk Premia Harvesting Through
  Dual Momentum* (2012 paper). Citations from general knowledge; check the details against the
  sources.
- Code: [`library/dual_momentum.py`](../../strategies/src/tp_strategies/library/dual_momentum.py)
  (`tp-backtest run dual-momentum`)
- Stage: 1 (backtest)

## Economic rationale

Two momentum effects stacked: **relative** momentum (hold the stronger of US and international
stocks) captures persistent leadership between regions, and **absolute** momentum (only hold
stocks while they beat T-bills, else bonds) is a trend filter that sidesteps most of a prolonged
bear market. The combination aims for equity-like returns with much smaller drawdowns.

Who is on the other side: home-biased investors who stay in one region, and buy-and-hold
investors who ride bear markets down.

## Rules

| | |
|---|---|
| Universe | US stocks (SPY), international developed stocks (EFA), US aggregate bonds (AGG), T-bills (BIL) as the hurdle |
| Absolute momentum | Last session of the month: if SPY's trailing 12-month return beats BIL's, stay in equities; otherwise hold AGG |
| Relative momentum | In equities, hold whichever of SPY and EFA had the higher 12-month return |
| Sizing | 100% of the sleeve in the one chosen ETF |
| Rebalance | Monthly; drift under 5% is ignored |
| Execution | Next session's open, 5 bps slippage |

Proxies: Antonacci used the S&P 500, MSCI ACWI ex-US (developed and emerging) and the Barclays
US Aggregate index. EFA leaves out emerging markets; swap in a broader fund with
`-p international=...` once it is in the universe.

## What the source reports (to check against)

Headline claim: over several decades, global equities momentum earned higher returns than
either the US or the world index, with a fraction of their maximum drawdown, because the
absolute-momentum filter moved to bonds before most of the large equity declines.

| Sample | CAGR | Volatility | Max drawdown |
|---|---|---|---|
| GEM | _fill in from the source_ | | |
| S&P 500 | _fill in from the source_ | | |

## Known failure regimes

- **Whipsaws around the 12-month line** (2011, 2015–16, late 2018): a switch to bonds just
  before a rebound, then back.
- **Fast crashes** (Feb–Mar 2020): the 12-month return is still positive when the drop hits;
  the filter reacts after most of the damage.
- **Stocks and bonds falling together** (2022): the bond fallback offers no shelter.
- **All-in-one-ETF risk**: one wrong switch moves the whole sleeve.

## Promotion notes

Trades only a few times a year (a switch is one sell and one buy), so it will not reach the
paper gate's 30 trades within the 60 days on its own; it rides along with the higher-turnover
strategies and is judged over a longer window.
