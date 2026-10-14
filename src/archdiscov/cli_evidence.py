"""Evidence presentation, separate from command routing and cloud contracts."""

import json
from pathlib import Path

import typer

from .models.cloud_contracts import CloudEvidence
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
