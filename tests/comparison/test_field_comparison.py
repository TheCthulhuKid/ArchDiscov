from archdiscov.models.enums import ComparisonSide, DifferenceKind, DifferenceSubject
from archdiscov.services.comparison_fields import FieldComparer
from archdiscov.services.comparison_matching import match_resources
from tests.comparison.snapshots import REGION, aws_resource, index
from tests.support.comparison_data import evidence, rid, vpc

FIELD, DEPENDENCY, CONTAINMENT = (
    DifferenceSubject.FIELD,
    DifferenceSubject.DEPENDENCY,
    DifferenceSubject.CONTAINMENT,
)
ADDED, REMOVED, CHANGED, INDETERMINATE = (
    DifferenceKind.ADDED,
    DifferenceKind.REMOVED,
    DifferenceKind.CHANGED,
    DifferenceKind.INDETERMINATE,
)
LEFT, RIGHT = ComparisonSide.LEFT, ComparisonSide.RIGHT
SUBNET = "AWS::EC2::Subnet"


def subnet(physical):
    return aws_resource(SUBNET, physical=physical)


def edge(source, target, *, kind=None, evidence_ids=("e",)):
    """A dependency to a local resource name, or to the given non-local target data."""
    local = {"kind": "local", "resource_id": rid(target)} if isinstance(target, str) else target
    return {
        "source_id": rid(source),
        "target": local,
        "relationship": "attached_to",
        "evidence_ids": list(evidence_ids),
        "attributes": {"kind": kind} if kind else {},
    }


def _side(seed, options):
    """Snapshot options; dependencies cite evidence "e" owned by their source, vpc-1."""
    options = dict(options or {})
    if options.get("dependencies"):
        options["evidence"] = {**options.get("evidence", {}), "e": evidence(seed, "vpc-1", "edge")}
    return {"seed": seed, **options}


def compare(left, right, *, sources=("aws", "aws"), left_kw=None, right_kw=None):
    """Differences of the pair matching the first physical resource of both sides."""
    left_index = index(left, sources[0], **_side(1, left_kw))
    right_index = index(right, sources[1], **_side(2, right_kw))
    outcome = match_resources(left_index, right_index)
    comparer = FieldComparer(left_index, right_index, outcome.matched)
    pair = next(item for item in outcome.matched if item.left_id == rid("vpc-1"))
    return comparer.compare(pair)


def summary(differences):
    return [(d.subject, d.path, d.kind) for d in differences]


# ---- fields ----------------------------------------------------------------------


def test_equal_resources_have_no_differences():
    attributes = {"cidr": "10.0.0.0/16", "tags": {"Name": "main"}}
    assert compare([vpc(attributes=attributes)], [vpc(attributes=attributes)]) == []


def test_changed_added_and_removed_fields_and_tags_have_pointer_paths():
    left = vpc(attributes={"cidr": "10.0.0.0/16", "tags": {"Name": "a", "Team": "x"}, "old": 1})
    right = vpc(attributes={"cidr": "10.1.0.0/16", "tags": {"Name": "b", "Env": "y"}, "new": 2})
    differences = compare([left], [right])
    assert summary(differences) == [
        (FIELD, "/attributes/cidr", CHANGED),
        (FIELD, "/attributes/new", ADDED),
        (FIELD, "/attributes/old", REMOVED),
        (FIELD, "/attributes/tags/Env", ADDED),
        (FIELD, "/attributes/tags/Name", CHANGED),
        (FIELD, "/attributes/tags/Team", REMOVED),
    ]
    by_path = {d.path: d for d in differences}
    assert (by_path["/attributes/cidr"].left_value, by_path["/attributes/cidr"].right_value) == (
        "10.0.0.0/16",
        "10.1.0.0/16",
    )
    assert by_path["/attributes/new"].left_value is None
    assert by_path["/attributes/new"].right_value == 2
    assert by_path["/attributes/old"].right_value is None


def test_names_and_pointer_special_characters_are_compared_and_escaped():
    left = vpc(name="a", attributes={"a/b~c": 1})
    right = vpc(name="b", attributes={"a/b~c": 2})
    assert summary(compare([left], [right])) == [
        (FIELD, "/attributes/a~1b~0c", CHANGED),
        (FIELD, "/name", CHANGED),
    ]


def test_lists_are_compared_by_position():
    left = vpc(attributes={"cidrs": ["a", "b"]})
    right = vpc(attributes={"cidrs": ["a", "c", "d"]})
    assert summary(compare([left], [right])) == [
        (FIELD, "/attributes/cidrs/1", CHANGED),
        (FIELD, "/attributes/cidrs/2", ADDED),
    ]


