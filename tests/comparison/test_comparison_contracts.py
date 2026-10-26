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
from archdiscov.models.comparison.results import (
    ComparisonLimitation,
    ComparisonResult,
    ComparisonSnapshot,
)
from archdiscov.models.enums import (
    ComparisonLimitationKind,
    ComparisonSide,
    DifferenceSubject,
    MatchBasis,
    QualificationKind,
    SnapshotSourceKind,
)

LEFT, RIGHT = ComparisonSide.LEFT, ComparisonSide.RIGHT
AWS = {"platform": "aws", "account_id": "123456789012", "partition": "aws"}
OTHER_AWS = {**AWS, "account_id": "999999999999"}
AZURE = {"platform": "azure", "subscription_id": "11111111-1111-4111-8111-111111111111"}
NOW = datetime(2026, 1, 1, tzinfo=UTC)
DELETION = {"actions": ["delete"], "value_scope": "before"}
COVERAGE = {"scope": AWS, "location": "eu-west-1", "service": "ec2", "operation": "DescribeVpcs"}


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


Q = qualification("unrecorded_coverage", service="ec2")  # a valid absence qualification


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
    value = {
        "left": snapshot(LEFT),
        "right": snapshot(RIGHT, seed=2),
        "created_at": NOW.isoformat(),
        **fields,
    }
    return value


def ordered(model, entries):
    built = [model.model_validate(entry) for entry in entries]
    return [entry.model_dump(mode="json") for entry in sorted(built, key=lambda e: e.id)]


# ---- qualifications -----------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        qualification(
            "planned_lifecycle", lifecycle={"actions": ["delete"], "value_scope": "before"}
        ),
        qualification("planned_lifecycle", "right", lifecycle={"actions": ["delete", "create"]}),
        qualification("limited_coverage", coverage=COVERAGE),
        qualification("unrecorded_coverage", service="ec2"),
        qualification("unknown_value", evidence_ids=["e1"]),
        qualification("unresolved_reference"),
        qualification("redacted_value", "right"),
    ],
)
def test_every_qualification_kind_round_trips(data):
    model = Qualification.model_validate(data)
    assert Qualification.model_validate_json(model.model_dump_json()) == model


@pytest.mark.parametrize(
    "data",
    [
        qualification("planned_lifecycle"),
        qualification("limited_coverage", coverage=COVERAGE, service="ec2"),
        qualification("unknown_value", lifecycle={"actions": ["delete"]}),
        qualification("unknown_value", evidence_ids=["b", "a"]),
        qualification("nonsense"),
    ],
)
def test_invalid_qualifications_are_rejected(data):
    with pytest.raises(ValidationError):
        Qualification.model_validate(data)


def test_every_qualification_kind_is_covered():
    kinds = {
        "planned_lifecycle", "limited_coverage", "unrecorded_coverage",
        "unknown_value", "unresolved_reference", "redacted_value",
    }  # fmt: skip
    assert kinds == {kind.value for kind in QualificationKind}


# ---- field differences --------------------------------------------------------------


@pytest.mark.parametrize("subject", DifferenceSubject)
@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "added", "left_value": None, "right_value": {"k": [1, None]}},
        {"kind": "removed", "left_value": "x", "right_value": None},
        {"kind": "changed", "left_value": None, "right_value": 0},
        {
            "kind": "indeterminate",
            "left_value": None,
            "right_value": None,
            "qualifications": [qualification()],
        },
    ],
    ids=["added", "removed", "null-to-value", "indeterminate"],
)
def test_every_difference_kind_and_subject_round_trips(subject, fields):
    model = FieldDifference.model_validate(difference(subject=subject.value, **fields))
    assert FieldDifference.model_validate_json(model.model_dump_json()) == model


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "added", "left_value": "x"},
        {"left_value": "same", "right_value": "same"},
        {"kind": "indeterminate", "left_value": None, "right_value": None},
        {"path": "no-slash"},
        {"left_evidence_ids": ["b", "a"]},
        {"qualifications": [qualification(), qualification()]},
    ],
)
def test_invalid_differences_are_rejected(fields):
    with pytest.raises(ValidationError):
        FieldDifference.model_validate(difference(**fields))


# ---- entries ------------------------------------------------------------------------


@pytest.mark.parametrize("basis", MatchBasis)
def test_matched_pairs_round_trip_for_every_basis(basis):
    model = MatchedPair.model_validate(pair(basis=basis.value, qualifications=[qualification()]))
    assert MatchedPair.model_validate_json(model.model_dump_json()) == model
    assert model.id


def test_a_matched_pair_needs_a_basis_and_a_shared_scope():
    data = pair()
    del data["basis"]
    with pytest.raises(ValidationError):
        MatchedPair.model_validate(data)
    with pytest.raises(ValidationError, match="complete scope"):
        MatchedPair.model_validate(pair(right=ref("vpc-1", OTHER_AWS)))
    with pytest.raises(ValidationError, match="complete scope"):
        MatchedPair.model_validate(pair(right=ref("vpc-1", AZURE)))


