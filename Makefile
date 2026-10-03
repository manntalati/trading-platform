.PHONY: sync sync-research fmt lint typecheck test check

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
