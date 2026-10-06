# Strategy 1: 10-month moving-average timing

- Family: trend following (time-series momentum, monthly)
- Source: Mebane Faber, *A Quantitative Approach to Tactical Asset Allocation* (2007; updated
  2013). Citation from general knowledge; check the details against the paper.
- Code: [`library/ma_timing.py`](../../strategies/src/tp_strategies/library/ma_timing.py)
  (event-driven, `tp-backtest run ma-timing`, paper trading) and the vectorised research
  version [`ma_timing.py`](../../strategies/src/tp_strategies/ma_timing.py); a parity test
  keeps them identical
- Notebook: [`research/notebooks/s01_faber_10m_ma.py`](../../research/notebooks/s01_faber_10m_ma.py)
- Stage: 1 (backtest)

## Economic rationale

Trends persist at multi-month horizons: investors under-react to news and then herd,
institutional capital moves slowly, and risk-averse sellers capitulate late in bear markets.
Being out of an asset whenever it trades below its long-run average gives up some upside in
whipsaws, but sidesteps most of the prolonged, deep declines where buy-and-hold loses the most.

Who is on the other side: holders who stay invested through long drawdowns (and accept the deeper
losses) and late sellers who are forced out near the bottom. The strategy is mostly a **risk
reducer**; it is not expected to beat buy-and-hold on raw return in a long bull market.

## Rules

| | |
|---|---|
| Universe | Single asset (SPY) or Faber's 5-asset GTAA: US stocks (SPY), foreign stocks (EFA), US 10-year Treasuries (IEF), REITs (VNQ), commodities (DBC) |
| Signal | On the last trading day of the month: invested if the month-end close is above the average of the last 10 month-end closes (inclusive), else in cash |
| Sizing | Equal `1/N` slice per asset; an asset that is out leaves its slice in cash |
| Rebalance | Monthly, back to target weights; weights drift with prices in between |
| Prices | Split- and dividend-adjusted closes (total return) |
| Execution | Paper: at the signal month-end close. Ours by default: the next session's close (`lag_days=1`), never the signal bar |
| Costs | 5 bps of traded notional by default (liquid ETFs); vary it in the notebook |
| Cash | 0% by default; T-bills (FRED DTB3) in the long-history run |

## What the paper reports (to check against)

Faber's headline claim: over a century of US stock data and in the 5-asset version, timing
produced **returns similar to buy-and-hold, with markedly lower volatility and much smaller
maximum drawdowns**, and was in the market most of the time.

| Paper sample | CAGR | Volatility | Sharpe | Max drawdown |
|---|---|---|---|---|
| S&P 500 buy and hold | _fill in from the paper_ | | | |
| S&P 500 timing | _fill in from the paper_ | | | |
| GTAA equal weight, buy and hold | _fill in from the paper_ | | | |
| GTAA timing | _fill in from the paper_ | | | |

The notebook checks the qualitative claims on our data (lower vol, shallower drawdown, return
close to buy-and-hold) for both execution timings.

## Known failure regimes

- **Sideways, choppy markets**: repeated whipsaws (e.g. 2011, 2015–16, late 2018) each cost a
  small loss plus trading costs.
- **Sharp V-shaped crashes**: a crash faster than a month (Oct 1987, Feb–Mar 2020) is mostly
  taken before the month-end signal fires, and the rebound is partly missed before it turns back
  on. Monthly sampling is the price of few trades.
- **Long, steady bull markets**: occasional false exits underperform buy-and-hold.
- **Rising-rate regimes** hurt the bond sleeve and the cash alternative alike (2022).

## Promotion notes

Candidate for the first paper-trading bot because it trades rarely (a few times a year per
asset), uses only liquid ETFs, and its rules are fully specified. Gate to paper: the backtest
must survive `lag_days=1`, 2x costs, and the locked 2024+ test window.
