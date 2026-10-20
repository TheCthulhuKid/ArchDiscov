"""The `compare` and `comparison` commands: thin routing to comparison services."""

from pathlib import Path
from typing import Annotated

import typer

from .cli_comparison_output import print_comparison
from .cli_diagnostics import emit_model, safe_failures
from .cli_options import JsonOption
from .models.comparison.results import DEFAULT_RESULT_LIMIT, MAX_RESULT_LIMIT
from .services import (
    SupportedSnapshot,
    compare_snapshots,
    load_comparison,
    load_snapshot,
    save_comparison,
)

LeftOption = Annotated[Path, typer.Option("--left", help="Snapshot JSON of the left side.")]
RightOption = Annotated[Path, typer.Option("--right", help="Snapshot JSON of the right side.")]


def _load_snapshots(
    left: Path, right: Path, as_json: bool
) -> tuple[SupportedSnapshot, SupportedSnapshot]:
    """Load both sides, naming the side's own path in any failure."""
    with safe_failures(left, as_json):
        left_snapshot = load_snapshot(left)
    with safe_failures(right, as_json):
        right_snapshot = load_snapshot(right)
    return left_snapshot, right_snapshot


def compare(
    left: LeftOption,
    right: RightOption,
    output: Annotated[
        Path, typer.Option("--output", "-o", help="New result path; never overwritten.")
    ],
    result_limit: Annotated[
        int,
        typer.Option(
            "--result-limit",
            min=1,
            max=MAX_RESULT_LIMIT,
            help="Entries kept per kind (and per side's unmatched resources).",
        ),
    ] = DEFAULT_RESULT_LIMIT,
    json_output: JsonOption = False,
) -> None:
    """Compare two snapshots and save the verified comparison.

    Reports recorded differences and qualified absences between the two selected snapshots,
    not verified drift. Neither snapshot is changed.
    """
    left_snapshot, right_snapshot = _load_snapshots(left, right, json_output)
    with safe_failures(output, json_output):
        result = compare_snapshots(left_snapshot, right_snapshot, result_limit=result_limit)
        save_comparison(result, output, left=left_snapshot, right=right_snapshot)
    if json_output:
        emit_model(result, left_path=str(left), right_path=str(right), output_path=str(output))
    else:
        typer.echo(f"Saved comparison: {output}")
        print_comparison(result, left, right)


def comparison(
    result_path: Annotated[Path, typer.Argument(help="Saved comparison result JSON.")],
    left: LeftOption,
    right: RightOption,
    json_output: JsonOption = False,
) -> None:
    """Show a saved comparison after verifying it against its two snapshots."""
    left_snapshot, right_snapshot = _load_snapshots(left, right, json_output)
    with safe_failures(result_path, json_output):
        result = load_comparison(result_path, left=left_snapshot, right=right_snapshot)
    if json_output:
        emit_model(result, result_path=str(result_path), left_path=str(left), right_path=str(right))
    else:
        typer.echo(f"Comparison result: {result_path}")
        print_comparison(result, left, right)
