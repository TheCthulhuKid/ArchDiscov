"""The `ingest` command and presentation for offline imports; parsing belongs to source
adapters."""

from pathlib import Path
from typing import Annotated

import typer

from .cli_diagnostics import fail_snapshot_error, print_error
from .cli_diagnostics import json_output as emit_json
from .cli_operations import operations_data, print_operations
from .cli_options import JsonOption
from .ingestion.adapter import IngestionError
from .services import IngestFormat, IngestResult, SnapshotError, ingest_snapshot


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
    json_output: JsonOption = False,
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
        fail_snapshot_error(error, json_output)
    except IngestionError as error:
        print_error({"code": error.code, "message": str(error)}, as_json=json_output)
        raise typer.Exit(2) from None
    if json_output:
        emit_json(ingestion_data(result))
    else:
        print_ingestion(result)
