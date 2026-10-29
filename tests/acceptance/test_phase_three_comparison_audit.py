"""Audit comparisons across representative pairs of snapshot source kinds.

Driven from `SnapshotSourceKind`: a new source kind has no builder, and the audit fails. The
pairs cover each distinct case: the same kind, two AWS kinds (one planned), and both clouds.
"""

import pytest

from archdiscov.models.enums import ComparisonLimitationKind, SnapshotSourceKind
from archdiscov.services import (
    ComparisonValidationError,
    SnapshotConflictError,
    compare_snapshots,
    load_comparison,
    save_comparison,
)
from tests.support.comparison_data import aws_snapshot, azure_snapshot, coverage, rid, vpc

KINDS = list(SnapshotSourceKind)
DELETE = {"actions": ["delete"], "value_scope": "before"}
COVERAGE = {"unrecorded": (), "failed": (coverage("failed"),), "success": (coverage("success"),)}


def build(kind, side, *, operations=()):
    """A snapshot of `kind` with vpc-1 (attribute `a` is the side) and one resource of its own."""
    if kind == SnapshotSourceKind.AZURE_FIXTURE:
        return azure_snapshot()
    shared = vpc("vpc-1", attributes={"a": side})
    only = vpc(f"vpc-only-{side}")
    if kind == SnapshotSourceKind.TERRAFORM_PLAN:
        shared, only = ({**item, "lifecycle": DELETE} for item in (shared, only))
    return aws_snapshot(
        [shared, only], kind.value, seed=side, operations=operations if side == 2 else ()
    )


def is_azure(kind):
    return kind == SnapshotSourceKind.AZURE_FIXTURE


def test_every_source_kind_is_built_by_the_audit():
    for kind in KINDS:
        assert build(kind, 1).source is not None


K = SnapshotSourceKind
AWS_PAIRS = [
    (K.AWS, K.AWS),
    (K.AWS, K.TERRAFORM_PLAN),
    (K.TERRAFORM_PLAN, K.CLOUDFORMATION),
    (K.TERRAFORM_STATE, K.MOCK),
]
PAIRS = [*AWS_PAIRS, (K.AWS, K.AZURE_FIXTURE), (K.AZURE_FIXTURE, K.AZURE_FIXTURE)]


@pytest.mark.parametrize(("left_kind", "right_kind"), PAIRS, ids=lambda kind: kind.value)
def test_every_source_kind_pair_compares_labels_and_limits_honestly(
    tmp_path, left_kind, right_kind
):
    left, right = build(left_kind, 1), build(right_kind, 2)
    before = (left.model_dump_json(), right.model_dump_json())
    result = compare_snapshots(left, right)
    assert (left.model_dump_json(), right.model_dump_json()) == before
    assert (
        result.left.snapshot_id == left.snapshot_id
        and result.right.snapshot_id == right.snapshot_id
    )
    assert (result.left.source_kind, result.right.source_kind) == (left_kind, right_kind)

    expected = set()
    if left_kind != right_kind:
        expected.add(ComparisonLimitationKind.SOURCE_KIND_MISMATCH)
    if is_azure(left_kind) != is_azure(right_kind):
        expected.add(ComparisonLimitationKind.CROSS_CLOUD)
    assert {item.kind for item in result.limitations} == expected

    path = save_comparison(result, tmp_path / "comparison.json", left=left, right=right)
    original = path.read_bytes()
    assert load_comparison(path, left=left, right=right) == result
    with pytest.raises(SnapshotConflictError):
        save_comparison(result, path, left=left, right=right)
    assert path.read_bytes() == original
    if left.snapshot_id != right.snapshot_id:  # swapping a snapshot with itself changes nothing
        with pytest.raises(ComparisonValidationError):
            load_comparison(path, left=right, right=left)
    assert path.read_bytes() == original


@pytest.mark.parametrize(("left_kind", "right_kind"), PAIRS, ids=lambda kind: kind.value)
def test_cross_cloud_pairs_match_nothing_and_same_cloud_pairs_match_identity(left_kind, right_kind):
    result = compare_snapshots(build(left_kind, 1), build(right_kind, 2))
    if is_azure(left_kind) != is_azure(right_kind):
        assert result.matched == [] and result.ambiguous == []
        coverage_kinds = {"limited_coverage", "unrecorded_coverage"}  # another cloud's coverage
        assert (
            not {str(q.kind) for u in result.unmatched for q in u.qualifications} & coverage_kinds
        )
    elif is_azure(left_kind):
        assert result.matched and not result.unmatched
    else:
        [pair] = result.matched
        assert pair.left.resource_id == pair.right.resource_id == rid("vpc-1")
        assert str(pair.basis) == "physical_identity"
        assert [(d.path, d.left_value, d.right_value) for d in pair.differences] == [
            ("/attributes/a", 1, 2)
        ]
        assert {(str(u.side), u.resource.resource_id) for u in result.unmatched} == {
            ("left", rid("vpc-only-1")),
            ("right", rid("vpc-only-2")),
        }


@pytest.mark.parametrize("recorded", COVERAGE)
@pytest.mark.parametrize(("left_kind", "right_kind"), AWS_PAIRS, ids=lambda kind: kind.value)
def test_absences_follow_the_other_sides_recorded_coverage(left_kind, right_kind, recorded):
    left = build(left_kind, 1, operations=())
    right = build(right_kind, 2, operations=COVERAGE[recorded])
    result = compare_snapshots(left, right)
    by_side = {str(item.side): item for item in result.unmatched}

    absent_from_right = {(str(q.kind), str(q.side)) for q in by_side["left"].qualifications}
    expected = {
        "unrecorded": {("unrecorded_coverage", "right")},
        "failed": {("limited_coverage", "right")},
    }
    expected_left = expected.get(recorded, set())
    if left_kind == SnapshotSourceKind.TERRAFORM_PLAN:
        expected_left = expected_left | {("planned_lifecycle", "left")}
    assert absent_from_right == expected_left

    # The left side records no coverage at all, so what is missing from it is never silently absent.
    absent_from_left = {(str(q.kind), str(q.side)) for q in by_side["right"].qualifications}
    expected_right = {("unrecorded_coverage", "left")}
    if right_kind == SnapshotSourceKind.TERRAFORM_PLAN:
        expected_right = expected_right | {("planned_lifecycle", "right")}
    assert absent_from_left == expected_right


@pytest.mark.parametrize("right_kind", [K.AWS, K.TERRAFORM_PLAN], ids=lambda kind: kind.value)
def test_planned_lifecycle_qualifies_the_pair_and_the_difference_is_not_an_absence(right_kind):
    result = compare_snapshots(build(SnapshotSourceKind.AWS, 1), build(right_kind, 2))
    [pair] = result.matched
    planned = [q for q in pair.qualifications if str(q.kind) == "planned_lifecycle"]
    if right_kind == SnapshotSourceKind.TERRAFORM_PLAN:
        [qualification] = planned
        assert str(qualification.side) == "right"
        assert qualification.lifecycle.actions == ["delete"]
    else:
        assert planned == []
    assert all(
        str(q.kind) != "planned_lifecycle"
        for entry in result.unmatched
        if str(entry.side) == "left"
        for q in entry.qualifications
    )
