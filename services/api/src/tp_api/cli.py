"""``tp-api``: run the dashboard API (and the built dashboard) with uvicorn."""

from __future__ import annotations

import logging
from typing import Annotated

import typer
import uvicorn

from tp_api.app import QuoteMode, create_app
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
) -> None:
    """Serve the API on http://HOST:PORT (the dashboard too, once built)."""
    settings = Settings()
    if host not in {"127.0.0.1", "localhost", "::1"} and settings.dashboard_token is None:
        raise typer.BadParameter(
            "refusing to listen beyond localhost without TP_DASHBOARD_TOKEN: the dashboard shows "
            "your brokerage holdings",
            param_hint="--host",
        )
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    uvicorn.run(create_app(settings, quotes=quotes), host=host, port=port, log_level="info")


def main() -> None:
    app()
