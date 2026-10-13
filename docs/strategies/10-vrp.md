# Strategy 10: the volatility risk premium: buy-write and put-write (the plan's strategy 9)

- Family: option selling, volatility risk premium (monthly)
- Sources: Whaley, "Return and Risk of CBOE Buy Write Monthly Index" (Journal of Derivatives,
  2002); Carr and Wu, "Variance Risk Premiums" (Review of Financial Studies, 2009); the Cboe BXM
  and PUT index methodologies. Citations from general knowledge; check the details against the
  sources.
- Code: [`library/vrp.py`](../../strategies/src/tp_strategies/library/vrp.py)
  (`tp-backtest research vrp`; research only, see below)
- Stage: 1 (research backtest)

## Economic rationale

Index options are priced at an implied volatility that has averaged a few points above the
volatility that followed: the VIX has averaged about 19 while the S&P 500's realized volatility
has averaged about 15. Buyers pay that premium for insurance against crashes; sellers collect it
in exchange for taking the crash risk. A covered call (own the index, sell a one-month call
every month) and a cash-secured put (hold T-bills, sell a one-month put) are the simplest ways to
earn it, and the Cboe has published both as indexes (BXM, PUT) since the 2000s.

## Rules

| | |
|---|---|
| Underlying | SPY (total return from the adjusted closes) |
| Roll | Each month on the session of (or before) the third Friday, the monthly expiration: the expiring option settles at its intrinsic value on the close, a new one-month option is sold at the close |
| Buy-write | Long SPY, short a call at the money (or `moneyness` out of the money) |
| Put-write | T-bills (the 3-month yield from FRED) as collateral for the strike, short a put at the money (or `moneyness` below) |
| Option prices | Black-Scholes at the VIX scaled to an at-the-money volatility (`iv_scale`, 0.9 by default, since the VIX includes the skew), with a linear skew in log-moneyness for strikes away from the money; marked daily at that day's VIX |
| Costs | 3% of the premium per roll (the bid/ask spread paid on the sale) |
| Execution | At the close on roll days |

This runs as a vectorized research backtest (`tp-backtest research vrp`), not on the event
engine: the engine and the paper bot trade shares only. The option prices are a model, not
history: see [options-backtesting.md](../options-backtesting.md) for what that hides.

## What the sources report (to check against)

| Index | Period | Annual return | Volatility | vs S&P 500 total return |
|---|---|---|---|---|
| BXM | 1988-2001 (Whaley) | _fill in from the paper_ | | |
| PUT | 1986-2018 (Cboe) | _fill in from Cboe_ | | |

## Known failure regimes

- **Crashes**: October 1987, autumn 2008, March 2020. The premium is the payment for these; a
  put-write loses about what the index does, with only the premium as a cushion.
- **V-shaped rallies**: the buy-write gives up the upside above the strike exactly when the index
  rebounds hardest (April 2009, April 2020).
- **Low-VIX years**: 2017's premiums were thin; small gains, same tail risk.
- **Model prices**: Black-Scholes at a scaled VIX with a guessed skew; the real BXM sells at
  the bid of actual quotes. Compare the backtest with the published BXM and PUT before trusting
  its level.
- **Sharpe flatters it**: returns are negatively skewed (small steady gains, rare large losses);
  read the maximum drawdown and the stress periods first.

## Promotion notes

Paper trading it needs option orders in the paper bot (Alpaca's paper account supports them),
which it doesn't place today. Until then this is research: the backtest against BXM and PUT,
then the stored option chains to check the model's premiums against real quotes.
