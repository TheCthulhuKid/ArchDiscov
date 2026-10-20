"""Readable presentation of comparison results, separate from routing and services."""

import json
from pathlib import Path

import typer

from .models.comparison.entries import (
    AmbiguousMatch,
    FieldDifference,
    MatchedPair,
    Qualification,
    UnmatchedResource,
)
from .models.comparison.results import ComparisonResult, ComparisonSnapshot
from .models.enums import QualificationKind

_VALUE_WIDTH = 100


def _side(snapshot: ComparisonSnapshot, path: Path) -> None:
    typer.echo(f"{snapshot.side.value.capitalize()}: {snapshot.snapshot_id} ({path})")
    label = f" {snapshot.source_label}" if snapshot.source_label else ""
    scope = ", ".join(
        f"{key}={value}" for key, value in snapshot.scope.model_dump(mode="json").items()
    )
    typer.echo(f"  Source: {snapshot.source_kind}{label}")
    typer.echo(f"  Scope: {scope}")
    started, completed = snapshot.collection_started_at, snapshot.collection_completed_at
    typer.echo(f"  Collected: {started.isoformat()} to {completed.isoformat()}")


def _qualification(item: Qualification) -> str:
    match item.kind:
        case QualificationKind.PLANNED_LIFECYCLE:
            detail = f"planned {'+'.join(item.lifecycle.actions)}" if item.lifecycle else ""
        case QualificationKind.LIMITED_COVERAGE if item.coverage is not None:
            ref = item.coverage
            detail = f"{ref.service}/{ref.operation} [{ref.location or 'scope-wide'}]"
        case QualificationKind.UNRECORDED_COVERAGE:
            detail = f"{item.service}: no coverage recorded"
        case _:
            detail = ""
    text = f"{item.kind} ({item.side})" + (f": {detail}" if detail else "")
    return text + (f" evidence {', '.join(item.evidence_ids)}" if item.evidence_ids else "")


def _value(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= _VALUE_WIDTH else text[: _VALUE_WIDTH - 3] + "..."


def _difference(item: FieldDifference, indent: str) -> None:
    change = (
        ""
        if item.kind == "indeterminate"
        else f": {_value(item.left_value)} -> {_value(item.right_value)}"
    )
    typer.echo(f"{indent}{item.kind} {item.subject} {item.path}{change}")
    for side, ids in (("left", item.left_evidence_ids), ("right", item.right_evidence_ids)):
        if ids:
            typer.echo(f"{indent}  {side} evidence: {', '.join(ids)}")
    for qualification in item.qualifications:
        typer.echo(f"{indent}  {_qualification(qualification)}")


def _matched(number: int, pair: MatchedPair) -> None:
    typer.echo(f"  {number}. {pair.left.resource_id} <-> {pair.right.resource_id} [{pair.basis}]")
    for qualification in pair.qualifications:
        typer.echo(f"     {_qualification(qualification)}")
    for difference in pair.differences:
        _difference(difference, "     ")


def _unmatched(number: int, item: UnmatchedResource) -> None:
    typer.echo(f"  {number}. {item.side} only: {item.resource.resource_id}")
    for qualification in item.qualifications:
        typer.echo(f"     {_qualification(qualification)}")


def _ambiguous(number: int, group: AmbiguousMatch) -> None:
    typer.echo(f"  {number}. {group.key} [{group.basis}] (no candidate chosen)")
    for side, candidates in (("left", group.left), ("right", group.right)):
        for candidate in candidates:
            typer.echo(f"     {side}: {candidate.resource_id}")


def print_comparison(result: ComparisonResult, left_path: Path, right_path: Path) -> None:
    _side(result.left, left_path)
    _side(result.right, right_path)
    typer.echo(f"Created: {result.created_at.isoformat()} (result limit {result.result_limit})")
    for limitation in result.limitations:
        typer.echo(f"Limitation {limitation.kind}: {limitation.message}")
    sections = (
        ("Matched", result.matched, _matched),
        ("Unmatched", result.unmatched, _unmatched),
        ("Ambiguous", result.ambiguous, _ambiguous),
    )
    for title, entries, show in sections:
        typer.echo(f"{title}: {len(entries)}")
        for number, entry in enumerate(entries, 1):
            show(number, entry)
    typer.echo(
        "Differences are recorded, not verified drift; qualified absences are not proof of "
        "removal. Inspect cited evidence with `archdiscov evidence ID --snapshot PATH`."
    )
