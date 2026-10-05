"""``tp-ideas``: print today's rules-based research ideas for the synced portfolio."""

from __future__ import annotations

from typing import Annotated

import typer

from tp_core.config import Settings
from tp_core.portfolio import Classifier
from tp_core.storage import Lake
from tp_strategies.ideas import ideas_from_lake

app = typer.Typer(add_completion=False)
ICONS = {"attention": "!!", "consider": " ?", "info": " i"}


@app.command()
def main_command(
    kind: Annotated[
        str | None, typer.Option(help="Only one kind: holding, portfolio, strategy, candidate.")
    ] = None,
) -> None:
    """Print ideas, most urgent first, each with the numbers behind it."""
    settings = Settings()
    report = ideas_from_lake(
        Lake(settings.data_root), Classifier.load(settings.classifications_file)
    )
    typer.echo(f"as of {report.as_of}: {report.disclaimer}\n")
    for idea in report.ideas:
        if kind and idea.kind != kind:
            continue
        typer.echo(f"{ICONS[idea.severity]} [{idea.kind}] {idea.title}")
        typer.echo(f"     {idea.summary}")
        for line in idea.rationale:
            typer.echo(f"     - {line}")
        typer.echo("")


def main() -> None:
    app()
