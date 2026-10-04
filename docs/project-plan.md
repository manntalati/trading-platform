# Trading Platform Project Plan

Oct 3, 2026 · Mann Talati

> Source of truth for scope and sequencing. Imported from the original planning doc; sections
> that did not survive the import are marked **TODO**.

## Goals and ground rules

Build a personal trading research and execution platform that does two jobs: help Mann make
better stock and options decisions, and give him hands-on reps with professional
trading-platform engineering. The trading side earns its keep through measured PnL on paper; the
platform side earns its keep by running many strategies reliably on Linux, Docker and
Kubernetes.

### Three deliverables

1. **Research stack:** data pipelines, a backtester, a strategy library (classic published
   strategies plus ML models), and an options analytics module.
2. **Decision-support bot:** generates signals, sizes positions, and proposes trades with
   reasoning. Mann approves every order. Paper first, small real money only after passing
   explicit gates.
3. **Platform:** multiple strategy bots, multiple data feeds, and multiple consumers
   (dashboard, alerts, broker gateway) wired through a message bus, deployed on Kubernetes with
   monitoring, CI/CD and runbooks.

### Ground rules

- **Human in the loop.** The bot proposes, Mann decides. No unattended live orders, ever, in v1.
- **Paper before real.** A strategy only touches real money after the promotion gates in the
  paper-to-live section.
- **Hard risk caps live in code**, outside strategy logic, and cannot be overridden by a
  strategy.
- No margin, no naked short options, no 0DTE until the platform has months of clean paper
  history.
- **Backtest results are hypotheses, not evidence.** Assume every good backtest is overfit until
  out-of-sample and paper results agree.
- **Track taxes from day one:** short-term gains are taxed as ordinary income, and wash sales can
  disallow losses.

## Phase 0: Learn the trading side first

Spend roughly 3 weeks on fundamentals before writing serious code, with a small coding exercise
attached to each module so the knowledge sticks. Each module ends with a one-page note in the
repo's `docs/` folder (see [`docs/phase0/`](phase0/README.md)).

| Module | What to understand | Hands-on exercise |
|---|---|---|
| 1. Market structure | Exchanges vs ATSs vs dark pools, NBBO, Reg NMS, how a retail order gets routed, payment for order flow, market makers vs takers | Diagram the life of a buy order from your broker app to a fill |
| 2. Order books and microstructure | Limit order book, bid/ask, spread, depth, queue priority, order types (market, limit, stop, IOC, FOK), slippage, market impact | Build a toy matching engine in Python: price-time priority, partial fills |
| 3. Returns and statistics | Simple vs log returns, volatility, Sharpe, Sortino, max drawdown, autocorrelation, fat tails, stationarity | Compute these metrics for SPY, QQQ and 5 stocks from raw daily bars |
| 4. Classic strategy families | Trend/momentum, mean reversion, stat arb/pairs, carry, factor investing, event-driven | Write a one-paragraph thesis for each: why should it make money and who loses |
| 5. Options basics | Calls/puts, moneyness, intrinsic vs extrinsic value, exercise styles, assignment, expiry mechanics | Payoff diagrams for 8 structures (long call, covered call, CSP, verticals, straddle, strangle, iron condor, calendar) |
| 6. Options pricing and Greeks | Black-Scholes, implied vol, delta, gamma, theta, vega, rho, put-call parity, vol smile/skew, term structure | Implement Black-Scholes and an IV solver; verify against a broker's chain |
| 7. Risk and sizing | Position sizing, Kelly and fractional Kelly, VaR, correlation risk, gap risk, liquidity risk | Size a 5-position portfolio under a 1% per-trade risk rule |
| 8. How trading firms operate | Prop trading vs market making vs hedge funds, latency tiers, colocation, market data feeds, the role of platform/infra teams | Write a short note: what infra a market maker needs that a retail bot doesn't |

