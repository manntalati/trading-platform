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
from typing import Annotated, NoReturn, cast

import typer

from tp_core.config import MissingCredentialsError, Settings, load_universes
from tp_core.portfolio import held_symbols
from tp_core.storage import Lake
from tp_core.validate import ValidationReport
from tp_ingest.jobs import bars as bars_jobs
from tp_ingest.jobs import fundamentals as fundamentals_jobs
from tp_ingest.jobs import intraday as intraday_jobs
from tp_ingest.jobs import macro as macro_jobs
from tp_ingest.jobs import options as options_jobs
from tp_ingest.sources.base import FullSource

log = logging.getLogger("tp_ingest")

app = typer.Typer(help="Market data ingest jobs.", no_args_is_help=True, add_completion=False)
bars_app = typer.Typer(help="Daily stock/ETF bars.", no_args_is_help=True)
app.add_typer(bars_app, name="bars")
options_app = typer.Typer(help="Option chain snapshots.", no_args_is_help=True)
app.add_typer(options_app, name="options")
macro_app = typer.Typer(
    help="Macro series: VIX and Treasury yields (FRED, Cboe, the Treasury).", no_args_is_help=True
)
app.add_typer(macro_app, name="macro")


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
    """Explicit ``--symbols``, else the [bars] universe plus everything in the synced portfolio
    (so holdings outside the universe get price history for the portfolio analytics)."""
    if symbols:
        return [s.strip().upper() for s in symbols.split(",") if s.strip()]
    try:
        universe = list(load_universes(settings.universes_file).bars)
    except (OSError, ValueError) as exc:
        _config_error(exc)
    extra = [s for s in held_symbols(Lake(settings.data_root)) if s not in universe]
    if extra:
        log.info("adding %d portfolio holding(s) to the bars universe: %s", len(extra), extra)
    return universe + extra


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
        typer.Option(
            help="Comma-separated underlyings. Defaults to the [options] universe plus holdings."
        ),
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
    if underlyings:
        symbols = [s.strip().upper() for s in underlyings.split(",") if s.strip()]
    else:  # the options universe, plus what you hold (for rolls and covered calls)
        held = held_symbols(Lake(settings.data_root))
        symbols = list(dict.fromkeys([*universes.options_underlyings, *held]))
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


class MacroSourceName(StrEnum):
    auto = "auto"
    fred_api = "fred-api"
    publishers = "publishers"
    fred_csv = "fred-csv"
    fake = "fake"


MacroSourceOpt = Annotated[
    MacroSourceName,
    typer.Option(
        help="`auto`: the FRED API if FRED_API_KEY is set, else Cboe and the Treasury, with "
        "FRED's download as a last resort. `fake`: synthetic, offline."
    ),
]


def _macro_source(name: MacroSourceName, settings: Settings) -> macro_jobs.MacroSource:
    from tp_ingest.sources.macro import (
        AutoMacroSource,
        CboeSource,
        FakeMacroSource,
        FredApiSource,
        FredCsvSource,
        TreasurySource,
    )

    key = settings.fred_api_key.get_secret_value() if settings.fred_api_key else None
    if name is MacroSourceName.fake:
        return FakeMacroSource()
    if name is MacroSourceName.fred_api:
        if not key:
            _config_error(MissingCredentialsError("FRED_API_KEY not set (free from FRED)"))
        return AutoMacroSource({"fred-api": FredApiSource(key)}, ["fred-api"])
    if name is MacroSourceName.publishers:
        return AutoMacroSource({"cboe": CboeSource(), "treasury": TreasurySource()}, ["publisher"])
    if name is MacroSourceName.fred_csv:
        return AutoMacroSource({"fred-csv": FredCsvSource()}, ["fred-csv"])
    return AutoMacroSource.default(key)


def _macro_finish(result: macro_jobs.MacroJobResult) -> None:
    typer.echo(result.summary())
    if not result.ok:
        raise typer.Exit(code=1)


@macro_app.command("daily")
def macro_daily(source: MacroSourceOpt = MacroSourceName.auto) -> None:
    """Download the last 30 days of VIX and Treasury yields and rebuild the clean table."""
    settings = Settings()
    _macro_finish(
        macro_jobs.run_macro(
            Lake(settings.data_root), _macro_source(source, settings), now=now_utc()
        )
    )


@macro_app.command("backfill")
def macro_backfill(source: MacroSourceOpt = MacroSourceName.auto) -> None:
    """Download every series from 1990 (run once; under a minute)."""
    settings = Settings()
    _macro_finish(
        macro_jobs.run_backfill(
            Lake(settings.data_root), _macro_source(source, settings), now=now_utc()
        )
    )


class SecSourceName(StrEnum):
    sec = "sec"
    fake = "fake"


news_app = typer.Typer(
    help="News headlines (Alpaca's feed) and their sentiment (scored by Claude).",
    no_args_is_help=True,
)
app.add_typer(news_app, name="news")


def _news_symbols(settings: Settings, symbols: str | None) -> list[str]:
    wanted = _symbols(settings, symbols)
    if symbols:
        return wanted
    try:
        research = list(load_universes(settings.universes_file).research)
    except (OSError, ValueError):
        research = []
    return list(dict.fromkeys([*wanted, *research]))


