"""Shared primitives for public Terraform state and plan inventories."""

import re
from collections.abc import Iterator
from typing import Any

from ..adapter import IngestionError


def _invalid(message: str) -> IngestionError:
    return IngestionError("invalid_request", message)


def parse_versions(data: dict, kind: str) -> tuple[str, str | None]:
    version = data.get("format_version")
    if not isinstance(version, str) or not re.fullmatch(r"1\.[0-9]+", version):
        raise _invalid(f"Unsupported Terraform {kind} format_version; supported major version: 1")
    terraform_version = data.get("terraform_version")
    if terraform_version is not None and (
        not isinstance(terraform_version, str)
        or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+[\w.+-]*", terraform_version)
    ):
        raise _invalid("Invalid Terraform version")
    return version, terraform_version


def resource_identity(entry: Any, *, object_error: str) -> tuple[str, str] | None:
    if not isinstance(entry, dict):
        raise _invalid(object_error)
    if entry.get("mode") == "data":
        return None
    if entry.get("mode") != "managed":
        raise _invalid("Unsupported Terraform resource mode")
    address, resource_type = entry.get("address"), entry.get("type")
    if (
        not isinstance(address, str)
        or not address.strip()
        or not isinstance(resource_type, str)
        or not re.fullmatch(r"aws_[a-z0-9_]+", resource_type)
        or entry.get("provider_name") != "registry.terraform.io/hashicorp/aws"
    ):
        raise _invalid("Managed resources require an AWS provider, type, and logical address")
    return address, resource_type


def walk_resources(
    module: Any, path: str, *, module_error: str, depth: int = 0
) -> Iterator[tuple[Any, str]]:
    if depth > 64:
        raise _invalid("Terraform module nesting exceeds supported limits")
    if not isinstance(module, dict):
        raise _invalid(module_error)
    entries, children = module.get("resources", []), module.get("child_modules", [])
    if not isinstance(entries, list) or not isinstance(children, list):
        raise _invalid("Terraform module resources and child_modules must be lists")
    for index, entry in enumerate(entries):
        yield entry, f"{path}/resources/{index}"
    for index, child in enumerate(children):
        yield from walk_resources(
            child, f"{path}/child_modules/{index}", module_error=module_error, depth=depth + 1
        )
