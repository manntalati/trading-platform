# Backtesting options strategies

The stock backtester (`tp_trading`) fills shares at the next open. Options need more care: the
price of a contract depends on a volatility surface that moves, quotes are wide, and contracts
expire, get assigned and stop existing. These notes describe how options strategies are tested
here, and what to distrust in the results.

## Two ways to get option prices for the past

| | Replaying stored chains | Pricing with a model |
|---|---|---|
| Source | the daily snapshots (`tp-data options snapshot`, 3:45 PM New York, [data.md](data.md#option-chain-snapshots)) | the underlying's daily bars plus an implied volatility: the VIX for the S&P 500, a stock's own IV history, or realized volatility scaled up |
| History | only since collection started (Alpaca can't backfill it) | as long as the bars (and the VIX, back to 1990) |
| Fills | the actual bid and ask that day | the model price plus an assumed half-spread |
| Smile and term structure | real | assumed: flat unless modelled, which matters most for out-of-the-money puts (the skew) |
| Good for | checking a rule's contract choice and costs on real quotes | long-history studies of systematic premium selling (strategy 9) |

Until the snapshots cover a year or more, strategies are tested with a model: Black-Scholes at
the VIX (or the stock's implied volatility, where its history is long enough), with a skew
adjustment for strikes away from the money and costs charged on the spread. The stored chains
are for checking that the model's prices and the chosen contracts look like the real ones.

## The rules for a fair test

- **No look-ahead.** Decide on a day's 3:45 PM snapshot (or the close) and fill at that
  snapshot's quotes or the next day's, never at prices from later in the life of the trade.
  Implied volatility used to price a trade is the one known that day.
- **Trade at the bid and ask**, not the mid. A round trip on an option often costs 5 to 15% of
  its price; strategies that trade often live or die on this.
- **Expiry and assignment.** At expiry an in-the-money contract is exercised at its intrinsic
  value (from the close). Short calls can be assigned early before an ex-dividend date when the
  remaining time value is below the dividend, and deep in-the-money short puts when their time
  value is near zero (`tp_core.american` prices that right). Model both, or say they weren't.
- **Only listed contracts.** Strikes and expiries come from what was (or would have been) listed:
  monthly expiries before weeklies existed, strikes on the exchange's increments.
- **Capital.** A cash-secured put ties up the strike times 100; a covered call ties up the
  shares; a credit spread ties up its maximum loss. Returns are on that capital, not on the
  premium.
- **Same gate as stocks.** The validation protocol (locked test set, walk-forward, Deflated
  Sharpe, stress periods; [backtesting.md](backtesting.md#validation-the-backtest-gate)) applies.

## What to distrust

- **Sharpe ratios of premium selling.** Selling options earns small, steady gains and gives
  them back in a few days (February 2018, March 2020). Returns are negatively skewed, so a
  Sharpe ratio flatters them: look at the maximum drawdown, the worst month and the stress
  periods first.
- **A flat volatility surface.** Pricing every strike at one implied volatility makes
  out-of-the-money puts look cheap to buy and poor to sell. The skew (`tp_core.smile`) is real
  and usually 3 to 8 volatility points for 25-delta puts on the S&P 500.
- **Sticky assumptions.** When the stock moves, does a strike keep its implied volatility
  (sticky strike) or does the smile move with the price (sticky delta)? Model prices depend on
  it; the stored chains show which one held.
- **Liquidity.** Open interest and the bid/ask spread on the snapshot day say whether a size
  could have traded. Wide markets on small names make backtested fills fiction.

## Benchmarks

Cboe publishes indexes for the two simplest premium-selling strategies on the S&P 500: **BXM**
(buy the index, sell a one-month at-the-money call each month) and **PUT** (sell a one-month
at-the-money put each month, fully cash-secured). Strategy 9 reproduces their rules on SPY,
priced with the VIX, so its results can be checked against them.
