import json
from pathlib import Path

import pytest

from archdiscov.services import (
    EvidenceNotFoundError,
    SnapshotValidationError,
    get_evidence,
    load_snapshot,
)

FIXTURES = Path(__file__).parent / "fixtures" / "snapshots"


@pytest.mark.parametrize("evidence_id", ["instance", "role"])
def test_nested_and_global_evidence_owners(evidence_id: str) -> None:
    snapshot = load_snapshot(FIXTURES / "hierarchy.json")
    result = get_evidence(snapshot, evidence_id)
    assert result.snapshot_id == snapshot.snapshot_id
    assert result.snapshot_source == snapshot.source
    assert result.evidence == snapshot.evidence[evidence_id]
    assert result.resource.id == result.evidence.resource_id
    assert result.fact_paths == (["/instance_type"] if evidence_id == "instance" else [])
    if evidence_id == "instance":
        assert result.resource.instance_type == "t3.micro"
        assert result.model_dump(mode="json")["resource"]["instance_type"] == "t3.micro"
    else:
        assert result.resource.identity.region == "global"
        assert result.resource.aws_resource_type == "AWS::IAM::Role"
    assert result.coverage == []


def test_evidence_context_is_detached_sanitized_and_retains_limitations() -> None:
    snapshot = load_snapshot(FIXTURES / "partial-coverage.json")
    snapshot.evidence["sg-rule"].value["Password"] = "do-not-print"
    result = get_evidence(snapshot, "sg-rule")
    assert result.evidence.value["Password"] == "[REDACTED]"
    assert result.evidence.value_status == "redacted"
    assert result.evidence.redacted_paths == ["/Password"]
    assert result.fact_paths == ["/attributes/ingress/0"]
    assert [operation.status for operation in result.coverage] == ["success", "failed", "success"]
    assert result.coverage[1].failures[0].code == "UnauthorizedOperation"
    assert result.coverage[2].result_count == 0
    assert snapshot.evidence["sg-rule"].value["Password"] == "do-not-print"
    result.resource.attributes.clear()
    assert snapshot.regions["eu-west-1"].resources[0].attributes


def test_context_excludes_other_regions_and_services() -> None:
    data = json.loads((FIXTURES / "partial-coverage.json").read_text())
    unrelated = dict(data["coverage"]["operations"][0], service="iam")
    data["coverage"]["operations"].append(unrelated)
    data["coverage"]["regions"].append("us-east-1")
    data["coverage"]["operations"].append(dict(unrelated, region="us-east-1", service="ec2"))
    assert len(get_evidence(data, "sg-rule").coverage) == 3


def test_uncited_evidence_remains_inspectable() -> None:
    snapshot = load_snapshot(FIXTURES / "partial-coverage.json")
    resource = snapshot.regions["eu-west-1"].resources[0]
    resource.evidence_ids.clear()
    resource.fact_evidence.clear()
    assert get_evidence(snapshot, "sg-rule").fact_paths == []


def test_fact_citation_does_not_require_broad_citation() -> None:
    snapshot = load_snapshot(FIXTURES / "partial-coverage.json")
    snapshot.regions["eu-west-1"].resources[0].evidence_ids.clear()
    assert get_evidence(snapshot, "sg-rule").fact_paths == ["/attributes/ingress/0"]


def test_unknown_evidence_is_a_domain_error() -> None:
    with pytest.raises(EvidenceNotFoundError) as error:
        get_evidence(load_snapshot(FIXTURES / "hierarchy.json"), "absent")
    assert error.value.code == "evidence_not_found"


def test_lookup_revalidates_mutated_references() -> None:
    snapshot = load_snapshot(FIXTURES / "hierarchy.json")
    snapshot.evidence["instance"].resource_id = "missing"
    with pytest.raises(SnapshotValidationError) as error:
        get_evidence(snapshot, "instance")
    assert "unknown resource owner" in error.value.details[0].message
