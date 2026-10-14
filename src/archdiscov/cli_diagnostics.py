"""Render safe service diagnostics without choosing command exit codes."""

import json

import typer


def json_output(data: dict) -> None:
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False))


def print_error(diagnostic: dict, *, as_json: bool) -> None:
    if as_json:
        json_output({"error": diagnostic})
        return
    typer.echo(f"Error [{diagnostic['code']}]: {diagnostic['message']}", err=True)
    if diagnostic.get("path") is not None:
        typer.echo(f"Path: {diagnostic['path']}", err=True)
    for issue in diagnostic.get("details", []):
        location = ".".join(map(str, issue["location"])) or "<snapshot>"
        typer.echo(f"  {location}: {issue['message']} ({issue['type']})", err=True)
