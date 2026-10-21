"""Public application services used by the CLI and future interfaces.

Names load on first use, so importing one service module (for example
`archdiscov.services.queries`) does not import collection and its AWS SDK.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

_EXPORTS = {
    "load_analysis": "analyses",
    "save_analysis": "analyses",
    "DEFAULT_REGISTRY": "analysis_execution",
    "run_analyzer": "analysis_execution",
    "Qualification": "analysis_qualification",
    "qualify_status": "analysis_qualification",
    "AnalysisValidationError": "analysis_validation",
    "AnalysisValidationIssue": "analysis_validation",
    "validate_analysis_result": "analysis_validation",
    "AnalyzerError": "analyzer_errors",
    "Analyzer": "analyzers",
    "AnalyzerRegistry": "analyzers",
    "build_result": "analyzers",
    "collect_and_save_async": "async_ingestion",
    "collect_snapshots_async": "async_ingestion",
    "compare_snapshots": "comparison",
    "ComparisonValidationError": "comparison_results",
    "load_comparison": "comparison_results",
    "save_comparison": "comparison_results",
    "validate_comparison_result": "comparison_results",
    "CrossAccountTrustAnalyzer": "cross_account_trust",
    "EvidenceInspection": "evidence",
    "EvidenceNotFoundError": "evidence",
    "get_evidence": "evidence",
    "IngestFormat": "importing",
    "IngestResult": "importing",
    "ingest_snapshot": "importing",
    "AccountOutcome": "ingestion",
    "collect_and_save": "ingestion",
    "collect_snapshots": "ingestion",
    "select_adapter": "ingestion",
    "query_neighbours": "neighbours",
    "OwnershipAnalyzer": "ownership",
    "query_paths": "paths",
    "inspect_resource": "queries",
    "search_resources": "queries",
    "QueryError": "query_errors",
    "SecurityGroupImpactAnalyzer": "security_group_impact",
    "SnapshotAccess": "snapshot_access",
    "SupportedSnapshot": "snapshot_access",
    "upgrade_snapshot": "snapshot_access",
    "SnapshotConflictError": "snapshots",
    "SnapshotError": "snapshots",
    "SnapshotIOError": "snapshots",
    "SnapshotInput": "snapshots",
    "SnapshotJSONError": "snapshots",
    "SnapshotValidationError": "snapshots",
    "SnapshotValidationIssue": "snapshots",
    "load_snapshot": "snapshots",
    "save_snapshot": "snapshots",
    "validate_snapshot": "snapshots",
}

__all__ = [
    "AccountOutcome",
    "AnalysisValidationError",
    "AnalysisValidationIssue",
    "Analyzer",
    "AnalyzerError",
    "AnalyzerRegistry",
    "ComparisonValidationError",
    "CrossAccountTrustAnalyzer",
    "DEFAULT_REGISTRY",
    "EvidenceInspection",
    "EvidenceNotFoundError",
    "IngestFormat",
    "IngestResult",
    "OwnershipAnalyzer",
    "Qualification",
    "QueryError",
    "SecurityGroupImpactAnalyzer",
    "SnapshotAccess",
    "SnapshotConflictError",
    "SnapshotError",
    "SnapshotIOError",
    "SnapshotInput",
    "SnapshotJSONError",
    "SnapshotValidationError",
    "SnapshotValidationIssue",
    "SupportedSnapshot",
    "build_result",
    "collect_and_save",
    "collect_and_save_async",
    "collect_snapshots",
    "collect_snapshots_async",
    "compare_snapshots",
    "get_evidence",
    "ingest_snapshot",
    "inspect_resource",
    "load_analysis",
    "load_comparison",
    "load_snapshot",
    "qualify_status",
    "query_neighbours",
    "query_paths",
    "run_analyzer",
    "save_analysis",
    "save_comparison",
    "save_snapshot",
    "search_resources",
    "select_adapter",
    "upgrade_snapshot",
    "validate_analysis_result",
    "validate_comparison_result",
    "validate_snapshot",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORTS})


if TYPE_CHECKING:
    from .analyses import load_analysis, save_analysis
    from .analysis_execution import DEFAULT_REGISTRY, run_analyzer
    from .analysis_qualification import Qualification, qualify_status
    from .analysis_validation import (
        AnalysisValidationError,
        AnalysisValidationIssue,
        validate_analysis_result,
    )
    from .analyzer_errors import AnalyzerError
    from .analyzers import Analyzer, AnalyzerRegistry, build_result
    from .async_ingestion import collect_and_save_async, collect_snapshots_async
    from .comparison import compare_snapshots
    from .comparison_results import (
        ComparisonValidationError,
        load_comparison,
        save_comparison,
        validate_comparison_result,
    )
    from .cross_account_trust import CrossAccountTrustAnalyzer
    from .evidence import EvidenceInspection, EvidenceNotFoundError, get_evidence
    from .importing import IngestFormat, IngestResult, ingest_snapshot
    from .ingestion import AccountOutcome, collect_and_save, collect_snapshots, select_adapter
    from .neighbours import query_neighbours
    from .ownership import OwnershipAnalyzer
    from .paths import query_paths
    from .queries import inspect_resource, search_resources
    from .query_errors import QueryError
    from .security_group_impact import SecurityGroupImpactAnalyzer
    from .snapshot_access import SnapshotAccess, SupportedSnapshot, upgrade_snapshot
    from .snapshots import (
        SnapshotConflictError,
        SnapshotError,
        SnapshotInput,
        SnapshotIOError,
        SnapshotJSONError,
        SnapshotValidationError,
        SnapshotValidationIssue,
        load_snapshot,
        save_snapshot,
        validate_snapshot,
    )
