"""``tp-data``: command-line entry point for the ingest jobs.

Exit codes (so systemd/cron can alert on failures):
    0  success (warnings allowed)
    1  validation found errors (rows were quarantined)
    2  configuration problem (missing keys, bad universe file)
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, NoReturn

import typer

from tp_core.config import MissingCredentialsError, Settings, load_universes
from tp_core.storage import Lake
from tp_core.validate import ValidationReport
from tp_ingest.jobs import bars as bars_jobs
from tp_ingest.jobs import options as options_jobs
from tp_ingest.sources.base import FullSource

log = logging.getLogger("tp_ingest")

app = typer.Typer(help="Market data ingest jobs.", no_args_is_help=True, add_completion=False)
bars_app = typer.Typer(help="Daily stock/ETF bars.", no_args_is_help=True)
app.add_typer(bars_app, name="bars")
options_app = typer.Typer(help="Option chain snapshots.", no_args_is_help=True)
app.add_typer(options_app, name="options")


class SourceName(StrEnum):
    alpaca = "alpaca"
    fake = "fake"


SourceOpt = Annotated[
    SourceName,
    typer.Option(help="Data source. `fake` is synthetic data for dry runs without API keys."),
]
SymbolsOpt = Annotated[
    str | None,
    typer.Option(help="Comma-separated symbols. Defaults to the [bars] universe."),
]


def now_utc() -> datetime:
    """Indirection so tests can freeze time."""
    return datetime.now(UTC)


def make_source(name: SourceName, settings: Settings) -> FullSource:
    if name is SourceName.fake:
        from tp_ingest.sources.fake import FakeSource

        return FakeSource()
    from tp_ingest.sources.alpaca import AlpacaSource

    try:
        return AlpacaSource.from_settings(settings)
    except MissingCredentialsError as exc:
        _config_error(exc)


def _config_error(exc: Exception) -> NoReturn:
    typer.echo(f"configuration error: {exc}", err=True)
    raise typer.Exit(code=2)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.Formatter.converter = time.gmtime  # log timestamps in UTC


def _symbols(settings: Settings, symbols: str | None) -> list[str]:
    if symbols:
        return [s.strip().upper() for s in symbols.split(",") if s.strip()]
    try:
        return list(load_universes(settings.universes_file).bars)
    except (OSError, ValueError) as exc:
        _config_error(exc)


def _finish(report: ValidationReport) -> None:
    typer.echo(report.summary())
    if not report.ok:
        for issue in report.errors[:20]:
            typer.echo(f"  ERROR {issue.symbol} {issue.session} {issue.check}: {issue.detail}")
        raise typer.Exit(code=1)


@app.callback()
def main_callback(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    _setup_logging(verbose)


@app.command()
def check(source: SourceOpt = SourceName.alpaca) -> None:
    """Verify credentials and connectivity, and show where data will be written."""
    settings = Settings()
    typer.echo(f"data root: {settings.data_root.resolve()}")
    typer.echo(f"bars feed: {settings.bars_feed}")
    if source is SourceName.fake:
        typer.echo("source: fake (no network)")
        return
    from tp_ingest.sources.alpaca import AlpacaSource, describe_connection

    try:
        alpaca = AlpacaSource.from_settings(settings)
    except MissingCredentialsError as exc:
        _config_error(exc)
    typer.echo(json.dumps(describe_connection(alpaca), indent=2, default=str))
    end = now_utc().date()
    sample = alpaca.daily_bars(["SPY"], end - timedelta(days=10), end)
    typer.echo(f"SPY daily bars, last 10 days: {len(sample)} (latest {sample['timestamp'].max()})")


@bars_app.command("backfill")
def bars_backfill(
    years: Annotated[int, typer.Option(min=1, max=11)] = bars_jobs.DEFAULT_BACKFILL_YEARS,
    symbols: SymbolsOpt = None,
    source: SourceOpt = SourceName.alpaca,
) -> None:
    """Fetch N years of daily bars + corporate actions, then rebuild the clean table."""
    settings = Settings()
    result = bars_jobs.run_backfill(
        Lake(settings.data_root),
        make_source(source, settings),
        _symbols(settings, symbols),
        now=now_utc(),
        years=years,
    )
    typer.echo(f"run {result.run_id}: {result.bars_fetched} bars {result.start}..{result.end}")
    _finish(result.report)


@bars_app.command("daily")
def bars_daily(symbols: SymbolsOpt = None, source: SourceOpt = SourceName.alpaca) -> None:
    """Append the latest sessions (with overlap), backfill new symbols, rebuild clean."""
    settings = Settings()
    result = bars_jobs.run_daily(
        Lake(settings.data_root),
        make_source(source, settings),
        _symbols(settings, symbols),
        now=now_utc(),
    )
    typer.echo(f"run {result.run_id}: {result.bars_fetched} bars {result.start}..{result.end}")
    _finish(result.report)


@options_app.command("snapshot")
def options_snapshot(
    underlyings: Annotated[
        str | None,
        typer.Option(help="Comma-separated underlyings. Defaults to the [options] universe."),
    ] = None,
    max_dte: Annotated[
        int | None, typer.Option(min=0, help="Skip expirations further out than this.")
    ] = None,
    source: SourceOpt = SourceName.alpaca,
) -> None:
    """Store today's full chain (quotes, IV, greeks, open interest) for each underlying."""
    settings = Settings()
    try:
        universes = load_universes(settings.universes_file)
    except (OSError, ValueError) as exc:
        _config_error(exc)
    symbols = (
        [s.strip().upper() for s in underlyings.split(",") if s.strip()]
        if underlyings
        else list(universes.options_underlyings)
    )
    result = options_jobs.run_snapshot(
        Lake(settings.data_root),
        make_source(source, settings),
        symbols,
        now=now_utc(),
        max_dte=universes.options_max_dte if max_dte is None else max_dte,
    )
    typer.echo(result.summary())
    for failure in result.failures:
        typer.echo(f"  FAILED {failure.underlying}: {failure.error}")
    if not result.ok:
        raise typer.Exit(code=1)


@bars_app.command("rebuild")
def bars_rebuild() -> None:
    """Rebuild the clean table from the raw zone only (no network)."""
    from tp_core.bars import build_clean_bars

    settings = Settings()
    _finish(build_clean_bars(Lake(settings.data_root), now=now_utc()))


def main() -> None:
    app()
