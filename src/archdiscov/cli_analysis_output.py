"""Readable presentation of analysis results, separate from routing and services."""

from pathlib import Path

import typer

from .models.analysis.references import AnalysisLimitation, PathStep
from .models.analysis.results import AnalysisFinding, AnalysisResult
from .models.architecture import LocalTarget


def _header(result: AnalysisResult, snapshot_path: Path) -> None:
    typer.echo(f"Snapshot: {result.snapshot_id} ({snapshot_path})")
    typer.echo(f"Analyzer: {result.analyzer} (version {result.analyzer_version})")
    typer.echo(f"Parameters: {result.parameters.model_dump_json()}")
    typer.echo(f"Created: {result.created_at.isoformat()}")


def _step_line(step: PathStep) -> str:
    if step.kind == "containment":
        return f"{step.source_id} --contains--> {step.target_id}"
    target = step.target.resource_id if isinstance(step.target, LocalTarget) else step.target.kind
    return f"{step.source_id} --{step.relationship.value}--> {target}"


def _limitation_lines(limitation: AnalysisLimitation) -> list[str]:
    lines = [f"    - {limitation.code}: {limitation.message}"]
    if limitation.coverage is not None:
        ref = limitation.coverage
        lines.append(
            f"      coverage: {ref.service}/{ref.operation} [{ref.location or 'scope-wide'}]"
        )
    if limitation.evidence_ids:
        lines.append(f"      evidence: {', '.join(limitation.evidence_ids)}")
    return lines


def _print_finding(number: int, finding: AnalysisFinding) -> None:
    typer.echo(f"  {number}. [{finding.status}] {finding.summary}")
    typer.echo(f"     Finding: {finding.id}")
    for reference in finding.resources:
        typer.echo(f"     Resource: {reference.resource_id}")
    if finding.evidence_ids:
        typer.echo(f"     Evidence: {', '.join(finding.evidence_ids)}")
    for step in finding.path:
        typer.echo(f"     Path: {_step_line(step)}")
    if finding.limitations:
        typer.echo("     Limitations:")
        for limitation in finding.limitations:
            for line in _limitation_lines(limitation):
                typer.echo(f"  {line}")


def _print_findings(result: AnalysisResult) -> None:
    typer.echo(f"Findings: {len(result.findings)}")
    for number, finding in enumerate(result.findings, 1):
        _print_finding(number, finding)
    typer.echo(
        "Findings are recorded signals, not proof; inspect cited evidence with "
        "`archdiscov evidence ID --snapshot PATH`."
    )


def print_saved(result: AnalysisResult, snapshot_path: Path, output: Path) -> None:
    typer.echo(f"Saved analysis: {output}")
    _header(result, snapshot_path)
    _print_findings(result)


def print_findings(result: AnalysisResult, result_path: Path, snapshot_path: Path) -> None:
    typer.echo(f"Analysis result: {result_path}")
    _header(result, snapshot_path)
    _print_findings(result)