**Vocabulary checkpoint:** by the end of Phase 0 you should be able to explain PnL attribution,
mark-to-market, realized vs unrealized PnL, fill rate, and why a strategy with a 2.0 backtest
Sharpe often trades at 0.5 live.

## Data sources

Start with Alpaca as the primary source because it bundles free market data, a paper trading
account and a broker API in one place, then layer free macro, fundamentals and factor data on
top. Use yfinance only for quick exploration, and spend Databento's free credits on order book
data, which is the closest thing to what trading-firm engineers actually handle.

| Source | Cost | What you get | Limits and gotchas | Use it for |
|---|---|---|---|---|
| Alpaca Market Data | Free (Basic), $99/mo (Algo Trader Plus) | US stocks and ETFs since 2016, options, news; REST + WebSocket; Python SDK | Free real-time is IEX only (a small slice of volume) and the options indicative feed; historical SIP data must be more than 15 min old; 200 calls/min; 30 WebSocket symbols | Primary bars, live streaming, paper trading, broker |
| Massive (formerly Polygon.io) | Free Stocks Basic; paid tiers above | Daily aggregates and reference data | Free tier is end-of-day only, 2 years of history, 5 calls/min | Second source to cross-check Alpaca bars |
| yfinance (Yahoo Finance) | Free | Daily and intraday bars, option chains, basic fundamentals | Unofficial scraper, can break or get rate-limited, no SLA, intraday history is short, no delisted tickers | Notebooks and quick checks only, never production |
| Databento | $125 free credits for new users, then pay per GB | Tick data, L1/L2/L3 order book (MBO), OPRA options, CME futures | Credits expire after 6 months, so pull a planned dataset in one go | Order book replay, microstructure, latency-style work |
| FRED (St. Louis Fed) | Free | Rates, yield curve, CPI, unemployment, credit spreads | Macro frequency (daily to monthly) | Regime features for ML |
| SEC EDGAR | Free | 10-K, 10-Q, 8-K filings, XBRL financial data API | Needs parsing; point-in-time care around filing dates | Fundamentals, earnings-event features |
| Kenneth French Data Library | Free | Fama-French factor returns, momentum, industry portfolios | Monthly/daily, academic format | Factor benchmarks, attributing your returns |
| Cboe VIX history | Free | VIX and related volatility index history | Index level only, not tradable | Volatility regime filter |
| iVolatility / ORATS | Pay per use / subscription; ORATS has free sample files | Historical options chains with IV and Greeks back to 2000s | Costs add up across many tickers | Buy only for the few underlyings you backtest options on |

**Build your own options history.** Free historical options chains are rare and incomplete. From
week one, run a daily job that snapshots full chains for 20 to 30 liquid underlyings (SPY, QQQ,
IWM, AAPL, MSFT, NVDA and so on) into your own storage. In six months you'll have a proprietary
dataset, and the collector itself is a real platform component.

### Data engineering rules

- Store raw data immutably (Parquet, partitioned by date and symbol), then derive cleaned tables
  from it.
- Adjust for splits and dividends explicitly and keep both raw and adjusted series.
- Use point-in-time data: a feature can only use information published before the decision time.
- Track survivorship bias: free sources usually drop delisted tickers, which inflates backtests.
- Validate every load: missing bars, zero volume, price jumps over 20%, timestamps in the wrong
  timezone.

## Strategy research: start with published strategies

Implement 10 well-documented strategies from the academic and practitioner literature before
inventing anything. Each one becomes a baseline your ML models must beat, and replicating a
known paper result is the best test that your backtester is correct.

