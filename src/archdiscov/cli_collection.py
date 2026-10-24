"""The `collect` command: request parsing, routing, and result presentation."""

from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel, ValidationError

from .cli_diagnostics import diagnostic_data, print_error, snapshot_exit_code
from .cli_diagnostics import json_output as emit_json
from .cli_operations import operations_data, print_operations
from .cli_options import JsonOption
from .ingestion.adapter import IngestionError
from .ingestion.requests import AwsAccountConfig, AwsCollectionRequest, RegionScope
from .models.enums import AwsRegion, AwsService
from .models.validation_errors import safe_validation_issues
from .services import AccountOutcome, SnapshotError, collect_and_save

_REQUEST_FIELDS = frozenset({*AwsAccountConfig.model_fields, *AwsCollectionRequest.model_fields})


class CollectionOptionError(ValueError):
    """An option the request cannot use; the message names the option, never its value."""

    def __init__(self, option: str, reason: str) -> None:
        super().__init__(f"{option}: {reason}")


def _help(text: str, model: type[BaseModel], field: str) -> str:
    """Option help naming the range the request model enforces, so the two cannot drift.

    The model, not Typer, enforces it, so a value out of range is an `invalid_request`
    diagnostic, in JSON with `--json`, rather than a usage error.
    """
    bounds = {type(item).__name__: item for item in model.model_fields[field].metadata}
    return f"{text} ({bounds['Ge'].ge}-{bounds['Le'].le})."


def _choice[E: Enum](kind: type[E], value: str, option: str, reason: str) -> E:
    try:
        return kind(value)
    except ValueError:
        raise CollectionOptionError(option, reason) from None


def collection_request(
    accounts: list[str],
    regions: list[str] | None,
    services: list[str] | None,
    *,
    all_services: bool,
    region_scope: RegionScope | None,
    bootstrap_region: str | None,
    max_concurrency: int,
    account_max_in_flight_calls: int,
    max_in_flight_calls: int,
    max_active_accounts: int,
    max_collector_jobs: int,
) -> AwsCollectionRequest:
    if all_services and services is not None:
        raise CollectionOptionError("--all-services", "cannot be combined with --service")
    region = "is not a supported AWS region"
    options = {
        "regions": tuple(_choice(AwsRegion, r, "--region", region) for r in (regions or [])),
        "all_services": all_services,
        "bootstrap_region": (
            _choice(AwsRegion, bootstrap_region, "--bootstrap-region", region)
            if bootstrap_region
            else None
        ),
        "max_concurrency": max_concurrency,
        "max_in_flight_calls": account_max_in_flight_calls,
    }
    if not all_services:
        options["services"] = tuple(
            _choice(AwsService, item, "--service", "is not a supported service")
            for item in (services or [AwsService.EC2])
        )
    if region_scope is not None:
        options["region_scope"] = region_scope
    configured = []
    for spec in accounts:
        profile, _, rest = spec.partition(":")
        account_id, _, role_arn = rest.partition(":")
        configured.append(
            AwsAccountConfig(
                profile=profile,
                expected_account_id=account_id,
                role_arn=role_arn or None,
                **options,
            )
        )
    return AwsCollectionRequest(
        accounts=tuple(configured),
        max_in_flight_calls=max_in_flight_calls,
        max_active_accounts=max_active_accounts,
        max_collector_jobs=max_collector_jobs,
    )


def outcome_data(outcome: AccountOutcome) -> dict:
    data: dict = {"account_id": outcome.account_id}
    if outcome.error is not None:
        data["error"] = {"code": outcome.error.code, "message": str(outcome.error)}
        return data
    if outcome.snapshot is None:
        raise ValueError("an outcome without an error has a snapshot")
    data.update(
        snapshot_id=str(outcome.snapshot.snapshot_id),
        path=str(outcome.path),
        complete=outcome.complete,
        collection_scope=outcome.snapshot.metadata.get("collection_scope"),
        operations=operations_data(outcome.snapshot.coverage.operations),
    )
    return data


def print_outcomes(results: list[dict]) -> None:
    for result in results:
        if "error" in result:
            error = result["error"]
            typer.echo(
                f"Account {result['account_id']}: FAILED [{error['code']}] {error['message']}"
            )
            continue
        state = (
            "complete within resolved supported scope"
            if result["complete"]
            else "PARTIAL (see operation coverage)"
        )
        typer.echo(f"Account {result['account_id']}: saved {result['path']} - {state}")
        if scope := result["collection_scope"]:
            typer.echo(f"  Services: {', '.join(scope['services'])}")
            typer.echo(
                f"  Regions ({scope['region_scope']}): {', '.join(scope['regions']) or 'none'}"
            )
            if AwsService.IAM in scope["services"]:
                typer.echo("  Account-wide: iam (global)")
        print_operations(result["operations"])


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
        int,
        typer.Option(
            "--max-in-flight-calls",
            help=_help("Run-wide SDK-call limit", AwsCollectionRequest, "max_in_flight_calls"),
        ),
    ] = 16,
    account_max_in_flight_calls: Annotated[
        int,
        typer.Option(
            "--account-max-in-flight-calls",
            help=_help("Per-account SDK-call limit", AwsAccountConfig, "max_in_flight_calls"),
        ),
    ] = 4,
    max_active_accounts: Annotated[
        int,
        typer.Option(
            "--max-active-accounts",
            help=_help("Concurrent account workflows", AwsCollectionRequest, "max_active_accounts"),
        ),
    ] = 2,
    max_collector_jobs: Annotated[
        int,
        typer.Option(
            "--max-collector-jobs",
            help=_help(
                "Run-wide active service/region jobs", AwsCollectionRequest, "max_collector_jobs"
            ),
        ),
    ] = 8,
    max_concurrency: Annotated[
        int,
        typer.Option(
            "--max-concurrency",
            help=_help("Local worker limit per runner", AwsAccountConfig, "max_concurrency"),
        ),
    ] = 4,
    json_output: JsonOption = False,
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
    except (CollectionOptionError, ValidationError, IngestionError) as error:
        print_error(_request_error(error), as_json=json_output)
        raise typer.Exit(2) from None
    results = [outcome_data(o) for o in outcomes]
    if json_output:
        emit_json({"accounts": results})
    else:
        print_outcomes(results)
    failed_outcomes = [o for o in outcomes if o.error is not None]
    if failed_outcomes:
        error = failed_outcomes[0].error
        code = snapshot_exit_code(error) if isinstance(error, SnapshotError) else 5
        raise typer.Exit(code)


def _request_error(error: CollectionOptionError | ValidationError | IngestionError) -> dict:
    """A request diagnostic: the option at fault, the request fields that failed, or the
    collection error; never an option's value."""
    if isinstance(error, IngestionError):
        return diagnostic_data(error.code, str(error))
    if isinstance(error, CollectionOptionError):
        return diagnostic_data("invalid_request", f"Invalid collection request: {error}.")
    issues = safe_validation_issues(error, _REQUEST_FIELDS, "collection request")
    return diagnostic_data("invalid_request", "Invalid collection request.", issues=issues)
