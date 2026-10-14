"""Detached result validation against an explicitly selected snapshot, not analysis."""

from collections.abc import Mapping
from typing import Any

from archdiscov.models.analysis_results import AnalysisResult

from .analysis_diagnostics import (
    AnalysisValidationError,
    AnalysisValidationIssue,
    parse_analysis_result,
)
from .analysis_integrity import validate_integrity
from .snapshot_access import SnapshotAccess, SupportedSnapshot

__all__ = ["AnalysisValidationError", "AnalysisValidationIssue", "validate_analysis_result"]


def validate_analysis_result(
    data: AnalysisResult | Mapping[str, Any], snapshot: SupportedSnapshot
) -> AnalysisResult:
    """Revalidate current detached data and all references; return a new model.

    Snapshot validation uses the existing common access boundary. This checks
    provenance and structural integrity, not whether an analyzer's conclusion is true.
    """
    result = parse_analysis_result(data)
    access = SnapshotAccess.from_snapshot(snapshot)
    validate_integrity(result, access)
    return result
