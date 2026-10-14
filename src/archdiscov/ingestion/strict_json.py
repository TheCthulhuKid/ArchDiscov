"""JSON decoder hooks; readers retain their own limits and error policies."""

from collections.abc import Callable
from typing import Any, NoReturn


def unique_object(
    pairs: list[tuple[str, Any]], *, error_factory: Callable[[], Exception] = ValueError
) -> dict:
    """Reject duplicate decoded keys without exposing keys or values in errors."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise error_factory()
        result[key] = value
    return result


def reject_constant(value: str, *, error_factory: Callable[[], Exception] = ValueError) -> NoReturn:
    """Reject NaN and infinities passed by parse_constant, not float overflow."""
    raise error_factory()