def test_snapshot_bookkeeping_is_not_compared():
    attributes = {"x": 1}
    left = vpc(
        attributes=attributes,
        evidence=["a"],
        fact_evidence={"/attributes/x": ["a"]},
        logical="aws_vpc.a",
    )
    right = vpc(
        attributes=attributes,
        evidence=["b"],
        fact_evidence={"/attributes/x": ["b"]},
        logical="aws_vpc.b",
    )
    evidence_left = {"a": evidence(1, "vpc-1", "x")}
    evidence_right = {"b": evidence(2, "vpc-1", "x")}
    differences = compare(
        [left],
        [right],
        left_kw={"evidence": evidence_left},
        right_kw={"evidence": evidence_right},
    )
    assert differences == []


# ---- citations -------------------------------------------------------------------


def test_differences_cite_the_fact_on_both_sides_or_fall_back_to_resource_evidence():
    left = vpc(
        attributes={"cidr": "a", "other": 1},
        evidence=["l-all", "l-cidr"],
        fact_evidence={"/attributes/cidr": ["l-cidr"]},
    )
    right = vpc(
        attributes={"cidr": "b", "other": 2},
        evidence=["r-all", "r-attributes"],
        fact_evidence={"/attributes": ["r-attributes"]},
    )
    left_evidence = {
        "l-all": evidence(1, "vpc-1", "all"),
        "l-cidr": evidence(1, "vpc-1", "cidr"),
    }
    right_evidence = {
        "r-all": evidence(2, "vpc-1", "all"),
        "r-attributes": evidence(2, "vpc-1", "attributes"),
    }
    differences = compare(
        [left], [right], left_kw={"evidence": left_evidence}, right_kw={"evidence": right_evidence}
    )
    by_path = {d.path: d for d in differences}
    cidr, other = by_path["/attributes/cidr"], by_path["/attributes/other"]
    assert (cidr.left_evidence_ids, cidr.right_evidence_ids) == (["l-cidr"], ["r-attributes"])
    # No fact on the left: the resource's own evidence; the right cites its ancestor fact.
    assert (other.left_evidence_ids, other.right_evidence_ids) == (
        ["l-all", "l-cidr"],
        ["r-attributes"],
    )


def test_no_citation_is_invented_when_a_side_records_no_evidence():
    differences = compare([vpc(attributes={"a": 1})], [vpc(attributes={"a": 2})])
    assert differences[0].left_evidence_ids == differences[0].right_evidence_ids == []


# ---- redacted, unknown, and omitted values ---------------------------------------


def test_redacted_values_are_never_compared_by_content():
    left = vpc(attributes={"a": "[REDACTED]", "b": "[REDACTED]", "c": "x", "d": "[REDACTED]"})
    right = vpc(attributes={"a": "[REDACTED]", "b": "x", "c": "[REDACTED]", "d": "[REDACTED]"})
    differences = compare([left], [right])
    assert summary(differences) == [
        (FIELD, "/attributes/b", INDETERMINATE),
        (FIELD, "/attributes/c", INDETERMINATE),
    ]
    b, c = differences
    assert [q.kind for q in b.qualifications + c.qualifications] == ["redacted_value"] * 2
    assert (b.qualifications[0].side, c.qualifications[0].side) == (LEFT, RIGHT)
    assert b.left_value is None and b.right_value is None


def test_a_field_absent_from_a_side_with_unknown_values_is_indeterminate_not_added():
    left = vpc(attributes={"a": 1}, evidence=["unknown"])
    right = vpc(attributes={"a": 1, "arn": "arn:x", "gone": 1})
    left_evidence = {"unknown": evidence(1, "vpc-1", "/after_unknown/arn", status="unresolved")}
    differences = compare([left], [right], left_kw={"evidence": left_evidence})
    assert summary(differences) == [
        (FIELD, "/attributes/arn", INDETERMINATE),
        (FIELD, "/attributes/gone", INDETERMINATE),
    ]
    qualification = differences[0].qualifications[0]
    assert (qualification.kind, qualification.side) == ("unknown_value", LEFT)
    assert qualification.evidence_ids == ["unknown"]


def test_omitted_values_qualify_the_absent_side_and_present_changes_stay_differences():
    left = vpc(attributes={"a": 1, "b": 1}, evidence=["omitted"])
    right = vpc(attributes={"a": 2, "b": 1, "c": 1}, evidence=["r"])
    differences = compare(
        [left],
        [right],
        left_kw={"evidence": {"omitted": evidence(1, "vpc-1", "x", status="omitted")}},
        right_kw={"evidence": {"r": evidence(2, "vpc-1", "x")}},
    )
    assert summary(differences) == [
        (FIELD, "/attributes/a", CHANGED),
        (FIELD, "/attributes/c", INDETERMINATE),
    ]


