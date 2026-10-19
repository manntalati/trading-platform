.PHONY: sync sync-research fmt lint typecheck test check web-install web-check web-build dashboard mac-agent mac-agent-remove

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

AGENT := $(HOME)/Library/LaunchAgents/com.trading-platform.api.plist
mac-agent:       ## macOS: run tp-api (dashboard + job scheduler) at login, restarted if it stops
	mkdir -p data/logs $(HOME)/Library/LaunchAgents
	sed "s|REPO|$(CURDIR)|" infra/launchd/com.trading-platform.api.plist > $(AGENT)
	-launchctl bootout gui/$$(id -u)/com.trading-platform.api 2>/dev/null
	launchctl bootstrap gui/$$(id -u) $(AGENT)
	@echo "running: http://127.0.0.1:8000 (log: data/logs/tp-api.log)"

mac-agent-remove: ## macOS: stop it and remove it from login
	-launchctl bootout gui/$$(id -u)/com.trading-platform.api
	rm -f $(AGENT)
