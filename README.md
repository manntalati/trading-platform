# trading-platform

A personal trading research and execution platform: a research stack (data, backtesting,
strategies, options analytics), a human-in-the-loop decision-support bot, and the platform that
runs it (message bus, Kubernetes, observability). The full plan is in
[`docs/project-plan.md`](docs/project-plan.md).

**Ground rules:** the bot proposes and a human decides; paper before real money; hard risk caps
live outside strategy code; backtests are hypotheses, not evidence.

## Layout

```
libs/core/         tp_core        shared library: config, data storage + validation, metrics
services/ingest/   tp_ingest      data jobs and vendor adapters
strategies/        tp_strategies  strategy library
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

## Branches

Work lands on feature branches named `step<phase>/<feature>`, one feature per branch, e.g.
`step0/monorepo-scaffold`. The phase number matches the roadmap in the project plan.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs `ruff check`, `ruff format --check`, `mypy` and
`pytest` on every push and pull request, after a `uv sync --locked` that fails if `uv.lock` is
stale.
