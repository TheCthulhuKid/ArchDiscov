"""Strict local parsing of the public terraform show -json plan format."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ...models.enums import LifecycleAction
from ...models.resource_base import ResourceLifecycle
from ..adapter import IngestionError
from .parsing import load_json
from .parsing_common import parse_versions, resource_identity, walk_resources
from .plan_values import sanitize_plan_values


@dataclass(frozen=True)
class PlanResource:
    address: str
    terraform_type: str
    values: dict
    path: str
    values_path: str
    redacted_paths: list[str]
    unknown_paths: list[str]
    actions: list[LifecycleAction]
    before_values: dict
    actions_path: str | None = None


@dataclass(frozen=True)
class PlanExport:
    format_version: str
    terraform_version: str | None
    resources: list[PlanResource]
    skipped_paths: list[str]


def _invalid(message: str) -> IngestionError:
    return IngestionError("invalid_request", message)


def _identity(entry: Any) -> tuple[str, str] | None:
    identity = resource_identity(entry, object_error="Terraform plan resource must be an object")
    if identity is None:
        return None
    if entry.get("deposed") is not None:
        raise _invalid("Deposed Terraform plan resources are not supported")
    return identity


def load_plan(path: Path) -> PlanExport:
    return parse_plan(load_json(path))


def parse_plan(data: Any) -> PlanExport:
    try:
        return _parse_plan(data)
    except RecursionError as error:
        raise _invalid("Terraform plan nesting exceeds supported limits") from error


def _parse_plan(data: Any) -> PlanExport:
    if (
        not isinstance(data, dict)
        or "values" in data
        or "version" in data
        or not any(key in data for key in ("planned_values", "resource_changes"))
    ):
        raise _invalid("Expected terraform show -json plan, not state or internal state")
    version, terraform_version = parse_versions(data, "plan")
    changes = data.get("resource_changes", [])
    if not isinstance(changes, list):
        raise _invalid("Terraform resource_changes must be a list")
    resources: list[PlanResource] = []
    skipped: list[str] = []
    changed: dict[str, PlanResource] = {}
    for index, entry in enumerate(changes):
        location = f"/resource_changes/{index}"
        identity = _identity(entry)
        if identity is None:
            skipped.append(location)
            continue
        address, resource_type = identity
        if address in changed:
            raise _invalid("Terraform plan contains duplicate logical addresses")
        change = entry.get("change")
        if not isinstance(change, dict):
            raise _invalid("Terraform resource change must be an object")
        try:
            actions = ResourceLifecycle(actions=change.get("actions")).actions
        except ValidationError:
            raise _invalid("Unsupported Terraform lifecycle action sequence") from None
        before, after = change.get("before"), change.get("after")
        deleting = actions == [LifecycleAction.DELETE]
        if (
            (before is not None and not isinstance(before, dict))
            or (after is not None and not isinstance(after, dict))
            or not isinstance(before if deleting else after, dict)
        ):
            raise _invalid(
                "Terraform change requires object values for its selected lifecycle scope"
            )
        safe_before, before_redactions, _ = sanitize_plan_values(
            {} if before is None else before, change.get("before_sensitive", {}), {}
        )
        safe_after, after_redactions, unknown = sanitize_plan_values(
            {} if after is None else after,
            change.get("after_sensitive", {}),
            change.get("after_unknown", {}),
        )
        before_context = {
            key: value
            for key, value in safe_before.items()
            if key in {"id", "arn", "account_id", "owner_id", "region"}
        }
        scope = "before" if deleting else "after"
        resource = PlanResource(
            address,
            resource_type,
            safe_before if deleting else safe_after,
            location,
            f"{location}/change/{scope}",
            before_redactions if deleting else after_redactions,
            [] if deleting else unknown,
            actions,
            before_context,
            f"{location}/change/actions",
        )
        resources.append(resource)
        changed[address] = resource

    inventory_addresses: set[str] = set()

    if "planned_values" in data:
        planned = data["planned_values"]
        if not isinstance(planned, dict):
            raise _invalid("Terraform planned_values must be an object")
        inventory = (
            walk_resources(
                planned["root_module"],
                "/planned_values/root_module",
                module_error="Terraform planned module must be an object",
            )
            if "root_module" in planned
            else ()
        )
        for entry, location in inventory:
            identity = _identity(entry)
            if identity is None:
                skipped.append(location)
                continue
            address, resource_type = identity
            if address in inventory_addresses:
                raise _invalid("Terraform planned inventory contains duplicate logical addresses")
            inventory_addresses.add(address)
            if not isinstance(entry.get("values"), dict):
                raise _invalid("Terraform planned resource values must be an object")
            if address in changed:
                if changed[address].terraform_type != resource_type:
                    raise _invalid("Terraform plan inventory conflicts with resource changes")
                continue
            safe, redactions, _ = sanitize_plan_values(
                entry["values"], entry.get("sensitive_values", {}), {}
            )
            resources.append(
                PlanResource(
                    address,
                    resource_type,
                    safe,
                    location,
                    f"{location}/values",
                    redactions,
                    [],
                    [LifecycleAction.NO_OP],
                    {},
                )
            )
    return PlanExport(version, terraform_version, resources, skipped)
