"""Persist and load analysis results, always checked against an explicit snapshot."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from archdiscov.models.analysis.results import AnalysisResult

from .analysis_diagnostics import (
    AnalysisValidationError,
    AnalysisValidationIssue,
    parse_analysis_result,
)
from .analysis_validation import validate_analysis_result
from .publication import SERIALIZATION_ERRORS, publish_text, read_json, render_json
from .snapshot_access import SupportedSnapshot

_SUBJECT = "analysis result"


def save_analysis(
    result: AnalysisResult | Mapping[str, Any], path: str | Path, *, snapshot: SupportedSnapshot
) -> Path:
    """Publish a result atomically, only after it passes integrity against its snapshot.

    The destination is never overwritten. Nothing unverified is persisted, so a
    saved file can later be reloaded against the same snapshot.
    """
    path = Path(path)
    try:
        validated = validate_analysis_result(result, snapshot)
    except AnalysisValidationError as error:
        error.path = path
        raise
    try:
        text = render_json(validated)
    except SERIALIZATION_ERRORS as error:
        raise AnalysisValidationError(
            (
                AnalysisValidationIssue(
                    (), "serialization_error", "Analysis result is not UTF-8 JSON."
                ),
            ),
            path=path,
        ) from error
    return publish_text(text, path, subject=_SUBJECT)


def load_analysis(path: str | Path, *, snapshot: SupportedSnapshot | None = None) -> AnalysisResult:
    """Read and validate a saved result; with a snapshot, also check its integrity.

    Without a snapshot only the result contract is checked, so references are not
    known to resolve. Pass the snapshot named by the result for the full check.
    """
    path = Path(path)
    data = read_json(path, subject=_SUBJECT)
    try:
        if snapshot is None:
            return parse_analysis_result(data)
        return validate_analysis_result(data, snapshot)
    except AnalysisValidationError as error:
        error.path = path
        raise
