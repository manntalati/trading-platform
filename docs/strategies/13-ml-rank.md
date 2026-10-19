# Strategy 13: cross-sectional ranking model

- Family: cross-sectional equity selection, monthly, with a machine-learning score
- Sources:
  - The method follows Gu, Kelly and Xiu, "Empirical Asset Pricing via Machine Learning",
    *Review of Financial Studies* (2020).
  - Measuring a signal by its information coefficient follows Grinold and Kahn, *Active
    Portfolio Management*.
  - The portfolio and the baseline are [strategy 3](03-xs-momentum.md).
  - These citations are from general knowledge; check the details against the sources.
- Code: [`library/ml_rank.py`](../../strategies/src/tp_strategies/library/ml_rank.py)
  (`tp-backtest run ml-rank`). The model and its evaluation are in
  [`tp_ml.ranking`](../../libs/ml/src/tp_ml/ranking.py) (`tp-ml rank`). See also
  [ml.md](../ml.md#cross-sectional-ranking).
- Stage: 1 (backtest)

## Economic rationale

Strategy 3 ranks stocks by one number, the 12-1 momentum return. The literature has found many
other cross-sectional effects, and they interact:

- short-term reversal (last month's losers bounce)
- low volatility
- trend (distance from moving averages)
- liquidity

Some of these are known to matter more in some stocks than in others. A model trained on the
cross-section can weigh them together.

The point is not to predict the market. Each month the stocks are only *ordered*. Both the
features and the target are ranks among that month's stocks, so a market-wide rally or crash
changes nothing. A model with no view on the market is still useful if it puts the next month's
winners above its losers.

On the other side of the trade are the investors behind each effect: momentum's slow
under-reaction to news, reversal's liquidity demanders. The model adds no new economics. It is a
better-weighted combination of known effects, or it is overfitting. The evaluation exists to
tell which.

## Rules

| | |
|---|---|
| Universe | The 38 large caps (survivorship caveat in config/universes.toml) |
| Features | At each month end: returns over 1 to 252 days, return against the sector, 21- and 63-day volatility and their ratio, ATR, range, volume z-score, dollar volume, distance from the 20/50/200-day averages, RSI(2), RSI(14). Each is ranked against that day's stocks ([ml.md](../ml.md#features)) |
| Target | The next 21 sessions' return, entered at the next open and ranked against that month's stocks |
| Model | Ridge regression (`model=ridge`, the default), gradient-boosted trees (`model=gbm`), or LightGBM with a ranking objective (`model=lgbm-rank`) |
| Training | Every 3 months (`retrain`), on every past month end whose next month had ended before that month began |
| Portfolio | The 10 best scores (`top_n`) at equal weight, at most 3 per sector, rebalanced at each month end; as strategy 3 |
| Warm-up | Until there are 24 month ends to learn from, it ranks by 12-1 momentum, i.e. it is strategy 3. Its orders say so |
| Execution | Next session's open, 5 bps slippage |

## How it's evaluated (beyond the backtest gate)

`tp-ml rank` scores every stock-month out of sample. The main scheme is walk-forward: each test
block is scored by a model trained only on labels that had ended before the block began.
Purged k-fold is shown alongside. Each run is logged to MLflow. The report has three rows:

- the model, walk-forward
- the model, purged k-fold
- **12-1 momentum over the same months**, the baseline

Each row reports:

- **rank IC**: the average across months of the rank correlation between score and next
  month's return.
- **t-stat**: how far that average is from zero, in standard errors.
- **IC>0**: the share of months it was positive.
- **spread/mo**: the top-minus-bottom bucket return per month.
- **Sharpe**: the spread's Sharpe ratio.
- **top turnover**: how much of the top bucket changes each month.

Two numbers calibrate expectations:

- A mean rank IC of 0.03 to 0.05 is a good equity signal.
- On random walks, the out-of-sample rank IC sits at zero. That's the leak check in
  `libs/ml/tests/test_ranking.py`.

Read the spread with care. With 38 stocks, each of the 5 buckets holds 7 or 8 names, and noise
alone can show a spread Sharpe of 0.5 over six years.

| Sample | Months | Rank IC (walk-forward) | t-stat | 12-1 momentum rank IC |
|---|---|---|---|---|
| Your lake, `tp-ml rank` | _fill in_ | | | |
| Your lake, `tp-ml rank --model gbm` | | | | |
| Your lake, `tp-ml rank --model lgbm-rank` | | | | |

## Known failure regimes

- **Momentum crashes** (2009, 2020). The model weighs momentum-like features, so a sharp
  reversal of past winners hurts it as it hurts strategy 3.
- **Factor rotations.** What predicted last decade's cross-section may not predict the next.
  Retraining every 3 months on all history adapts slowly by design.
- **A small universe.** 38 stocks give 38 data points a month. A model has little to learn
  from, and the survivorship bias of today's large caps flatters everything.
- **Overfitting by iteration.** Each feature or model tried after looking at the results is
  another trial. `tp-ml runs` lists them, and the Deflated Sharpe in validation is only honest
  if you count them.

## Promotion notes

It's not in the paper book by default. Promote it only if all three hold:

- the walk-forward rank IC is positive with a t-stat above 2 on your data;
- it beats 12-1 momentum's rank IC over the same months;
- `tp-backtest validate ml-rank` passes and beats `xs-momentum`'s validation.

Otherwise strategy 3 does the same job with one number. If it qualifies, add a
`[strategies.ml-rank]` sleeve to `config/paper.toml`.
