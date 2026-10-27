"""``tp-api``: run the dashboard API (and the built dashboard) with uvicorn."""

from __future__ import annotations

import logging
import socket
import urllib.request
from typing import Annotated

import typer
import uvicorn

from tp_api.app import QuoteMode, create_app
from tp_api.dashboard import ensure_built
from tp_core.config import Settings

app = typer.Typer(add_completion=False)


@app.command()
def serve(
    host: Annotated[
        str, typer.Option(help="Bind address. Keep localhost unless a token is set.")
    ] = ("127.0.0.1"),
    port: Annotated[int, typer.Option()] = 8000,
    quotes: Annotated[
        QuoteMode, typer.Option(help="Live prices: alpaca (IEX stream), fake (demo) or off.")
    ] = QuoteMode.alpaca,
    build: Annotated[
        bool, typer.Option(help="Rebuild the dashboard first if its source changed (needs Node).")
    ] = True,
    jobs: Annotated[
        bool,
        typer.Option(
            help="Run the data jobs and recommendations on their schedule while the server is up "
            "(see tp-jobs)."
        ),
    ] = True,
) -> None:
    """Serve the API on http://HOST:PORT (the dashboard too, once built)."""
    settings = Settings()
    if host not in {"127.0.0.1", "localhost", "::1"} and settings.dashboard_token is None:
        raise typer.BadParameter(
            "refusing to listen beyond localhost without TP_DASHBOARD_TOKEN: the dashboard shows "
            "your brokerage holdings",
            param_hint="--host",
        )
    taken = already_serving(host, port)
    if taken is not None:
        typer.echo(taken, err=True)
        raise typer.Exit(1)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if build:
        ensure_built()
    uvicorn.run(
        create_app(settings, quotes=quotes, jobs=jobs), host=host, port=port, log_level="info"
    )


def already_serving(host: str, port: int) -> str | None:
    """Why this server can't start on ``host:port``, if something is listening there: most
    often another tp-api (the Mac agent, or one started earlier), which would also hold
    Alpaca's single live price stream."""
    try:
        with socket.create_connection((host, port), timeout=0.5):
            pass
    except OSError:
        return None
    url = f"http://{host}:{port}"
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # it's this machine
    try:
        with direct.open(f"{url}/api/health", timeout=2) as response:
            ours = response.status == 200
    except (OSError, ValueError):
        ours = False
    if not ours:
        return f"port {port} is taken by another program: use another, e.g. --port {port + 1}"
    return (
        f"tp-api is already running at {url}: open that.\n"
        "It's usually the Mac agent (make mac-agent), which starts at login and restarts itself; "
        "after pulling new code, `make mac-agent` restarts it with the new code.\n"
        "To run it by hand instead: `make mac-agent-remove` (or stop the other tp-api), then "
        "run this again. Two servers would also fight over Alpaca's one live price stream "
        '("connection limit exceeded").'
    )


def main() -> None:
    app()
