"""Remove unknown plan facts and redact sensitive values without changing list indexes."""

from copy import deepcopy
from typing import Any

from ...models.json_pointer import escape_pointer_token
from ...models.sanitization import sanitize_value
from ..adapter import IngestionError
from .sensitivity import REDACTED

_MISSING = object()


def _invalid() -> IngestionError:
    return IngestionError("invalid_request", "Terraform plan mask does not match its value shape")


def _has_unknown(mask: Any) -> bool:
    if isinstance(mask, dict):
        return any(_has_unknown(item) for item in mask.values())
    if isinstance(mask, list):
        return any(_has_unknown(item) for item in mask)
    return mask is True


def sanitize_plan_values(
    values: dict, sensitive: Any, unknown: Any
) -> tuple[dict, list[str], list[str]]:
    """Return safe facts, redactions, and unknown paths relative to the input values.

    A list containing an unknown descendant is omitted as a whole: placeholders
    would imply explicit nulls, while compaction would corrupt evidence indexes.
    Both the omitted list and the exact unknown descendants are recorded.
    """
    unknown_paths: list[str] = []
    redacted_paths: list[str] = []

    def remove(value: Any, mask: Any, path: str, depth: int) -> Any:
        if depth > 64:
            raise _invalid()
        if mask is True:
            unknown_paths.append(path)
            return _MISSING
        if mask is False:
            return value
        if isinstance(mask, dict):
            if value is _MISSING or value is None:
                source = {}
            elif isinstance(value, dict):
                source = value
            else:
                raise _invalid()
            result = {}
            for key in dict.fromkeys([*source, *mask]):
                if not isinstance(key, str):
                    raise _invalid()
                item = remove(
                    source.get(key, _MISSING),
                    mask.get(key, False),
                    f"{path}/{escape_pointer_token(key)}",
                    depth + 1,
                )
                if item is not _MISSING:
                    result[key] = item
            return result if isinstance(value, dict) else _MISSING
        if isinstance(mask, list):
            absent = value is _MISSING or value is None
            if not absent and not isinstance(value, list):
                raise _invalid()
            source = [] if absent else value
            if not absent and len(mask) > len(source):
                raise _invalid()
            previous = len(unknown_paths)
            result = [
                remove(
                    source[index] if index < len(source) else _MISSING,
                    mask[index] if index < len(mask) else False,
                    f"{path}/{index}",
                    depth + 1,
                )
                for index in range(max(len(source), len(mask)))
            ]
            if len(unknown_paths) > previous:
                unknown_paths.append(path)
                return _MISSING
            return _MISSING if absent else result
        raise _invalid()

    def redact(value: Any, mask: Any, unknown_mask: Any, path: str, depth: int) -> Any:
        if depth > 64:
            raise _invalid()
        if mask is True:
            redacted_paths.append(path)
            return _MISSING if value is _MISSING else REDACTED
        if mask is False:
            return value
        if isinstance(mask, dict):
            if isinstance(value, dict):
                source = value
            elif (value is _MISSING or value is None) and _has_unknown(unknown_mask):
                source = {}
            else:
                raise _invalid()
            result = {}
            for key in dict.fromkeys([*source, *mask]):
                if not isinstance(key, str):
                    raise _invalid()
                child_unknown = (
                    unknown_mask.get(key, False)
                    if isinstance(unknown_mask, dict)
                    else unknown_mask is True
                )
                if key not in source and not _has_unknown(child_unknown):
                    raise _invalid()
                item = redact(
                    source.get(key, _MISSING),
                    mask.get(key, False),
                    child_unknown,
                    f"{path}/{escape_pointer_token(key)}",
                    depth + 1,
                )
                if item is not _MISSING:
                    result[key] = item
            return result if isinstance(value, dict) else _MISSING
        if isinstance(mask, list):
            absent = value is _MISSING or value is None
            if not isinstance(value, list) and not (absent and _has_unknown(unknown_mask)):
                raise _invalid()
            source = [] if absent else value
            result = []
            for index in range(max(len(source), len(mask))):
                child_unknown = (
                    (unknown_mask[index] if index < len(unknown_mask) else False)
                    if isinstance(unknown_mask, list)
                    else unknown_mask is True
                )
                if index >= len(source) and not _has_unknown(child_unknown):
                    raise _invalid()
                result.append(
                    redact(
                        source[index] if index < len(source) else _MISSING,
                        mask[index] if index < len(mask) else False,
                        child_unknown,
                        f"{path}/{index}",
                        depth + 1,
                    )
                )
            return _MISSING if absent else result
        raise _invalid()

    # Validate/remove unknowns independently, even inside fully sensitive containers.
    remove(values, unknown, "", 0)
    masked = redact(deepcopy(values), sensitive, unknown, "", 0)
    safe, generic_paths = sanitize_value(masked)

    # Reapply unknown removal to the redacted copy. Sensitive containers are opaque,
    # so their unknown descendants have already been recorded and need no inspection.
    def omit(value: Any, path: str) -> Any:
        if path in unknown_paths:
            return _MISSING
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                child = omit(item, f"{path}/{escape_pointer_token(key)}")
                if child is not _MISSING:
                    result[key] = child
            return result
        if isinstance(value, list):
            return [omit(item, f"{path}/{index}") for index, item in enumerate(value)]
        return value

    result = omit(safe, "")
    return (
        result if isinstance(result, dict) else {},
        sorted(set(redacted_paths + generic_paths)),
        sorted(set(unknown_paths)),
    )
