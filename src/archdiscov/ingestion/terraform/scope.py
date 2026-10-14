"""Resolve owning account and per-resource regions from explicit context and facts."""

import re
from dataclasses import dataclass

from ...models.enums import AwsRegion, AwsService
from ..adapter import IngestionError
from .catalog import service_for_type
from .parsing import StateExport, StateResource
from .requests import TerraformStateRequest
from .sensitivity import REDACTED


def identifier(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() and value != REDACTED else None


def _context(entry: StateResource) -> tuple[set[str], set[AwsRegion]]:
    accounts: set[str] = set()
    regions: set[AwsRegion] = set()
    for key in ("account_id", "owner_id"):
        value = identifier(entry.values.get(key))
        if value is not None:
            if not re.fullmatch(r"[0-9]{12}", value):
                raise IngestionError("invalid_request", "Invalid resource account context")
            accounts.add(value)
    candidates = [entry.values.get("arn"), entry.values.get("id")]
    for value in candidates:
        value = identifier(value)
        if value is None or not value.startswith("arn:"):
            continue
        parts = value.split(":", 5)
        if len(parts) != 6:
            raise IngestionError("invalid_request", "Invalid resource ARN context")
        if parts[4]:
            if not re.fullmatch(r"[0-9]{12}", parts[4]):
                raise IngestionError("invalid_request", "Invalid resource ARN account")
            accounts.add(parts[4])
        if parts[3]:
            regions.add(_region(parts[3]))
    if identifier(entry.values.get("region")) is not None:
        regions.add(_region(entry.values["region"]))
    return accounts, regions


def _region(value: str) -> AwsRegion:
    try:
        region = AwsRegion(value)
    except ValueError as error:
        raise IngestionError(
            "unsupported_region", "Unsupported Terraform resource region"
        ) from error
    if region == AwsRegion.GLOBAL:
        raise IngestionError("invalid_request", "Resource region must be a concrete AWS region")
    return region


@dataclass(frozen=True)
class StateScope:
    account_id: str
    regions_by_address: dict[str, AwsRegion]
    regions: list[AwsRegion]


def resolve_scope(export: StateExport, request: TerraformStateRequest) -> StateScope:
    accounts = {request.account_id} if request.account_id else set()
    contexts = {}
    for entry in export.resources:
        owning_accounts, regions = _context(entry)
        accounts.update(owning_accounts)
        if len(regions) > 1 or (request.region and regions and regions != {request.region}):
            raise IngestionError("invalid_request", "Conflicting Terraform resource region context")
        contexts[entry.address] = regions
    if len(accounts) != 1:
        raise IngestionError(
            "invalid_request",
            "Supply one account_id; missing, conflicting, or mixed-account context",
        )
    resolved = {}
    for entry in export.resources:
        if service_for_type(entry.terraform_type) == AwsService.IAM:
            resolved[entry.address] = AwsRegion.GLOBAL
            continue
        known = contexts[entry.address]
        region = next(iter(known)) if known else request.region
        if region is None:
            raise IngestionError(
                "invalid_request", "Supply region for resources without region facts"
            )
        resolved[entry.address] = region
    regions = sorted(set(resolved.values()) - {AwsRegion.GLOBAL})
    if not resolved:
        if request.region is None:
            raise IngestionError(
                "invalid_request", "An empty export requires explicit account and region"
            )
        regions = [request.region]
    return StateScope(next(iter(accounts)), resolved, regions)
