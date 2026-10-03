# ADR 0001: uv workspace monorepo and step branches

- Status: accepted
- Date: 2026-10-03

## Context

The project will grow from notebooks into many services (ingest jobs, strategy bots, a risk
service, an execution gateway) that each ship as their own container, while sharing data models,
validation and metrics. Research code must use exactly the same library code as the services, so
backtest/live parity is not undermined by copy-paste.

## Decision

- One git repo, organised as a **uv workspace**. Every top-level area is an installable package
  with a `src/` layout and its own `pyproject.toml`:

  | Directory | Package | Role |
  |---|---|---|
  | `libs/core` | `tp_core` | Shared library: config, data storage/validation, metrics |
  | `services/ingest` | `tp_ingest` | Data jobs and vendor adapters (future CronJob image) |
  | `strategies` | `tp_strategies` | Strategy library |
  | `research` | `tp_research` | Learning exercises + jupytext notebooks under `research/notebooks/` |
  | `infra` | — | systemd/cron now; Docker, Helm, Terraform later |
  | `docs` | — | Plan, ADRs, Phase 0 notes, runbooks |

  Packages are prefixed `tp_` so they never shadow a PyPI distribution.
- A single lockfile (`uv.lock`) pins every dependency across the workspace; CI installs with
  `uv sync --locked`, so a drifting lockfile fails the build.
- Tooling is configured once at the root: ruff (lint + format), mypy `strict` over every `src/`
  tree, pytest in importlib mode over every `tests/` tree.
- Notebooks are jupytext percent-format `.py` files: reviewable diffs, linted by ruff, no
  committed outputs.
- Work is delivered on feature branches named `step<phase>/<feature>` (e.g.
  `step0/market-data-ingest`), one feature per branch.

## Consequences

- A service image can install just its package plus `tp_core`.
- Adding a package means adding a directory under an existing glob (or a new member entry) and
  a path to `[tool.mypy].files` / `[tool.pytest.ini_options].testpaths`.
- Notebook tooling (Jupyter, yfinance, statsmodels) sits in the `research` dependency group and
  stays out of CI and service images.
