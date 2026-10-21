"""A tool session: the snapshots the user selected, indexed once, and the run's record.

The model never chooses data. Snapshots (and, for comparisons, a second snapshot) are
fixed when the session is created from user input; tool inputs cannot name paths or
snapshots.
"""

from pathlib import Path

from archdiscov.graph import GraphIndex
from archdiscov.services.snapshot_access import SupportedSnapshot
from archdiscov.services.snapshots import load_snapshot

from .contracts import ToolLimits
from .record import RunRecord


class ToolSession:
    """Owns one `GraphIndex` per selected snapshot, built once, and the run record."""

    def __init__(
        self,
        snapshot: SupportedSnapshot,
        *,
        comparison: SupportedSnapshot | None = None,
        limits: ToolLimits | None = None,
    ) -> None:
        self._index = GraphIndex(snapshot)
        self._comparison_index = None if comparison is None else GraphIndex(comparison)
        self._limits = ToolLimits() if limits is None else limits
        self._record = RunRecord()

    @classmethod
    def from_paths(
        cls,
        snapshot_path: Path,
        *,
        comparison_path: Path | None = None,
        limits: ToolLimits | None = None,
    ) -> "ToolSession":
        """Load user-selected snapshot files with the shared snapshot validation."""
        return cls(
            load_snapshot(snapshot_path),
            comparison=None if comparison_path is None else load_snapshot(comparison_path),
            limits=limits,
        )

    @property
    def index(self) -> GraphIndex:
        return self._index

    @property
    def comparison_index(self) -> GraphIndex | None:
        """The second snapshot selected for comparisons, if any."""
        return self._comparison_index

    @property
    def limits(self) -> ToolLimits:
        return self._limits

    @property
    def record(self) -> RunRecord:
        return self._record
