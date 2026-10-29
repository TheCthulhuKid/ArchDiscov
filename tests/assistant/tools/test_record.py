from uuid import UUID

import pytest
from pydantic import ValidationError

from archdiscov.assistant.tools.contracts import (
    BoundedList,
    IdKind,
    ToolOutput,
    UntrustedText,
    bounded,
    untrusted,
)
from archdiscov.assistant.tools.record import ReturnedIds, RunRecord, SnapshotReturnedIds
from archdiscov.models.resource_base import Model

LEFT = UUID("00000000-0000-4000-8000-000000000001")
RIGHT = UUID("00000000-0000-4000-8000-000000000002")


class _Citation(Model):
    evidence_id: str
    fact_path: str


class _Resource(Model):
    resource_id: str
    name: UntrustedText
    evidence_ids: tuple[str, ...] = ()
    citations: tuple[_Citation, ...] = ()


class _Search(ToolOutput):
    snapshot_id: UUID
    resources: BoundedList[_Resource]


class _Finding(Model):
    finding_id: str
    resource_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    related_resource_id: str | None = None


class _Side(Model):
    snapshot_id: UUID
    resource_id: str | None = None


class _Comparison(ToolOutput):
    left: _Side
    right: _Side


class _Unscoped(ToolOutput):
    resource_id: str


def _search(*resource_ids: str) -> _Search:
    return _Search(
        snapshot_id=LEFT,
        resources=bounded(
            [
                _Resource(
                    resource_id=resource_id,
                    name=untrusted(f"name of {resource_id}: evidence ev-not-an-id"),
                    evidence_ids=(f"ev-{resource_id}",),
                    citations=(_Citation(evidence_id=f"cite-{resource_id}", fact_path="/a"),),
                )
                for resource_id in resource_ids
            ],
            10,
        ),
    )


def test_outputs_record_every_id_field_exactly() -> None:
    # Untrusted text and other fields, such as a fact path, are not IDs.
    record = RunRecord()
    record.record_output(_search("vpc-b", "vpc-a"))
    assert record.returned() == ReturnedIds(
        snapshots=(
            SnapshotReturnedIds(
                snapshot_id=LEFT,
                evidence_ids=("cite-vpc-a", "cite-vpc-b", "ev-vpc-a", "ev-vpc-b"),
                resource_ids=("vpc-a", "vpc-b"),
            ),
        )
    )


def test_ids_are_scoped_to_the_nearest_snapshot_and_need_one() -> None:
    record = RunRecord()
    record.record_output(
        _Comparison(
            left=_Side(snapshot_id=LEFT, resource_id="db-1"), right=_Side(snapshot_id=RIGHT)
        )
    )
    returned = record.returned()
    assert returned.contains(IdKind.RESOURCE, LEFT, "db-1")
    assert not returned.contains(IdKind.RESOURCE, RIGHT, "db-1")
    # A snapshot that returned no IDs has no entry.
    assert [entry.snapshot_id for entry in returned.snapshots] == [LEFT]
    with pytest.raises(ValueError, match="no enclosing snapshot_id"):
        RunRecord().record_output(_Unscoped(resource_id="vpc-a"))


def test_explicit_recording_sorts_deduplicates_and_validates_ids() -> None:
    record = RunRecord()
    record.record(LEFT, IdKind.FINDING, ["f-2", "f-1"])
    record.record(LEFT, IdKind.FINDING, "f-1")
    record.record(RIGHT, IdKind.RESOURCE, [])
    with pytest.raises(ValueError):
        record.record(LEFT, IdKind.RESOURCE, [" "])
    assert record.returned() == ReturnedIds(
        snapshots=(SnapshotReturnedIds(snapshot_id=LEFT, finding_ids=("f-1", "f-2")),)
    )


def test_findings_record_finding_resource_and_evidence_ids() -> None:
    class _Findings(ToolOutput):
        snapshot_id: UUID
        findings: tuple[_Finding, ...]

    record = RunRecord()
    record.record_output(
        _Findings(
            snapshot_id=LEFT,
            findings=(
                _Finding(
                    finding_id="ownership:vpc-a",
                    resource_ids=("vpc-a",),
                    evidence_ids=("ev-1",),
                ),
            ),
        )
    )
    entry = record.returned().snapshots[0]
    assert entry.finding_ids == ("ownership:vpc-a",)
    assert entry.resource_ids == ("vpc-a",)
    assert entry.evidence_ids == ("ev-1",)


def test_returned_ids_must_be_sorted_and_unique() -> None:
    for snapshots in (
        [{"snapshot_id": str(RIGHT)}, {"snapshot_id": str(LEFT)}],
        [{"snapshot_id": str(LEFT), "resource_ids": ["b", "a"]}],
    ):
        with pytest.raises(ValidationError) as raised:
            ReturnedIds.model_validate({"snapshots": snapshots})
        assert raised.value.errors()[0]["type"] == "tool_returned_ids_order"
