"""Analyzer execution over an already-built graph index."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from archdiscov.models.analysis.parameters import AnalyzerParameters
from archdiscov.models.analysis.results import AnalysisFinding, AnalysisResult
from archdiscov.models.enums import AnalyzerErrorCode, AnalyzerKind

from .analyzer_errors import AnalyzerError

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex


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
