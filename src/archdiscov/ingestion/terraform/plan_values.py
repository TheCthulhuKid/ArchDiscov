"""Remove unknown plan facts and redact sensitive values without changing list indexes."""

from copy import deepcopy
from typing import Any

from ...models.json_pointer import escape_pointer_token
from ...models.sanitization import sanitize_value
from ..adapter import IngestionError
from .sensitivity import REDACTED

_MISSING = object()
_MAX_DEPTH = 64


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
    # Validate/remove unknowns independently, even inside fully sensitive containers.
    _remove(values, unknown, "", 0, unknown_paths)
    masked = _redact(deepcopy(values), sensitive, unknown, "", 0, redacted_paths)
    safe, generic_paths = sanitize_value(masked)
    # Reapply unknown removal to the redacted copy. Sensitive containers are opaque,
    # so their unknown descendants have already been recorded and need no inspection.
    result = _omit(safe, "", set(unknown_paths))
    return (
        result if isinstance(result, dict) else {},
        sorted(set(redacted_paths + generic_paths)),
        sorted(set(unknown_paths)),
    )


def _absent(value: Any) -> bool:
    return value is _MISSING or value is None


def _at(items: list, index: int, default: Any = _MISSING) -> Any:
    return items[index] if index < len(items) else default


def _child(path: str, key: str) -> str:
    return f"{path}/{escape_pointer_token(key)}"


def _keys(source: dict, mask: dict) -> list[str]:
    """The keys of a value and its mask, in order; a mask key need not be in the value."""
    keys = list(dict.fromkeys([*source, *mask]))
    if not all(isinstance(key, str) for key in keys):
        raise _invalid()
    return keys


def _remove(value: Any, mask: Any, path: str, depth: int, unknown_paths: list[str]) -> Any:
    """Check `value` against its unknown mask; record unknown paths, return what is known."""
    if depth > _MAX_DEPTH:
        raise _invalid()
    if mask is True:
        unknown_paths.append(path)
        return _MISSING
    if mask is False:
        return value
    if isinstance(mask, dict):
        return _remove_object(value, mask, path, depth, unknown_paths)
    if isinstance(mask, list):
        return _remove_list(value, mask, path, depth, unknown_paths)
    raise _invalid()


def _remove_object(value: Any, mask: dict, path: str, depth: int, unknown_paths: list[str]) -> Any:
    if _absent(value):
        source = {}
    elif isinstance(value, dict):
        source = value
    else:
        raise _invalid()
    result = {}
    for key in _keys(source, mask):
        item = _remove(
            source.get(key, _MISSING),
            mask.get(key, False),
            _child(path, key),
            depth + 1,
            unknown_paths,
        )
        if item is not _MISSING:
            result[key] = item
    return result if isinstance(value, dict) else _MISSING


def _remove_list(value: Any, mask: list, path: str, depth: int, unknown_paths: list[str]) -> Any:
    absent = _absent(value)
    if not absent and not isinstance(value, list):
        raise _invalid()
    source = [] if absent else value
    if not absent and len(mask) > len(source):
        raise _invalid()
    previous = len(unknown_paths)
    result = [
        _remove(
            _at(source, index), _at(mask, index, False), f"{path}/{index}", depth + 1, unknown_paths
        )
        for index in range(max(len(source), len(mask)))
    ]
    if len(unknown_paths) > previous:
        unknown_paths.append(path)
        return _MISSING
    return _MISSING if absent else result


def _redact(
    value: Any, mask: Any, unknown_mask: Any, path: str, depth: int, redacted_paths: list[str]
) -> Any:
    """Replace each value under a sensitive mask; a value may be absent only where unknown."""
    if depth > _MAX_DEPTH:
        raise _invalid()
    if mask is True:
        redacted_paths.append(path)
        return _MISSING if value is _MISSING else REDACTED
    if mask is False:
        return value
    if isinstance(mask, dict):
        return _redact_object(value, mask, unknown_mask, path, depth, redacted_paths)
    if isinstance(mask, list):
        return _redact_list(value, mask, unknown_mask, path, depth, redacted_paths)
    raise _invalid()


def _redact_object(
    value: Any, mask: dict, unknown_mask: Any, path: str, depth: int, redacted_paths: list[str]
) -> Any:
    if isinstance(value, dict):
        source = value
    elif _absent(value) and _has_unknown(unknown_mask):
        source = {}
    else:
        raise _invalid()
    result = {}
    for key in _keys(source, mask):
        child_unknown = (
            unknown_mask.get(key, False) if isinstance(unknown_mask, dict) else unknown_mask is True
        )
        if key not in source and not _has_unknown(child_unknown):
            raise _invalid()
        item = _redact(
            source.get(key, _MISSING),
            mask.get(key, False),
            child_unknown,
            _child(path, key),
            depth + 1,
            redacted_paths,
        )
        if item is not _MISSING:
            result[key] = item
    return result if isinstance(value, dict) else _MISSING


def _redact_list(
    value: Any, mask: list, unknown_mask: Any, path: str, depth: int, redacted_paths: list[str]
) -> Any:
    absent = _absent(value)
    if not isinstance(value, list) and not (absent and _has_unknown(unknown_mask)):
        raise _invalid()
    source = [] if absent else value
    result = []
    for index in range(max(len(source), len(mask))):
        child_unknown = (
            _at(unknown_mask, index, False)
            if isinstance(unknown_mask, list)
            else unknown_mask is True
        )
        if index >= len(source) and not _has_unknown(child_unknown):
            raise _invalid()
        result.append(
            _redact(
                _at(source, index),
                _at(mask, index, False),
                child_unknown,
                f"{path}/{index}",
                depth + 1,
                redacted_paths,
            )
        )
    return _MISSING if absent else result


def _omit(value: Any, path: str, unknown_paths: set[str]) -> Any:
    """`value` without the recorded unknown paths."""
    if path in unknown_paths:
        return _MISSING
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            child = _omit(item, _child(path, key), unknown_paths)
            if child is not _MISSING:
                result[key] = child
        return result
    if isinstance(value, list):
        return [_omit(item, f"{path}/{index}", unknown_paths) for index, item in enumerate(value)]
    return value
