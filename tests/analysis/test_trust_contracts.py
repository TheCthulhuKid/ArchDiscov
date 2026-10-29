import json

import pytest
from pydantic import ValidationError

from archdiscov.graph import GraphIndex
from archdiscov.models.analysis.trust import CrossAccountTrust
from archdiscov.models.resource_base import ResourceIdentity
from archdiscov.services import AnalysisValidationError, validate_analysis_result, validate_snapshot
from tests.paths import FIXTURES_ROOT


def _snapshot():
    """The IAM fixture plus one evidence record owned by its role."""
    data = json.loads((FIXTURES_ROOT / "snapshots" / "iam.json").read_text())
    role = next(r for r in data["global_resources"] if r["resource_type"] == "AWS::IAM::Role")
    resource_id = ResourceIdentity.model_validate(role["identity"]).stable_id
    role["evidence_ids"] = [*role.get("evidence_ids", []), "trust-statement"]
    data["evidence"] = {}
    data["evidence"]["trust-statement"] = {
        "snapshot_id": data["snapshot_id"],
        "resource_id": resource_id,
        "source": "mock",
        "locator": "iam:GetRole",
        "configuration_path": "Role.AssumeRolePolicyDocument.Statement[0]",
        "value": {"Effect": "Allow"},
    }
    return validate_snapshot(data)


SNAPSHOT = _snapshot()
INDEX = GraphIndex(SNAPSHOT)
ROLE = next(r.id for r in INDEX.resources() if r.resource_type == "AWS::IAM::Role")
PROFILE = next(r.id for r in INDEX.resources() if r.resource_type == "AWS::IAM::InstanceProfile")
EVIDENCE = "trust-statement"
SNAPSHOT_ID = str(INDEX.snapshot_id)
SCOPE = INDEX.scope.model_dump(mode="json")
ROLE_TRUST = {
    "source": "role_trust_policy",
    "classification": "external_account",
    "effect": "Allow",
    "conditional": False,
}


def trust(**fields):
    return {**ROLE_TRUST, **fields}


def result(trust_data, resource=ROLE):
    finding = {
        "snapshot_id": SNAPSHOT_ID,
        "summary": "A recorded trust statement names this principal.",
        "status": "supported",
        "evidence_ids": [EVIDENCE],
        "resources": [{"resource_id": resource, "scope": SCOPE}],
        "trust": trust_data,
    }
    return {
        "snapshot_id": SNAPSHOT_ID,
        "analyzer": "cross_account_trust",
        "analyzer_version": "1",
        "parameters": {"kind": "cross_account_trust"},
        "created_at": "2026-10-13T09:00:00Z",
        "findings": [finding],
    }


def test_not_principal_must_be_unresolved():
    ok = CrossAccountTrust.model_validate(trust(classification="unresolved", not_principal=True))
    assert ok.not_principal is True
    with pytest.raises(ValidationError, match="unresolved"):
        CrossAccountTrust.model_validate(trust(not_principal=True))


def test_integrity_requires_the_trust_source_to_match_the_trusting_resource():
    [item] = validate_analysis_result(result(trust(conditional=True)), SNAPSHOT).findings
    assert item.evidence_ids == [EVIDENCE]
    assert item.trust.conditional is True
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(result(trust(), resource=PROFILE), SNAPSHOT)
    assert error.value.details[0].type == "trust_source_mismatch"