def test_differences_must_be_unique_and_in_canonical_order():
    first, second = difference(path="/a"), difference(path="/b")
    assert MatchedPair.model_validate(pair(differences=[first, second]))
    with pytest.raises(ValidationError, match="canonical"):
        MatchedPair.model_validate(pair(differences=[second, first]))
    with pytest.raises(ValidationError, match="canonical"):
        MatchedPair.model_validate(pair(differences=[first, first]))


@pytest.mark.parametrize("side", ComparisonSide)
def test_unmatched_resources_round_trip_for_each_side(side):
    model = UnmatchedResource.model_validate(
        unmatched(
            side.value,
            qualifications=[qualification("limited_coverage", side.value, coverage=COVERAGE)],
        )
    )
    assert UnmatchedResource.model_validate_json(model.model_dump_json()) == model


@pytest.mark.parametrize("kind", ["unknown_value", "unresolved_reference", "redacted_value"])
def test_an_unmatched_resource_is_qualified_by_lifecycle_or_coverage_only(kind):
    with pytest.raises(ValidationError, match="lifecycle or coverage"):
        UnmatchedResource.model_validate(unmatched(qualifications=[qualification(kind)]))
    planned = qualification("planned_lifecycle", lifecycle=DELETION)
    assert UnmatchedResource.model_validate(unmatched(qualifications=[planned]))


def test_a_planned_lifecycle_of_an_unmatched_resource_concerns_its_own_side():
    with pytest.raises(ValidationError, match="own side"):
        UnmatchedResource.model_validate(
            unmatched(
                "left",
                qualifications=[qualification("planned_lifecycle", "right", lifecycle=DELETION)],
            )
        )


def test_an_unmatched_resource_may_be_qualified_by_the_coverage_of_either_side():
    for side in ("left", "right"):
        assert UnmatchedResource.model_validate(
            unmatched(
                "left", qualifications=[qualification("unrecorded_coverage", side, service="ec2")]
            )
        )


@pytest.mark.parametrize("basis", MatchBasis)
def test_ambiguous_matches_list_every_candidate(basis):
    model = AmbiguousMatch.model_validate(ambiguous(basis=basis.value))
    assert AmbiguousMatch.model_validate_json(model.model_dump_json()) == model
    assert len(model.left) == 2 and len(model.right) == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"left": [ref("a")], "right": [ref("a")]},  # one candidate per side is a match
        {"left": [], "right": [ref("a"), ref("b")]},
        {"right": []},
        {"left": [ref("b"), ref("a")]},
        {"left": [ref("a"), ref("a")]},
        {"key": ""},
    ],
    ids=["unambiguous", "no-left", "no-right", "unsorted", "duplicate", "empty-key"],
)
def test_invalid_ambiguous_matches_are_rejected(fields):
    with pytest.raises(ValidationError):
        AmbiguousMatch.model_validate(ambiguous(**fields))


# ---- deterministic IDs --------------------------------------------------------------


def test_entry_ids_are_deterministic_and_must_match_their_content():
    first = MatchedPair.model_validate(pair())
    assert MatchedPair.model_validate(pair()).id == first.id
    assert MatchedPair.model_validate(pair(id=first.id)).id == first.id
    with pytest.raises(ValidationError, match="deterministic"):
        MatchedPair.model_validate(pair(id="0" * 64))


@pytest.mark.parametrize(
    "variant",
    [
        {"basis": "logical_address"},
        {"right": ref("vpc-9")},
        {"differences": []},
        {"differences": [difference(right_value="c")]},
        {"differences": [difference(left_evidence_ids=["e9"])]},
        {"differences": [difference(subject="dependency")]},
        {"qualifications": [qualification()]},
        {"qualifications": [qualification(side="right")]},
    ],
)
def test_every_pair_field_is_part_of_its_identity(variant):
    assert MatchedPair.model_validate(pair()).id != MatchedPair.model_validate(pair(**variant)).id


def test_unmatched_and_ambiguous_identities_cover_their_semantic_fields():
    base = UnmatchedResource.model_validate(unmatched()).id
    assert base != UnmatchedResource.model_validate(unmatched("right")).id
    assert base != UnmatchedResource.model_validate(unmatched(name="vpc-9")).id
    assert base != UnmatchedResource.model_validate(unmatched(qualifications=[Q])).id
    first = AmbiguousMatch.model_validate(ambiguous()).id
    assert first != AmbiguousMatch.model_validate(ambiguous(basis="logical_address")).id
    assert first != AmbiguousMatch.model_validate(ambiguous(key="other")).id
    assert first != AmbiguousMatch.model_validate(ambiguous(right=[ref("vpc-3"), ref("vpc-4")])).id


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


