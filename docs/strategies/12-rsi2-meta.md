# Strategy 12: RSI(2) with a meta-labeling filter

- Family: mean reversion (short-term, daily) with a machine-learning filter
- Source: Marcos López de Prado, *Advances in Financial Machine Learning* (2018), chapter 3
  (meta-labeling, the triple barrier), chapter 4 (sample uniqueness), chapter 7 (purged
  cross-validation) and chapter 10 (bet sizing). The primary strategy is
  [strategy 5](05-rsi2.md). Citations from general knowledge; check the details against the book.
- Code: [`library/rsi2_meta.py`](../../strategies/src/tp_strategies/library/rsi2_meta.py)
  (`tp-backtest run rsi2-meta`); the model and its evaluation in
  [`tp_ml.meta`](../../libs/ml/src/tp_ml/meta.py) (`tp-ml meta`); see [ml.md](../ml.md)
- Stage: 1 (backtest)

## Economic rationale

RSI(2) is right about *what* to do (buy a sharp pullback in an uptrend, sell the bounce) but not
every pullback is the same trade. A dip on a quiet day in a calm market is a different bet from
a dip in a name already falling hard while volatility is rising, and a dip on a day when half the
universe is down is a different bet again. Meta-labeling keeps the primary strategy's economic
logic and its side, and asks a narrower question a model can answer: **given this signal and
what was known when it fired, how likely is this trade to make money?**

The point is precision, not recall: the filter can only remove trades. If it removes the worst
ones, the average trade, the hit rate and the drawdowns improve; the cost is fewer trades and a
model that has to keep earning its place.

Who is on the other side: the same short-term sellers as strategy 5. The filter only decides
which of them to provide liquidity to.

## Rules

| | |
|---|---|
| Primary signals | Strategy 5 unchanged: RSI(2) under 10, close above the 200-day average |
| Exits | Strategy 5 unchanged: a close above the 5-day average, or a 10-session time stop |
| Label | Each past signal is labelled by the trade those rules made (entry at the next open, exit at the open after the exit signal): 1 if it made money (`tp_ml.labels.rule_labels`) |
| Features | Point in time on the signal day: returns over 1 to 126 days, against SPY and the sector, realized volatility and its trend, ATR, today's range, volume z-score, dollar volume, distance from the 20/50/200-day averages, RSI(2), RSI(14) ([ml.md](../ml.md#features)) |
| Model | Gradient-boosted trees on quantile-binned features (`model=gbm`, the default) or a penalised logistic regression (`model=logistic`); samples weighted by label uniqueness |
| Training | Every 3 months (`retrain`), on all labelled signals whose trades ended before that month; never on anything after |
| Filter | Buy a signal only if the model gives it at least 55% (`threshold`), the most likely first |
| Sizing | Up to 10 positions at 10% each, at most 3 per sector, as strategy 5; `sized=true` scales each by the model's confidence (`2N(z) - 1`, López de Prado ch. 10) |
| Warm-up | Until 200 labelled signals exist there is no model and the strategy trades as plain RSI(2); its orders say so |
| Execution | Next session's open, 5 bps slippage |

## How it's evaluated (beyond the backtest gate)

`tp-ml meta` scores every signal out of sample two ways, and logs the run to MLflow with the
feature set version, the data version and the git commit:

- **Purged, embargoed 5-fold**: each block of signals predicted by a model trained on the
  others, with every training signal whose trade overlaps the test block removed and a 1%
  embargo after it.
- **Walk-forward**: the first 40% only trains; each later block is predicted by a model trained
  on the signals whose trades ended before it started. This is what the engine strategy does.

It reports the AUC (0.5 is a coin flip), the hit rate and average trade of the signals the model
keeps against all of them, and the confidence-weighted average trade. On random-walk data the
out-of-fold AUC sits at 0.5, which is the leak check: a model that scores better than a coin flip
on noise is seeing the future (`libs/ml/tests/test_meta.py`).

| Sample | Signals | AUC (walk-forward) | Kept | Hit rate kept vs all | Avg trade kept vs all |
|---|---|---|---|---|---|
| Your lake, `tp-ml meta` | _fill in_ | | | | |

## Known failure regimes

- **No edge to find**: if the features say nothing about which dips bounce, the model's
  probabilities hover around the base rate, the filter keeps nearly everything (or nothing at a
  high threshold), and the strategy is RSI(2) with more moving parts. Check the AUC first.
- **Regime shifts**: the model learns from the past few years of signals; a market unlike them
  (a crash, a squeeze) is exactly where its probabilities are least trustworthy. Retraining every
  3 months adapts slowly by design.
- **Overfitting by iteration**: every change to the features, labels or model after looking at
  results is another trial. `tp-ml runs` lists them; count them honestly when reading the
  Deflated Sharpe in validation.
- **Thin samples**: a few hundred signals a year across the universe is little data for a model;
  the label is noisy (a trade's sign) and the classes overlap heavily.
- **Everything strategy 5 fails at** (trend breaks, gaps, the daily loss limit), since the
  filter can only remove trades, not add protection.

## Promotion notes

Not in the paper book by default. Promote only if the walk-forward AUC is meaningfully above 0.5
on your data, `tp-backtest validate rsi2-meta` passes, and its validation beats plain `rsi2`'s
over the same periods; otherwise the filter isn't paying for its complexity. Then add a
`[strategies.rsi2-meta]` sleeve to `config/paper.toml` alongside (or instead of) `rsi2`, so the
paper gate compares them on the same days.
