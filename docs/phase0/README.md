# Phase 0: learning notes

Each module ends with a **one-page note written by hand** (copy [`_template.md`](_template.md)
to `NN-<slug>.md`). Code exercises live next to the rest of the code so they get linted, typed
and tested like everything else.

| # | Module | Exercise | Code | Note | Status |
|---|---|---|---|---|---|
| 1 | Market structure | Diagram the life of a buy order from broker app to fill | — (diagram in the note) | `01-market-structure.md` | todo |
| 2 | Order books and microstructure | Toy matching engine: price-time priority, partial fills | [`matching_engine.py`](../../research/src/tp_research/phase0/matching_engine.py) | `02-order-books.md` | code done, note todo |
| 3 | Returns and statistics | Metrics for SPY, QQQ and 5 stocks from raw daily bars | `libs/core/src/tp_core/metrics.py`, `research/notebooks/phase0_03_return_stats.py` (`step0/return-metrics`) | `03-returns-and-statistics.md` | todo |
| 4 | Classic strategy families | One-paragraph thesis per family: why it pays, who loses | — | `04-strategy-families.md` | todo |
| 5 | Options basics | Payoff diagrams for 8 structures | — | `05-options-basics.md` | todo |
| 6 | Options pricing and Greeks | Black-Scholes + IV solver, verified against a broker chain | — | `06-options-pricing.md` | todo |
| 7 | Risk and sizing | Size a 5-position portfolio under a 1% per-trade rule | — | `07-risk-and-sizing.md` | todo |
| 8 | How trading firms operate | What infra a market maker needs that a retail bot doesn't | — | `08-how-firms-operate.md` | todo |

## Vocabulary checkpoint

By the end of Phase 0 you should be able to explain, without notes:

- [ ] PnL attribution
- [ ] Mark-to-market
- [ ] Realized vs unrealized PnL
- [ ] Fill rate
- [ ] Why a strategy with a 2.0 backtest Sharpe often trades at 0.5 live
