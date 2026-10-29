from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from archdiscov.models.comparison.entries import (
    AmbiguousMatch,
    FieldDifference,
    MatchedPair,
    Qualification,
    UnmatchedResource,
)
from archdiscov.models.comparison.results import ComparisonResult
from archdiscov.models.enums import ComparisonSide

LEFT, RIGHT = ComparisonSide.LEFT, ComparisonSide.RIGHT
AWS = {"platform": "aws", "account_id": "123456789012", "partition": "aws"}
OTHER_AWS = {**AWS, "account_id": "999999999999"}
NOW = datetime(2026, 1, 1, tzinfo=UTC)
DELETION = {"actions": ["delete"], "value_scope": "before"}


def ref(name, scope=AWS):
    return {"resource_id": f"123456789012/eu-west-1/ec2/physical/{name}", "scope": scope}


def snapshot(side, scope=AWS, kind="aws", seed=1, **fields):
    return {
        "side": side.value,
        "snapshot_id": str(UUID(int=seed)),
        "platform": scope["platform"],
        "scope": scope,
        "source_kind": kind,
        "collection_started_at": NOW.isoformat(),
        "collection_completed_at": NOW.isoformat(),
        **fields,
    }


def qualification(kind="unknown_value", side="left", **fields):
    return {"kind": kind, "side": side, **fields}


def difference(**fields):
    return {
        "path": "/attributes/tags/Owner",
        "kind": "changed",
        "left_value": "a",
        "right_value": "b",
        "left_evidence_ids": ["e1"],
        "right_evidence_ids": ["e2"],
        **fields,
    }


def pair(**fields):
    return {
        "left": ref("vpc-1"),
        "right": ref("vpc-1"),
        "basis": "physical_identity",
        "differences": [difference()],
        **fields,
    }


def unmatched(side="left", name="vpc-2", **fields):
    return {"side": side, "resource": ref(name), **fields}


def ambiguous(**fields):
    return {
        "basis": "physical_identity",
        "key": "vpc-3",
        "left": [ref("vpc-3"), ref("vpc-3b")],
        "right": [ref("vpc-3")],
        **fields,
    }


def result(**fields):
    return {
        "left": snapshot(LEFT),
        "right": snapshot(RIGHT, seed=2),
        "created_at": NOW.isoformat(),
        **fields,
    }


def ordered(model, entries):
    built = [model.model_validate(entry) for entry in entries]
    return [entry.model_dump(mode="json") for entry in sorted(built, key=lambda e: e.id)]


# ---- entries ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "data", "message"),
    [
        (Qualification, qualification("planned_lifecycle"), "require lifecycle"),
        (
            FieldDifference,
            difference(kind="indeterminate", left_value=None, right_value=None),
            "require a qualification",
        ),
        (MatchedPair, pair(right=ref("vpc-1", OTHER_AWS)), "complete scope"),
        (AmbiguousMatch, ambiguous(left=[ref("a")], right=[ref("a")]), "more than one candidate"),
    ],
    ids=["qualification-fields", "unqualified-indeterminate", "pair-scope", "unambiguous"],
)
def test_each_entry_rejects_what_its_kind_forbids(model, data, message):
    with pytest.raises(ValidationError, match=message):
        model.model_validate(data)


def test_an_unmatched_resource_is_qualified_by_its_own_lifecycle_or_either_coverage():
    with pytest.raises(ValidationError, match="lifecycle or coverage"):
        UnmatchedResource.model_validate(unmatched(qualifications=[qualification()]))
    planned = qualification("planned_lifecycle", lifecycle=DELETION)
    assert UnmatchedResource.model_validate(unmatched(qualifications=[planned]))
    with pytest.raises(ValidationError, match="own side"):
        UnmatchedResource.model_validate(
            unmatched(
                "left",
                qualifications=[qualification("planned_lifecycle", "right", lifecycle=DELETION)],
            )
        )
    for side in ("left", "right"):
        assert UnmatchedResource.model_validate(
            unmatched(
                "left", qualifications=[qualification("unrecorded_coverage", side, service="ec2")]
            )
        )


# ---- deterministic IDs --------------------------------------------------------------


def test_entry_ids_are_deterministic_and_must_match_their_content():
    first = MatchedPair.model_validate(pair())
    assert MatchedPair.model_validate(pair()).id == first.id
    assert MatchedPair.model_validate(pair(id=first.id)).id == first.id
    cited = MatchedPair.model_validate(pair(differences=[difference(left_evidence_ids=["e9"])]))
    assert cited.id != first.id
    with pytest.raises(ValidationError, match="deterministic"):
        MatchedPair.model_validate(pair(id="0" * 64))


# ---- the result ---------------------------------------------------------------------


def full_result():
    return result(
        matched=ordered(MatchedPair, [pair(), pair(left=ref("vpc-5"), right=ref("vpc-5"))]),
        unmatched=ordered(UnmatchedResource, [unmatched("left"), unmatched("right", "vpc-6")]),
        ambiguous=ordered(AmbiguousMatch, [ambiguous()]),
        limitations=[{"kind": "source_kind_mismatch", "message": "A declaration was compared."}],
    )


def test_a_full_result_round_trips_and_labels_both_sides():
    model = ComparisonResult.model_validate(full_result())
    assert ComparisonResult.model_validate_json(model.model_dump_json()) == model
    assert (model.left.side, model.right.side) == (LEFT, RIGHT)
    assert model.schema_version == "1"


def test_sides_must_be_labelled_left_then_right_and_match_their_platform():
    with pytest.raises(ValidationError, match="labelled"):
        ComparisonResult.model_validate(result(left=snapshot(RIGHT), right=snapshot(LEFT)))
    with pytest.raises(ValidationError, match="platform"):
        ComparisonResult.model_validate(result(left={**snapshot(LEFT), "platform": "azure"}))


def test_references_must_carry_the_scope_of_their_side():
    entry = unmatched("right")
    entry["resource"] = ref("vpc-2", OTHER_AWS)
    with pytest.raises(ValidationError, match="scope of its side"):
        ComparisonResult.model_validate(result(unmatched=ordered(UnmatchedResource, [entry])))
    foreign = result(
        right=snapshot(RIGHT, OTHER_AWS, seed=2),
        unmatched=ordered(UnmatchedResource, [entry]),
    )
    assert ComparisonResult.model_validate(foreign).right.scope.account_id == "999999999999"


def test_a_resource_appears_in_only_one_entry_per_side():
    data = result(
        matched=ordered(MatchedPair, [pair()]),
        ambiguous=ordered(AmbiguousMatch, [ambiguous(left=[ref("vpc-1"), ref("z")])]),
    )
    with pytest.raises(ValidationError, match="only one"):
        ComparisonResult.model_validate(data)
    # The same resource ID on the two sides is two resources, not a conflict.
    both_sides = result(
        unmatched=ordered(
            UnmatchedResource, [unmatched("left", "vpc-1"), unmatched("right", "vpc-1")]
        )
    )
    assert len(ComparisonResult.model_validate(both_sides).unmatched) == 2


def test_entries_must_be_in_canonical_order():
    entries = ordered(UnmatchedResource, [unmatched("left"), unmatched("right", "vpc-6")])
    with pytest.raises(ValidationError, match="canonical"):
        ComparisonResult.model_validate(result(unmatched=entries[::-1]))