| # | Strategy | Family | Original source | Data needed | Holding period |
|---|---|---|---|---|---|
| 1 | 10-month moving average timing | Trend | Faber (2007), tactical asset allocation | Daily ETF bars | Weeks to months |
| 2 | Time-series momentum | Trend | Moskowitz, Ooi, Pedersen (2012) | Daily bars, many assets | 1 month |
| 3 | Cross-sectional momentum (12-1) | Momentum | Jegadeesh and Titman (1993) | Daily bars, S&P 500 universe | 1 month |
| 4 | Dual momentum | Momentum | Antonacci (2014) | Daily ETF bars | 1 month |
| 5 | RSI(2) short-term reversion | Mean reversion | Connors and Alvarez (2009) | Daily bars | 2 to 5 days |
| 6 | Pairs trading via cointegration | Stat arb | Gatev, Goetzmann, Rouwenhorst (2006) | Daily bars, sector peers | Days to weeks |
| 7 | Factor portfolios (value, quality, low vol) | Factor | Fama-French; Frazzini and Pedersen (2014) | Bars + fundamentals | Monthly rebalance |
| 8 | Post-earnings announcement drift | Event-driven | Bernard and Thomas (1989) | Earnings dates, surprises | Days to weeks |
| 9 | Volatility risk premium (covered calls, cash-secured puts) | Options income | Cboe BXM and PUT index methodology | Options chains | 1 month |
| 10 | Opening range breakout | Intraday | Practitioner literature (Crabel, 1990) | 1-min or 5-min bars | Intraday |

For each strategy, write a short spec before coding: the economic rationale (who is on the other
side and why they lose), entry and exit rules, universe, rebalance frequency, expected Sharpe
from the paper, and known failure regimes (momentum crashes in sharp reversals like 2009; short
vol blows up in events like Feb 2018).

**Trend and market-regime checks.** Add a small market-context module that labels each day:
trend (price vs 200-day MA), volatility regime (VIX level and term structure), breadth (share of
S&P 500 above its 50-day MA) and rate regime from FRED. Strategies get evaluated per regime,
which tells you when each one should be switched off.

**Where to find more:** Quantpedia (encyclopedia of published strategies), SSRN, the Journal of
Portfolio Management, and Alpha Architect's blog.

## Backtesting framework and methodology

Run two engines: a fast vectorized one for research sweeps, and an event-driven one you write
yourself that replays data bar by bar through the exact same strategy code that runs live.
Writing the event-driven engine is where most of the learning happens, and it's the same pattern
trading firms use: one strategy interface, swappable data and execution backends.

### Engines

- **Research (vectorized):** vectorbt or plain pandas/Polars. Test thousands of parameter
  combinations in seconds.
- **Production-faithful (event-driven):** your own engine with components DataFeed, Strategy,
  Portfolio, RiskManager, ExecutionHandler, communicating through an event queue (MarketEvent,
  SignalEvent, OrderEvent, FillEvent). The live system swaps in a real feed and broker without
  touching strategy code.
- **Reference to compare against:** NautilusTrader (Rust core, Python API, event-driven, built
  for backtest/live parity) or QuantConnect LEAN. Read their architecture docs even if you don't
  adopt them.

### Realism requirements

- Commissions, SEC/FINRA fees, and options per-contract fees.
- Slippage model: start with half the spread plus a fixed bps impact; later, size-dependent
  impact.
- Fills at the next bar's open (or worse), never at the bar that generated the signal.
- Short borrow costs and hard-to-borrow restrictions.
- Corporate actions, delistings, and trading halts.

### Validation protocol

1. Split time into train, validation and a locked test set (e.g., 2016 to 2021 / 2022 to 2023 /
   2024 onward). Touch the test set once per strategy.
2. Walk-forward analysis: re-fit on a rolling window, trade the next period, repeat.
3. For ML, purged and embargoed k-fold cross-validation (López de Prado) so overlapping labels
   don't leak.
4. Correct for multiple testing: log every variant you try and compute the Deflated Sharpe Ratio.
5. Stress tests: 2020 COVID crash, 2022 rate shock, Aug 2024 vol spike; Monte Carlo resampling of
   trade sequences.
6. Parameter stability: performance should degrade smoothly around the chosen parameters, not
   fall off a cliff.

