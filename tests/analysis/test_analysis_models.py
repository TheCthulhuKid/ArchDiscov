import json
from copy import deepcopy
from uuid import UUID

import pytest
from pydantic import ValidationError

from archdiscov.models.analysis.results import (
    AnalysisFinding,
    AnalysisResult,
    deterministic_finding_id,
)
from tests.paths import FIXTURES_ROOT

SNAPSHOT_ID = UUID("00000000-0000-4000-8000-000000000001")
# The saved ownership fixture's finding ID: adding finding blocks must not change it.
OWNERSHIP_ID = "1533e8b92a834782f05afc35c2a7bb1dd0aec49203036c971c8261163357700c"
AWS_SCOPE = {"platform": "aws", "account_id": "123456789012"}
AZURE_SCOPE = {
    "platform": "azure",
    "subscription_id": "00000000-0000-4000-8000-000000000002",
}


def finding_data(status="supported") -> dict:
    return {
        "snapshot_id": str(SNAPSHOT_ID),
        "summary": "The recorded rule permits TCP port 443.",
        "status": status,
        "evidence_ids": ["rule", "attachment"],
        "resources": [
            {"resource_id": "sg", "scope": AWS_SCOPE},
            {"resource_id": "vm", "scope": AZURE_SCOPE},
        ],
        "path": [
            {
                "kind": "dependency",
                "source_id": "sg",
                "target": {"kind": "local", "resource_id": "vm"},
                "relationship": "attached_to",
                "evidence_ids": ["rule", "attachment"],
                "attributes": {"kind": "security_group_membership", "password": "private"},
            },
            {"kind": "containment", "source_id": "vm", "target_id": "child"},
        ],
        "limitations": [] if status == "supported" else [limitation_data()],
    }


def limitation_data() -> dict:
    return {
        "code": "collection_failed",
        "message": "Route collection failed.",
        "coverage": {
            "scope": AWS_SCOPE,
            "location": "eu-west-1",
            "service": "ec2",
            "operation": "DescribeRouteTables",
        },
        "unresolved_target": {
            "source_id": "sg",
            "target": {"kind": "unresolved", "reason": "Unknown", "source_path": "/target"},
            "evidence_ids": ["rule", "attachment"],
        },
        "evidence_ids": ["rule", "attachment"],
    }


def result_data() -> dict:
    return {
        "snapshot_id": str(SNAPSHOT_ID),
        "analyzer": "ownership",
        "analyzer_version": "1",
        "parameters": {"kind": "ownership"},
        "created_at": "2026-01-01T00:00:00Z",
        "findings": [finding_data()],
    }


def test_finding_round_trip_redacts_attributes_and_keeps_status_rules() -> None:
    data = finding_data()
    finding = AnalysisFinding.model_validate(data)
    assert finding.path[0].attributes == {
        "kind": "security_group_membership",
        "password": "[REDACTED]",
    }
    assert finding.id == deterministic_finding_id(finding)
    assert AnalysisFinding.model_validate_json(finding.model_dump_json()) == finding
    with pytest.raises(ValidationError, match="require limitations"):
        AnalysisFinding.model_validate({**data, "status": "partial"})
    with pytest.raises(ValidationError, match="require evidence_ids"):
        AnalysisFinding.model_validate({**data, "evidence_ids": []})
    # A persisted ID is verified, so a changed conclusion cannot keep it.
    with pytest.raises(ValidationError, match="deterministic_finding_id"):
        AnalysisFinding.model_validate({**finding.model_dump(mode="json"), "summary": "Changed."})


def test_id_canonicalizes_set_like_order_but_tracks_the_conclusion() -> None:
    data = finding_data("partial")
    original = AnalysisFinding.model_validate(data)
    reordered = deepcopy(data)
    reordered["evidence_ids"].reverse()
    reordered["resources"].reverse()
    reordered["path"][0]["evidence_ids"].reverse()
    reordered["limitations"][0]["evidence_ids"].reverse()
    reordered["limitations"][0]["unresolved_target"]["evidence_ids"].reverse()
    reordered["limitations"][0]["message"] = "Different explanation wording."
    assert AnalysisFinding.model_validate(reordered).id == original.id
    changed = deepcopy(data)
    changed["path"].reverse()
    assert AnalysisFinding.model_validate(changed).id != original.id
    assert (
        AnalysisFinding.model_validate({**data, "summary": "A different conclusion."}).id
        != original.id
    )
    saved = json.loads((FIXTURES_ROOT / "analysis" / "ownership.json").read_text())
    assert AnalysisFinding.model_validate(saved["findings"][0]).id == OWNERSHIP_ID


@pytest.mark.parametrize(
    ("change", "message"),
    [("kind", "kind must match analyzer"), ("snapshot", "finding snapshot_id must match")],
)
def test_result_envelope_must_match_its_parameters_and_findings(change, message) -> None:
    data = result_data()
    if change == "kind":
        data["analyzer"] = "internet_exposure"
    else:
        data["findings"][0]["snapshot_id"] = "00000000-0000-4000-8000-000000000002"
    with pytest.raises(ValidationError, match=message):
        AnalysisResult.model_validate(data)
