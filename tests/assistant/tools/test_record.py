from uuid import UUID

import pytest
from pydantic import Field, ValidationError

from archdiscov.assistant.tools.contracts import (
    BoundedList,
    IdKind,
    ToolLimits,
    ToolOutput,
    UntrustedText,
    bounded,
    render,
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
    findings: dict[str, _Finding] = Field(default_factory=dict)


class _Unscoped(ToolOutput):
    resource_id: str


def _search(*resource_ids: str, snapshot_id: UUID = LEFT) -> _Search:
    return _Search(
        snapshot_id=snapshot_id,
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


def test_ids_are_scoped_to_the_nearest_snapshot() -> None:
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


def test_ids_outside_any_snapshot_are_rejected() -> None:
    with pytest.raises(ValueError, match="no enclosing snapshot_id"):
        RunRecord().record_output(_Unscoped(resource_id="vpc-a"))
    with pytest.raises(ValueError, match="no enclosing snapshot_id"):
        RunRecord().record_output(
            _Comparison(
                left=_Side(snapshot_id=LEFT),
                right=_Side(snapshot_id=RIGHT),
                findings={"f": _Finding(finding_id="f-1", resource_ids=(), evidence_ids=())},
            )
        )


def test_untrusted_text_and_other_fields_are_not_recorded() -> None:
    record = RunRecord()
    record.record_output(_search("vpc-a"))
    returned = record.returned()
    assert not returned.contains(IdKind.EVIDENCE, LEFT, "ev-not-an-id")
    assert not returned.contains(IdKind.RESOURCE, LEFT, "/a")


def test_recording_is_deterministic_and_deduplicated() -> None:
    first, second = RunRecord(), RunRecord()
    first.record_output(_search("b", "a"))
    first.record_output(_search("a", snapshot_id=RIGHT))
    second.record_output(_search("a", snapshot_id=RIGHT))
    second.record_output(_search("a", "b"))
    second.record_output(_search("b"))
    assert first.returned() == second.returned()
    assert first.returned().model_dump_json() == second.returned().model_dump_json()


def test_returned_ids_are_detached_from_later_recording_and_caller_changes() -> None:
    record = RunRecord()
    finding_ids = ["f-2", "f-1"]
    record.record(LEFT, IdKind.FINDING, finding_ids)
    before = record.returned()
    finding_ids.append("f-3")
    record.record(LEFT, IdKind.FINDING, "f-4")
    assert before.snapshots[0].finding_ids == ("f-1", "f-2")
    assert record.returned().snapshots[0].finding_ids == ("f-1", "f-2", "f-4")
    with pytest.raises(ValidationError):
        before.snapshots = ()


def test_explicit_recording_validates_ids() -> None:
    record = RunRecord()
    for bad in ([""], [" "], [None], [3]):
        with pytest.raises(ValueError):
            record.record(LEFT, IdKind.RESOURCE, bad)
    assert record.returned() == ReturnedIds()


def test_optional_id_fields_left_empty_are_skipped() -> None:
    record = RunRecord()
    record.record_output(
        _Comparison(
            left=_Side(snapshot_id=LEFT),
            right=_Side(snapshot_id=RIGHT),
        )
    )
    assert record.returned() == ReturnedIds()


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
                    related_resource_id=None,
                ),
            ),
        )
    )
    entry = record.returned().snapshots[0]
    assert entry.finding_ids == ("ownership:vpc-a",)
    assert entry.resource_ids == ("vpc-a",)
    assert entry.evidence_ids == ("ev-1",)


def test_only_delivered_outputs_are_recorded() -> None:
    record = RunRecord()
    oversized = _search(*[f"vpc-{index:03}" for index in range(10)])
    _, delivered = render("call_1", oversized, ToolLimits(max_output_chars=1_024))
    if isinstance(delivered, ToolOutput):
        record.record_output(delivered)
    assert record.returned() == ReturnedIds()


@pytest.mark.parametrize(
    "data",
    [
        {"snapshots": [{"snapshot_id": str(RIGHT)}, {"snapshot_id": str(LEFT)}]},
        {"snapshots": [{"snapshot_id": str(LEFT)}, {"snapshot_id": str(LEFT)}]},
        {"snapshots": [{"snapshot_id": str(LEFT), "resource_ids": ["b", "a"]}]},
        {"snapshots": [{"snapshot_id": str(LEFT), "resource_ids": ["a", "a"]}]},
    ],
)
def test_returned_ids_must_be_sorted_and_unique(data: dict) -> None:
    with pytest.raises(ValidationError) as raised:
        ReturnedIds.model_validate(data)
    assert raised.value.errors()[0]["type"] == "tool_returned_ids_order"


def test_returned_ids_round_trip() -> None:
    record = RunRecord()
    record.record_output(_search("vpc-a", "vpc-b"))
    returned = record.returned()
    assert ReturnedIds.model_validate_json(returned.model_dump_json()) == returned
    assert not returned.contains(IdKind.RESOURCE, RIGHT, "vpc-a")


def test_recording_no_ids_adds_no_snapshot_entry() -> None:
    record = RunRecord()
    record.record(LEFT, IdKind.RESOURCE, [])
    assert record.returned() == ReturnedIds()


def test_bounded_lists_of_ids_are_recorded() -> None:
    class _Listed(ToolOutput):
        snapshot_id: UUID
        resource_ids: BoundedList[str]

    record = RunRecord()
    record.record_output(_Listed(snapshot_id=LEFT, resource_ids=bounded(["c", "a", "b"], 2)))
    assert record.returned().snapshots[0].resource_ids == ("a", "c")