**Standard tear sheet for every run:** CAGR, annualized vol, Sharpe, Sortino, Calmar, max
drawdown and duration, hit rate, average win/loss, turnover, exposure, beta to SPY, factor
attribution, and PnL by regime. Every run is logged to MLflow with its git commit, data version
and config so results are reproducible.

**Biases to guard against:** look-ahead, survivorship, data snooping, overfitting, ignoring
costs, and unrealistic fills on illiquid names or wide option spreads.

## Machine learning for signals and strategy generation

Use ML in four places, in this order: filtering rule-based signals (meta-labeling),
cross-sectional ranking, regime and volatility forecasting, and finally automated strategy
search. Financial data is noisy and non-stationary, so simple well-regularized models with honest
validation beat deep models with leaky validation almost every time.

1. **Meta-labeling (start here).** A classic strategy from the library proposes a trade; a
   classifier predicts whether that trade will hit its profit target before its stop, and sizes
   the position by the predicted probability. This keeps the economic logic in the base strategy
   and lets ML improve precision. Labels come from the triple-barrier method (profit target, stop
   loss, time limit).
2. **Cross-sectional ranking.** Each day, predict next-week or next-month relative returns across
   the S&P 500 and go long the top decile. Models: ridge regression baseline, then LightGBM with
   a ranking objective. Evaluate with information coefficient (IC) and rank IC per period, plus
   decile spread returns after costs.
3. **Regime and volatility forecasting.**
   - Hidden Markov Model on returns and VIX to label calm, trending and stressed regimes; use it
     to switch strategies on and off.
   - Volatility forecasting: GARCH(1,1) and HAR-RV baselines vs gradient boosting on realized-vol
     features. Better vol forecasts feed position sizing and options decisions (IV vs forecast
     RV).
4. **Strategy search.** Genetic programming or Bayesian search over a constrained grammar of
   signals (indicators, lookbacks, combinators). Guardrails: every candidate is logged, scored on
   validation only, penalized for complexity, and must survive Deflated Sharpe and walk-forward
   tests before it's even considered.

### Later experiments

- News sentiment: score Alpaca's free news feed with FinBERT or an LLM, aggregated per ticker per
  day as a feature.
- Sequence models (TCN, LSTM, small Transformer) on intraday bars, only after tree models are a
  solid baseline.
- Reinforcement learning for execution (slicing an order to minimize slippage) rather than for
  picking stocks, since the reward signal is much cleaner.

**Feature families:** returns over multiple lookbacks, volatility and range measures, volume and
dollar-volume anomalies, distance from moving averages, RSI and other oscillators,
sector-relative strength, earnings proximity and surprise, implied vs realized vol, options skew
and put/call ratios, macro features from FRED.

**MLOps:** versioned features in Parquet with a feature registry, MLflow for experiments and the
model registry, scheduled retraining with walk-forward windows, and drift monitoring on feature
distributions and live prediction accuracy. A model is promoted only through the same
paper-trading gates as any other strategy.

## Options module

Treat options as a volatility instrument: the module's job is to tell Mann when implied vol is
rich or cheap relative to a forecast, and which defined-risk structure expresses that view best.
Build the pricing core yourself first for understanding, then validate it against QuantLib and
py_vollib.

### Pricing core

- Black-Scholes-Merton for European options with continuous dividend yield; binomial tree
  (Cox-Ross-Rubinstein) for American early exercise.
- Implied vol solver (Newton-Raphson with a Brent fallback).
- Greeks: delta, gamma, theta, vega, rho, plus vanna and charm for the curious.
- Volatility surface: per-expiry smile fits (SVI parameterization), interpolated across expiries;
  flag arbitrage violations.

The Black-Scholes call price, where
`d1 = (ln(S/K) + (r − q + σ²/2)T) / (σ√T)` and `d2 = d1 − σ√T`:

```
C = S·e^(−qT)·N(d1) − K·e^(−rT)·N(d2)
```

### Analytics the bot produces daily

