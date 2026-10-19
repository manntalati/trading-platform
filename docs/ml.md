# Machine learning

Phase 4 of the plan. The ML code lives in [`libs/ml`](../libs/ml/src/tp_ml) (`tp_ml`). It
depends only on `tp_core`, scikit-learn and MLflow, so strategies can use it and nothing in it
can reach a broker. The rules are the same as for any strategy:

- Everything a model sees is **point in time**.
- Every score is **out of sample**.
- Every run is **recorded**.

```bash
uv run tp-ml features              # compute and store the feature set (versioned)
uv run tp-ml meta                  # evaluate the RSI(2) meta-labeling filter; logged to MLflow
uv run tp-ml rank                  # evaluate cross-sectional ranking against 12-1 momentum
uv run tp-ml vol                   # volatility forecasts (GARCH, HAR, ...) for SPY, vs the VIX
uv run tp-ml vol --symbol XYZ      # any symbol: against its options' implied volatility
uv run tp-ml regime                # market regimes from a hidden Markov model
uv run tp-ml runs                  # what's been tried, and how it scored
uv run tp-ml ui                    # MLflow's UI on http://127.0.0.1:5000
uv run tp-backtest run rsi2-meta   # the filter as an engine strategy (spec 12)
uv run tp-backtest run ml-rank     # the ranker as an engine strategy (spec 13)
```

All of it runs on the daily bars you already have. The macro and earnings features are filled
in when `tp-data macro` and `tp-data fundamentals` have run.

## Features

[`tp_ml.features`](../libs/ml/src/tp_ml/features.py) computes one row per (session, symbol).
Each row uses only data up to that session's close.

