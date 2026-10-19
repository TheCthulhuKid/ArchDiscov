"""Run registered analyzers with validated parameters and checked results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter

from archdiscov.models.analysis.parameters import AnalyzerParameters
from archdiscov.models.analysis.results import AnalysisResult
from archdiscov.models.enums import AnalyzerErrorCode

from .analyzer_errors import AnalyzerError
from .analyzers import AnalyzerRegistry
from .cross_account_trust import CrossAccountTrustAnalyzer
from .ownership import OwnershipAnalyzer
from .security_group_impact import SecurityGroupImpactAnalyzer
from .validation_data import detach_data

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

_PARAMETERS = TypeAdapter(AnalyzerParameters)

DEFAULT_REGISTRY = AnalyzerRegistry(
    [OwnershipAnalyzer(), SecurityGroupImpactAnalyzer(), CrossAccountTrustAnalyzer()]
)


def run_analyzer(
    index: GraphIndex,
    parameters: Mapping[str, Any] | Any,
    *,
    registry: AnalyzerRegistry = DEFAULT_REGISTRY,
) -> AnalysisResult:
    """Validate parameters, run the registered analyzer, and check its result's identity.

    The returned result is revalidated and detached from the analyzer's objects.
    Integrity against the snapshot itself is checked separately by
    `validate_analysis_result`.
    """
    try:
        typed = _PARAMETERS.validate_python(detach_data(parameters))
    except ValueError as error:  # includes pydantic validation and serialization errors
        raise AnalyzerError(AnalyzerErrorCode.INVALID_PARAMETERS) from error
    analyzer = registry.get(typed.kind)
    result = analyzer.analyze(index, typed)
    try:
        checked = AnalysisResult.model_validate(detach_data(result))
    except (ValueError, TypeError) as error:
        raise AnalyzerError(AnalyzerErrorCode.INVALID_RESULT) from error
    if (
        checked.snapshot_id != index.snapshot_id
        or checked.analyzer != typed.kind
        or checked.analyzer_version != analyzer.version
        or checked.parameters != typed
    ):
        raise AnalyzerError(AnalyzerErrorCode.INVALID_RESULT)
    return checked
