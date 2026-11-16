# Strategy 17: IBS mean reversion on QQQ

- Family: mean reversion (short-term, daily)
- Source: Alexander Pagonidis, "The IBS Effect: Mean Reversion in Equity ETFs" (NAAIM Wagner
  Award paper, 2014). Citation from general knowledge; check the details against the paper.
  The optional turn-of-month leg: Josef Lakonishok and Seymour Smidt, "Are Seasonal Anomalies
  Real? A Ninety-Year Perspective" (Review of Financial Studies, 1988), also from general
  knowledge.
- Code: [`library/ibs.py`](../../strategies/src/tp_strategies/library/ibs.py)
  (`tp-backtest run ibs`)
- Research: Rex's study (`data/reports/research/proven-strategies/FINDINGS-proven-strategies.md`,
  local only), 2016-01 → 2026-11
- Stage: 1 (backtest). Paper only once the user picks the sleeve size.

## Economic rationale

Internal bar strength, `IBS = (close − low) / (high − low)`, is where the close sits in the
day's range: 0 at the low, 1 at the high. A close near the low means sellers were still pushing
into the last minutes, often for reasons that have nothing to do with value (forced selling,
hedging flows, closing-auction imbalances). On a diversified index fund the price tends to
recover part of that overnight and the next day. The source found the effect across equity
index ETFs, and stronger the lower the IBS.

Who is on the other side: end-of-day sellers paying for immediacy. The strategy provides that
liquidity for a day. QQQ is used because it holds 100 large companies, so one earnings report
can't break the bounce, and Rex's study found the edge thin on SPY and DIA and weak on single
stocks.

## Rules

| | |
|---|---|
| Universe | `universe`: QQQ by default; `QQQ,SPY,DIA` also supported |
| Signal | After each close, IBS = (close − low) / (high − low). No reading when high = low or the bar is missing |
| Entry | IBS below `entry` (0.2, fixed; not tuned): buy at the next open |
| Exit | Sell at the open after that, unless that session's IBS is also below 0.2, in which case hold one more session. Each decision uses only the latest bar, so a position is never held past one session after a non-signal close |
| Turn of month (`tom`, off by default) | Also hold from the open of the month's last session through the close of the third session of the next month (sold at the open of the fourth), using the exchange calendar |
| Sizing | The sleeve split equally among the symbols that signal; the rest in `cash_symbol` (BIL; `none` for plain cash). Drift under 5% (`min_change`) is ignored, so a held position isn't topped up daily |
| Stops | None: the holding period is the stop |
| Execution | Market-on-open at the next session (`tp-paper bot`), whole shares |

```bash
uv run tp-backtest run ibs --slippage-bps 2
uv run tp-backtest run ibs -p tom=true                       # with the turn-of-month leg
uv run tp-backtest run ibs -p universe=QQQ,SPY,DIA -p cash_symbol=none
uv run tp-backtest validate ibs
```

## What the source reports (to check against)

Headline claim: across US and international equity index ETFs, next-day returns after a close
in the bottom of the day's range were well above average and those after a close near the high
below average. The effect was strongest at the lowest IBS readings and survived simple costs.

| Sample | Average next-day return, low IBS | High IBS | Win rate |
|---|---|---|---|
| Equity index ETFs | _fill in from the source_ | | |

Rex's replication (Alpaca SIP bars, open fills, 2 bp/side, BIL on idle cash):

| Period | CAGR | Sharpe | Max drawdown | Time invested |
|---|---|---|---|---|
| 2016–2020 | 12.4% | 1.10 | | 17% |
| 2021-01 → 2026-11-13 | 11.2% | 0.96 | −13.2% (2022-03-24 → 07-13) | |

Average net trade +0.29% over 397 merged holdings, t = 3.5, positive in 10 of 11 calendar
years (2024 was the exception: −3.2%). His per-trade figure leaves out the exit fill's cost
(the daily returns include it), so with it the +0.29% is about +0.27%.

## This platform's backtest (lake data, 2021-10-05 → 2026-11-13)

The lake's daily bars start in October 2021, so the engine covers the second half of Rex's
window only. The engine and Rex's study agree on whether QQQ is held on all 1,284 sessions, and
they show the same 199 holdings. The remaining differences come from costs.

| Run (next-open fills, BIL on idle cash unless noted) | CAGR | Sharpe | Max drawdown | Avg trade | Trades |
|---|---|---|---|---|---|
| Rex's study, same window (his costs: 2 bp/side on QQQ only) | 11.4% | 0.95 | −13.2% | +0.24% | 200 |
| Rex's study, same window, also 2 bp/side on the BIL switches | 9.7% | 0.82 | −13.6% | +0.22% | 199 |
| Engine, 2 bp per fill, whole shares, risk limits on | 9.5% | 0.81 | −13.6% (2022-03-24 → 07-15) | +0.22% | 199 |
| Engine, 2 bp, plain cash (`cash_symbol=none`) | 8.0% | 0.69 | −13.2% | +0.22% | 199 |
| Engine, 5 bp per fill (the validate default) | 4.5% | 0.42 | −15.0% | | |
| Engine, 5 bp, plain cash | 5.5% | 0.50 | −14.2% | | |

Costs matter more here than anywhere else in the library. Every holding is four fills (BIL out,
QQQ in, QQQ out, BIL in) against an average gross gain of about a quarter of a percent. At 5 bp
per fill, rotating through BIL costs more than BIL earns, and plain cash does better. QQQ and
BIL in the opening auction pay close to no spread (both trade at a one-cent spread on a $90–600
price), so 2 bp is the realistic assumption. Check it against the paper fills before trusting
either number.

Neighbouring thresholds (engine, full window): entry 0.1 Sharpe 1.11 at 2 bp / 0.78 at 5 bp;
0.2: 0.81 / 0.42; 0.3: 0.65 / 0.19. The effect gets stronger as the threshold gets lower, as the
source reports. The default stays at the published 0.2.

## Known failure regimes

- **Fast crashes with no bounce** (2022-03 → 07): low closes keep coming, and each one extends
  the hold. This was the worst stretch, −13% to −15%.
- **Strong, quiet uptrends** (2024): few low closes, and the ones that come don't bounce. The
  sleeve sits in BIL, and the trades it does take lose a little.
- **Costs**: above about 3 bp per fill the edge is mostly gone (see the table).
- **Interaction with the daily loss limit**: `config/risk.toml` blocks new buys after the
  *whole paper account* is down 2% on the day. A big down day for QQQ is when this strategy most
  wants to buy and when the other long-only sleeves pull the account down, so some of the best
  entries will be rejected in paper. The backtest measures that limit against the sleeve alone
  (which sits in BIL before an entry), so it doesn't show these misses. Watch the rejected
  orders.
- **The edge is published and well known**, and Rex's study shows it already thin on SPY.

## Promotion notes

`tp-backtest validate ibs` (5 bp per fill, the gate's default): the locked test set, walk-forward,
Deflated Sharpe and stress checks pass. Parameter stability fails: the 0.3 neighbour keeps 0.19
of 0.42 Sharpe, under the required half. At 2 bp the same check gives 0.65 against 0.81, so the
failure comes from the cost assumption, not from a fragile peak.

About 39 holdings a year on QQQ alone, so the paper gate's 30 trades take roughly 9–10 months.
Judge it at 60+ holdings: each one swings about ±1.7%, so a 30-trade sample comes out negative
about one time in six even if the edge is real. Invalidation (Rex): after 60 paper trades, a net
average trade at or below zero, a drawdown worse than −15%, or a realized edge under half the
backtest's (+0.15% per trade).
