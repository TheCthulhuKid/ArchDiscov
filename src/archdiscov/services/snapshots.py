"""Validation and explicit-path persistence for architecture snapshots."""

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticSerializationError

from archdiscov.models.architecture import ArchitectureSnapshot

SnapshotInput = ArchitectureSnapshot | Mapping[str, Any]

_logger = logging.getLogger(__name__)


class SnapshotError(Exception):
    """A safe public diagnostic; the original exception remains in __cause__."""

    def __init__(self, message: str, *, code: str, path: Path | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


@dataclass(frozen=True)
class SnapshotValidationIssue:
    location: tuple[str | int, ...]
    type: str
    message: str


class SnapshotValidationError(SnapshotError):
    def __init__(
        self,
        details: tuple[SnapshotValidationIssue, ...],
        *,
        code: str = "invalid_snapshot",
        path: Path | None = None,
    ) -> None:
        super().__init__("Snapshot validation failed.", code=code, path=path)
        self.details = details


class SnapshotJSONError(SnapshotError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        path: Path,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        super().__init__(message, code=code, path=path)
        self.line = line
        self.column = column


class SnapshotConflictError(SnapshotError):
    def __init__(self, path: Path) -> None:
        super().__init__(
            "Destination already exists; choose a new path.", code="snapshot_conflict", path=path
        )


class SnapshotIOError(SnapshotError):
    def __init__(self, message: str, *, path: Path) -> None:
        super().__init__(message, code="snapshot_io_error", path=path)


def save_snapshot(snapshot: SnapshotInput, path: str | Path) -> Path:
    """Publish a freshly validated snapshot atomically, without overwriting a path."""
    path = Path(path)
    try:
        validated = validate_snapshot(snapshot)
        text = (
            json.dumps(
                validated.model_dump(mode="json"), indent=2, ensure_ascii=False, allow_nan=False
            )
            + "\n"
        )
        # Detect incompatible text before creating a temporary file.
        text.encode("utf-8")
    except SnapshotValidationError as exc:
        exc.path = path
        raise
    except (PydanticSerializationError, TypeError, ValueError, OverflowError) as exc:
        raise SnapshotValidationError(
            (SnapshotValidationIssue((), "serialization_error", "Snapshot is not UTF-8 JSON."),),
            path=path,
        ) from exc

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
        raise SnapshotIOError(
            "Unable to save snapshot; check the parent directory, write permissions, "
            "and filesystem hard-link support.",
            path=path,
        ) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                # Publication is the commit point; cleanup cannot turn it into a failed save.
                _logger.warning(
                    "Unable to remove snapshot temporary file %s (committed=%s).",
                    temporary_path,
                    committed,
                )
    return path


def _reject_json_constant(value: str) -> None:
    raise ValueError("Nonstandard JSON constant")


def load_snapshot(path: str | Path) -> ArchitectureSnapshot:
    """Read a UTF-8 JSON snapshot and apply the shared validation contract."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise SnapshotJSONError(
            "Snapshot JSON must use UTF-8 encoding.", code="invalid_encoding", path=path
        ) from exc
    except OSError as exc:
        raise SnapshotIOError(
            "Unable to read snapshot; check the path and read permissions.", path=path
        ) from exc
    try:
        data = json.loads(text, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as exc:
        raise SnapshotJSONError(
            f"Invalid JSON syntax at line {exc.lineno}, column {exc.colno}.",
            code="invalid_json",
            path=path,
            line=exc.lineno,
            column=exc.colno,
        ) from exc
    except (ValueError, RecursionError) as exc:
        raise SnapshotJSONError(
            "Invalid JSON value or excessive nesting.", code="invalid_json", path=path
        ) from exc
    try:
        return validate_snapshot(data)
    except SnapshotValidationError as exc:
        exc.path = path
        raise


def _safe_validation_message(error: dict[str, Any]) -> str:
    # Pydantic messages can interpolate raw discriminator tags, values, and contexts.
    if error["type"] == "unsupported_schema_version":
        return 'Unsupported snapshot schema version; supported versions: "1", "2".'
    if error["type"] == "value_error":
        for rule in (
            "resource identity requires a physical_id or logical_address",
            "resource id must match identity.stable_id",
            "duplicate resource",
            "account must match snapshot account",
            "region must match enclosing region",
            "snapshot_id must match snapshot",
            "unknown resource owner",
            "unknown evidence citation",
            "belongs to another resource",
            "unknown unresolved resource",
            "unknown source_id",
            "unknown target_id",
            "dependency requires exactly one of target_id or target",
            "schema 1 dependencies require target_id",
            "schema 2 dependencies require target",
            "unresolved target source_path must not name a sensitive field",
            "unsupported lifecycle action sequence",
            "lifecycle requires schema 2 and a Terraform plan",
            "grouping must match source resource region",
            "missing key",
            "invalid array index",
            "array index out of bounds",
            "cannot traverse scalar",
            "present or redacted evidence requires an explicit value",
            "unavailable evidence requires a limitation and no value/redactions",
            "redacted evidence requires redacted_paths",
            "coverage completed_at must not precede started_at",
            "successful coverage requires a count and no failures/unresolved values",
            "incomplete coverage requires a retained count and a limitation",
            "only successful or incomplete coverage can have a result_count",
            "failed coverage requires a failure",
            "skipped/unsupported coverage requires a reason and no collected data",
            "account-wide coverage uses account_wide, not regions",
            "coverage regions must be unique",
            "coverage requires at least one region or account_wide collection",
            "coverage operation outcomes must be unique",
            "global operation coverage requires account_wide scope",
            "operation coverage region must be included in requested scope",
            "collection_completed_at must not precede collection_started_at",
            "snapshot regions must be included in coverage.regions",
            "global_resources require account_wide coverage",
            "operation coverage account must match snapshot account",
            "operation coverage timestamps must fall within snapshot collection",
        ):
            if rule in error["msg"]:
                return f"Snapshot constraint failed: {rule}."
    messages = {
        "missing": "Required field is missing.",
        "extra_forbidden": "Unexpected field; remove it from the snapshot data.",
        "union_tag_invalid": "Unsupported discriminator; use a supported resource or source type.",
        "union_tag_not_found": "Resource or source discriminator is missing.",
        "string_type": "Expected a string.",
        "string_too_short": "String is too short; provide a nonempty value.",
        "string_pattern_mismatch": "String does not match the required field format.",
        "enum": "Value is not a supported enum member.",
        "model_type": "Expected snapshot object data.",
    }
    return messages.get(error["type"], f"Invalid snapshot field ({error['type']}).")


def _detach_data(data: Any) -> Any:
    if isinstance(data, BaseModel):
        return data.model_dump(mode="json", warnings="error")
    if isinstance(data, Mapping):
        return {key: _detach_data(value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [_detach_data(value) for value in data]
    return data


def validate_snapshot(data: SnapshotInput) -> ArchitectureSnapshot:
    """Validate detached current data, never trusting an already mutable model."""
    try:
        return ArchitectureSnapshot.model_validate(_detach_data(data))
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_context=False, include_url=False)
        details = tuple(
            SnapshotValidationIssue(error["loc"], error["type"], _safe_validation_message(error))
            for error in errors
        )
        code = (
            "unsupported_schema_version"
            if any(error["type"] == "unsupported_schema_version" for error in errors)
            else "invalid_snapshot"
        )
        raise SnapshotValidationError(details, code=code) from exc
    except (
        PydanticSerializationError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as exc:
        raise SnapshotValidationError(
            (
                SnapshotValidationIssue(
                    (),
                    "serialization_error",
                    "Snapshot data could not be serialized for validation.",
                ),
            )
        ) from exc
