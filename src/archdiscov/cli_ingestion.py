"""Presentation for offline imports; parsing belongs to source adapters."""

import typer

from .cli_operations import operations_data, print_operations
from .services import IngestResult


def ingestion_data(result: IngestResult) -> dict:
    return {
        "snapshot_id": str(result.snapshot.snapshot_id),
        "path": str(result.path),
        "source_kind": result.snapshot.source.kind,
        "complete": result.complete,
        "operations": operations_data(result.snapshot.coverage.operations),
    }


def print_ingestion(result: IngestResult) -> None:
    state = (
        "all recorded operations succeeded (mapping limits still apply)"
        if result.complete
        else "PARTIAL (see operation coverage)"
    )
    typer.echo(f"Saved snapshot {result.snapshot.snapshot_id} to {result.path} - {state}")
    print_operations(operations_data(result.snapshot.coverage.operations))
