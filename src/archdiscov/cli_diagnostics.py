"""Render safe service diagnostics without choosing command exit codes."""

import json
from dataclasses import asdict
from typing import NoReturn

import typer

from .services import (
    EvidenceNotFoundError,
    SnapshotConflictError,
    SnapshotError,
    SnapshotIOError,
    SnapshotJSONError,
    SnapshotValidationError,
)


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


def snapshot_exit_code(error: SnapshotError) -> int:
    """Stable exit code for a snapshot-level failure."""
    if isinstance(error, SnapshotIOError):
        return 1
    if isinstance(error, SnapshotConflictError):
        return 3
    if isinstance(error, EvidenceNotFoundError):
        return 4
    return 2


def fail_snapshot_error(error: SnapshotError, as_json: bool) -> NoReturn:
    """Print a safe snapshot diagnostic and exit with its stable code."""
    diagnostic = {"code": error.code, "message": str(error)}
    if error.path is not None:
        diagnostic["path"] = str(error.path)
    if isinstance(error, SnapshotValidationError):
        diagnostic["details"] = [asdict(issue) for issue in error.details]
    if isinstance(error, SnapshotJSONError):
        diagnostic.update(line=error.line, column=error.column)
    print_error(diagnostic, as_json=as_json)
    raise typer.Exit(snapshot_exit_code(error))
