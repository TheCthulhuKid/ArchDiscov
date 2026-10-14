"""Strict, local parsing of the public terraform show -json state format."""

import json
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from ..adapter import IngestionError
from ..strict_json import reject_constant, unique_object
from .parsing_common import parse_versions, resource_identity, walk_resources
from .sensitivity import sanitize_values


@dataclass(frozen=True)
class StateResource:
    address: str
    terraform_type: str
    values: dict
    path: str
    redacted_paths: list[str]


@dataclass(frozen=True)
class StateExport:
    format_version: str
    terraform_version: str | None
    resources: list[StateResource]
    skipped_paths: list[str]


def _invalid(message: str) -> IngestionError:
    return IngestionError("invalid_request", message)


def load_json(path: Path) -> Any:
    try:
        data = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=partial(
                unique_object,
                error_factory=lambda: _invalid("Terraform JSON contains duplicate object keys"),
            ),
            parse_constant=partial(
                reject_constant,
                error_factory=lambda: _invalid("Terraform JSON must contain finite JSON values"),
            ),
        )
    except UnicodeError as error:
        raise _invalid("Terraform export must be UTF-8 JSON") from error
    except json.JSONDecodeError as error:
        raise _invalid(
            f"Invalid Terraform JSON at line {error.lineno}, column {error.colno}"
        ) from error
    except OSError as error:
        raise _invalid("Could not read Terraform export") from error
    except RecursionError as error:
        raise _invalid("Terraform JSON nesting exceeds supported limits") from error
    return data


def load_export(path: Path) -> StateExport:
    return parse_export(load_json(path))


def parse_export(data: Any) -> StateExport:
    if not isinstance(data, dict) or any(
        key in data for key in ("planned_values", "resource_changes", "prior_state", "version")
    ):
        raise _invalid("Expected terraform show -json state, not a plan or internal state file")
    version, terraform_version = parse_versions(data, "state")
    values = data.get("values")
    if not isinstance(values, dict) or not isinstance(values.get("root_module"), dict):
        raise _invalid("Terraform state requires values.root_module (an empty module is allowed)")
    resources: list[StateResource] = []
    skipped: list[str] = []
    addresses: set[str] = set()

    for entry, location in walk_resources(
        values["root_module"],
        "/values/root_module",
        module_error="Terraform child module must be an object",
    ):
        identity = resource_identity(
            entry, object_error="Terraform resource entry must be an object"
        )
        if identity is None:
            skipped.append(location)
            continue
        address, resource_type = identity
        if address in addresses:
            raise _invalid("Terraform state contains duplicate logical addresses")
        addresses.add(address)
        raw_values = entry.get("values")
        if not isinstance(raw_values, dict):
            raise _invalid("Terraform resource values must be an object")
        safe, redactions = sanitize_values(raw_values, entry.get("sensitive_values", {}))
        resources.append(StateResource(address, resource_type, safe, location, redactions))
    return StateExport(version, terraform_version, resources, skipped)