# ---- containment -----------------------------------------------------------------


def test_matched_parents_are_equal_and_a_changed_or_missing_parent_is_a_difference():
    # subnet-1 sits in vpc-1 on the left; in vpc-2 on the right.
    left = [vpc("vpc-1", children=[subnet("subnet-1")]), vpc("vpc-2")]
    right = [vpc("vpc-1"), vpc("vpc-2", children=[subnet("subnet-1")])]
    left_index, right_index = index(left, seed=1), index(right, seed=2)
    outcome = match_resources(left_index, right_index)
    comparer = FieldComparer(left_index, right_index, outcome.matched)
    pair = next(m for m in outcome.matched if m.left_id == rid("subnet-1"))
    [difference] = comparer.compare(pair)
    assert (difference.subject, difference.path, difference.kind) == (
        CONTAINMENT,
        "/parent",
        CHANGED,
    )
    assert difference.left_value == {"left": rid("vpc-1"), "right": rid("vpc-1")}
    assert difference.right_value == {"left": rid("vpc-2"), "right": rid("vpc-2")}

    same = index([vpc("vpc-1", children=[subnet("subnet-1")])], seed=3)
    same_outcome = match_resources(
        same, index([vpc("vpc-1", children=[subnet("subnet-1")])], seed=4)
    )
    again = FieldComparer(
        same, index([vpc("vpc-1", children=[subnet("subnet-1")])], seed=4), same_outcome.matched
    )
    assert all(again.compare(m) == [] for m in same_outcome.matched)


def test_a_parent_added_or_removed_is_reported_with_the_missing_side_null():
    left = [vpc("vpc-1", children=[subnet("subnet-1")])]
    right = [vpc("vpc-1"), subnet("subnet-1")]
    left_index, right_index = index(left, seed=1), index(right, seed=2)
    outcome = match_resources(left_index, right_index)
    comparer = FieldComparer(left_index, right_index, outcome.matched)
    pair = next(m for m in outcome.matched if m.left_id == rid("subnet-1"))
    [difference] = comparer.compare(pair)
    assert difference.kind == REMOVED
    assert difference.right_value is None
    assert difference.left_value == {"left": rid("vpc-1"), "right": rid("vpc-1")}


# ---- dependencies ----------------------------------------------------------------


def test_equal_dependency_edges_are_equal_and_a_missing_edge_is_added_or_removed():
    resources = [vpc("vpc-1"), vpc("vpc-2"), vpc("vpc-3")]
    left = {"dependencies": [edge("vpc-1", "vpc-2", kind="route_target"), edge("vpc-1", "vpc-3")]}
    right = {"dependencies": [edge("vpc-1", "vpc-2", kind="route_target")]}
    [removed] = compare(resources, resources, left_kw=left, right_kw=right)
    assert (removed.subject, removed.kind, removed.right_value) == (DEPENDENCY, REMOVED, None)
    assert removed.left_value == {"left": rid("vpc-3"), "right": rid("vpc-3")}
    # The edge's own citation on the left; the right falls back to the resource's evidence.
    assert removed.left_evidence_ids == ["e"] and removed.right_evidence_ids == ["e"]

    [added] = compare(resources, resources, left_kw=right, right_kw=left)
    assert (added.kind, added.left_value) == (ADDED, None)


def test_dependency_targets_are_compared_by_matched_identity_and_unmatched_ones_differ():
    # The edge targets matched vpc-2 on both sides, but different unmatched resources too.
    left = [vpc("vpc-1"), vpc("vpc-2"), vpc("only-left")]
    right = [vpc("vpc-1"), vpc("vpc-2"), vpc("only-right")]
    differences = compare(
        left,
        right,
        left_kw={"dependencies": [edge("vpc-1", "vpc-2"), edge("vpc-1", "only-left")]},
        right_kw={"dependencies": [edge("vpc-1", "vpc-2"), edge("vpc-1", "only-right")]},
    )
    assert sorted(d.kind for d in differences) == [ADDED, REMOVED]
    removed = next(d for d in differences if d.kind == REMOVED)
    assert removed.left_value == {"left": rid("only-left"), "right": None}


