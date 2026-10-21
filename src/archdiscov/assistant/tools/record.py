"""The per-run record of every ID a tool returned to the model.

Step 37 accepts an answer only if each finding, evidence, and resource ID it cites was
returned by a tool in the same run, for the same snapshot. The record collects IDs from
delivered outputs by field name (`ID_FIELDS`), scoped to the nearest enclosing
`snapshot_id`; its snapshot is sorted and detached from later recording or caller changes.
"""

from collections.abc import Iterable
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, model_validator

from archdiscov.models.resource_base import NonEmptyString
from archdiscov.models.validation_errors import validation_error

from ..conversation import Contract
from .contracts import ID_FIELDS, IdKind, ToolOutput


class SnapshotReturnedIds(Contract):
    snapshot_id: UUID
    finding_ids: tuple[NonEmptyString, ...] = ()
    evidence_ids: tuple[NonEmptyString, ...] = ()
    resource_ids: tuple[NonEmptyString, ...] = ()

    @model_validator(mode="after")
    def require_sorted_unique(self) -> Self:
        for ids in (self.finding_ids, self.evidence_ids, self.resource_ids):
            if list(ids) != sorted(set(ids)):
                raise validation_error("tool_returned_ids_order", "sorted and unique")
        return self


class ReturnedIds(Contract):
    """Every ID returned in a run, per snapshot, in sorted order."""

    snapshots: tuple[SnapshotReturnedIds, ...] = ()

    @model_validator(mode="after")
    def require_sorted_snapshots(self) -> Self:
        ids = [entry.snapshot_id for entry in self.snapshots]
        if ids != sorted(set(ids)):
            raise validation_error("tool_returned_ids_order", "sorted and unique snapshots")
        return self

    def contains(self, kind: IdKind, snapshot_id: UUID, value: str) -> bool:
        for entry in self.snapshots:
            if entry.snapshot_id == snapshot_id:
                return value in getattr(entry, f"{IdKind(kind).value}_ids")
        return False


class RunRecord:
    """Accumulates returned IDs; `returned()` is an immutable sorted snapshot."""

    def __init__(self) -> None:
        self._ids: dict[UUID, dict[IdKind, set[str]]] = {}

    def record(self, snapshot_id: UUID, kind: IdKind, ids: Iterable[str]) -> None:
        values = [ids] if isinstance(ids, str) else list(ids)
        if not all(isinstance(value, str) and value.strip() for value in values):
            raise ValueError("IDs must be non-blank strings")
        if not values:
            return
        bucket = self._ids.setdefault(UUID(str(snapshot_id)), {kind: set() for kind in IdKind})
        bucket[IdKind(kind)].update(values)

    def record_output(self, output: ToolOutput) -> None:
        """Collect every ID field in a delivered output; IDs outside a snapshot are an error."""
        found: list[tuple[UUID, IdKind, Any]] = []
        _collect(output, None, found)
        for snapshot_id, kind, value in found:
            self.record(snapshot_id, kind, value)

    def returned(self) -> ReturnedIds:
        return ReturnedIds(
            snapshots=tuple(
                SnapshotReturnedIds(
                    snapshot_id=snapshot_id,
                    finding_ids=tuple(sorted(ids[IdKind.FINDING])),
                    evidence_ids=tuple(sorted(ids[IdKind.EVIDENCE])),
                    resource_ids=tuple(sorted(ids[IdKind.RESOURCE])),
                )
                for snapshot_id, ids in sorted(self._ids.items())
            )
        )


def _collect(value: Any, snapshot_id: UUID | None, found: list) -> None:
    if isinstance(value, BaseModel):
        fields = type(value).model_fields
        if "snapshot_id" in fields and getattr(value, "snapshot_id") is not None:
            snapshot_id = getattr(value, "snapshot_id")
        for name in fields:
            item = getattr(value, name)
            if name in ID_FIELDS:
                if item is None:
                    continue
                if snapshot_id is None:
                    raise ValueError(f"{type(value).__name__}.{name} has no enclosing snapshot_id")
                found.append((snapshot_id, ID_FIELDS[name], item))
            else:
                _collect(item, snapshot_id, found)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect(item, snapshot_id, found)
    elif isinstance(value, dict):
        for item in value.values():
            _collect(item, snapshot_id, found)
