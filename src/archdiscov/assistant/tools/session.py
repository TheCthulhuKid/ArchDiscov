"""A tool session: the data the user selected, indexed and verified once, and the run's record.

The model never chooses data. Snapshots (and, for comparisons, a second snapshot), saved
analysis results, and saved comparisons are fixed when the session is created from user
input, and each saved result is verified against the snapshot it names before any tool can
read it. Tool inputs cannot name paths or snapshots.
"""

from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from archdiscov.graph import GraphIndex
from archdiscov.models.analysis.results import AnalysisResult
from archdiscov.models.comparison.results import ComparisonResult
from archdiscov.services.analyses import load_analysis
from archdiscov.services.analysis_validation import validate_analysis_result
from archdiscov.services.comparison_results import (
    parse_comparison_result,
    validate_comparison_result,
)
from archdiscov.services.publication import read_json
from archdiscov.services.snapshot_access import SupportedSnapshot
from archdiscov.services.snapshots import load_snapshot

from .contracts import ToolLimits
from .record import RunRecord


class ToolSessionError(ValueError):
    """A saved result that names no selected snapshot; the message is fixed."""

    def __init__(self) -> None:
        super().__init__("Saved result does not belong to the selected snapshots.")


class ToolSession:
    """Owns one `GraphIndex` per selected snapshot, verified saved results, and the record."""

    def __init__(
        self,
        snapshot: SupportedSnapshot,
        *,
        comparison: SupportedSnapshot | None = None,
        analyses: Sequence[AnalysisResult] = (),
        comparisons: Sequence[ComparisonResult] = (),
        limits: ToolLimits | None = None,
    ) -> None:
        self._index = GraphIndex(snapshot)
        self._comparison_index = None if comparison is None else GraphIndex(comparison)
        self._snapshots: dict[UUID, SupportedSnapshot] = {self._index.snapshot_id: snapshot}
        if comparison is not None:
            self._snapshots.setdefault(self._comparison_index.snapshot_id, comparison)
        self._analyses = tuple(
            validate_analysis_result(result, self._snapshot_for(result.snapshot_id))
            for result in analyses
        )
        self._comparisons = tuple(
            validate_comparison_result(
                result,
                self._snapshot_for(result.left.snapshot_id),
                self._snapshot_for(result.right.snapshot_id),
            )
            for result in comparisons
        )
        self._limits = ToolLimits() if limits is None else limits
        self._record = RunRecord()
        for index in (self._index, self._comparison_index):
            if index is not None:
                self._record.record_snapshot(
                    index.snapshot_id, index.source_kind, index.collection_window
                )

    @classmethod
    def from_paths(
        cls,
        snapshot_path: Path,
        *,
        comparison_path: Path | None = None,
        analysis_paths: Sequence[Path] = (),
        comparison_result_paths: Sequence[Path] = (),
        limits: ToolLimits | None = None,
    ) -> "ToolSession":
        """Load user-selected files with the shared snapshot and result validation."""
        return cls(
            load_snapshot(snapshot_path),
            comparison=None if comparison_path is None else load_snapshot(comparison_path),
            analyses=[load_analysis(path) for path in analysis_paths],
            comparisons=[
                parse_comparison_result(read_json(Path(path), subject="comparison result"))
                for path in comparison_result_paths
            ],
            limits=limits,
        )

    def _snapshot_for(self, snapshot_id: UUID) -> SupportedSnapshot:
        snapshot = self._snapshots.get(snapshot_id)
        if snapshot is None:
            raise ToolSessionError()
        return snapshot

    def snapshot(self, snapshot_id: UUID) -> SupportedSnapshot | None:
        """A selected snapshot by ID, for integrity checks of new results."""
        return self._snapshots.get(snapshot_id)

    @property
    def index(self) -> GraphIndex:
        return self._index

    @property
    def comparison_index(self) -> GraphIndex | None:
        """The second snapshot selected for comparisons, if any."""
        return self._comparison_index

    @property
    def analyses(self) -> tuple[AnalysisResult, ...]:
        """Saved analysis results the user selected, each verified against its snapshot."""
        return self._analyses

    @property
    def comparisons(self) -> tuple[ComparisonResult, ...]:
        """Saved comparisons the user selected, each verified against both snapshots."""
        return self._comparisons

    @property
    def limits(self) -> ToolLimits:
        return self._limits

    @property
    def record(self) -> RunRecord:
        return self._record