- IV rank and IV percentile per underlying (1-year lookback).
- Implied vs forecast realized vol spread (forecast from the ML vol model).
- Expected move from the at-the-money straddle, especially before earnings.
- Skew (25-delta put IV minus 25-delta call IV) and term structure slope.
- Portfolio Greeks and a scenario grid: PnL under ±1, 2, 3 sigma moves and ±5 vol points.

### Strategy playbook (defined risk only at first)

| Structure | View | When the bot suggests it |
|---|---|---|
| Covered call | Neutral to mildly bullish, IV elevated | Holding 100+ shares and IV rank above 50 |
| Cash-secured put | Bullish, willing to own stock | Want to buy a name you like at a lower price, IV rank above 50 |
| Vertical spread (debit) | Directional, IV low | Strong directional signal and IV rank below 30 |
| Vertical spread (credit) | Directional, IV high | Directional signal and IV rank above 50 |
| Iron condor | Range-bound, IV high | Low trend score, IV well above forecast RV |
| Calendar spread | Neutral, term structure steep | Front-month IV elevated vs back month |

**Options backtesting:** use your collected daily chain snapshots plus a small paid history for 3
to 5 underlyings. Fill at mid minus a fraction of the spread (never at mid), model early
assignment on short calls before ex-dividend dates, and close positions before expiry to avoid
pin risk.

## Risk management and position sizing

Risk lives in its own service that every order passes through, and strategies cannot bypass or
reconfigure it. This mirrors how trading firms separate pre-trade risk checks from strategy code,
and it's the single most important safety feature of the bot.

### Starting limits (tune later with evidence)

| Limit | Starting value | Enforced where |
|---|---|---|
| Risk per trade (distance to stop × size) | 1% of account equity | Pre-trade check |
| Max position size | 10% of equity in one name | Pre-trade check |
| Max sector exposure | 30% of equity | Pre-trade check |
| Max gross exposure | 100% of equity (no leverage) | Pre-trade check |
| Options premium at risk | 5% of equity total, defined-risk structures only | Pre-trade check |
| Daily loss limit | 2% of equity, then block new orders for the day | Kill switch |
| Strategy drawdown limit | 10% from peak, then auto-disable and alert | Strategy monitor |
| Order sanity | Price within 5% of last trade; size below 2% of average daily volume | Pre-trade check |

**Sizing methods to implement and compare:** fixed fractional (risk 1% per trade), volatility
targeting (size inversely to ATR or forecast vol so each position contributes similar risk), and
fractional Kelly (quarter Kelly at most, because estimated edges are noisy).

**Portfolio-level monitoring:** correlation between open positions, beta to SPY, aggregate
options Greeks, concentration, and historical/parametric VaR. The dashboard shows all of these
live.

**Operational risk:** a hard kill switch (one command cancels all open orders and blocks new
ones), duplicate-order protection via idempotency keys, reconciliation of internal positions vs
broker positions every few minutes, and alerts on any mismatch, stale data, or disconnected feed.

## Paper trading to live trading

Every strategy moves through four stages, and it only advances when it passes the gate. Paper
trading runs on Alpaca's paper account first because it shares the same API as live, so
promotion is a config change rather than a rewrite.

| Stage | Capital | Gate to advance |
|---|---|---|
| 1. Backtest | None | Passes walk-forward and locked test set; Deflated Sharpe above 0; survives stress periods; positive after realistic costs |
| 2. Paper | Simulated | At least 60 trading days and 30 trades; live Sharpe within half of backtest Sharpe; slippage within 1.5x the modeled value; zero reconciliation breaks |
| 3. Live, micro | $500 to $2,000 total | 60 more trading days; results consistent with paper; no risk-limit breaches; Mann reviews every trade |
| 4. Live, scaled | Increase in steps of 25%, max one step per month | Rolling 3-month results stay within the expected range; any drawdown limit hit sends it back to paper |

