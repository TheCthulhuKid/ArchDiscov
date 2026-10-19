"""Validation and explicit-path persistence for architecture snapshots."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from archdiscov.models.architecture import ArchitectureSnapshot
from archdiscov.models.cloud.snapshot import CloudSnapshot
from archdiscov.models.validation_errors import SAFE_VALIDATION_MESSAGES

from .publication import SERIALIZATION_ERRORS, publish_text, read_json, render_json
from .snapshot_access import SupportedSnapshot
from .snapshot_errors import (
    SnapshotConflictError,
    SnapshotError,
    SnapshotIOError,
    SnapshotJSONError,
    SnapshotValidationError,
    SnapshotValidationIssue,
)
from .validation_data import detach_data as _detach_data

SnapshotInput = SupportedSnapshot | Mapping[str, Any]


__all__ = [
    "SnapshotConflictError",
    "SnapshotError",
    "SnapshotIOError",
    "SnapshotInput",
    "SnapshotJSONError",
    "SnapshotValidationError",
    "SnapshotValidationIssue",
    "load_snapshot",
    "save_snapshot",
    "validate_snapshot",
]


def save_snapshot(snapshot: SnapshotInput, path: str | Path) -> Path:
    """Publish a freshly validated snapshot atomically, without overwriting a path."""
    path = Path(path)
    try:
        validated = validate_snapshot(snapshot)
    except SnapshotValidationError as exc:
        exc.path = path
        raise
    return _save_validated_snapshot(validated, path)


def _save_validated_snapshot(snapshot: SupportedSnapshot, path: str | Path) -> Path:
    """Publish an already detached, validated snapshot without validating it again."""
    path = Path(path)
    try:
        text = render_json(snapshot)
    except SERIALIZATION_ERRORS as exc:
        raise SnapshotValidationError(
            (SnapshotValidationIssue((), "serialization_error", "Snapshot is not UTF-8 JSON."),),
            path=path,
        ) from exc
    return publish_text(text, path, subject="snapshot")


def load_snapshot(path: str | Path) -> SupportedSnapshot:
    """Read a UTF-8 JSON snapshot and apply the shared validation contract."""
    path = Path(path)
    data = read_json(path, subject="snapshot")
    try:
        return validate_snapshot(data)
    except SnapshotValidationError as exc:
        exc.path = path
        raise


def _safe_validation_message(error: dict[str, Any]) -> str:
    # Pydantic messages can interpolate raw discriminator tags, values, and contexts.
    if error["type"] in SAFE_VALIDATION_MESSAGES:
        return SAFE_VALIDATION_MESSAGES[error["type"]]
    if error["type"] == "unsupported_schema_version":
        return 'Unsupported snapshot schema version; supported versions: "1", "2", "3".'
    if error["type"] == "value_error":
        # Incremental compatibility for validators not yet migrated to custom codes.
        # Migrated checks above never inspect validator text or context.
        for rule in (
            "ARM resource type must match identity native_type",
            "platform must match",
            "native_type must match",
            "scope must match",
            "subscription/tenant must match",
            "account must match snapshot scope",
            "location must match enclosing location",
            "evidence context must match owning resource",
            "coverage locations must be unique",
            "scope-wide placement uses null location",
            "AWS location must match scope partition",
            "resource ARN partition must match snapshot scope",
            "Azure location must be a canonical lowercase location",
            "operation coverage location must be included in requested scope",
            "snapshot locations must be included in coverage",
            "scope_resources require scope_wide coverage",
            "dependency grouping must match source resource location",
            "lifecycle requires AWS Terraform plan source semantics",
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
    return messages.get(error["type"], "Invalid snapshot field.")


def validate_snapshot(data: SnapshotInput) -> SupportedSnapshot:
    """Validate detached current data, never trusting an already mutable model."""
    try:
        detached = _detach_data(data)
        if isinstance(detached, Mapping):
            version = detached.get("schema_version")
            if "schema_version" in detached and version not in ("1", "2", "3"):
                raise SnapshotValidationError(
                    (
                        SnapshotValidationIssue(
                            ("schema_version",),
                            "unsupported_schema_version",
                            "Unsupported snapshot schema version; "
                            'supported versions: "1", "2", "3".',
                        ),
                    ),
                    code="unsupported_schema_version",
                )
            model = CloudSnapshot if version == "3" else ArchitectureSnapshot
        else:
            model = ArchitectureSnapshot
        return model.model_validate(detached)
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
