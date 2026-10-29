from itertools import product

import pytest
from pydantic import ValidationError

from archdiscov.graph import GraphIndex
from archdiscov.models.analysis.references import CoverageReference
from archdiscov.models.enums import NetworkBlockerCode, NetworkDecision
from archdiscov.models.network_path import NetworkBlocker, NetworkResult
from archdiscov.services import load_snapshot
from archdiscov.services.network_decisions import (
    all_of,
    allow,
    any_of,
    blocker,
    deny,
    first_match,
    missing,
    qualify_coverage,
    require_aws,
    unknown,
    unsupported,
)
from tests.paths import FIXTURES_ROOT

D = NetworkDecision
B = NetworkBlockerCode
SNAPSHOTS = FIXTURES_ROOT / "snapshots"


def denied(name="rule"):
    return deny([blocker(B.DENIED, f"Denied by {name}.", element=name, evidence_ids=[name])])


def make(decision, name="x"):
    return {
        D.ALLOW: lambda: allow([f"ev-{name}"]),
        D.DENY: lambda: denied(name),
        D.UNKNOWN: lambda: missing(f"{name} unknown", element=name),
    }[decision]()


# ---- contract ----------------------------------------------------------------------


def test_results_round_trip_and_serialize_every_blocker_field():
    reference = CoverageReference(
        scope={"platform": "aws", "account_id": "123456789012"},
        location="eu-west-1",
        service="ec2",
        operation="DescribeRouteTables",
    )
    result = unknown(
        [
            blocker(
                B.COVERAGE_LIMITED, "limited", element="op", resource_id="r", coverage=reference
            ),
            blocker(B.UNSUPPORTED_ELEMENT, "ipv6", element="route", evidence_ids=["b", "a"]),
        ]
    )
    assert NetworkResult.model_validate_json(result.model_dump_json()) == result
    assert [b.code for b in result.blockers] == [B.COVERAGE_LIMITED, B.UNSUPPORTED_ELEMENT]
    assert result.blockers[1].evidence_ids == ["a", "b"]


@pytest.mark.parametrize(
    "build",
    [
        lambda: NetworkResult(decision=D.ALLOW),
        lambda: NetworkResult(decision=D.DENY),
        lambda: NetworkResult(decision=D.UNKNOWN),
        lambda: NetworkBlocker(code=B.COVERAGE_LIMITED, message="m"),
    ],
    ids=["allow-uncited", "deny-bare", "unknown-bare", "limited-without-coverage"],
)
def test_invalid_results_and_blockers_are_rejected(build):
    with pytest.raises(ValidationError):
        build()


def test_missing_evidence_can_never_allow():
    assert missing("m").decision == D.UNKNOWN
    assert unsupported("m").blockers[0].code == B.UNSUPPORTED_ELEMENT
    with pytest.raises(ValidationError):
        allow([])


# ---- composition -------------------------------------------------------------------


@pytest.mark.parametrize("pair", list(product(D, D)))
def test_all_of_is_deny_then_unknown_then_allow(pair):
    result = all_of(make(d, str(i)) for i, d in enumerate(pair))
    if D.DENY in pair:
        assert result.decision == D.DENY
    elif D.UNKNOWN in pair:
        assert result.decision == D.UNKNOWN
    else:
        assert result.decision == D.ALLOW


@pytest.mark.parametrize("pair", list(product(D, D)))
def test_any_of_is_allow_then_unknown_then_deny(pair):
    result = any_of(make(d, str(i)) for i, d in enumerate(pair))
    if D.ALLOW in pair:
        assert result.decision == D.ALLOW
    elif D.UNKNOWN in pair:
        assert result.decision == D.UNKNOWN
    else:
        assert result.decision == D.DENY


def test_empty_compositions_are_unknown_never_a_vacuous_decision():
    for compose in (all_of, any_of):
        result = compose([])
        assert result.decision == D.UNKNOWN
        assert result.blockers[0].code == B.MISSING_EVIDENCE