**Human approval flow.** The bot posts each proposed trade to a private Discord or Slack channel
with ticker, side, size, entry/stop/target, the signal that triggered it, model confidence, risk
used, and current portfolio exposure. Mann taps Approve, Modify or Reject; only approved orders
reach the broker. Every decision is logged, which later lets you measure whether your overrides
add or subtract value.

### Brokers to consider

- **Alpaca:** paper and live with one API, commission-free stocks, options trading, Python SDK.
  Default choice.
- **Interactive Brokers:** paper account, the broadest options and global coverage, more complex
  API (TWS or Client Portal). Worth learning because its API resembles what institutional systems
  deal with.
- **Schwab Trader API or Tradier:** alternatives if you prefer a mainstream broker or
  options-focused pricing.

**Regulatory note.** FINRA has approved replacing the Pattern Day Trader rule (the $25,000
minimum and four-day-trades-in-five-days limit) with an intraday margin framework, with a
12-month transition in which brokers may apply either regime. Check your broker's current rules
before day trading on margin; a cash account sidesteps the issue entirely.

## Platform architecture

Every component talks through a message bus, so adding a bot, a data feed or a new consumer means
subscribing to topics rather than rewiring services. That's what makes it possible to run many
bots across different servers and send the same data and signals to many clients at once.

> **TODO:** the architecture diagram did not survive the import. Data flows top to bottom: feeds
> land on the bus, bots subscribe and publish order intents, and nothing reaches a broker without
> passing the risk service and your approval. Fills from the execution gateway go back onto the
> bus, where the recorder, dashboard and bots all see them.

### Design principles

- **One strategy contract.** Every bot implements the same interface (`on_bar`, `on_quote`,
  `on_fill`, `on_timer`) and emits order intents, never broker orders. The same class runs in the
  backtester, paper and live.
- **Multiple bots, multiple servers.** Each bot is its own Deployment with its own config,
  resource limits and kill switch; node affinity can pin latency-sensitive bots to specific
  nodes.
- **Multiple clients.** Any consumer (dashboard, Discord bot, a friend's read-only view, a mobile
  push service) subscribes to the topics it needs. Producers never know who is listening.
- **Typed, versioned messages.** Protobuf schemas in a shared repo, with backward-compatible
  changes only.
- **Replayability.** The recorder persists every bus message, so any trading day can be replayed
  through the system for debugging or regression tests.
- **State in one place.** Positions and orders live in Postgres, owned by the execution gateway;
  bots read state, they don't keep their own copies of truth.

## Infrastructure: Linux, Docker, Kubernetes, observability

Run everything on a small Kubernetes cluster you operate yourself (k3s), managed through GitOps,
with full metrics, logs and alerting. The goal is to practice the operational habits of a trading
platform team: reproducible deploys, no changes during market hours, fast rollback, and knowing
something is wrong before it costs money.

### Hosting options (pick one to start)

- **Cheapest:** k3s on 2 to 3 cloud VMs (Oracle Cloud's Always Free ARM tier or a low-cost
  provider), or on an old laptop/mini PC at home.
- **Later:** a managed cluster (EKS, GKE or OKE) for a few weeks to learn the differences, then
  tear it down.

### Layer by layer

| Layer | Tools | What you practice |
|---|---|---|
| Linux | Ubuntu Server, systemd, cgroups, iptables/nftables, chrony for time sync | Process management, networking, resource limits, why clock accuracy matters for timestamps |
| Performance | perf, htop, strace, tcpdump, flame graphs | Finding latency and CPU hot spots in your own services |
| Containers | Docker multi-stage builds, distroless or slim images, image scanning (Trivy) | Small, secure, reproducible images |
| Orchestration | k3s, Helm charts, StatefulSets for databases, Deployments for bots, CronJobs for batch | Scheduling, resource requests/limits, rolling updates, node affinity |
| GitOps and IaC | Argo CD, Terraform for cloud resources, Kustomize overlays (dev/paper/live) | Declarative environments, drift detection, one-click rollback |
| CI/CD | GitHub Actions: lint, type-check, unit tests, backtest regression tests, image build and push | Blocking a deploy if a strategy's backtest output changes unexpectedly |
| Observability | Prometheus, Grafana, Loki for logs, OpenTelemetry tracing, Alertmanager to Discord | Dashboards for PnL, latency, fill rates, data freshness; SLOs and alerts |
| Secrets and security | External Secrets or Sealed Secrets, separate API keys for paper vs live, network policies | Never committing keys; least privilege between services |
| Reliability | Pod disruption budgets, health/readiness probes, chaos tests (kill a pod mid-session), backups of Postgres/TimescaleDB | Recovering cleanly from failures |