@pytest.mark.parametrize("kind", SnapshotSourceKind)
def test_every_source_kind_can_label_a_side(kind):
    data = result(left=snapshot(LEFT, kind=kind.value, source_label="label"))
    assert ComparisonResult.model_validate(data).left.source_kind == kind


@pytest.mark.parametrize("kind", ComparisonLimitationKind)
def test_every_limitation_kind_is_representable(kind):
    data = result(limitations=[{"kind": kind.value, "message": "Qualified."}])
    assert ComparisonResult.model_validate(data).limitations[0].kind == kind


def test_a_cross_cloud_pair_is_two_unmatched_sides_with_a_limitation():
    data = result(
        left=snapshot(LEFT),
        right=snapshot(RIGHT, AZURE, "azure_fixture", seed=2),
        unmatched=ordered(
            UnmatchedResource,
            [unmatched("left"), {"side": "right", "resource": ref("vm-1", AZURE)}],
        ),
        limitations=[{"kind": "cross_cloud", "message": "Nothing can match across clouds."}],
    )
    model = ComparisonResult.model_validate(data)
    assert {item.side for item in model.unmatched} == {LEFT, RIGHT}
    with pytest.raises(ValidationError, match="complete scope"):
        MatchedPair.model_validate(pair(right=ref("vm-1", AZURE)))


def test_sides_must_be_labelled_left_then_right_and_match_their_scope():
    with pytest.raises(ValidationError, match="labelled"):
        ComparisonResult.model_validate(result(left=snapshot(RIGHT), right=snapshot(LEFT)))
    with pytest.raises(ValidationError, match="labelled"):
        ComparisonResult.model_validate(result(right=snapshot(LEFT, seed=2)))
    with pytest.raises(ValidationError, match="platform"):
        ComparisonResult.model_validate(result(left={**snapshot(LEFT), "platform": "azure"}))
    with pytest.raises(ValidationError, match="complete"):
        data = snapshot(LEFT, collection_completed_at="2025-12-31T00:00:00+00:00")
        ComparisonResult.model_validate(result(left=data))


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


@pytest.mark.parametrize(
    "entries",
    [
        {"matched": [pair()], "unmatched": [unmatched("left", "vpc-1")]},
        {"matched": [pair()], "unmatched": [unmatched("right", "vpc-1")]},
        {"matched": [pair()], "ambiguous": [ambiguous(left=[ref("vpc-1"), ref("z")])]},
        {
            "unmatched": [
                unmatched("left", "vpc-2"),
                unmatched("left", "vpc-2", qualifications=[Q]),
            ]
        },
    ],
    ids=[
        "matched-and-unmatched-left",
        "matched-and-unmatched-right",
        "matched-and-ambiguous",
        "twice-unmatched",
    ],
)
def test_a_resource_appears_in_only_one_entry_per_side(entries):
    models = {"matched": MatchedPair, "unmatched": UnmatchedResource, "ambiguous": AmbiguousMatch}
    data = result(**{key: ordered(models[key], value) for key, value in entries.items()})
    with pytest.raises(ValidationError, match="only one"):
        ComparisonResult.model_validate(data)


def test_the_same_name_on_the_two_sides_is_not_a_conflict_between_unmatched_entries():
    data = result(
        unmatched=ordered(
            UnmatchedResource, [unmatched("left", "vpc-1"), unmatched("right", "vpc-1")]
        )
    )
    assert len(ComparisonResult.model_validate(data).unmatched) == 2


def test_entries_and_limitations_must_be_in_canonical_order():
    entries = ordered(UnmatchedResource, [unmatched("left"), unmatched("right", "vpc-6")])
    with pytest.raises(ValidationError, match="canonical"):
        ComparisonResult.model_validate(result(unmatched=entries[::-1]))
    twice = result(unmatched=[entries[0], entries[0]])
    with pytest.raises(ValidationError, match="canonical"):
        ComparisonResult.model_validate(twice)
    limits = [{"kind": kind, "message": "m"} for kind in ("source_kind_mismatch", "cross_cloud")]
    with pytest.raises(ValidationError, match="canonical"):
        ComparisonResult.model_validate(result(limitations=limits))


def test_limitation_messages_are_not_identity_but_kinds_are_unique():
    assert ComparisonLimitation(kind="cross_cloud", message="a").kind == "cross_cloud"
    duplicated = [{"kind": "cross_cloud", "message": "a"}, {"kind": "cross_cloud", "message": "b"}]
    with pytest.raises(ValidationError, match="canonical"):
        ComparisonResult.model_validate(result(limitations=duplicated))


def test_the_snapshot_label_rejects_invalid_values():
    for fields in ({"source_kind": "unknown"}, {"snapshot_id": "nope"}, {"source_label": ""}):
        with pytest.raises(ValidationError):
            ComparisonSnapshot.model_validate({**snapshot(LEFT), **fields})