def test_composition_keeps_only_contributing_evidence_and_blockers_deterministically():
    mixed = all_of(
        [allow(["z", "a"]), missing("later", element="b"), missing("early", element="a")]
    )
    assert mixed.decision == D.UNKNOWN
    assert [b.element for b in mixed.blockers] == ["a", "b"]
    assert any_of([allow(["e2"]), allow(["e1"]), missing("m")]).evidence_ids == ["e1", "e2"]
    assert all_of([allow(["e"]), allow(["e"])]).evidence_ids == ["e"]
    twice = all_of([denied("same"), denied("same")])
    assert len(twice.blockers) == 1
    assert all_of([allow(["b"]), allow(["a"])]) == all_of([allow(["a"]), allow(["b"])])


def test_first_match_decides_on_the_first_match_else_the_default():
    default = denied("implicit")
    ordered = [(False, allow(["skipped"])), (True, denied("hit")), (True, allow(["later"]))]
    assert first_match(ordered, default).decision == D.DENY
    assert first_match(ordered, default).blockers[0].element == "hit"
    assert first_match([(False, allow(["x"]))], default) == default
    assert first_match([], default) == default


def test_an_entry_that_cannot_be_matched_makes_first_match_unknown():
    default = denied("implicit")
    result = first_match([(None, allow(["maybe"])), (True, allow(["later"]))], default)
    assert result.decision == D.UNKNOWN
    assert result.blockers[0].code == B.MISSING_EVIDENCE
    own = unsupported("ipv6 entry", element="entry")
    assert first_match([(None, own)], default) == own


# ---- coverage qualification --------------------------------------------------------


@pytest.fixture
def partial():
    index = GraphIndex(load_snapshot(SNAPSHOTS / "partial-coverage.json"))
    return index, index.resources()


@pytest.mark.parametrize("decision", [D.ALLOW, D.DENY])
def test_limited_relevant_collection_turns_allow_and_deny_into_unknown(partial, decision):
    index, resources = partial
    result = qualify_coverage(index, make(decision), resources, {"DescribeRouteTables"})
    assert result.decision == D.UNKNOWN
    [limited] = [b for b in result.blockers if b.code == B.COVERAGE_LIMITED]
    assert limited.coverage.operation == "DescribeRouteTables"
    assert limited.coverage.service == "ec2"
    assert result.evidence_ids == make(decision).evidence_ids


def test_unknown_keeps_its_blockers_and_unrelated_operations_do_not_downgrade(partial):
    index, resources = partial
    original = make(D.UNKNOWN)
    result = qualify_coverage(index, original, resources)
    assert {b.code for b in result.blockers} == {B.MISSING_EVIDENCE, B.COVERAGE_LIMITED}
    successful = allow(["e"])
    assert qualify_coverage(index, successful, resources, {"DescribeSecurityGroups"}) == successful
    assert qualify_coverage(index, successful, resources, {"DescribeInstances"}) == successful


def test_contexts_without_limited_coverage_are_unchanged():
    index = GraphIndex(load_snapshot(SNAPSHOTS / "network.json"))
    result = allow(["e"])
    assert qualify_coverage(index, result, index.resources()) == result


def test_results_are_detached():
    result = allow(["e"])
    copy = NetworkResult.model_validate(result.model_dump())
    copy.evidence_ids.append("changed")
    assert result.evidence_ids == ["e"]


# ---- platform ----------------------------------------------------------------------


def test_non_aws_snapshots_get_the_shared_actionable_unsupported_result():
    azure = GraphIndex(load_snapshot(SNAPSHOTS / "azure.json"))
    result = require_aws(azure)
    assert result.decision == D.UNKNOWN
    [item] = result.blockers
    assert item.code == B.UNSUPPORTED_PLATFORM
    assert "AWS" in item.message and "Analyze an AWS snapshot" in item.message
    assert require_aws(GraphIndex(load_snapshot(SNAPSHOTS / "network.json"))) is None
