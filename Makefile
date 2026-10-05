.PHONY: sync sync-research fmt lint typecheck test check web-install web-check web-build dashboard

sync:            ## install all workspace packages + dev tools
	uv sync --locked

sync-research:   ## also install notebook tooling (jupyterlab, yfinance, ...)
	uv sync --locked --group research

fmt:             ## auto-format and auto-fix lint
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

test:
	uv run pytest

check: lint typecheck test   ## everything CI runs

web-install:     ## install the dashboard's npm dependencies
	cd apps/dashboard && npm ci

web-check:       ## dashboard type check + tests
	cd apps/dashboard && npm run typecheck && npm test

web-build:       ## bundle the dashboard into apps/dashboard/dist (served by tp-api)
	cd apps/dashboard && npm run build

dashboard: web-build   ## build the UI, then serve it with simulated live prices
	uv run tp-api --quotes fake
