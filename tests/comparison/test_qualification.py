import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.comparison.results import ComparisonResult
from archdiscov.models.enums import ComparisonLimitationKind, ComparisonSide
from archdiscov.services.comparison_fields import FieldComparer
from archdiscov.services.comparison_matching import match_resources
from archdiscov.services.comparison_qualification import (
    EntryBuilder,
    comparison_snapshot,
    snapshot_limitations,
)
from tests.comparison.snapshots import REGION, aws_resource, azure_snapshot, coverage, index
from tests.support.comparison_data import evidence, rid, vpc

LEFT, RIGHT = ComparisonSide.LEFT, ComparisonSide.RIGHT
DELETE = {"actions": ["delete"], "value_scope": "before"}
REPLACE = {"actions": ["delete", "create"], "value_scope": "after"}


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
        ("mock", "mock", "test"),
        ("terraform_state", "terraform_state", "state.json"),
        ("terraform_plan", "terraform_plan", "plan.json"),
        ("cloudformation", "cloudformation", "t.json"),
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
    other_account = index(
        [aws_resource(physical="vpc-1", account="999999999999")], account="999999999999", seed=4
    )
    scoped = snapshot_limitations(
        aws, other_account, match_resources(aws, other_account).limitations
    )
    assert [str(item.kind) for item in scoped] == ["scope_mismatch"]


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


def test_a_planned_resource_only_on_one_side_is_qualified_on_its_own_side_for_each_action():
    for lifecycle in (DELETE, REPLACE, {"actions": ["create"], "value_scope": "after"}):
        _, _, outcome, qualifier = qualify(
            [vpc("vpc-9")], [planned("vpc-1", lifecycle)], sources=("aws", "terraform_plan")
        )
        entry = qualifier.unmatched(RIGHT, outcome.unmatched_right[0])
        [planned_lifecycle] = [q for q in entry.qualifications if q.kind == "planned_lifecycle"]
        assert planned_lifecycle.side == RIGHT
        assert planned_lifecycle.lifecycle.actions == lifecycle["actions"]


def test_module_addressed_planned_resources_match_and_carry_their_lifecycle():
    address = "module.net.aws_vpc.main"
    left = aws_resource(logical=address)
    right = {**aws_resource(logical=address), "lifecycle": REPLACE}
    _, _, outcome, qualifier = qualify(
        [left], [right], sources=("terraform_state", "terraform_plan")
    )
    [pair] = outcome.matched
    assert kinds(qualifier.matched(pair, []).qualifications) == [("planned_lifecycle", "right")]


def test_unplanned_resources_have_no_lifecycle_qualification():
    _, _, outcome, qualifier = qualify([vpc()], [vpc()])
    [pair] = outcome.matched
    assert qualifier.matched(pair, []).qualifications == []


# ---- coverage ----------------------------------------------------------------------


def test_an_absence_is_qualified_by_the_other_sides_failed_incomplete_or_skipped_coverage():
    for status in ("failed", "incomplete", "skipped", "unsupported"):
        _, _, outcome, qualifier = qualify(
            [vpc("vpc-1")], [vpc("vpc-2")], right_kw={"operations": [coverage(status)]}
        )
        entry = qualifier.unmatched(LEFT, outcome.unmatched_left[0])
        [qualification] = entry.qualifications
        assert (str(qualification.kind), qualification.side) == ("limited_coverage", RIGHT)
        assert qualification.coverage.operation == "DescribeVpcs"
        assert qualification.coverage.service == "ec2"
        assert qualification.coverage.location == REGION


def test_successful_coverage_leaves_an_absence_unqualified():
    _, _, outcome, qualifier = qualify(
        [vpc("vpc-1")], [vpc("vpc-2")], right_kw={"operations": [coverage("success")]}
    )
    assert qualifier.unmatched(LEFT, outcome.unmatched_left[0]).qualifications == []


def test_coverage_of_another_service_does_not_qualify_the_resource():
    operations = [coverage("success"), coverage("failed", service="s3", operation="ListBuckets")]
    _, _, outcome, qualifier = qualify(
        [vpc("vpc-1")], [vpc("vpc-2")], right_kw={"operations": operations}
    )
    assert qualifier.unmatched(LEFT, outcome.unmatched_left[0]).qualifications == []


def test_unrecorded_coverage_names_only_the_service():
    _, _, outcome, qualifier = qualify([vpc("vpc-1")], [vpc("vpc-2")])
    [qualification] = qualifier.unmatched(LEFT, outcome.unmatched_left[0]).qualifications
    assert (str(qualification.kind), qualification.side) == ("unrecorded_coverage", RIGHT)
    assert qualification.service == "ec2"
    assert qualification.coverage is None


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


def test_qualified_entries_form_a_valid_result():
    left_index, right_index, outcome, qualifier = qualify(
        [vpc("vpc-1", attributes={"a": 1}), vpc("vpc-2")],
        [planned("vpc-1", cited=False, attributes={"a": 2}), vpc("vpc-3")],
        sources=("aws", "terraform_plan"),
        right_kw={"operations": [coverage("failed")]},
    )
    comparer = FieldComparer(left_index, right_index, outcome.matched)
    matched = [qualifier.matched(p, comparer.compare(p)) for p in outcome.matched]
    unmatched = [qualifier.unmatched(LEFT, i) for i in outcome.unmatched_left] + [
        qualifier.unmatched(RIGHT, i) for i in outcome.unmatched_right
    ]
    result = ComparisonResult(
        left=comparison_snapshot(LEFT, left_index),
        right=comparison_snapshot(RIGHT, right_index),
        created_at="2026-01-02T00:00:00Z",
        matched=sorted(matched, key=lambda e: e.id),
        unmatched=sorted(unmatched, key=lambda e: e.id),
        limitations=snapshot_limitations(left_index, right_index, outcome.limitations),
    )
    assert ComparisonResult.model_validate_json(result.model_dump_json()) == result
    assert rid("vpc-1") == result.matched[0].left.resource_id