@news_app.command("fetch")
def news_fetch(
    source: SourceOpt = SourceName.alpaca,
    symbols: SymbolsOpt = None,
    days: Annotated[int, typer.Option(help="How many days back to fetch.")] = 3,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Fetch the latest headlines for the bars universe, your research list and holdings."""
    from tp_ingest.jobs.news import run_news
    from tp_ingest.sources.base import NewsSource

    _setup_logging(verbose)
    settings = Settings()
    feed = cast(NewsSource, make_source(source, settings))
    result = run_news(
        Lake(settings.data_root), feed, _news_symbols(settings, symbols), now=now_utc(), days=days
    )
    typer.echo(result.summary())


@news_app.command("score")
def news_score(
    limit: Annotated[int, typer.Option(help="Most headlines to score in this run.")] = 400,
    days: Annotated[int, typer.Option(help="Score unscored headlines from this many days.")] = 10,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Score unscored headlines with Claude (-1 bad news for the stock .. +1 good). Needs
    ANTHROPIC_API_KEY; costs API tokens (about 40 headlines a request)."""
    from tp_ingest.jobs.news import run_scoring
    from tp_ingest.sentiment import ClaudeScorer

    _setup_logging(verbose)
    settings = Settings()
    try:
        key = settings.require_anthropic_key()
    except MissingCredentialsError as exc:
        _config_error(exc)
    scorer = ClaudeScorer(key, settings.news_model or settings.ask_model)
    result = run_scoring(Lake(settings.data_root), scorer, now=now_utc(), days=days, limit=limit)
    typer.echo(result.summary())
    if result.failed_batches and not result.scored:
        raise typer.Exit(code=1)


@news_app.command("show")
def news_show(
    symbol: Annotated[str, typer.Argument(help="Ticker.")],
    days: Annotated[int, typer.Option(help="Days back.")] = 7,
) -> None:
    """A ticker's recent headlines with their scores."""
    from tp_core.news import load_news, load_scores

    lake = Lake(Settings().data_root)
    since = (now_utc() - timedelta(days=days)).date()
    news = load_news(lake, since=since)
    mine = news[news["symbols"].str.upper().str.split(",").apply(lambda s: symbol.upper() in s)]
    scores = load_scores(lake).set_index("id")["score"]
    if mine.empty:
        typer.echo(f"no headlines for {symbol.upper()} in the last {days} days")
        return
    for when, key, headline in zip(mine["created_at"], mine["id"], mine["headline"], strict=True):
        score = scores.get(key)
        mark = f"{score:+.1f}" if score is not None else "  ? "
        typer.echo(f"{when:%Y-%m-%d %H:%M}  {mark}  {headline}")


@app.command()
def fundamentals(
    symbols: SymbolsOpt = None,
    source: Annotated[
        SecSourceName, typer.Option(help="`sec` (EDGAR, no key) or `fake` (synthetic, offline).")
    ] = SecSourceName.sec,
) -> None:
    """Company facts (EPS, net income, revenue, gross profit, equity, assets, shares) and
    earnings-release dates from SEC EDGAR, for the bars universe, your research list and holdings
    (funds have none). Needs TP_SEC_USER_AGENT (a name and an email address)."""
    from tp_ingest.sources.sec import FakeSecSource, SecSource

    settings = Settings()
    wanted = _symbols(settings, symbols)
    if not symbols:
        try:
            research = list(load_universes(settings.universes_file).research)
        except (OSError, ValueError):
            research = []
        wanted = list(dict.fromkeys([*wanted, *research]))
    if source is SecSourceName.fake:
        sec: fundamentals_jobs.FundamentalsSource = FakeSecSource(wanted)
    else:
        try:
            sec = SecSource(settings.require_sec_user_agent())
        except MissingCredentialsError as exc:
            _config_error(exc)
    result = fundamentals_jobs.run_fundamentals(
        Lake(settings.data_root), sec, wanted, now=now_utc()
    )
    typer.echo(result.summary())
    if not result.ok:
        raise typer.Exit(code=1)


@bars_app.command("intraday")
def bars_intraday(
    symbols: Annotated[str, typer.Option(help="Comma-separated symbols.")] = "SPY,QQQ",
    days: Annotated[int, typer.Option(min=1, max=2500, help="Sessions back from today.")] = 60,
    minutes: Annotated[int, typer.Option(min=1, max=60, help="Bar length in minutes.")] = 5,
    source: SourceOpt = SourceName.alpaca,
) -> None:
    """Intraday bars (regular hours) for intraday research, e.g. the opening-range breakout."""
    settings = Settings()
    names = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    result = intraday_jobs.run_intraday(
        Lake(settings.data_root),
        make_source(source, settings),
        names,
        now=now_utc(),
        days=days,
        minutes=minutes,
    )
    typer.echo(result.summary())


@bars_app.command("rebuild")
def bars_rebuild() -> None:
    """Rebuild the clean table from the raw zone only (no network)."""
    from tp_core.bars import build_clean_bars

    settings = Settings()
    _finish(build_clean_bars(Lake(settings.data_root), now=now_utc()))


def main() -> None:
    app()
