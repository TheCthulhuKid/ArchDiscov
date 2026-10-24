"""Shared UTF-8 JSON file reading and atomic, no-overwrite publication."""

import json
import logging
import os
import tempfile
from functools import partial
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_core import PydanticSerializationError

from archdiscov.core.strict_json import (
    MAX_JSON_FILE_BYTES,
    FileTooLargeError,
    read_bounded,
    reject_constant,
    unique_object,
)

from .snapshot_errors import SnapshotConflictError, SnapshotIOError, SnapshotJSONError

SERIALIZATION_ERRORS = (PydanticSerializationError, TypeError, ValueError, OverflowError)

_logger = logging.getLogger(__name__)


def render_json(model: BaseModel) -> str:
    """Render strict UTF-8 JSON text; raises one of SERIALIZATION_ERRORS if impossible."""
    text = (
        json.dumps(model.model_dump(mode="json"), indent=2, ensure_ascii=False, allow_nan=False)
        + "\n"
    )
    # Detect incompatible text before creating a temporary file.
    text.encode("utf-8")
    return text


def _unwritable(path: Path, *, subject: str) -> SnapshotIOError:
    return SnapshotIOError(
        f"Unable to save {subject}; check the parent directory, write permissions, "
        "and filesystem hard-link support.",
        path=path,
    )


def require_new_path(path: Path, *, subject: str) -> None:
    """Refuse a destination `publish_text` would refuse, before any work is done for it.

    Publication still makes the atomic check; this only fails early and cheaply.
    """
    if os.path.lexists(path):
        raise SnapshotConflictError(path)
    if not path.parent.is_dir():
        raise _unwritable(path, subject=subject)


def publish_text(text: str, path: Path, *, subject: str) -> Path:
    """Atomically publish text at a new path; never overwrite an existing destination."""
    temporary_path: Path | None = None
    committed = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=".archdiscov-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError as exc:
            raise SnapshotConflictError(path) from exc
        committed = True
    except OSError as exc:
        raise _unwritable(path, subject=subject) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                # Publication is the commit point; cleanup cannot turn it into a failed save.
                _logger.warning(
                    "Unable to remove temporary file %s (committed=%s).",
                    temporary_path,
                    committed,
                )
    return path


class _DuplicateKeysError(ValueError):
    pass


def read_json(path: Path, *, subject: str, limit: int = MAX_JSON_FILE_BYTES) -> Any:
    """Read bounded, strict UTF-8 JSON with safe diagnostics; the caller validates it.

    Strict like every other reader: duplicate object keys and non-finite numbers are
    invalid, since either would make a file mean something other than it appears to.
    """
    try:
        content = read_bounded(path, limit)
    except FileTooLargeError as exc:
        raise SnapshotJSONError(
            f"{subject.capitalize()} JSON exceeds {limit} bytes.",
            code="json_too_large",
            path=path,
        ) from exc
    except OSError as exc:
        raise SnapshotIOError(
            f"Unable to read {subject}; check the path and read permissions.", path=path
        ) from exc
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise SnapshotJSONError(
            f"{subject.capitalize()} JSON must use UTF-8 encoding.",
            code="invalid_encoding",
            path=path,
        ) from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=partial(unique_object, error_factory=_DuplicateKeysError),
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise SnapshotJSONError(
            f"Invalid JSON syntax at line {exc.lineno}, column {exc.colno}.",
            code="invalid_json",
            path=path,
            line=exc.lineno,
            column=exc.colno,
        ) from exc
    except _DuplicateKeysError as exc:
        raise SnapshotJSONError(
            "JSON contains duplicate object keys.", code="invalid_json", path=path
        ) from exc
    except (ValueError, RecursionError) as exc:
        raise SnapshotJSONError(
            "Invalid JSON value or excessive nesting.", code="invalid_json", path=path
        ) from exc
