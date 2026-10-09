"""Request parsing and result presentation for AWS collection."""

import typer

from .ingestion.requests import AwsAccountConfig, AwsCollectionRequest, RegionScope
from .models.enums import AwsRegion, AwsService
from .services import AccountOutcome


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
        raise ValueError("all-services cannot be combined with service selections")
    options = {
        "regions": tuple(AwsRegion(r) for r in (regions or [])),
        "all_services": all_services,
        "bootstrap_region": AwsRegion(bootstrap_region) if bootstrap_region else None,
        "max_concurrency": max_concurrency,
        "max_in_flight_calls": account_max_in_flight_calls,
    }
    if not all_services:
        options["services"] = tuple(AwsService(s) for s in (services or [AwsService.EC2]))
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
    assert outcome.snapshot is not None
    data.update(
        snapshot_id=str(outcome.snapshot.snapshot_id),
        path=str(outcome.path),
        complete=outcome.complete,
        collection_scope=outcome.snapshot.metadata.get("collection_scope"),
        operations=[
            {
                "region": op.region.value,
                "service": op.service.value,
                "operation": op.operation,
                "status": op.status.value,
                "result_count": op.result_count,
                "failures": [f.code for f in op.failures],
            }
            for op in outcome.snapshot.coverage.operations
        ],
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
        for op in result["operations"]:
            count = "" if op["result_count"] is None else f" ({op['result_count']} results)"
            failed = f" [{', '.join(op['failures'])}]" if op["failures"] else ""
            typer.echo(
                f"  {op['region']} {op['service']} {op['operation']}: {op['status']}{count}{failed}"
            )
