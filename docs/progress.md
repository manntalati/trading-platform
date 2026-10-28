# Progress against the plan

Where the project stands against [the plan](project-plan.md), and the order of what's next. The
plan's phase table didn't survive the import, so the phases here follow the branch names
(`step<phase>/…`) and ADR 0003 (the message bus is Phase 3).

✓ done · ◐ partly · ○ not started

## Phase 0: the trading side and the data (`step0/…`)

| | Item | Notes |
|---|---|---|
| ✓ | Monorepo, uv, ruff, mypy, pytest, CI | |
| ✓ | Daily bars into the Parquet lake, validation, daily job | `tp-data`, [data.md](data.md) |
| ✓ | Daily option chain snapshots | now also your holdings, your research list, and LEAPS (800 days out) |
| ✓ | Toy matching engine (module 2), return metrics (module 3) | `research/` |
| ◐ | Strategy 1 reproducing Faber | code and notebook done; the paper's numbers are yours to fill in |
| ○ | Phase 0 one-page notes | yours to write, in [phase0/](phase0/README.md) |

## Phase 1: your portfolio and the dashboard (`step1/…`)

| | Item | Notes |
|---|---|---|
| ✓ | Fidelity, read-only through SnapTrade | positions, transactions, P&L, freshness, [portfolio.md](portfolio.md) |
| ✓ | Rules-based portfolio checks | [ideas.md](ideas.md) |
| ✓ | Dashboard with live quotes | six sections in a workspace: the page in the middle, panels on the right ([dashboard.md](dashboard.md)) |
| ✓ | How you trade | win rate, typical return, holding times by calls/puts/shares, from your round trips (Trades → How you trade) |
| ✓ | Fidelity sync that tells you why trades are missing | `tp-broker check`, reconnecting a disabled connection, SnapTrade's real-time plan |

## Phase 2: strategies, backtesting, risk, paper trading, options (`step2/…`): done

