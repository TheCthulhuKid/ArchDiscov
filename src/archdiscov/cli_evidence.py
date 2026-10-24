"""The `evidence` command and evidence presentation, separate from cloud contracts."""

import json
from pathlib import Path
from typing import Annotated

import typer

from .cli_diagnostics import emit_model, fail_snapshot_error
from .cli_options import JsonOption
from .models.cloud.contracts import CloudEvidence
from .services import SnapshotError, get_evidence, load_snapshot
from .services.evidence import EvidenceInspection


def print_evidence(result: EvidenceInspection, path: Path) -> None:
    evidence = result.evidence
    resource = result.resource
    typer.echo(f"Snapshot: {result.snapshot_id} ({path})")
    typer.echo(f"Snapshot source: {result.snapshot_source.model_dump_json()}")
    typer.echo(f"Evidence: {result.evidence_id}")
    typer.echo(f"Resource: {resource.id} (name: {resource.name or '<unnamed>'})")
    typer.echo(f"Source: {evidence.source}; operation/file: {evidence.locator}")
    if isinstance(evidence, CloudEvidence):
        typer.echo(f"Cloud scope: {evidence.scope.model_dump_json()}")
        typer.echo(f"Location: {evidence.location or 'scope-wide'}; service: {evidence.service}")
    typer.echo(f"Configuration path: {evidence.configuration_path}")
    typer.echo(f"Fact citations: {', '.join(result.fact_paths) or '<none>'}")
    typer.echo(f"Value status: {evidence.value_status}")
    typer.echo("Sanitized value:")
    typer.echo(json.dumps(evidence.value, indent=2, ensure_ascii=False, allow_nan=False))
    if evidence.redacted_paths:
        typer.echo(f"Redacted paths: {json.dumps(evidence.redacted_paths)}")
    if evidence.limitation:
        typer.echo(f"Limitation: {evidence.limitation}")
    context = (
        "platform/scope/location/service"
        if isinstance(evidence, CloudEvidence)
        else "region/service"
    )
    typer.echo(f"Coverage context (same {context}; not a completeness guarantee):")
    if not result.coverage:
        typer.echo(f"  No operation coverage recorded for this resource's {context}.")
    for operation in result.coverage:
        count = "" if operation.result_count is None else f"; results: {operation.result_count}"
        typer.echo(f"  {operation.operation}: {operation.status}{count}")
        if operation.reason:
            typer.echo(f"    Reason: {operation.reason}")
        for failure in operation.failures:
            typer.echo(f"    Failure [{failure.code}]: {failure.message}")
        for unresolved in operation.unresolved_values:
            owner = unresolved.resource_id or "<unspecified resource>"
            typer.echo(
                f"    Unresolved {owner} {unresolved.configuration_path}: {unresolved.reason}"
            )


def evidence(
    evidence_id: Annotated[str, typer.Argument(help="Evidence-map ID in the selected snapshot.")],
    snapshot_path: Annotated[
        Path, typer.Option("--snapshot", help="Snapshot JSON path to inspect.")
    ],
    json_output: JsonOption = False,
) -> None:
    """Inspect one sanitized evidence record and its collection limitations."""
    try:
        result = get_evidence(load_snapshot(snapshot_path), evidence_id)
    except SnapshotError as error:
        if error.path is None:
            error.path = snapshot_path
        fail_snapshot_error(error, json_output)
    if json_output:
        emit_model(result, snapshot_path=str(snapshot_path))
    else:
        print_evidence(result, snapshot_path)
