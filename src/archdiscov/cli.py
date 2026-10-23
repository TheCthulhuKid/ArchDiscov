"""CLI presentation; architecture operations belong in application services."""

from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from archdiscov.cli_analysis import analyze, findings
from archdiscov.cli_ask import ask
from archdiscov.cli_collection import collection_request, outcome_data, print_outcomes
from archdiscov.cli_comparison import compare, comparison
from archdiscov.cli_diagnostics import emit_model, print_error, snapshot_exit_code
from archdiscov.cli_diagnostics import fail_snapshot_error as _fail
from archdiscov.cli_diagnostics import json_output as _json_output
from archdiscov.cli_evaluation import evaluate, evaluation
from archdiscov.cli_evidence import print_evidence
from archdiscov.cli_ingestion import ingestion_data, print_ingestion
from archdiscov.cli_query import query_app
from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.requests import RegionScope
from archdiscov.services import (
    IngestFormat,
    SnapshotError,
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

app.add_typer(query_app, name="query")
app.command()(analyze)
app.command()(findings)
app.command()(compare)
app.command()(comparison)
app.command()(ask)
app.command()(evaluate)
app.command()(evaluation)


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
        emit_model(result, snapshot_path=str(snapshot_path))
    else:
        print_evidence(result, snapshot_path)


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
        code = snapshot_exit_code(error) if isinstance(error, SnapshotError) else 5
        raise typer.Exit(code)
