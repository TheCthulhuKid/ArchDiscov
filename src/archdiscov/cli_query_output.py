"""Readable presentation of query results, separate from routing and services."""

import json
from pathlib import Path

import typer

from .graph.dependencies import DependencyEdge
from .graph.identity import native_resource_type
from .models.enums import QueryDirection
from .models.query.edges import QueryTraversalStep
from .models.query.limitations import (
    QueryExternalTargetLimitation,
    QueryLimitation,
    QueryLimitedCoverageLimitation,
    QueryMissingCoverageLimitation,
    QueryProvenanceLimitation,
    QueryUnresolvedTargetLimitation,
)
from .models.query.results import (
    QueryInspectResult,
    QueryNeighboursResult,
    QueryPathsResult,
    QuerySearchResult,
    QueryTruncation,
)
from .models.resource_base import Resource

ResultWithContext = (
    QuerySearchResult | QueryInspectResult | QueryNeighboursResult | QueryPathsResult
)


def _header(result: ResultWithContext, path: Path) -> None:
    typer.echo(f"Snapshot: {result.snapshot_id} ({path})")
    typer.echo(f"Cloud scope: {result.scope.model_dump_json()}")


def _resource_line(resource: Resource) -> str:
    return f"{resource.id}  {native_resource_type(resource)}  {resource.name or '<unnamed>'}"


def _limitation_line(limitation: QueryLimitation) -> str:
    match limitation:
        case QueryLimitedCoverageLimitation(coverage=ref, status=status):
            head = f"coverage limited ({status}): {ref.service}/{ref.operation}"
            return f"{head} [{ref.location or 'scope-wide'}]"
        case QueryMissingCoverageLimitation(coverage=ref):
            return (
                f"coverage missing: {ref.service}/{ref.operation} [{ref.location or 'scope-wide'}]"
            )
        case QueryProvenanceLimitation(resource_id=resource_id, fact_path=fact_path):
            return f"provenance unavailable: {resource_id} {fact_path}"
        case QueryExternalTargetLimitation() | QueryUnresolvedTargetLimitation():
            return f"{limitation.kind}: {limitation.terminal_id}"
    return str(limitation.kind)


def _print_limitations(limitations: list[QueryLimitation], indent: str = "") -> None:
    if not limitations:
        return
    typer.echo(f"{indent}Limitations:")
    for limitation in limitations:
        typer.echo(f"{indent}  - {_limitation_line(limitation)}: {limitation.message}")


def _print_truncation(truncation: QueryTruncation) -> None:
    reasons = ", ".join(truncation.reasons)
    typer.echo(f"Truncated: {reasons}" if reasons else "Truncated: no")


def step_line(step: QueryTraversalStep) -> str:
    """Show the stored edge as recorded, noting when it was walked in reverse."""
    stored = step.edge.edge
    if isinstance(stored, DependencyEdge):
        label = stored.relationship.value
        if stored.attachment_kind is not None:
            label += f"/{stored.attachment_kind.value}"
        detail = f" [evidence: {', '.join(stored.evidence_ids) or 'none'}]"
    else:
        label, detail = "contains", " [uncited structure]"
    reverse = " (walked incoming)" if step.direction == QueryDirection.INCOMING else ""
    return f"{stored.source_id} --{label}--> {stored.target_id}{reverse}{detail}"


def print_search(result: QuerySearchResult, path: Path) -> None:
    _header(result, path)
    typer.echo(f"Matches: {len(result.resources)}")
    for resource in result.resources:
        typer.echo(f"  {_resource_line(resource)}")
    _print_truncation(result.truncation)
    _print_limitations(result.limitations)
    typer.echo("No matches means no recorded matches, not proof of absence.")


def print_inspect(result: QueryInspectResult, path: Path) -> None:
    _header(result, path)
    resource = result.resource
    typer.echo(f"Resource: {_resource_line(resource)}")
    typer.echo("Attributes:")
    typer.echo(json.dumps(resource.attributes, indent=2, ensure_ascii=False, allow_nan=False))
    typer.echo(f"Fact citations: {json.dumps(resource.fact_evidence, sort_keys=True)}")
    typer.echo(f"Owned evidence: {', '.join(result.evidence) or '<none>'}")
    typer.echo("Coverage context (not a completeness guarantee):")
    if not result.coverage:
        typer.echo("  No operation coverage recorded for this resource's context.")
    for operation in result.coverage:
        typer.echo(f"  {operation.service}/{operation.operation}: {operation.status}")
    _print_limitations(result.limitations)


def print_neighbours(result: QueryNeighboursResult, path: Path) -> None:
    _header(result, path)
    typer.echo(f"Resource: {result.resource_id}")
    typer.echo(f"Neighbours: {len(result.steps)}")
    for step in result.steps:
        typer.echo(f"  {step_line(step)}")
    _print_truncation(result.truncation)
    _print_limitations(result.limitations)
    typer.echo("Recorded neighbours only; this is not an exhaustive list.")


def print_paths(result: QueryPathsResult, path: Path) -> None:
    _header(result, path)
    typer.echo(f"Start: {result.resource_id}")
    typer.echo(f"Paths: {len(result.paths)}")
    for number, item in enumerate(result.paths, 1):
        typer.echo(f"  {number}. depth {len(item.steps)}")
        for step in item.steps:
            typer.echo(f"       {step_line(step)}")
        _print_limitations(item.limitations, indent="     ")
    _print_truncation(result.truncation)
    _print_limitations(result.limitations)
    typer.echo("Structural paths only; not reachability or effective access.")
