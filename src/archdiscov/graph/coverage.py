from collections.abc import Iterable

from archdiscov.models.cloud_contracts import CloudOperationCoverage
from archdiscov.models.coverage_key import CoverageKey


class CoverageIndex:
    """Recorded outcomes only; absence never implies successful collection."""

    def __init__(self, operations: Iterable[CloudOperationCoverage]) -> None:
        self._records = {CoverageKey.from_record(record): record for record in operations}
        self.keys = tuple(
            sorted(self._records, key=lambda key: (key.location or "", key.service, key.operation))
        )

    def get(self, key: CoverageKey) -> CloudOperationCoverage | None:
        record = self._records.get(key)
        return record.model_copy(deep=True) if record is not None else None

    def records(self) -> tuple[CloudOperationCoverage, ...]:
        return tuple(self._records[key].model_copy(deep=True) for key in self.keys)
