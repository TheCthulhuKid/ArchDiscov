"""Public application services used by the CLI and future interfaces."""

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
from .evidence import EvidenceInspection, EvidenceNotFoundError, get_evidence
from .importing import IngestFormat, IngestResult, ingest_snapshot
from .ingestion import AccountOutcome, collect_and_save, collect_snapshots, select_adapter
from .neighbours import query_neighbours
from .ownership import OwnershipAnalyzer
from .paths import query_paths
from .queries import inspect_resource, search_resources
from .query_errors import QueryError
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

__all__ = [
    "AccountOutcome",
    "AnalysisValidationError",
    "AnalysisValidationIssue",
    "Analyzer",
    "AnalyzerError",
    "AnalyzerRegistry",
    "DEFAULT_REGISTRY",
    "OwnershipAnalyzer",
    "Qualification",
    "EvidenceInspection",
    "EvidenceNotFoundError",
    "IngestFormat",
    "IngestResult",
    "QueryError",
    "SnapshotConflictError",
    "SnapshotAccess",
    "SupportedSnapshot",
    "SnapshotError",
    "SnapshotInput",
    "SnapshotIOError",
    "SnapshotJSONError",
    "SnapshotValidationError",
    "SnapshotValidationIssue",
    "build_result",
    "collect_and_save",
    "collect_and_save_async",
    "collect_snapshots",
    "collect_snapshots_async",
    "get_evidence",
    "ingest_snapshot",
    "inspect_resource",
    "load_analysis",
    "load_snapshot",
    "query_neighbours",
    "query_paths",
    "qualify_status",
    "run_analyzer",
    "save_analysis",
    "save_snapshot",
    "search_resources",
    "select_adapter",
    "validate_snapshot",
    "validate_analysis_result",
    "upgrade_snapshot",
]
