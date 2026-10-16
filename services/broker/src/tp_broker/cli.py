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
from tp_broker.jobs import check_sync, render_check, run_sync
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
    reconnect: Annotated[
        bool,
        typer.Option(
            help="Repair the existing connection (after it was disabled: a changed password, an "
            "expired session) instead of adding a new one, so the account keeps its id."
        ),
    ] = False,
) -> None:
    """Print a SnapTrade Connection Portal link to connect Fidelity read-only (valid 5 min)."""
    from tp_broker.snaptrade import SdkSnapTradeApi, SnapTradeSource

    try:
        client_id, key = Settings().require_snaptrade_keys()
    except MissingCredentialsError as exc:
        _config_error(exc)
    api = SdkSnapTradeApi(client_id, key)
    target = None
    if reconnect:
        connections = SnapTradeSource(api).connections()
        disabled = [c for c in connections.values() if c.get("disabled")]
        candidates = disabled or list(connections.values())
        if len(candidates) != 1:
            names = ", ".join(str((c.get("brokerage") or {}).get("name")) for c in candidates)
            typer.echo(
                f"{len(candidates)} connections ({names or 'none'}): can't tell which to repair; "
                "fix it from https://dashboard.snaptrade.com or run without --reconnect",
                err=True,
            )
            raise typer.Exit(code=2)
        target = str(candidates[0]["id"])
        state = "disabled" if candidates[0].get("disabled") else "active"
        typer.echo(f"reconnecting the {state} connection; log in to the broker in this page:")
    typer.echo(api.connection_portal_url(broker, reconnect=target))


@app.command()
def sync(
    source: SourceOpt = BrokerName.snaptrade,
    refresh: Annotated[
        bool,
        typer.Option(
            help="First have SnapTrade pull fresh positions from Fidelity (otherwise up to a day "
            "old). SnapTrade charges a small fee per refresh; see its billing page. On its "
            "real-time plan the data is already live, so the refresh is skipped."
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
    for warning in result.warnings:
        typer.echo(f"WARNING {warning}")


@app.command()
def check(
    source: SourceOpt = BrokerName.snaptrade,
    days: Annotated[int, typer.Option(help="How far back to compare.")] = 45,
) -> None:
    """Why trades might be missing: compare SnapTrade's transactions with the lake's, and show
    the connection's state and how current its data is. Reads only (no refresh, no fees)."""
    settings = Settings()
    now = now_utc()
    result = check_sync(Lake(settings.data_root), make_source(source, settings), now=now, days=days)
    typer.echo(render_check(result, now))


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