| | Item | Notes |
|---|---|---|
| ✓ | Event-driven backtester, one strategy contract for backtest and paper | [backtesting.md](backtesting.md), ADR 0003 |
| ✓ | Strategies 1 to 5 of the plan | plus 6, a high-risk leveraged ETF rotation outside the plan's list |
| ✓ | Risk service: pre-trade limits, kill switch, strategy drawdown limit | [risk.md](risk.md) |
| ✓ | Paper bot with approvals, reconciliation and paper-gate progress | [paper-trading.md](paper-trading.md) |
| ✓ | Backtest validation protocol: locked test set, walk-forward, Deflated Sharpe, stress periods, Monte Carlo, parameter stability | [backtesting.md](backtesting.md#validation-the-backtest-gate) |
| ✓ | Market regime labels (trend, volatility, breadth, rates, curve), returns by regime | `tp_core.regime` |
| ✓ | Options: Black-Scholes, IV, greeks, IV rank, events, odds, scenarios | [positions.md](positions.md), [research.md](research.md) |
| ✓ | Options research: swing and LEAPS ideas, actions on held positions, Claude | [research.md](research.md) |
| ✓ | Fundamentals and earnings dates, point in time | `tp-data fundamentals` from SEC EDGAR; trailing EPS, valuation and SUE; earnings dates on Research → Look up ([data.md](data.md#fundamentals-sec-edgar)) |
| ✓ | Strategies 6 to 10: pairs (cointegration), factor portfolios, post-earnings drift, volatility risk premium (covered calls, cash-secured puts), opening-range breakout | specs 07 to 11; pairs, factors and drift on the engine with point-in-time fundamentals (`ctx.reference`); the long/short pairs, the volatility risk premium and the opening-range breakout as research backtests (`tp-backtest research`), with 5-minute bars from `tp-data bars intraday` |
| ✓ | Position sizing methods to compare: fixed fractional, volatility targeting, quarter Kelly | `tp-backtest sizing`, `--sizing`, per sleeve in paper.toml, [risk.md](risk.md#position-sizing) |
| ✓ | Portfolio risk monitoring: correlation, beta, aggregate Greeks, VaR | Portfolio → Risk, with a scenario grid (S&P ±10%, IV ±5 points), [risk.md](risk.md#portfolio-risk) |
| ✓ | Rest of the options module: American (binomial) pricing, SVI smile fits, 25-delta skew, options backtesting | `tp_core.american`, `tp_core.smile`, Newton IV, rho/vanna/charm/vomma; cash-secured puts, credit spreads, iron condors, calendars and premium-selling ideas ([research.md](research.md)); [options-backtesting.md](options-backtesting.md) |
| ✓ | Rate regime from FRED, VIX for the volatility regime | `tp-data macro` (Cboe and the Treasury, or FRED's API), the 3-month yield as the risk-free rate, [data.md](data.md#macro-series-vix-and-treasury-yields) |
| ✓ | Experiment tracking | every backtest and validation run is saved with its git commit, data version and config; model experiments in MLflow (Phase 4) |
| ✓ | Options ideas from setups that fire, priced by expected edge, with entry and exit plans | ~100 optionable names; pullback, breakout, trend leader (LEAPS), earnings drift, bounce and breakdown setups, plus expensive and cheap volatility; each contract chosen by its modeled return under the volatility forecast, with greeks, gamma against theta, an entry limit and an exit plan; the track record follows the plans ([research.md](research.md#the-playbook)) |
| ✓ | Stock ideas from the same setups | Shares on the upward setups for every name with bars: a limit, an ATR stop and optional target, a time stop and the stock-side exit, all calibrated by the backtest (fills where an order would, judged on the return over the average stock); sized to the buying power and the risk guideline; ordered by drift over volatility and the ML ranker where it held up; downward setups on what you hold flagged ([research.md](research.md#stock-ideas)) |
| ✓ | Playbook backtest and calibration | `tp-recs calibrate`: every setup's signals over the bar history, the stock's drift after them, and the option trade under 72 exit combinations chosen on the earlier signals and judged on the later; real option prices from Alpaca for the trades since February 2024 (`tp-data options history`); weekly on the scheduler ([research.md](research.md#what-to-run)) |
| ✓ | Recommendations kept up to date | a job scheduler in `tp-api` (or `tp-jobs`) runs every data job in its window on the exchange calendar, catching up after sleep, and remakes the options ideas whenever new data lands; each run is stored, new ideas are marked, and Research → Track record shows how past ones did ([setup.md](setup.md#6-schedules), [research.md](research.md#kept-refreshed-and-scored)); `make mac-agent` keeps it running on a Mac |

## Phase 3: the platform (`step3/…`)

| | Item |
|---|---|
| ○ | Docker images for each service |
| ○ | Message bus (NATS JetStream) with typed messages; a recorder for replay |
| ○ | Postgres/TimescaleDB for orders, fills and bars; DuckDB over the Parquet lake |
| ○ | k3s, Helm charts, Argo CD, Terraform |
| ○ | Prometheus, Grafana, Loki; alerts to Discord; runbooks |
| ○ | Pre-market checklist (8:30 ET) and end-of-day jobs; deploy freeze in market hours |
| ○ | Approvals in Discord or Slack (today they're on the dashboard) |

## Phase 4: machine learning (`step4/…`): done, two later experiments waiting

Taken ahead of Phase 3 at your call; nothing in it depends on the platform. See [ml.md](ml.md).

| | Item | Notes |
|---|---|---|
| ✓ | MLflow experiment tracking (deferred from Phase 2) | SQLite store in the data root, `tp-ml runs`, `tp-ml ui` |
| ✓ | Point-in-time features and a versioned feature store | `tp-ml features`; returns, volatility, volume, trend, macro, earnings |
| ✓ | Purged, embargoed cross-validation; walk-forward; uniqueness weights | `tp_ml.cv`, with a leak check on random data |
| ✓ | Meta-labeling with triple-barrier labels | `tp-ml meta`; strategy 12 (`rsi2-meta`) on the engine; labels by the strategy's own exits or the triple barrier |
| ✓ | Cross-sectional ranking (ridge, then gradient boosting) | `tp-ml rank`: rank IC, t-stat, bucket spreads and turnover against 12-1 momentum; strategy 13 (`ml-rank`) on the engine |
| ✓ | Regime HMM; GARCH and HAR volatility forecasts (feeding IV vs forecast) | `tp-ml regime` (filtered probabilities, out of sample), `tp-ml vol` (naive, EWMA, GARCH, HAR-RV, gradient boosting and implied, walk-forward); the options research judges implied against the HAR forecast |
| ✓ | Strategy search, guarded by Deflated Sharpe and walk-forward | `tp-backtest search`: 41 rule sets in three families; train → validation → one look at test; Deflated Sharpe over every rule set ever tried; probability of backtest overfitting (CSCV); walk-forward of the search itself ([backtesting.md](backtesting.md#strategy-search)) |
| ✓ | Genetic search over a grammar of signals | `--families grammar`: rules bred from averages, momentum, crosses, a low-volatility filter and RSI on the training period, penalized for size, every one counted as a trial; a pick runs as strategy 14 (`rule-timing`) |
| ✓ | LightGBM with a ranking objective | `model=lgbm-rank` for strategy 13 and `tp-ml rank` (lambdarank, the stocks of a date as one group) |
| ✓ | The regime model switching strategies on and off | `--regime hmm` (or `trend`, `vix`) in backtests and validation, `regime =` per strategy in `config/paper.toml`; `tp-backtest regime <name>` compares ([ml.md](ml.md#switching-strategies-off-in-a-stressed-market)) |
| ✓ | Scheduled retraining, model registry, drift and live accuracy | `tp-models monitor` after the evening bars: retrain when due, register, predict, PSI drift per input, live rank IC / hit rate; System → Models ([ml.md](ml.md#models-in-use-retraining-registry-drift-live-accuracy)) |
| ✓ | News sentiment as a feature (later experiment) | `tp-data news fetch` (Alpaca's feed) and `score` (Claude, opt in: `TP_NEWS_SCORING=on`); `news_sentiment_5d`, `news_count_5d` ([data.md](data.md#news-and-sentiment)) |
| ○ | Sequence models on intraday bars (later experiment) | the plan puts them after the tree models have a solid baseline on your data; they also need PyTorch and a long intraday history |
| ○ | Reinforcement learning for execution (later experiment) | only matters with real order flow to learn from: live trading, after the paper gate |

## The money gates

| Stage | Where it stands |
|---|---|
| 1. Backtest | `tp-backtest validate`; the dashboard shows each strategy's checks |
| 2. Paper (60 trading days, 30 trades, Sharpe within half of backtest, slippage within 1.5x, no reconciliation breaks) | tracked per sleeve on the dashboard |
| 3. Live, micro | not before stage 2; nothing in the code can trade a live account |

## Next, in order

Phases 0, 1, 2 and 4 are done in code. What's left of them waits on time and your data: the
paper gate's 60 trading days for each sleeve, the fill-in-the-numbers tables in the strategy
specs, Faber's numbers for strategy 1, and your Phase 0 notes. The two ML experiments the plan
puts last (sequence models, RL execution) wait on the tree models proving themselves on your data.

1. **Keep it running**: `tp-api` (or `make mac-agent` on a Mac) so the scheduler keeps the data,
   the recommendations and the model checks current. After updating, run the options playbook's
   first calibration by hand ([research.md](research.md#what-to-run)); the scheduler keeps it
   weekly from then on.
2. **Phase 4 on your data**: `tp-ml meta`, `rank` (try `--model lgbm-rank`), `vol` and
   `regime`; `tp-backtest search` (and `--families grammar`); `tp-backtest regime` on the
   strategies you paper trade. Fill in the tables in specs 12 to 14, and promote to paper only
   what clears `tp-backtest validate`.
3. **The platform** (`step3/…`): Docker images first, then the message bus, then k3s with
   monitoring and Discord alerts.