### Trading-specific ops rules

- Deploy freeze from 9:00 to 16:15 ET on trading days, except emergency fixes.
- Pre-market checklist job at 8:30 ET: data feeds connected, positions reconciled, risk limits
  loaded, clocks synced.
- End-of-day job: reconcile fills vs broker, compute PnL attribution, archive the day's data,
  post a summary.
- Runbooks in the repo for every alert: what it means, how to diagnose, how to fix.

## Tech stack

Python for research, strategies and ML; one performance-critical service rewritten in Rust or C++
once the system works, because that's where trading infra skills get tested.

| Area | Choice | Why |
|---|---|---|
| Research | Python 3.12, Polars, pandas, NumPy, Jupyter, vectorbt | Fast iteration, huge ecosystem |
| ML | scikit-learn, LightGBM, PyTorch, statsmodels, hmmlearn, MLflow | Covers baselines through deep models plus tracking |
| Options math | Own implementation, validated with QuantLib and py_vollib | Learn it, then trust it |
| Services | Python (FastAPI, asyncio) first; Rust or C++ for the market data handler and order book later | Correctness first, then speed where it matters |
| Messaging | NATS JetStream to start; Kafka (Redpanda) to learn the industry standard | Pub/sub between feeds, bots and consumers |
| Serialization | Protobuf or FlatBuffers for messages | Typed, versioned, compact |
| Storage | Parquet on object storage (MinIO or S3) for raw data; TimescaleDB for bars and fills; Postgres for orders and config; Redis for hot state | Each store matches its access pattern |
| Query | DuckDB over Parquet | SQL analytics on local files without a server |
| Frontend | Grafana for ops; a small React or Streamlit dashboard for trades and approvals | Fast to build |
| Infra | Docker, k3s, Helm, Argo CD, Terraform, GitHub Actions | Standard platform toolchain |
| Observability | Prometheus, Grafana, Loki, OpenTelemetry | Metrics, logs, traces |
| Broker and data | Alpaca (primary), IBKR (second), Databento, FRED, EDGAR | See data and broker sections |

## Roadmap and milestones

Five phases over about eight months, paced around school and other
commitments. The key scheduling choice is getting one simple bot onto paper trading by
mid-December, so that by spring there's a real track record to judge instead of only backtests.

> **TODO:** the phase table did not survive the import; re-add it here.

Phases overlap in practice: the options snapshot collector and the first paper bot keep running
in the background through every later phase. If a phase slips, cut scope from ML experiments
before cutting infrastructure, since the platform work is the main thing this project is for.

### First two weeks, concretely

Each item is delivered on its own `step0/<feature>` branch (see the README).

- [ ] Create the monorepo: `research/`, `services/`, `strategies/`, `infra/`, `docs/`; set up uv,
  ruff, mypy, pytest and GitHub Actions — `step0/monorepo-scaffold`
- [ ] Open an Alpaca account, generate paper API keys, pull 5 years of daily bars for 50 tickers
  into Parquet — `step0/market-data-ingest`
- [ ] Write the data validation checks and a daily ingest script; run it as a cron job on a Linux
  box or VM — `step0/market-data-ingest`
- [ ] Start the daily options chain snapshot job for 20 liquid underlyings —
  `step0/options-chain-snapshot`
