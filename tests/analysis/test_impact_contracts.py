from contextlib import suppress
from itertools import product

import pytest
from pydantic import ValidationError

from archdiscov.models.analysis.impact import SecurityGroupImpact
from archdiscov.models.analysis.results import AnalysisResult
from archdiscov.models.enums import ImpactKind, ImpactLabel, RuleDirection, RuleReferenceSide
from archdiscov.services import (
    AnalysisValidationError,
    load_snapshot,
    validate_analysis_result,
)
from tests.paths import FIXTURES_ROOT

PREFIX = "123456789012/eu-west-1/ec2/physical/"
SGW, ENI, RTB = PREFIX + "sg-web", PREFIX + "eni-web", PREFIX + "rtb-main"
SNAPSHOT_ID = "00000000-0000-4000-8000-000000000007"
SCOPE = {"platform": "aws", "account_id": "123456789012", "partition": "aws"}

DIRECT = {"kind": "direct_attachment", "label": "observed"}
RULE = {
    "kind": "rule_reference",
    "label": "observed",
    "rule_direction": "ingress",
    "reference_side": "other_references_selected",
}


def impact(selected=SGW, **fields):
    return {"selected_group_id": selected, **fields}


def finding(impact_data):
    return {
        "snapshot_id": SNAPSHOT_ID,
        "summary": "Recorded structure relates this resource to the selected group.",
        "status": "supported",
        "evidence_ids": ["sg"],
        "resources": [{"resource_id": ENI, "scope": SCOPE}],
        "impact": impact_data,
    }


def result(findings, resource_id=SGW, kind="security_group_impact"):
    parameters = {"kind": kind, "resource_id": resource_id}
    if kind == "ownership":
        parameters = {"kind": kind}
    return {
        "snapshot_id": SNAPSHOT_ID,
        "analyzer": kind,
        "analyzer_version": "1",
        "parameters": parameters,
        "created_at": "2026-10-13T09:00:00Z",
        "findings": findings,
    }


def test_labels_follow_the_kind_and_rule_fields_only_rule_references():
    def validate(kind, label):
        fields = {"selected_group_id": SGW, "kind": kind, "label": label}
        if kind == ImpactKind.RULE_REFERENCE:
            fields |= {
                "rule_direction": RuleDirection.INGRESS,
                "reference_side": RuleReferenceSide.OTHER_REFERENCES_SELECTED,
            }
        SecurityGroupImpact.model_validate(fields)

    valid = []
    for kind, label in product(ImpactKind, ImpactLabel):
        with suppress(ValidationError):
            validate(kind, label)
            valid.append((kind, label))
    assert valid == [
        (ImpactKind.DIRECT_ATTACHMENT, ImpactLabel.OBSERVED),
        (ImpactKind.RULE_REFERENCE, ImpactLabel.OBSERVED),
        (ImpactKind.DOWNSTREAM_DEPENDENCY, ImpactLabel.POTENTIAL),
    ]
    for fields in (
        {k: v for k, v in RULE.items() if k != "rule_direction"},
        {**DIRECT, "rule_direction": "ingress"},
    ):
        with pytest.raises(ValidationError, match="rule"):
            SecurityGroupImpact.model_validate(impact(**fields))


def test_impact_requires_matching_security_group_parameters():
    AnalysisResult.model_validate(result([finding(impact(**DIRECT))]))
    with pytest.raises(ValidationError, match="require security_group_impact"):
        AnalysisResult.model_validate(result([finding(impact(**DIRECT))], kind="ownership"))
    with pytest.raises(ValidationError, match="selected security group"):
        AnalysisResult.model_validate(result([finding(impact(RTB, **DIRECT))]))


def test_integrity_requires_the_selected_resource_to_be_a_security_group():
    snapshot = load_snapshot(FIXTURES_ROOT / "snapshots" / "network.json")
    items = [finding(impact(**fields)) for fields in (DIRECT, RULE)]
    valid = validate_analysis_result(result(items), snapshot)
    assert [f.impact.label for f in valid.findings] == [ImpactLabel.OBSERVED] * 2
    data = result([finding(impact(RTB, **DIRECT))], resource_id=RTB)
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(data, snapshot)
    assert error.value.details[0].type == "resource_not_security_group"
