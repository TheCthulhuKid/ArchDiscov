"""CLI presentation; architecture operations belong in application services."""

import json
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from archdiscov.cli_collection import collection_request, outcome_data, print_outcomes
from archdiscov.cli_diagnostics import json_output as _json_output
from archdiscov.cli_diagnostics import print_error
from archdiscov.cli_ingestion import ingestion_data, print_ingestion
from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.requests import RegionScope
from archdiscov.services import (
    EvidenceInspection,
    EvidenceNotFoundError,
    IngestFormat,
    SnapshotConflictError,
    SnapshotError,
    SnapshotIOError,
    SnapshotJSONError,
    SnapshotValidationError,
    collect_and_save,
    get_evidence,
    ingest_snapshot,
    load_snapshot,
)

app = typer.Typer(
    help="Discover AWS architecture and inspect traceable evidence.",
    add_completion=False,
    pretty_exceptions_enable=False,
)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    show_version: Annotated[
        bool,
        typer.Option("--version", help="Show the installed version and exit.", is_eager=True),
    ] = False,
) -> None:
    if show_version:
        typer.echo(f"archdiscov {version('archdiscov')}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


def _fail(error: SnapshotError, json_output: bool) -> None:
    diagnostic = {"code": error.code, "message": str(error)}
    if error.path is not None:
        diagnostic["path"] = str(error.path)
    if isinstance(error, SnapshotValidationError):
        diagnostic["details"] = [asdict(issue) for issue in error.details]
    if isinstance(error, SnapshotJSONError):
        diagnostic.update(line=error.line, column=error.column)
    print_error(diagnostic, as_json=json_output)
    exit_code = 2
    if isinstance(error, SnapshotIOError):
        exit_code = 1
    elif isinstance(error, SnapshotConflictError):
        exit_code = 3
    elif isinstance(error, EvidenceNotFoundError):
        exit_code = 4
    raise typer.Exit(exit_code)


@app.command()
def ingest(
    input_path: Annotated[
        Path, typer.Argument(help="Snapshot JSON or an explicitly selected local IaC export.")
    ],
    output: Annotated[
        Path, typer.Option("--output", "-o", help="New snapshot path; never overwritten.")
    ],
    source_format: Annotated[
        IngestFormat, typer.Option("--format", help="Source format; never inferred from the file.")
    ] = IngestFormat.SNAPSHOT,
    account_id: Annotated[
        str | None, typer.Option("--account-id", help="AWS account context for IaC imports.")
    ] = None,
    region: Annotated[
        str | None, typer.Option("--region", help="Concrete AWS region context for IaC imports.")
    ] = None,
    workspace: Annotated[
        str | None, typer.Option("--workspace", help="Terraform workspace provenance.")
    ] = None,
    stack_name: Annotated[
        str | None, typer.Option("--stack-name", help="CloudFormation stack name context.")
    ] = None,
    stack_id: Annotated[
        str | None, typer.Option("--stack-id", help="CloudFormation stack ARN context.")
    ] = None,
    parameters_file: Annotated[
        Path | None,
        typer.Option("--parameters-file", help="CloudFormation parameter-name/value JSON object."),
    ] = None,
    sensitive_path: Annotated[
        list[str] | None,
        typer.Option("--sensitive-path", help="CloudFormation sensitive JSON Pointer; repeatable."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit JSON results and service errors.")
    ] = (False),
) -> None:
    """Import snapshot JSON, Terraform state/plan exports, or CloudFormation JSON/YAML."""
    try:
        result = ingest_snapshot(
            input_path,
            output,
            source_format=source_format,
            account_id=account_id,
            region=region,
            workspace=workspace,
            stack_name=stack_name,
            stack_id=stack_id,
            parameters_file=parameters_file,
            sensitive_paths=tuple(sensitive_path or ()),
        )
    except SnapshotError as error:
        _fail(error, json_output)
    except IngestionError as error:
        print_error({"code": error.code, "message": str(error)}, as_json=json_output)
        raise typer.Exit(2) from None
    if json_output:
        _json_output(ingestion_data(result))
    else:
        print_ingestion(result)


def _print_evidence(result: EvidenceInspection, path: Path) -> None:
    evidence = result.evidence
    resource = result.resource
    typer.echo(f"Snapshot: {result.snapshot_id} ({path})")
    typer.echo(f"Snapshot source: {result.snapshot_source.model_dump_json()}")
    typer.echo(f"Evidence: {result.evidence_id}")
    typer.echo(f"Resource: {resource.id} (name: {resource.name or '<unnamed>'})")
    typer.echo(f"Source: {evidence.source}; operation/file: {evidence.locator}")
    typer.echo(f"Configuration path: {evidence.configuration_path}")
    typer.echo(f"Fact citations: {', '.join(result.fact_paths) or '<none>'}")
    typer.echo(f"Value status: {evidence.value_status}")
    typer.echo("Sanitized value:")
    typer.echo(json.dumps(evidence.value, indent=2, ensure_ascii=False, allow_nan=False))
    if evidence.redacted_paths:
        typer.echo(f"Redacted paths: {json.dumps(evidence.redacted_paths)}")
    if evidence.limitation:
        typer.echo(f"Limitation: {evidence.limitation}")
    typer.echo("Coverage context (same region/service; not a completeness guarantee):")
    if not result.coverage:
        typer.echo("  No operation coverage recorded for this resource's region/service.")
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


@app.command()
def evidence(
    evidence_id: Annotated[str, typer.Argument(help="Evidence-map ID in the selected snapshot.")],
    snapshot_path: Annotated[
        Path, typer.Option("--snapshot", help="Snapshot JSON path to inspect.")
    ],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit JSON results and service errors.")
    ] = False,
) -> None:
    """Inspect one sanitized evidence record and its collection limitations."""
    try:
        result = get_evidence(load_snapshot(snapshot_path), evidence_id)
    except SnapshotError as error:
        if error.path is None:
            error.path = snapshot_path
        _fail(error, json_output)
    if json_output:
        _json_output({"snapshot_path": str(snapshot_path), **result.model_dump(mode="json")})
    else:
        _print_evidence(result, snapshot_path)


@app.command()
def collect(
    account: Annotated[
        list[str],
        typer.Option(
            "--account", help="PROFILE:ACCOUNT_ID[:ROLE_ARN]; repeat for several accounts."
        ),
    ],
    output: Annotated[
        list[Path],
        typer.Option("--output", "-o", help="New snapshot path per account, in order."),
    ],
    region: Annotated[
        list[str] | None,
        typer.Option("--region", help="Explicit AWS region; repeatable. Bypasses discovery."),
    ] = None,
    service: Annotated[
        list[str] | None,
        typer.Option(
            "--service",
            help=(
                "Collect ec2, iam, elasticloadbalancing, ecs, lambda, rds, or sqs; "
                "repeatable. Defaults to ec2."
            ),
        ),
    ] = None,
    all_services: Annotated[
        bool,
        typer.Option(
            "--all-services", help="Collect all supported services; discover enabled regions."
        ),
    ] = False,
    region_scope: Annotated[
        RegionScope | None,
        typer.Option(
            "--region-scope", help="explicit or enabled; enabled conflicts with --region."
        ),
    ] = None,
    bootstrap_region: Annotated[
        str | None,
        typer.Option(
            "--bootstrap-region", help="Partition-appropriate authentication/discovery region."
        ),
    ] = None,
    max_in_flight_calls: Annotated[
        int, typer.Option("--max-in-flight-calls", help="Run-wide SDK-call limit (1–64).")
    ] = 16,
    account_max_in_flight_calls: Annotated[
        int,
        typer.Option("--account-max-in-flight-calls", help="Per-account SDK-call limit (1–16)."),
    ] = 4,
    max_active_accounts: Annotated[
        int, typer.Option("--max-active-accounts", help="Concurrent account workflows (1–16).")
    ] = 2,
    max_collector_jobs: Annotated[
        int,
        typer.Option("--max-collector-jobs", help="Run-wide active service/region jobs (1–64)."),
    ] = 8,
    max_concurrency: Annotated[
        int, typer.Option("--max-concurrency", help="Local worker limit per runner (1–16).")
    ] = 4,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON results.")] = False,
) -> None:
    """Collect selected AWS infrastructure configuration (read-only)."""
    try:
        request = collection_request(
            account,
            region,
            service,
            all_services=all_services,
            region_scope=region_scope,
            bootstrap_region=bootstrap_region,
            max_concurrency=max_concurrency,
            account_max_in_flight_calls=account_max_in_flight_calls,
            max_in_flight_calls=max_in_flight_calls,
            max_active_accounts=max_active_accounts,
            max_collector_jobs=max_collector_jobs,
        )
        outcomes = collect_and_save(request, output)
    except (ValueError, ValidationError, IngestionError) as error:
        message = "invalid collection request"
        code = error.code if isinstance(error, IngestionError) else "invalid_request"
        if isinstance(error, IngestionError):
            message = str(error)
        print_error({"code": code, "message": message}, as_json=json_output)
        raise typer.Exit(2) from None
    results = [outcome_data(o) for o in outcomes]
    if json_output:
        _json_output({"accounts": results})
    else:
        print_outcomes(results)
    failed_outcomes = [o for o in outcomes if o.error is not None]
    if failed_outcomes:
        error = failed_outcomes[0].error
        code = 5
        if isinstance(error, SnapshotIOError):
            code = 1
        elif isinstance(error, SnapshotConflictError):
            code = 3
        elif isinstance(error, SnapshotError):
            code = 2
        raise typer.Exit(code)
