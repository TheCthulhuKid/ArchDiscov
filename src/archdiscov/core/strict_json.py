"""JSON decoder hooks and a bounded file read; readers keep their own error policies."""

from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

# The largest JSON file a reader accepts unless it sets its own limit: snapshots of large
# accounts are big, but a file past this is far more likely a mistake than an architecture.
MAX_JSON_FILE_BYTES = 256 * 1024 * 1024


class FileTooLargeError(ValueError):
    """A file exceeds its reader's limit; it is not read past the limit."""


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


def read_bounded(path: Path, limit: int = MAX_JSON_FILE_BYTES) -> bytes:
    """The file's bytes, reading at most one byte past `limit` to detect a larger file.

    Raises `FileTooLargeError` past the limit, and `OSError` when the file cannot be read.
    """
    with path.open("rb") as file:
        content = file.read(limit + 1)
    if len(content) > limit:
        raise FileTooLargeError
    return content
