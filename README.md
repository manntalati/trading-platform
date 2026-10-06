# trading-platform

A personal trading research and execution platform: a research stack (data, backtesting,
strategies, options analytics), a human-in-the-loop decision-support bot, and the platform that
runs it (message bus, Kubernetes, observability). The full plan is in
[`docs/project-plan.md`](docs/project-plan.md).

**Ground rules:** the bot proposes and a human decides; paper before real money; hard risk caps
live outside strategy code; backtests are hypotheses, not evidence.

## Layout

```
libs/core/         tp_core        shared library: config, data storage + validation, metrics, portfolio
libs/trading/      tp_trading     strategy contract, event-driven backtester, simulated execution
services/ingest/   tp_ingest      data jobs and vendor adapters
services/broker/   tp_broker      read-only brokerage sync (Fidelity via SnapTrade, Alpaca, demo)
services/api/      tp_api         dashboard API: REST + live WebSocket
apps/dashboard/                   dashboard web app (React + TypeScript + Vite)
strategies/        tp_strategies  strategy library (tp-backtest), research versions, ideas
research/          tp_research    Phase 0 exercises; notebooks/ holds jupytext .py notebooks
infra/                            deployment (systemd/cron now; Docker, k3s, Terraform later)
docs/                             plan, ADRs (docs/decisions), Phase 0 notes (docs/phase0)
```

It's a [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/): one lockfile, one
virtualenv, each package importable from the others. See
[ADR 0001](docs/decisions/0001-monorepo-layout.md).

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for you).

```bash
make sync            # uv sync --locked: all packages + dev tools into .venv
make check           # ruff lint + format check, mypy --strict, pytest (what CI runs)
make sync-research   # optional: jupyterlab, jupytext, yfinance, matplotlib, statsmodels
```

## Market data

Daily bars for the 50-symbol universe in `config/universes.toml` come from Alpaca into a local
Parquet lake (`data/`, git-ignored), with explicit split/dividend adjustment and validation on
every load. Details: [docs/data.md](docs/data.md).

```bash
cp .env.example .env                      # add Alpaca *paper* keys
uv run tp-data check                      # verify keys + connectivity
uv run tp-data bars backfill --years 5    # one-off history
uv run tp-data bars daily                 # what the scheduler runs each weekday evening
uv run tp-data bars backfill --source fake --years 1   # try it all without keys
uv run tp-data options snapshot           # today's option chains for the [options] universe
```

Scheduling (systemd timers or cron) is in [infra/](infra/README.md).

## Portfolio

Your Fidelity account, read-only through SnapTrade, measured against SPY, QQQ and 60/40.
Setup and caveats: [docs/portfolio.md](docs/portfolio.md).

```bash
uv run tp-broker link                     # one-time: connect Fidelity (read-only)
uv run tp-broker sync                     # snapshot holdings + transactions
uv run tp-broker show                     # print the latest portfolio
uv run tp-broker sync --source fake       # demo portfolio, no keys needed
uv run tp-ideas                           # rules-based research ideas, with the numbers behind each
```

How the ideas are generated: [docs/ideas.md](docs/ideas.md).

## Dashboard

A FastAPI service with live prices over WebSocket, serving a React dashboard: portfolio vs
benchmarks, ideas, strategies, options and system health. Details: [docs/dashboard.md](docs/dashboard.md).

```bash
make web-install web-build                # once: build the UI (needs Node 22+)
uv run tp-api --quotes fake               # http://127.0.0.1:8000 (API docs at /docs)
```

UI development (hot reload): [apps/dashboard/README.md](apps/dashboard/README.md).

## Research and strategies

Strategies live in `strategies/` with a written spec in [`docs/strategies/`](docs/strategies/)
before any code; notebooks in [`research/`](research/README.md) run them against the lake.
Library strategies implement one contract that both backtests and paper-trades
([docs/backtesting.md](docs/backtesting.md), [ADR 0003](docs/decisions/0003-strategy-contract.md)):

```bash
uv run tp-backtest list
uv run tp-backtest run ma-timing --start 2018-01-01      # tear sheet vs SPY; saved under data/reports/backtests
```

| # | Strategy | Spec | Status |
|---|---|---|---|
| 1 | 10-month MA timing (Faber) | [01-ma-timing.md](docs/strategies/01-ma-timing.md) | backtest |

## Branches

Work lands on feature branches named `step<phase>/<feature>`, one feature per branch, e.g.
`step0/monorepo-scaffold`. The phase number matches the roadmap in the project plan.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs `ruff check`, `ruff format --check`, `mypy` and
`pytest` on every push and pull request, after a `uv sync --locked` that fails if `uv.lock` is
stale.
