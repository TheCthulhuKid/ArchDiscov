"""Apply Terraform's recursive sensitive_values masks before inspecting facts."""

from typing import Any

from ...models.json_pointer import escape_pointer_token
from ...models.json_pointer import read_pointer as read_pointer
from ...models.sanitization import sanitize_value
from ..adapter import IngestionError

REDACTED = "[REDACTED]"


def sanitize_values(values: dict, mask: Any) -> tuple[dict, list[str]]:
    paths: list[str] = []

    def visit(value: Any, sensitive: Any, path: str) -> Any:
        if sensitive is True:
            paths.append(path)
            return REDACTED
        if sensitive is False or sensitive is None:
            return value
        if isinstance(sensitive, dict) and isinstance(value, dict):
            if set(sensitive) - set(value):
                raise IngestionError("invalid_request", "Sensitive mask names an absent value")
            return {
                key: visit(item, sensitive.get(key, False), f"{path}/{escape_pointer_token(key)}")
                for key, item in value.items()
            }
        if isinstance(sensitive, list) and isinstance(value, list):
            if len(sensitive) > len(value):
                raise IngestionError("invalid_request", "Sensitive mask exceeds its value list")
            return [
                visit(
                    item, sensitive[index] if index < len(sensitive) else False, f"{path}/{index}"
                )
                for index, item in enumerate(value)
            ]
        raise IngestionError("invalid_request", "Sensitive mask does not match its value shape")

    masked = visit(values, mask, "")
    if not isinstance(masked, dict):
        # Whole-resource sensitivity means no attributes can be inspected, including IDs.
        return {}, [""]
    safe, generic_paths = sanitize_value(masked)
    return safe, sorted(set(paths + generic_paths))
