"""The `analyze` and `findings` commands: thin routing to analyzer and persistence services."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer

from .cli_analysis_output import print_findings, print_saved
from .cli_diagnostics import emit_model, fail_analysis_error, fail_snapshot_error
from .cli_options import JsonOption
from .graph import GraphIndex
from .models.enums import AnalyzerKind
from .services import (
    DEFAULT_REGISTRY,
    AnalysisValidationError,
    AnalyzerError,
    SnapshotError,
    load_analysis,
    load_snapshot,
    run_analyzer,
    save_analysis,
)

SnapshotOption = Annotated[Path, typer.Option("--snapshot", help="Snapshot JSON the result uses.")]


@contextmanager
def _safe_failures(snapshot_path: Path, as_json: bool) -> Iterator[None]:
    """Turn expected service failures into safe diagnostics and stable exit codes."""
    try:
        yield
    except SnapshotError as error:
        if error.path is None:
            error.path = snapshot_path
        fail_snapshot_error(error, as_json)
    except (AnalyzerError, AnalysisValidationError) as error:
        fail_analysis_error(error, as_json)


def analyze(
    snapshot: SnapshotOption,
    output: Annotated[
        Path, typer.Option("--output", "-o", help="New result path; never overwritten.")
    ],
    analyzer: Annotated[
        AnalyzerKind,
        typer.Option(
            "--analyzer", help="ownership (default), security_group_impact, or cross_account_trust."
        ),
    ] = AnalyzerKind.OWNERSHIP,
    ownership_key: Annotated[
        list[str] | None,
        typer.Option("--ownership-key", help="Ownership: tag key counted as owner; repeatable."),
    ] = None,
    resource_id: Annotated[
        str | None,
        typer.Option("--resource-id", help="Security-group impact: stable ID of the group."),
    ] = None,
    max_depth: Annotated[
        int | None,
        typer.Option(
            "--max-depth", help="Security-group impact: downstream steps (1-10, default 3)."
        ),
    ] = None,
    result_limit: Annotated[
        int | None,
        typer.Option(
            "--result-limit",
            help="Security-group impact or trust: findings (1-10000, default 100).",
        ),
    ] = None,
    json_output: JsonOption = False,
) -> None:
    """Run an analyzer over a snapshot and save the verified result.

    Options belong to specific analyzers: --ownership-key for ownership; --resource-id and
    --max-depth for security_group_impact; --result-limit for security_group_impact and
    cross_account_trust.
    """
    with _safe_failures(snapshot, json_output):
        DEFAULT_REGISTRY.get(analyzer)  # fail fast, before touching any file
        options = {
            "ownership_keys": ownership_key or None,
            "resource_id": resource_id,
            "max_depth": max_depth,
            "result_limit": result_limit,
        }
        # Options an analyzer does not accept are rejected by its parameter validation.
        parameters = {"kind": analyzer, **{k: v for k, v in options.items() if v is not None}}
        loaded = load_snapshot(snapshot)
        result = run_analyzer(GraphIndex(loaded), parameters)
        save_analysis(result, output, snapshot=loaded)
    if json_output:
        emit_model(result, snapshot_path=str(snapshot), output_path=str(output))
    else:
        print_saved(result, snapshot, output)


def findings(
    result_path: Annotated[Path, typer.Argument(help="Saved analysis result JSON.")],
    snapshot: SnapshotOption,
    json_output: JsonOption = False,
) -> None:
    """Show a saved result after verifying it against its snapshot."""
    with _safe_failures(snapshot, json_output):
        result = load_analysis(result_path, snapshot=load_snapshot(snapshot))
    if json_output:
        emit_model(result, result_path=str(result_path), snapshot_path=str(snapshot))
    else:
        print_findings(result, result_path, snapshot)
