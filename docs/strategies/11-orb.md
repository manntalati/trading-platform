# Strategy 11: opening-range breakout (the plan's strategy 10)

- Family: intraday momentum (5-minute bars)
- Sources: Toby Crabel, *Day Trading with Short Term Price Patterns and Opening Range Breakout*
  (1990); Zarattini, Barbon and Aziz, "Can Day Trading Really Be Profitable? Evidence of
  Sustainable Long-term Profits from Opening Range Breakout (ORB) Day Trading Strategy vs.
  Benchmark in the US Stock Market" (SSRN, 2023). Citations from general knowledge; check the
  details against the sources.
- Code: [`library/orb.py`](../../strategies/src/tp_strategies/library/orb.py)
  (`tp-backtest research orb`; research only)
- Stage: 1 (research backtest)

## Economic rationale

The first minutes of the session absorb the overnight news and the opening auction's imbalance.
When the price then breaks out of that first range, it often keeps going for the rest of the day:
the break attracts momentum traders and forces the side caught wrong to cover, and large orders
worked through the day keep pushing in the same direction. The edge, where it exists, is
strongest on days with unusual volume (the "stocks in play").

## Rules

| | |
|---|---|
| Data | Alpaca 5-minute bars, regular hours only (`tp-data bars intraday`) |
| Universe | SPY and QQQ by default (the liquid index funds the recent study uses) |
| Opening range | The high and low of the first 30 minutes (six 5-minute bars) |
| Entry | Buy at the range high the first time a later bar trades above it (a stop order); with `direction=both`, also sell short at the range low (research only) |
| Stop | The other side of the range (or its midpoint, `stop=mid`) |
| Exit | The stop, or the last bar's close (no position held overnight) |
| Same-bar ambiguity | If the bar that triggers the entry also reaches the stop, it counts as stopped out (the worst case) |
| Sizing | Equal capital per symbol: the daily return is the average over the symbols, a symbol without a trade that day contributing nothing |
| Costs | 2 bps per side plus a 1 bp slippage on the stop entry |

A vectorized research backtest on stored intraday bars: the event engine runs on daily bars, and
the paper bot trades at the open, so intraday entries aren't paper-tradable yet.

## What the sources report (to check against)

| Sample | Rule | Annual return | Sharpe | Hit rate |
|---|---|---|---|---|
| QQQ, 2016-2023 (Zarattini et al.) | 5-minute ORB, stop at the range's other side | _fill in from the paper_ | | |

## Known failure regimes

- **Choppy, range-bound days**: false breakouts both ways; the stop is hit at the cost of the
  whole range.
- **Gap-and-fade days**: a large opening gap that reverses through the morning.
- **Costs**: a trade a day per symbol; a few basis points of slippage on stop entries are a large
  share of the average trade. The stop entry assumes a fill at the range high, which fast
  markets don't give.
- **Short history**: Alpaca's intraday bars go back to 2016; a few years of 5-minute data is a
  modest sample for a day-trading rule.

## Promotion notes

Needs intraday execution (orders placed during the session) before it can paper trade; the
research backtest decides whether that is worth building.