- [ ] Finish Phase 0 modules 1 to 4, including the toy matching engine — `step0/matching-engine`,
  `step0/return-metrics` (notes are written by hand in `docs/phase0/`)
- [ ] Implement strategy 1 (10-month MA timing) in a notebook and reproduce Faber's headline
  result — `step0/ma-timing-strategy`

## How this maps to trading-platform engineering

Platform engineering at a trading firm centers on service lifecycle, availability, latency,
capacity, disaster recovery and automation, with Linux, Kubernetes, Docker, CI/CD, networking and
Python/Bash scripting as the core toolset. Each of those has a concrete counterpart in this
project, so you'll have run the same kinds of systems on a smaller scale.

| What the work involves | Where you practice it here |
|---|---|
| Service lifecycle: design, deploy, operate, refine | Every service goes through design doc, Helm chart, Argo CD deploy, dashboards, runbook, postmortems |
| Availability, scalability, latency, efficiency | SLOs on data freshness and order latency; load tests on the market data fan-out; profiling hot paths; a Rust/C++ rewrite of the feed handler |
| Capacity, performance, disaster recovery | Resource requests and limits tuned from Prometheus data; Postgres/TimescaleDB backups with restore drills; rebuilding the whole cluster from Terraform + Git |
| Solving mission-critical problems and automating them away | Chaos tests during paper trading hours; every incident gets an automated check or alert so it can't recur silently |
| Linux, networking, systems performance | Bare k3s nodes, nftables, chrony, tcpdump on the broker connection, perf and flame graphs |
| Kubernetes, Docker, CI/CD, IaC | The entire infrastructure layer, plus backtest regression tests in CI |
| Working with traders, quants and researchers | You play all three roles: the research stack is your internal customer, which forces platform decisions to serve real users |
| "A system breaks during peak trading hours" | Kill switch, reconciliation alerts, deploy freezes and practiced runbooks give you real stories to tell |

**Talking points this produces:** a multi-strategy platform on Kubernetes with event-driven
backtest/live parity, a market data fan-out with measured latency, pre-trade risk checks isolated
from strategy code, and an incident history with postmortems.

## Reading list and sources

Read in the order the phases need them: market structure and options first, quant methods during
backtesting, systems books during the platform phase.

| Phase | Book or resource | Why |
|---|---|---|
| 0 | Larry Harris, *Trading and Exchanges* | The market microstructure reference |
| 0 | Sheldon Natenberg, *Option Volatility and Pricing* | How options traders actually think about vol |
| 0 | John Hull, *Options, Futures, and Other Derivatives* | Pricing theory reference |
| 0 | MIT OCW 18.S096, *Topics in Mathematics with Applications in Finance* | Free lectures, some by industry practitioners |
| 1 | Ernest Chan, *Quantitative Trading* and *Algorithmic Trading* | Practical strategy building and backtesting |
| 1 | Robert Carver, *Systematic Trading* | Position sizing and combining strategies sensibly |
| 2 | Marcos López de Prado, *Advances in Financial Machine Learning* | Labeling, purged CV, meta-labeling, backtest overfitting |
| 2 | Euan Sinclair, *Volatility Trading* | Turning vol forecasts into options trades |
| 3 | Martin Kleppmann, *Designing Data-Intensive Applications* | Messaging, storage, consistency |
| 3 | Google, *Site Reliability Engineering* (free online) | SLOs, incident response, postmortems |
| 3 | Brendan Gregg, *Systems Performance* | Linux performance analysis |
| 3 | *Kubernetes Up and Running* | Practical Kubernetes |

### Sources used in this plan

- Alpaca: About Market Data API
- Alpaca: FINRA's new intraday margin rule and the end of PDT
- Databento: Usage-based pricing and credits
- Massive (Polygon.io) free plan details via Veryfront integration docs
- iVolatility Data Download
- ORATS historical data

Paper citations in the strategy table and book recommendations are from general knowledge, not
looked up.
