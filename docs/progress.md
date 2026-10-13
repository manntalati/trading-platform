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
| ✓ | Dashboard with live quotes | six sections, [dashboard.md](dashboard.md) |

## Phase 2: strategies, backtesting, risk, paper trading, options (`step2/…`)

| | Item | Notes |
|---|---|---|
| ✓ | Event-driven backtester, one strategy contract for backtest and paper | [backtesting.md](backtesting.md), ADR 0003 |
| ✓ | Strategies 1 to 5 of the plan | plus 6, a high-risk leveraged ETF rotation outside the plan's list |
| ✓ | Risk service: pre-trade limits, kill switch, strategy drawdown limit | [risk.md](risk.md) |
| ✓ | Paper bot with approvals, reconciliation and paper-gate progress | [paper-trading.md](paper-trading.md) |
| ✓ | Backtest validation protocol: locked test set, walk-forward, Deflated Sharpe, stress periods, Monte Carlo, parameter stability | [backtesting.md](backtesting.md#validation-the-backtest-gate) |
| ✓ | Market regime labels (trend, volatility, breadth), returns by regime | `tp_core.regime` |
| ✓ | Options: Black-Scholes, IV, greeks, IV rank, events, odds, scenarios | [positions.md](positions.md), [research.md](research.md) |
| ✓ | Options research: swing and LEAPS ideas, actions on held positions, Claude | [research.md](research.md) |
| ○ | Strategies 6 to 10: pairs (cointegration), factor portfolios, post-earnings drift, volatility risk premium (covered calls, cash-secured puts), opening-range breakout | 7 needs fundamentals and 8 earnings data, neither in the lake yet; 10 needs intraday bars |
| ✓ | Position sizing methods to compare: fixed fractional, volatility targeting, quarter Kelly | `tp-backtest sizing`, `--sizing`, per sleeve in paper.toml, [risk.md](risk.md#position-sizing) |
| ✓ | Portfolio risk monitoring: correlation, beta, aggregate Greeks, VaR | Portfolio → Risk, with a scenario grid (S&P ±10%, IV ±5 points), [risk.md](risk.md#portfolio-risk) |
| ○ | Rest of the options module: American (binomial) pricing, SVI smile fits, 25-delta skew, options backtesting | the portfolio Greek scenarios are on Portfolio → Risk |
| ○ | Rate regime from FRED, VIX for the volatility regime | |
| ◐ | Experiment tracking | every backtest and validation run is saved with its git commit, data version and config; MLflow not yet |

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

## Phase 4: machine learning (`step4/…`)

| | Item |
|---|---|
| ○ | Meta-labeling with triple-barrier labels |
| ○ | Cross-sectional ranking (ridge, then LightGBM) |
| ○ | Regime HMM; GARCH and HAR volatility forecasts (feeding IV vs forecast) |
| ○ | Strategy search, guarded by Deflated Sharpe and walk-forward |

## The money gates

| Stage | Where it stands |
|---|---|
| 1. Backtest | `tp-backtest validate`; the dashboard shows each strategy's checks |
| 2. Paper (60 trading days, 30 trades, Sharpe within half of backtest, slippage within 1.5x, no reconciliation breaks) | tracked per sleeve on the dashboard |
| 3. Live, micro | not before stage 2; nothing in the code can trade a live account |

## Next, in order

1. **Macro data** (`step2/macro-data`): VIX and Treasury rates from FRED, for the volatility
   regime and a rate regime.
2. **The rest of the options module** (`step2/options-module`): American pricing, SVI smile
   fits, skew and term-structure measures, more structures, options backtesting notes.
3. **Fundamentals** (`step2/fundamentals`): SEC EDGAR company facts, point in time, for the
   factor portfolios and post-earnings drift.
4. **Strategies 6 to 10** (`step2/more-strategies`): pairs, factor portfolios, post-earnings
   drift, the volatility risk premium, opening-range breakout.
5. **The platform** (`step3/…`): Docker images first, then the message bus, then k3s with
   monitoring and Discord alerts. The plan puts this ahead of ML: "cut scope from ML experiments
   before cutting infrastructure".
6. **Machine learning** (`step4/…`), starting with meta-labeling.
