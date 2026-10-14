"""Parameter preparation and context-only pseudo parameters; no AWS lookups."""

import math
from copy import deepcopy

from ...models.json_pointer import escape_pointer_token
from ...models.sanitization import is_sensitive_path, sanitize_value
from ..adapter import IngestionError
from ..evidence_builder import REDACTED, redact_tag_pairs
from ..region_scope import region_partition


def _is_permitted_sensitive_path(parts: list[str]) -> bool:
    if len(parts) < 2:
        return False
    section = parts[0]
    if section == "Conditions":
        return True
    if len(parts) < 3:
        return False
    if section == "Resources":
        return parts[2] == "Properties"
    if section == "Parameters":
        return parts[2] == "Default"
    return section == "Mappings"


def prepare_template(template, request):
    safe = deepcopy(template)
    for path in request.sensitive_paths:
        parts = [part.replace("~1", "/").replace("~0", "~") for part in path.split("/")[1:]]
        node = safe
        try:
            if not _is_permitted_sensitive_path(parts):
                raise ValueError
            for part in parts[:-1]:
                node = node[_index(part)] if isinstance(node, list) else node[part]
            key = _index(parts[-1]) if isinstance(node, list) else parts[-1]
            if isinstance(node, dict) and key not in node:
                raise KeyError(key)
            node[key] = REDACTED
        except (KeyError, IndexError, TypeError, ValueError):
            raise IngestionError(
                "invalid_request", "Sensitive path does not identify a value"
            ) from None
    return safe


def _index(token):
    if not token.isascii() or not token.isdigit() or str(int(token)) != token:
        raise ValueError
    return int(token)


def parameters_for(template, request):
    definitions = template.get("Parameters", {})
    if set(request.parameters) - set(definitions):
        raise IngestionError("invalid_request", "Supplied parameter is not declared in template")
    result = {}
    for name, definition in definitions.items():
        if not isinstance(definition, dict) or not isinstance(definition.get("Type"), str):
            raise IngestionError("invalid_request", "Parameter requires a declared type")
        if "NoEcho" in definition and not isinstance(definition["NoEcho"], bool):
            raise IngestionError("invalid_request", "NoEcho must be a boolean")
        if name not in request.parameters and "Default" not in definition:
            continue
        value = request.parameters.get(name, definition.get("Default"))
        if (
            definition.get("NoEcho")
            or is_sensitive_path(name)
            or definition.get("Default") == REDACTED
            or value == REDACTED
        ):
            result[name] = REDACTED
            continue
        kind = definition["Type"]
        if kind.startswith("AWS::SSM::Parameter::Value"):
            continue
        try:
            if kind in ("CommaDelimitedList", "List<Number>") or kind.startswith("List<AWS::"):
                values = value.split(",") if isinstance(value, str) else value
                if not isinstance(values, list) or any(
                    not isinstance(item, (str, int, float)) or isinstance(item, bool)
                    for item in values
                ):
                    raise ValueError
                value = [item.strip() if isinstance(item, str) else item for item in values]
                if kind == "List<Number>":
                    value = [_number(item) for item in value]
            elif kind == "Number":
                value = _number(value)
            elif kind == "String" or kind.startswith("AWS::"):
                if not isinstance(value, str):
                    raise ValueError
            else:
                continue
        except (ValueError, TypeError, OverflowError):
            raise IngestionError(
                "invalid_request", "Parameter value does not match its type"
            ) from None
        result[name] = sanitize_value(value)[0]
    return result


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError
    number = float(value)
    if not math.isfinite(number):
        raise ValueError
    return int(number) if number.is_integer() else number


def pseudo_parameters(request):
    region = request.region.value
    partition = region_partition(request.region)
    if request.stack_id and request.stack_id.split(":")[1] != partition:
        raise IngestionError("invalid_request", "Stack partition must match its region")
    result = {
        "AWS::AccountId": request.account_id,
        "AWS::Region": region,
        "AWS::Partition": partition,
    }
    suffixes = {"aws": "amazonaws.com", "aws-cn": "amazonaws.com.cn", "aws-us-gov": "amazonaws.com"}
    if partition in suffixes:
        result["AWS::URLSuffix"] = suffixes[partition]
    if request.stack_id:
        result["AWS::StackId"] = request.stack_id
        result["AWS::StackName"] = request.stack_id.split(":", 5)[5].split("/")[1]
    if request.stack_name:
        result["AWS::StackName"] = request.stack_name
    return result


def safe_properties(value):
    safe, tags = redact_tag_pairs(value)
    safe, paths = sanitize_value(safe)
    return safe, sorted(set(tags + paths + marker_paths(safe)))


def marker_paths(value, path=""):
    if value == REDACTED:
        return [path]
    if isinstance(value, dict):
        return [
            p
            for key, child in value.items()
            for p in marker_paths(child, f"{path}/{escape_pointer_token(key)}")
        ]
    if isinstance(value, list):
        return [
            p for index, child in enumerate(value) for p in marker_paths(child, f"{path}/{index}")
        ]
    return []
