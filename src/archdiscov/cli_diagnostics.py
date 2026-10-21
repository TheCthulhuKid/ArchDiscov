"""Render safe service diagnostics without choosing command exit codes."""

import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, NoReturn

import typer
from pydantic import BaseModel

from .assistant.config import ProviderConfigError, ProviderConfigErrorCode
from .assistant.fake import FakeScriptError, FakeScriptErrorCode
from .services import (
    AnalysisValidationError,
    AnalyzerError,
    EvidenceNotFoundError,
    SnapshotConflictError,
    SnapshotError,
    SnapshotIOError,
    SnapshotJSONError,
    SnapshotValidationError,
)


def json_output(data: dict) -> None:
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False))


def emit_model(model: BaseModel, **context: str) -> None:
    """Emit a result model's JSON preceded by the paths it was produced from."""
    json_output({**context, **model.model_dump(mode="json")})


def _diagnostic(
    code: str, message: str, path: Path | None = None, issues: Iterable[Any] | None = None
) -> dict[str, Any]:
    diagnostic: dict[str, Any] = {"code": code, "message": message}
    if path is not None:
        diagnostic["path"] = str(path)
    if issues is not None:
        diagnostic["details"] = [asdict(issue) for issue in issues]
    return diagnostic


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
    issues = error.details if isinstance(error, SnapshotValidationError) else None
    diagnostic = _diagnostic(error.code, str(error), error.path, issues)
    if isinstance(error, SnapshotJSONError):
        diagnostic.update(line=error.line, column=error.column)
    print_error(diagnostic, as_json=as_json)
    raise typer.Exit(snapshot_exit_code(error))


def fail_analysis_error(error: AnalysisValidationError | AnalyzerError, as_json: bool) -> NoReturn:
    """Print a safe analysis diagnostic; both kinds are request/data errors (exit 2)."""
    if isinstance(error, AnalyzerError):
        diagnostic = _diagnostic(error.code.value, error.message)
    else:
        diagnostic = _diagnostic(error.code, str(error), error.path, error.details)
    print_error(diagnostic, as_json=as_json)
    raise typer.Exit(2)


@contextmanager
def safe_failures(snapshot_path: Path, as_json: bool) -> Iterator[None]:
    """Turn expected service failures into safe diagnostics and stable exit codes.

    A snapshot failure without a path of its own names `snapshot_path`.
    """
    try:
        yield
    except SnapshotError as error:
        if error.path is None:
            error.path = snapshot_path
        fail_snapshot_error(error, as_json)
    except (AnalyzerError, AnalysisValidationError) as error:
        fail_analysis_error(error, as_json)


# An unreadable file is an I/O error; every other configuration or script error is invalid input.
_UNREADABLE = {ProviderConfigErrorCode.UNREADABLE, FakeScriptErrorCode.UNREADABLE}


def assistant_exit_code(error: ProviderConfigError | FakeScriptError) -> int:
    return 1 if error.code in _UNREADABLE else 2


def fail_assistant_error(error: ProviderConfigError | FakeScriptError, as_json: bool) -> NoReturn:
    """Print a safe provider-configuration or fake-script diagnostic and exit."""
    diagnostic = _diagnostic(error.code.value, str(error), error.path, error.details or None)
    for name in ("variable", "kind"):
        if (value := getattr(error, name, None)) is not None:
            diagnostic[name] = str(value)
    print_error(diagnostic, as_json=as_json)
    raise typer.Exit(assistant_exit_code(error))


@contextmanager
def safe_assistant_failures(snapshot_path: Path, as_json: bool) -> Iterator[None]:
    """`safe_failures`, plus provider-configuration and fake-script failures."""
    with safe_failures(snapshot_path, as_json):
        try:
            yield
        except (ProviderConfigError, FakeScriptError) as error:
            fail_assistant_error(error, as_json)
