"""Safe public diagnostics for snapshot and analysis-result files."""

from dataclasses import dataclass
from pathlib import Path


class SnapshotError(Exception):
    """A safe public diagnostic; the original exception remains in __cause__."""

    def __init__(self, message: str, *, code: str, path: Path | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


@dataclass(frozen=True)
class SnapshotValidationIssue:
    location: tuple[str | int, ...]
    type: str
    message: str


class SnapshotValidationError(SnapshotError):
    def __init__(
        self,
        details: tuple[SnapshotValidationIssue, ...],
        *,
        code: str = "invalid_snapshot",
        path: Path | None = None,
    ) -> None:
        super().__init__("Snapshot validation failed.", code=code, path=path)
        self.details = details


class SnapshotJSONError(SnapshotError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        path: Path,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        super().__init__(message, code=code, path=path)
        self.line = line
        self.column = column


class SnapshotConflictError(SnapshotError):
    def __init__(self, path: Path) -> None:
        super().__init__(
            "Destination already exists; choose a new path.", code="snapshot_conflict", path=path
        )


class SnapshotIOError(SnapshotError):
    def __init__(self, message: str, *, path: Path) -> None:
        super().__init__(message, code="snapshot_io_error", path=path)
