"""``tp-broker``: link and sync brokerage accounts (read-only).

Exit codes: 0 ok, 2 configuration problem (missing keys).
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, NoReturn

import pandas as pd
import typer

from tp_broker.base import BrokerSource
from tp_broker.jobs import run_sync
from tp_broker.sources import open_source
from tp_core.config import MissingCredentialsError, Settings
from tp_core.portfolio import Classifier, holdings_table, latest_snapshot
from tp_core.storage import Lake

app = typer.Typer(
    help="Read-only brokerage connections.", no_args_is_help=True, add_completion=False
)


class BrokerName(StrEnum):
    snaptrade = "snaptrade"
    alpaca = "alpaca"
    fake = "fake"


SourceOpt = Annotated[
    BrokerName,
    typer.Option(help="snaptrade = Fidelity via SnapTrade; alpaca = Alpaca account; fake = demo."),
]


def now_utc() -> datetime:
    return datetime.now(UTC)


def _config_error(exc: Exception) -> NoReturn:
    typer.echo(f"configuration error: {exc}", err=True)
    raise typer.Exit(code=2)


def make_source(name: BrokerName, settings: Settings) -> BrokerSource:
    try:
        return open_source(name.value, settings)
    except MissingCredentialsError as exc:
        _config_error(exc)


@app.callback()
def main_callback(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.Formatter.converter = time.gmtime


@app.command()
def link(
    broker: Annotated[str, typer.Option(help="SnapTrade broker slug.")] = "FIDELITY",
) -> None:
    """Print a SnapTrade Connection Portal link to connect Fidelity read-only (valid 5 min)."""
    from tp_broker.snaptrade import SdkSnapTradeApi

    try:
        client_id, key = Settings().require_snaptrade_keys()
    except MissingCredentialsError as exc:
        _config_error(exc)
    typer.echo(SdkSnapTradeApi(client_id, key).connection_portal_url(broker))


@app.command()
def sync(
    source: SourceOpt = BrokerName.snaptrade,
    refresh: Annotated[
        bool,
        typer.Option(
            help="First have SnapTrade pull fresh positions from Fidelity (otherwise up to a day "
            "old). SnapTrade charges a small fee per refresh; see its billing page."
        ),
    ] = False,
) -> None:
    """Snapshot balances and positions, and fetch new transactions, into the lake."""
    settings = Settings()
    result = run_sync(
        Lake(settings.data_root), make_source(source, settings), now=now_utc(), refresh=refresh
    )
    if result.refresh:
        typer.echo(f"refresh: {result.refresh}")
    typer.echo(
        f"{result.source}: {result.accounts} accounts, {result.holdings} holdings, "
        f"{result.activities} activities (run {result.run_id})"
    )


@app.command()
def show() -> None:
    """Print the latest synced portfolio."""
    settings = Settings()
    snap = latest_snapshot(Lake(settings.data_root))
    if snap.empty:
        typer.echo("no portfolio synced yet: run `tp-broker sync`")
        raise typer.Exit(code=0)
    table = holdings_table(snap, Classifier.load(settings.classifications_file))
    with pd.option_context("display.width", 140, "display.max_columns", 20):
        typer.echo(table[["symbol", "kind", "quantity", "market_value", "weight", "sector"]])
    typer.echo(f"total value: {snap.total_value:,.2f}")


def main() -> None:
    app()
