"""Analyzer execution over an already-built graph index."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import TypeAdapter

from archdiscov.models.analysis.parameters import AnalyzerParameters
from archdiscov.models.analysis.results import AnalysisFinding, AnalysisResult
from archdiscov.models.enums import AnalyzerErrorCode, AnalyzerKind

from .analyzer_errors import AnalyzerError
from .validation_data import detach_data

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

_PARAMETERS = TypeAdapter(AnalyzerParameters)


class Analyzer(Protocol):
    """Reads recorded structure from an index; it never rebuilds or mutates it."""

    kind: AnalyzerKind
    version: str

    def analyze(self, index: GraphIndex, parameters: AnalyzerParameters) -> AnalysisResult: ...


def build_result(
    analyzer: Analyzer,
    index: GraphIndex,
    parameters: AnalyzerParameters,
    findings: Iterable[AnalysisFinding],
    *,
    created_at: datetime | None = None,
) -> AnalysisResult:
    """Bind findings to the index's snapshot, the analyzer identity, and parameters."""
    return AnalysisResult(
        snapshot_id=index.snapshot_id,
        analyzer=analyzer.kind,
        analyzer_version=analyzer.version,
        parameters=parameters,
        created_at=created_at or datetime.now(UTC),
        findings=sorted(findings, key=lambda finding: finding.id),
    )


class AnalyzerRegistry:
    """Deterministic, immutable mapping from analyzer kind to implementation."""

    def __init__(self, analyzers: Iterable[Analyzer] = ()) -> None:
        registered: dict[AnalyzerKind, Analyzer] = {}
        for analyzer in analyzers:
            if analyzer.kind in registered:
                raise ValueError(f"duplicate analyzer for kind {analyzer.kind}")
            registered[analyzer.kind] = analyzer
        self._analyzers = registered

    @property
    def kinds(self) -> tuple[AnalyzerKind, ...]:
        return tuple(sorted(self._analyzers))

    def get(self, kind: AnalyzerKind) -> Analyzer:
        analyzer = self._analyzers.get(kind)
        if analyzer is None:
            raise AnalyzerError(AnalyzerErrorCode.UNSUPPORTED_ANALYZER)
        return analyzer


# No analyzer is implemented yet; the ownership analyzer is registered in step 26.3.
DEFAULT_REGISTRY = AnalyzerRegistry()


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
