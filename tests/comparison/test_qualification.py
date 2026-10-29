import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import ComparisonLimitationKind, ComparisonSide
from archdiscov.services.comparison_fields import FieldComparer
from archdiscov.services.comparison_matching import match_resources
from archdiscov.services.comparison_qualification import (
    EntryBuilder,
    comparison_snapshot,
    snapshot_limitations,
)
from tests.support.comparison_data import (
    REGION,
    aws_resource,
    azure_snapshot,
    coverage,
    evidence,
    index,
    vpc,
)

LEFT, RIGHT = ComparisonSide.LEFT, ComparisonSide.RIGHT
DELETE = {"actions": ["delete"], "value_scope": "before"}
CREATE = {"actions": ["create"], "value_scope": "after"}


def planned(physical="vpc-1", lifecycle=DELETE, *, cited=False, **kw):
    data = vpc(physical, **kw)
    data["lifecycle"] = lifecycle
    if cited:
        data["fact_evidence"] = {"/lifecycle/actions": ["plan"]}
    return data


def qualify(left, right, *, sources=("aws", "aws"), left_kw=None, right_kw=None):
    left_index = index(left, sources[0], seed=1, **(left_kw or {}))
    right_index = index(right, sources[1], seed=2, **(right_kw or {}))
    outcome = match_resources(left_index, right_index)
    return left_index, right_index, outcome, EntryBuilder(left_index, right_index)


def kinds(qualifications):
    return [(str(q.kind), str(q.side)) for q in qualifications]


# ---- labels and snapshot-level limitations -----------------------------------------


@pytest.mark.parametrize(
    ("source", "kind", "label"),
    [
        ("aws", "aws", "test"),
        ("terraform_state", "terraform_state", "state.json"),
    ],
)
def test_each_side_is_labelled_with_its_source_kind_label_and_collection_window(
    source, kind, label
):
    snapshot = comparison_snapshot(RIGHT, index([vpc()], source, seed=5))
    assert (snapshot.side, str(snapshot.source_kind), snapshot.source_label) == (RIGHT, kind, label)
    assert snapshot.snapshot_id.int == 5
    assert snapshot.collection_started_at < snapshot.collection_completed_at
    assert snapshot.scope.platform == snapshot.platform


def test_a_declaration_or_plan_compared_with_an_observation_is_limited():
    observed, plan = index([vpc()], "aws"), index([vpc()], "terraform_plan", seed=2)
    [limitation] = snapshot_limitations(observed, plan, ())
    assert limitation.kind == ComparisonLimitationKind.SOURCE_KIND_MISMATCH
    assert "aws" in limitation.message and "terraform_plan" in limitation.message
    assert snapshot_limitations(observed, index([vpc()], "aws", seed=3), ()) == []
    assert len(snapshot_limitations(plan, index([vpc()], "terraform_state", seed=3), ())) == 1


def test_cross_cloud_pairs_carry_their_limitation_in_canonical_order():
    aws, azure = index([vpc()]), GraphIndex(azure_snapshot())
    outcome = match_resources(aws, azure)
    limitations = snapshot_limitations(aws, azure, outcome.limitations)
    assert [str(item.kind) for item in limitations] == ["cross_cloud", "source_kind_mismatch"]


# ---- planned lifecycle -------------------------------------------------------------


def test_a_planned_deletion_qualifies_the_pair_and_is_not_an_observed_absence():
    right = planned(cited=True, evidence=["plan"])
    _left_index, _right_index, outcome, qualifier = qualify(
        [vpc()],
        [right],
        sources=("aws", "terraform_plan"),
        right_kw={"evidence": {"plan": evidence(2, "vpc-1", "actions")}},
    )
    [pair] = outcome.matched
    entry = qualifier.matched(pair, [])
    [qualification] = entry.qualifications
    assert (str(qualification.kind), qualification.side) == ("planned_lifecycle", RIGHT)
    assert qualification.lifecycle.actions == ["delete"]
    assert qualification.evidence_ids == ["plan"]


def test_a_planned_resource_only_on_one_side_is_qualified_on_its_own_side():
    _, _, outcome, qualifier = qualify(
        [vpc("vpc-9")], [planned("vpc-1", CREATE)], sources=("aws", "terraform_plan")
    )
    entry = qualifier.unmatched(RIGHT, outcome.unmatched_right[0])
    [planned_lifecycle] = [q for q in entry.qualifications if q.kind == "planned_lifecycle"]
    assert planned_lifecycle.side == RIGHT
    assert planned_lifecycle.lifecycle.actions == ["create"]


# ---- coverage ----------------------------------------------------------------------


def named(qualification):
    # Limited coverage names the recorded operation; unrecorded coverage only the service.
    if qualification.coverage is None:
        return qualification.service
    coverage = qualification.coverage
    return coverage.service, coverage.location, coverage.operation


@pytest.mark.parametrize(
    ("operations", "expected"),
    [
        (
            [coverage("failed")],
            [("limited_coverage", "right", ("ec2", REGION, "DescribeVpcs"))],
        ),
        ([coverage("success")], []),
        ([coverage("success"), coverage("failed", service="s3", operation="ListBuckets")], []),
        ([], [("unrecorded_coverage", "right", "ec2")]),
    ],
    ids=["limited", "complete", "other-service", "unrecorded"],
)
def test_an_absence_is_qualified_by_the_other_sides_coverage_of_its_service(operations, expected):
    _, _, outcome, qualifier = qualify(
        [vpc("vpc-1")], [vpc("vpc-2")], right_kw={"operations": operations}
    )
    qualifications = qualifier.unmatched(LEFT, outcome.unmatched_left[0]).qualifications
    assert [(str(q.kind), str(q.side), named(q)) for q in qualifications] == expected


def test_coverage_of_a_different_scope_never_qualifies_an_absence():
    other = index(
        [aws_resource(physical="vpc-2", account="999999999999")], account="999999999999", seed=2
    )
    left = index([vpc("vpc-1")], seed=1)
    outcome = match_resources(left, other)
    qualifier = EntryBuilder(left, other)
    assert qualifier.unmatched(LEFT, outcome.unmatched_left[0]).qualifications == []


def test_differences_are_qualified_by_either_sides_limited_coverage():
    left_kw = {"operations": [coverage("incomplete")]}
    right_kw = {"operations": [coverage("failed")]}
    left_index, right_index, outcome, qualifier = qualify(
        [vpc(attributes={"a": 1, "x": "[REDACTED]"})],
        [vpc(attributes={"a": 2, "x": "y"})],
        left_kw=left_kw,
        right_kw=right_kw,
    )
    [pair] = outcome.matched
    differences = FieldComparer(left_index, right_index, outcome.matched).compare(pair)
    entry = qualifier.matched(pair, differences)
    assert [d.path for d in entry.differences] == ["/attributes/a", "/attributes/x"]
    changed, indeterminate = entry.differences
    assert kinds(changed.qualifications) == [
        ("limited_coverage", "left"),
        ("limited_coverage", "right"),
    ]
    # The indeterminate difference keeps its own qualification beside the coverage ones.
    assert sorted(str(q.kind) for q in indeterminate.qualifications) == [
        "limited_coverage",
        "limited_coverage",
        "redacted_value",
    ]
    assert entry.qualifications == []
