"""``tp-risk``: inspect the limits and operate the switches.

tp-risk status                       kill switch, disabled strategies, peaks, limits
tp-risk kill --reason "..."          block every new order (tp-paper kill also cancels open ones)
tp-risk resume                       lift the kill switch
tp-risk disable NAME --reason "..."  stop a strategy from opening risk
tp-risk enable NAME                  re-enable it (e.g. after a drawdown breach you reviewed)
"""

from __future__ import annotations

from typing import Annotated

import typer

from tp_core.config import Settings
from tp_risk.limits import Limits
from tp_risk.state import FileRiskState

app = typer.Typer(add_completion=False, no_args_is_help=True)


def _state() -> FileRiskState:
    return FileRiskState.under(Settings().data_root)


@app.command()
def status() -> None:
    """Show the kill switch, every strategy's state, and the limits in force."""
    settings = Settings()
    state = FileRiskState.under(settings.data_root).snapshot()
    kill = state["kill_switch"]
    typer.echo(f"kill switch: {'ENGAGED ' + kill['at'] + ': ' + kill['reason'] if kill else 'off'}")
    strategies = state["strategies"]
    if strategies:
        typer.echo("strategies:")
    for name, entry in sorted(strategies.items()):
        disabled = entry.get("disabled")
        peak = entry.get("peak")
        line = f"  {name:20} {'DISABLED ' + disabled['reason'] if disabled else 'enabled'}"
        if peak is not None:
            line += f"  (peak equity {peak:,.2f})"
        typer.echo(line)
    typer.echo(f"limits ({settings.risk_file}):")
    for key, value in Limits.load(settings.risk_file).describe().items():
        typer.echo(f"  {key:28} {value}")


@app.command()
def kill(reason: Annotated[str, typer.Option(help="Why; shown on every rejected order.")]) -> None:
    """Block every new order until `tp-risk resume`."""
    _state().set_kill_switch(reason)
    typer.echo("kill switch ENGAGED: no new orders. Open broker orders are untouched; use "
               "`tp-paper kill` to cancel them too.")  # fmt: skip


@app.command()
def resume() -> None:
    """Lift the kill switch."""
    _state().set_kill_switch(None)
    typer.echo("kill switch off")


@app.command()
def disable(
    strategy: str, reason: Annotated[str, typer.Option(help="Why it is being stopped.")]
) -> None:
    """Stop a strategy from opening new risk (it may still sell what it holds)."""
    _state().set_disabled(strategy, reason)
    typer.echo(f"{strategy} disabled")


@app.command()
def enable(strategy: str) -> None:
    """Re-enable a strategy and reset its drawdown peak to the next equity it reports."""
    state = _state()
    state.set_disabled(strategy, None)
    state.set_peak(strategy, None)
    typer.echo(f"{strategy} enabled; drawdown is measured from its next equity")


def main() -> None:
    app()
