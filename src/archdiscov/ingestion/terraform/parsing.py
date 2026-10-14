"""Strict, local parsing of the public terraform show -json state format."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..adapter import IngestionError
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


def load_export(path: Path) -> StateExport:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise _invalid("Terraform JSON contains duplicate object keys")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise _invalid("Terraform JSON must contain finite JSON values")

    try:
        data = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=invalid_constant,
        )
    except UnicodeError as error:
        raise _invalid("Terraform export must be UTF-8 JSON") from error
    except json.JSONDecodeError as error:
        raise _invalid(
            f"Invalid Terraform JSON at line {error.lineno}, column {error.colno}"
        ) from error
    except OSError as error:
        raise _invalid("Could not read Terraform state export") from error
    except RecursionError as error:
        raise _invalid("Terraform JSON nesting exceeds supported limits") from error
    return parse_export(data)


def parse_export(data: Any) -> StateExport:
    if not isinstance(data, dict) or any(
        key in data for key in ("planned_values", "resource_changes", "prior_state", "version")
    ):
        raise _invalid("Expected terraform show -json state, not a plan or internal state file")
    version = data.get("format_version")
    if not isinstance(version, str) or not re.fullmatch(r"1\.[0-9]+", version):
        raise _invalid("Unsupported Terraform state format_version; supported major version: 1")
    terraform_version = data.get("terraform_version")
    if terraform_version is not None and (
        not isinstance(terraform_version, str)
        or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+[\w.+-]*", terraform_version)
    ):
        raise _invalid("Invalid Terraform version")
    values = data.get("values")
    if not isinstance(values, dict) or not isinstance(values.get("root_module"), dict):
        raise _invalid("Terraform state requires values.root_module (an empty module is allowed)")
    resources: list[StateResource] = []
    skipped: list[str] = []
    addresses: set[str] = set()

    def walk(module: dict, path: str, depth: int) -> None:
        if depth > 64:
            raise _invalid("Terraform module nesting exceeds supported limits")
        entries = module.get("resources", [])
        children = module.get("child_modules", [])
        if not isinstance(entries, list) or not isinstance(children, list):
            raise _invalid("Terraform module resources and child_modules must be lists")
        for index, entry in enumerate(entries):
            location = f"{path}/resources/{index}"
            if not isinstance(entry, dict):
                raise _invalid("Terraform resource entry must be an object")
            mode = entry.get("mode")
            if mode == "data":
                skipped.append(location)
                continue
            if mode != "managed":
                raise _invalid("Unsupported Terraform resource mode")
            address, resource_type = entry.get("address"), entry.get("type")
            if (
                not isinstance(address, str)
                or not address.strip()
                or not isinstance(resource_type, str)
                or not re.fullmatch(r"aws_[a-z0-9_]+", resource_type)
                or entry.get("provider_name") != "registry.terraform.io/hashicorp/aws"
            ):
                raise _invalid(
                    "Managed resources require an AWS provider, type, and logical address"
                )
            if address in addresses:
                raise _invalid("Terraform state contains duplicate logical addresses")
            addresses.add(address)
            raw_values = entry.get("values")
            if not isinstance(raw_values, dict):
                raise _invalid("Terraform resource values must be an object")
            safe, redactions = sanitize_values(raw_values, entry.get("sensitive_values", {}))
            resources.append(StateResource(address, resource_type, safe, location, redactions))
        for index, child in enumerate(children):
            if not isinstance(child, dict):
                raise _invalid("Terraform child module must be an object")
            walk(child, f"{path}/child_modules/{index}", depth + 1)

    walk(values["root_module"], "/values/root_module", 0)
    return StateExport(version, terraform_version, resources, skipped)
