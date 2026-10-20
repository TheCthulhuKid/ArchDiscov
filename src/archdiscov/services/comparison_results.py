"""Persist and load comparison results, always verified against both selected snapshots.

A saved comparison is verified by recomputing it from the two snapshots with its recorded
result limit: both snapshot UUIDs must match, and every label, entry, qualification, and
limitation must equal the recomputation. A reference or citation that does not exist on its own
side, a qualification that no longer matches recorded coverage or lifecycle, swapped sides, and
any tampering all fail that equality. Only the creation time is taken from the file.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from archdiscov.models.analysis.references import CoverageReference, ResourceReference
from archdiscov.models.cloud.scope import AwsScope, AzureScope
from archdiscov.models.comparison.entries import (
    AmbiguousMatch,
    FieldDifference,
    MatchedPair,
    Qualification,
    UnmatchedResource,
)
from archdiscov.models.comparison.results import (
    ComparisonLimitation,
    ComparisonResult,
    ComparisonSnapshot,
)
from archdiscov.models.resource_base import ResourceLifecycle

from .analysis_diagnostics import (
    AnalysisValidationError,
    AnalysisValidationIssue,
    safe_validation_issues,
    serialization_issue,
)
from .comparison import compare_indexes
from .publication import SERIALIZATION_ERRORS, publish_text, read_json, render_json
from .snapshot_access import SupportedSnapshot
from .validation_data import detach_data

_SUBJECT = "comparison result"
_COMPARED_FIELDS = ("left", "right", "matched", "unmatched", "ambiguous", "limitations")
_DIAGNOSTIC_FIELDS = frozenset(
    field
    for model in (
        ComparisonResult,
        ComparisonSnapshot,
        ComparisonLimitation,
        MatchedPair,
        UnmatchedResource,
        AmbiguousMatch,
        FieldDifference,
        Qualification,
        ResourceReference,
        CoverageReference,
        ResourceLifecycle,
        AwsScope,
        AzureScope,
    )
    for field in model.model_fields
)


class ComparisonValidationError(AnalysisValidationError):
    """Safe diagnostics for a comparison result that is invalid or does not fit its snapshots."""

    subject = "Comparison result"
    code = "invalid_comparison_result"


def parse_comparison_result(data: ComparisonResult | Mapping[str, Any]) -> ComparisonResult:
    """Revalidate detached data without exposing untrusted diagnostic details."""
    try:
        return ComparisonResult.model_validate(detach_data(data))
    except ValidationError as exc:
        raise ComparisonValidationError(
            safe_validation_issues(exc, _DIAGNOSTIC_FIELDS, _SUBJECT)
        ) from exc
    except (
        PydanticSerializationError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as exc:
        raise ComparisonValidationError((serialization_issue("Comparison result"),)) from exc


def validate_comparison_result(
    data: ComparisonResult | Mapping[str, Any], left: SupportedSnapshot, right: SupportedSnapshot
) -> ComparisonResult:
    """Revalidate detached data and verify it against the two snapshots it names."""
    result = parse_comparison_result(data)
    from archdiscov.graph import GraphIndex  # the graph package imports the services package

    left_index, right_index = GraphIndex(left), GraphIndex(right)
    for side, saved, index in (
        ("left", result.left, left_index),
        ("right", result.right, right_index),
    ):
        if saved.snapshot_id != index.snapshot_id:
            raise ComparisonValidationError(
                (
                    AnalysisValidationIssue(
                        (side, "snapshot_id"),
                        "snapshot_mismatch",
                        f"The {side} snapshot is not the one this comparison was made from.",
                    ),
                )
            )
    expected = compare_indexes(
        left_index, right_index, result_limit=result.result_limit, now=result.created_at
    )
    differing = tuple(
        AnalysisValidationIssue(
            (name,), "comparison_mismatch", f"The {name} do not match the selected snapshots."
        )
        for name in _COMPARED_FIELDS
        if getattr(result, name) != getattr(expected, name)
    )
    if differing:
        raise ComparisonValidationError(differing)
    return result


def save_comparison(
    result: ComparisonResult | Mapping[str, Any],
    path: str | Path,
    *,
    left: SupportedSnapshot,
    right: SupportedSnapshot,
) -> Path:
    """Publish a result atomically, only after it is verified against both snapshots.

    The destination is never overwritten, and nothing unverified is persisted.
    """
    path = Path(path)
    try:
        validated = validate_comparison_result(result, left, right)
        text = render_json(validated)
    except ComparisonValidationError as error:
        error.path = path
        raise
    except SERIALIZATION_ERRORS as error:
        raise ComparisonValidationError(
            (serialization_issue("Comparison result"),), path=path
        ) from error
    return publish_text(text, path, subject=_SUBJECT)


def load_comparison(
    path: str | Path, *, left: SupportedSnapshot, right: SupportedSnapshot
) -> ComparisonResult:
    """Read a saved result and verify it against the snapshots it was made from."""
    path = Path(path)
    data = read_json(path, subject=_SUBJECT)
    try:
        return validate_comparison_result(data, left, right)
    except ComparisonValidationError as error:
        error.path = path
        raise