def test_attachment_kinds_separate_edges_to_the_same_target():
    resources = [vpc("vpc-1"), vpc("vpc-2")]
    left = {"dependencies": [edge("vpc-1", "vpc-2", kind="route_target")]}
    right = {"dependencies": [edge("vpc-1", "vpc-2", kind="role_policy")]}
    differences = compare(resources, resources, left_kw=left, right_kw=right)
    assert sorted(d.kind for d in differences) == [ADDED, REMOVED]
    assert {d.path.split("/")[2] for d in differences} == {"route_target", "role_policy"}


def test_external_targets_compare_by_their_recorded_target():
    resources = [vpc("vpc-1")]
    external = {"kind": "external", "identifier": "arn:aws:s3:::bucket"}
    other = {**external, "identifier": "arn:aws:s3:::other"}
    left = {"dependencies": [edge("vpc-1", external)]}
    right = {"dependencies": [edge("vpc-1", other)]}
    assert [d.kind for d in compare(resources, resources, left_kw=left, right_kw=left)] == []
    assert len(compare(resources, resources, left_kw=left, right_kw=right)) == 2


def test_unresolved_references_are_indeterminate_rather_than_differences():
    unresolved = {"kind": "unresolved", "reason": "unknown", "source_path": "a.b"}
    resources = [vpc("vpc-1"), vpc("vpc-2")]
    left = {"dependencies": [edge("vpc-1", unresolved)]}
    right = {"dependencies": [edge("vpc-1", "vpc-2")]}
    [difference] = compare(resources, resources, left_kw=left, right_kw=right)
    assert difference.kind == INDETERMINATE
    qualification = difference.qualifications[0]
    assert (qualification.kind, qualification.side) == ("unresolved_reference", LEFT)
    assert qualification.evidence_ids == ["e"]
    assert difference.left_evidence_ids == ["e"] and difference.right_evidence_ids == ["e"]

    # Unresolved on both sides, nothing else differing: nothing to report.
    assert compare(resources, resources, left_kw=left, right_kw=left) == []

    # An unresolved reference with no extra target on the other side is still reported.
    [alone] = compare(resources, resources, left_kw=left)
    assert alone.kind == INDETERMINATE
    assert alone.path == f"/attached_to//{REGION}"  # the group, with no target
    assert alone.qualifications[0].side == LEFT


def test_differences_are_unique_and_in_canonical_order():
    left = vpc(attributes={"b": 1, "a": 1}, name="x")
    right = vpc(attributes={"b": 2, "a": 2})
    differences = compare([left], [right])
    paths = [d.path for d in differences]
    assert paths == ["/attributes/a", "/attributes/b", "/name"]


# ---- unknown typed values and source bookkeeping ----------------------------------


def typed(cidr, *, cited=False, evidence=()):
    """A VPC whose typed `cidr_blocks` is `cidr` (None stands for null)."""
    data = vpc(evidence=evidence, fact_evidence={"/cidr_blocks": ["fact"]} if cited else None)
    return {**data, "cidr_blocks": cidr}


def test_an_unknown_typed_value_stored_as_null_is_indeterminate_not_changed():
    left = typed(["10.0.0.0/16"])
    right = typed(None, evidence=["unknown"])
    right_evidence = {"unknown": evidence(2, "vpc-1", "/after_unknown/cidr", status="unresolved")}
    [difference] = compare([left], [right], right_kw={"evidence": right_evidence})
    assert (difference.path, difference.kind) == ("/cidr_blocks", INDETERMINATE)
    qualification = difference.qualifications[0]
    assert (qualification.kind, qualification.side) == ("unknown_value", RIGHT)
    assert qualification.evidence_ids == ["unknown"]


def test_a_cited_null_is_a_known_value_even_beside_unknown_values():
    left = typed(["10.0.0.0/16"])
    right = typed(None, cited=True, evidence=["unknown", "fact"])
    right_evidence = {
        "unknown": evidence(2, "vpc-1", "/after_unknown/other", status="unresolved"),
        "fact": evidence(2, "vpc-1", "cidr"),
    }
    [difference] = compare([left], [right], right_kw={"evidence": right_evidence})
    assert (difference.path, difference.kind) == ("/cidr_blocks", CHANGED)
    assert difference.right_value is None and difference.right_evidence_ids == ["fact"]


def test_a_null_without_unknown_values_is_still_a_change():
    [difference] = compare([typed(["10.0.0.0/16"])], [typed(None)])
    assert difference.kind == CHANGED


def test_the_terraform_type_bookkeeping_attribute_is_not_a_difference():
    left = vpc(attributes={"a": 1})
    right = vpc(attributes={"a": 1, "terraform_type": "aws_vpc"})
    assert compare([left], [right]) == []