| Family | Columns |
|---|---|
| Returns | `ret_1`, `ret_5`, `ret_21`, `ret_63`, `ret_126`, `ret_252` (log); `rel_market_63` (less SPY's); `rel_sector_63` (less the sector median) |
| Volatility | `vol_21`, `vol_63` (annualized), `vol_ratio`, `atr_pct` (14-day ATR / close), `range_pct` (today's range / close) |
| Volume | `volume_z` (vs its 20-day mean), `dollar_volume` (log, 20-day average) |
| Trend and oscillators | `dist_ma20`, `dist_ma50`, `dist_ma200`, `rsi_2`, `rsi_14` |
| Macro | `vix`, `vix_ratio` (VIX / 3-month VIX), `curve_10y_3m`, `rate_10y_chg_63` |
| Earnings | `days_since_results`, `last_sue` (the latest results' standardized surprise) |

Two tests guard this:

- **Point in time.** Tripling every price after a given day leaves every feature up to that day
  unchanged.
- **Train and serve match.** `latest_price_features` is what a strategy scores each day from the
  last 260 sessions. It equals the training panel's last row.

**Feature store.** `tp-ml features` writes `data/features/<name>/<version>/part.parquet` and lists
it in `data/features/registry.json` with:

- its columns, rows, symbols and sessions
- the data version
- the git commit

The version hashes the feature definitions and the data. The same code on the same data gives the
same version, so a model can name exactly what it was trained on.

## Labels

[`tp_ml.labels`](../libs/ml/src/tp_ml/labels.py) provides two ways to say whether a signal
"worked". In both, the trade enters at the next session's open.

- **Triple barrier** (López de Prado ch. 3). A profit target and a stop, both in daily
  volatilities (an exponentially weighted estimate as of the signal day), plus a time limit.
  - The label is whichever barrier the price touches first.
  - When one bar touches both, the stop counts first.
  - A gap through a barrier fills at the open.
- **The strategy's own exits** (`rule_labels`). The label is the trade the primary strategy's
  exit rule would have made: for RSI(2), sell at the open after a close above the 5-day average,
  or after 10 sessions.
  - This is the default for meta-labeling, because the model then learns about the trades the
    strategy actually takes.

A signal whose trade hadn't ended by the last bar gets no label: its outcome isn't known yet.

## Cross-validation that doesn't leak

Labels span days, so ordinary k-fold trains on trades that overlap the ones it tests.
[`tp_ml.cv`](../libs/ml/src/tp_ml/cv.py) provides three tools:

- **Purged k-fold.** Folds are contiguous blocks of time. Every training sample whose trade
  overlaps the test block is dropped, and so is everything that starts within an embargo (1% of
  the sample's span) after it.
- **Walk-forward.** The first 40% only trains. Each later block is predicted by a model trained
  on trades that ended before it began, which is what a live model does.
- **Uniqueness weights** (ch. 4). Each label is weighted by 1 / (labels alive the same day),
  averaged over its life. Twenty signals on one sell-off day carry about as much information as
  a few, and are weighted that way.

**The leak check.** On random-walk data the out-of-fold AUC is 0.5 (±0.06 in the tests). If a
change makes noise look predictable, the change leaks.

## Models

[`tp_ml.models`](../libs/ml/src/tp_ml/models.py) provides two classifiers for meta-labeling:

- **`logistic`**: median imputation, standardization, then L2-penalised logistic regression. The
  baseline.
- **`gbm`**: scikit-learn's histogram gradient boosting with shallow trees and a slow learning
  rate, on features cut into 32 quantile bins first. The bins regularize, and they are also how
  this stays fast: with sample weights, scikit-learn otherwise re-sorts every feature once per
  bin edge.

It also provides two regressors for ranking, both on features already ranked across stocks:

- **`ridge`**: the linear baseline. A missing rank is the middle, 0.
- **`gbm`**: gradient-boosted regression trees.

Columns with nothing in them, such as macro features before `tp-data macro` has run, are left
out before fitting.

## Meta-labeling

[`tp_ml.meta`](../libs/ml/src/tp_ml/meta.py) and [spec 12](strategies/12-rsi2-meta.md) cover it in
full. In short:

- RSI(2) proposes and the model decides.
- `tp-ml meta` labels every RSI(2) signal in the lake, attaches its signal-day features and
  weights, then scores every signal out of fold, both ways above.
- It prints what keeping only the signals at p ≥ 0.55 would have done to the hit rate and the
  average trade.

```
4914 RSI(2) signals 2016-10-14 to 2025-12-12; 64% made money, average trade +0.10%.
Purged 5-fold: AUC 0.493 (0.5 is a coin flip). Keeping trades at p >= 0.55: ...
Walk-forward: AUC 0.497 (0.5 is a coin flip). ...
```

That example ran on synthetic random walks, where there is nothing to find. A filter worth using
shows a walk-forward AUC clearly above 0.5 *and* a better average trade for what it keeps.

Options:

- `--model logistic`
- `--label barrier` (triple barrier instead of RSI(2)'s exits)
- `--features all` (add macro and earnings)
- `--threshold 0.6`
- `--no-log`

The engine strategy `rsi2-meta` retrains every 3 months on the bars before that month. It trades
as plain RSI(2) until it has 200 labelled signals.

A 10-year backtest takes about a minute and a half, against about 20 seconds for `rsi2`. Models
are cached, so `tp-backtest validate rsi2-meta` reuses them across its threshold variants.

## Experiment tracking (MLflow)

Every `tp-ml meta`, `rank`, `vol` and `regime` run is recorded in MLflow, under the
experiments `meta-labeling`, `cross-sectional-ranking`, `volatility` and `regimes`. The store is a SQLite file,
`data/mlflow/mlflow.db`, with artifacts beside it, so no server is needed. Each run records:

- **Parameters**: the full config (labels, model, threshold, folds, features, symbols)
- **Tags**: the git commit and whether the tree was dirty, the feature set version, and the data
  version
- **Metrics**:
  - for meta-labeling: events, base rate and average trade, plus for each scheme (`cv_*`,
    `wf_*`) the AUC, log loss, Brier, kept share, precision and average trade, kept vs all;
  - for ranking: rank IC and its t-stat, IC, spread and its Sharpe, and top-bucket turnover, for
    the walk-forward (`wf_*`), the k-fold (`cv_*`) and momentum (`mom_*`)
- **Artifacts**: `report.txt`, `report.json`, and `predictions.csv` (every signal with its label,
  return and both out-of-fold probabilities)

`tp-ml runs` lists recent runs in the terminal. `tp-ml ui` opens MLflow's own UI on this machine
only, for comparing runs and charts.

Backtests and validation keep their own records under `data/reports/` with the same commit and
data versions ([backtesting.md](backtesting.md)).

## Discipline

- **Count the trials.** Every feature, label or model change made after seeing a score is
  another trial. MLflow keeps the list, and the Deflated Sharpe in `tp-backtest validate` is only
  honest if you count them.
- **The locked test set stays locked.** Tune on `tp-ml meta`'s cross-validation and on
  validation's train and validation periods, then look at the test set once.
- **A model has to beat its baseline.** `rsi2-meta` is promoted only if it beats plain `rsi2` on
  the same validation, not merely if it passes the gate.

## Cross-sectional ranking

[`tp_ml.ranking`](../libs/ml/src/tp_ml/ranking.py) and [spec 13](strategies/13-ml-rank.md)
cover it in full. Each month end, every stock is described by its features. Each feature is
*ranked* against that day's stocks, so the model learns about relative standing and a
market-wide move changes nothing. The target is the stock's rank on the next 21 sessions'
return, entered at the next open.

`tp-ml rank` scores every stock-month out of sample, walk-forward (the main scheme) and purged
k-fold. It reports what the factor literature measures, next to 12-1 momentum over the same
months:

```
                            rank IC  t-stat   IC>0  spread/mo  Sharpe  top turnover
Model, walk-forward          +0.022    +1.2    56%     +0.20%    0.30           61%
Model, purged k-fold         +0.003    +0.2    50%     -0.13%   -0.17           64%
12-1 momentum (baseline)     +0.007    +0.4    51%     +0.51%    0.61           23%
```

That ran on synthetic random walks. A t-stat of 1.2 is noise, and so is the momentum row's
Sharpe of 0.6: with 38 stocks in 5 buckets, spreads are noisy. On real data, look for:

- a rank IC with a t-stat above 2;
- a rank IC above momentum's;
- bucket returns that rise from the worst bucket to the best.

Options:

- `--model gbm`
- `--horizon 63` (rank the next quarter's return)
- `--buckets 3`
- `--features all` (adds the earnings features)
- `--no-log`

Runs go to the MLflow experiment `cross-sectional-ranking`.

The engine strategy `ml-rank` holds strategy 3's portfolio, the top 10 at equal weight with at
most 3 per sector, but ranked by the model. It retrains every 3 months on the month ends before
that month, and ranks by 12-1 momentum until it has 24 month ends to learn from. A 10-year
backtest takes about 15 seconds.

## Volatility forecasts

[`tp_ml.vol`](../libs/ml/src/tp_ml/vol.py) forecasts the annualized volatility of a stock's
daily returns over the next `horizon` sessions (21 by default) with five models:

| Model | How |
|---|---|
| Trailing realized | the last 21 sessions' volatility: the naive forecast, and what the options research used to compare with |
| EWMA | RiskMetrics' exponentially weighted variance (lambda 0.94) |
| GARCH(1,1) | variance reverts to a long-run level at a fitted speed, pushed up by each squared return; maximum likelihood |
| HAR-RV | tomorrow's variance from today's, last week's and last month's, a regression on logs (Corsi 2009); daily variance from the open, high, low and close (Garman-Klass plus the overnight gap) |
| Gradient boosting | the HAR terms plus recent returns and the VIX |

`tp-ml vol` scores them walk-forward. Models are refitted every quarter on what was known then,
and forecast weekly. For SPY the VIX is scored as one more forecast; for another symbol it's the
30-day implied volatility from the stored chain snapshots. The losses:

- **QLIKE** (Patton 2011): the standard loss for variance forecasts, robust to how noisy
  realized variance is. Lower is better.
- **error**: the RMSE in volatility points.
- **bias**: the forecast minus what happened.
- **MZ R²**: how much of the variation in realized variance the forecast explains.

The report ends with today's forecasts and GARCH's persistence (half-life) and long-run level.
It also gives implied now against the best forecaster: that ratio is the volatility risk premium
an option seller would collect.

Implied volatility usually runs above what happens. That gap is the volatility risk premium
(spec 10), and it's why the VIX tends to score worse than the models as a pure forecast.

**In the options research.** Each symbol now gets a HAR forecast, which takes about 10 ms. It
needs roughly 200 sessions of bars, and the research loads about 14 months. "Expensive options"
is judged on implied volatility against that forecast instead of against the last 20 days'
realized ([research.md](research.md#how-expensive-are-the-options)).

## Regimes

[`tp_ml.regime`](../libs/ml/src/tp_ml/regime.py) is a Gaussian hidden Markov model fitted to two
daily observations of SPY: its log return, and the log of its 5-day range-based volatility. The
market switches between 2 (or 3) hidden states, calm to stressed, each with its own
distribution, and tends to stay in one. That is what the transition matrix captures.
`tp-ml regime` reports each state's return and volatility, how much of the time it held, how long
it lasts, and today's probabilities.

Two rules keep it honest:

- **Filtered, not smoothed.** A day's state probability uses only the days up to it. Libraries
  usually report smoothed probabilities, which also use the days after. Any regime "signal"
  built on those looks better than it can be.
- **Out of sample.** The model is refitted each January on the years before. The report says
  what the state it gave each day meant for the *next* month's volatility and return. It also
  shows SPY held only while the model said calm (decided a day late) against buy and hold.

On regime-switching test data the stressed state predicts higher next-month volatility out of
sample. On random walks it doesn't, and the report shows that too. The rule-based labels (trend,
VIX level, breadth, rates) are still in `tp_core.regime`; this is the learned counterpart.

Runs go to the MLflow experiments `volatility` and `regimes`.

### Switching strategies off in a stressed market

The plan's use for the regime model is to switch strategies on and off, and any engine strategy
can be: a **regime gate** keeps it in cash while the gate says risk is off, and puts it straight
back in when it clears (it takes the positions its rules call for that day, instead of waiting
for its next month end). Three gates, each with a band so a reading at the line doesn't flip it
in and out every few days ([`tp_strategies/regime_gate.py`](../strategies/src/tp_strategies/regime_gate.py)):

| Gate | Off when | On again when |
|---|---|---|
| `hmm` | the out-of-sample filtered probability of the stressed state reaches 0.7 | it falls to 0.3 |
| `trend` | SPY closes 1% below its 200-day average | 1% above it |
| `vix` | the VIX closes above 30 | below 25 |

```bash
uv run tp-backtest regime ts-momentum            # always in vs each gate: CAGR, vol, Sharpe, drawdown, time off
uv run tp-backtest run rsi2 --regime hmm         # one gated backtest
uv run tp-backtest validate ts-momentum --regime trend
```

Every value uses data up to that day's close; the `hmm` gate's model is refitted each January on
the years before. A gate earns its place by cutting the drawdown by more than it costs in
return; momentum strategies already step aside in downtrends, so the gain there is often small.
Validate the gated version before trading it: the gate is recorded with the trial (another trial
for the Deflated Sharpe), and `regime = "hmm"` (or `"trend"`, `"vix"`) in a strategy's block in
`config/paper.toml` then gates it in paper trading, where `tp-backtest validate` picks it up too.

## Strategy search

`tp-backtest search` tries 41 simple rule sets (trend, momentum and RSI(2) reversion, vectorized
versions of the library strategies). It picks one by train, then validation, then the locked
test period. It only trusts the pick if four guards hold: a Deflated Sharpe over every rule set
it has ever tried, the probability of backtest overfitting, a walk-forward of the search itself,
and the test period. [backtesting.md](backtesting.md#strategy-search) has the protocol.

That completes Phase 4's list. What's left is yours to run on real data: `tp-ml meta`, `rank`,
`vol` and `regime`, and `tp-backtest search`. Fill in the tables in specs 12 and 13, and promote
only what clears the engine's validation.
