"""CLI presentation; architecture operations belong in application services."""

from importlib.metadata import version
from typing import Annotated

import typer

from archdiscov.cli_analysis import analyze, findings
from archdiscov.cli_ask import ask
from archdiscov.cli_collection import collect
from archdiscov.cli_comparison import compare, comparison
from archdiscov.cli_evaluation import evaluate, evaluation
from archdiscov.cli_evidence import evidence
from archdiscov.cli_ingestion import ingest
from archdiscov.cli_mcp import mcp
from archdiscov.cli_query import query_app

app = typer.Typer(
    help="Discover AWS architecture and inspect traceable evidence.",
    add_completion=False,
    pretty_exceptions_enable=False,
)

app.add_typer(query_app, name="query")
app.command()(analyze)
app.command()(findings)
app.command()(compare)
app.command()(comparison)
app.command()(ask)
app.command()(evaluate)
app.command()(evaluation)
app.command()(mcp)
app.command()(ingest)
app.command()(evidence)
app.command()(collect)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    show_version: Annotated[
        bool,
        typer.Option("--version", help="Show the installed version and exit.", is_eager=True),
    ] = False,
) -> None:
    if show_version:
        typer.echo(f"archdiscov {version('archdiscov')}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
