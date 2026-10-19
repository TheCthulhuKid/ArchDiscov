import json
from contextlib import suppress
from itertools import product

import pytest
from pydantic import ValidationError

from archdiscov.models.analysis.impact import SecurityGroupImpact
from archdiscov.models.analysis.references import (
    UNSUPPORTED_PLATFORM_CODE,
    AnalysisLimitation,
    ResourceReference,
)
from archdiscov.models.analysis.results import AnalysisFinding, AnalysisResult
from archdiscov.models.enums import (
    AnalysisStatus,
    AnalyzerErrorCode,
    ImpactKind,
    ImpactLabel,
    RuleDirection,
    RuleReferenceSide,
)
from archdiscov.services import (
    AnalysisValidationError,
    AnalyzerError,
    load_snapshot,
    validate_analysis_result,
)
from tests.paths import FIXTURES_ROOT

OWNERSHIP_ID = "1533e8b92a834782f05afc35c2a7bb1dd0aec49203036c971c8261163357700c"
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
DOWNSTREAM = {"kind": "downstream_dependency", "label": "potential"}


def impact(selected=SGW, **fields):
    return {"selected_group_id": selected, **fields}


def finding(impact_data=None, evidence=("sg",), resource=ENI, **overrides):
    data = {
        "snapshot_id": SNAPSHOT_ID,
        "summary": "Recorded structure relates this resource to the selected group.",
        "status": "supported",
        "evidence_ids": list(evidence),
        "resources": [{"resource_id": resource, "scope": SCOPE}],
        **overrides,
    }
    if impact_data is not None:
        data["impact"] = impact_data
    return data


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


@pytest.mark.parametrize("fields", [DIRECT, RULE, DOWNSTREAM], ids=["direct", "rule", "downstream"])
def test_valid_impacts_round_trip(fields):
    model = SecurityGroupImpact.model_validate(impact(**fields))
    assert SecurityGroupImpact.model_validate_json(model.model_dump_json()) == model


@pytest.mark.parametrize("direction", RuleDirection)
@pytest.mark.parametrize("side", RuleReferenceSide)
def test_every_rule_direction_and_side_is_representable(direction, side):
    model = SecurityGroupImpact(
        selected_group_id=SGW,
        kind=ImpactKind.RULE_REFERENCE,
        label=ImpactLabel.OBSERVED,
        rule_direction=direction,
        reference_side=side,
    )
    assert (model.rule_direction, model.reference_side) == (direction, side)


@pytest.mark.parametrize(
    "fields",
    [
        {**DIRECT, "label": "potential"},
        {**DOWNSTREAM, "label": "observed"},
        {**RULE, "label": "potential"},
        {k: v for k, v in RULE.items() if k != "rule_direction"},
        {k: v for k, v in RULE.items() if k != "reference_side"},
        {**DIRECT, "rule_direction": "ingress"},
        {**DOWNSTREAM, "reference_side": "selected_references_other"},
        {**DIRECT, "kind": "nonsense"},
    ],
    ids=[
        "direct-potential",
        "downstream-observed",
        "rule-potential",
        "rule-no-direction",
        "rule-no-side",
        "direct-with-rule-field",
        "downstream-with-rule-field",
        "unknown-kind",
    ],
)
def test_invalid_combinations_are_rejected(fields):
    with pytest.raises(ValidationError):
        SecurityGroupImpact.model_validate(impact(**fields))


def test_labels_and_kinds_are_part_of_finding_identity():
    ids = {
        AnalysisFinding.model_validate(finding(impact(**fields))).id
        for fields in (DIRECT, RULE, DOWNSTREAM)
    }
    plain = AnalysisFinding.model_validate(finding()).id
    assert len(ids | {plain}) == 4
    other_side = {**RULE, "reference_side": "selected_references_other"}
    assert AnalysisFinding.model_validate(finding(impact(**other_side))).id not in ids
    again = AnalysisFinding.model_validate(finding(impact(**DIRECT)))
    assert again.id == AnalysisFinding.model_validate(finding(impact(**DIRECT))).id


def test_findings_without_impact_keep_their_existing_identity():
    data = json.loads((FIXTURES_ROOT / "analysis" / "ownership.json").read_text())
    assert AnalysisFinding.model_validate(data["findings"][0]).id == OWNERSHIP_ID


def test_impact_requires_matching_security_group_parameters():
    valid = AnalysisResult.model_validate(result([finding(impact(**DIRECT))]))
    assert AnalysisResult.model_validate_json(valid.model_dump_json()) == valid
    with pytest.raises(ValidationError, match="require security_group_impact"):
        AnalysisResult.model_validate(result([finding(impact(**DIRECT))], kind="ownership"))
    with pytest.raises(ValidationError, match="selected security group"):
        AnalysisResult.model_validate(result([finding(impact(RTB, **DIRECT))]))


def test_results_validate_against_a_snapshot_and_keep_scope_and_citations():
    snapshot = load_snapshot(FIXTURES_ROOT / "snapshots" / "network.json")
    items = [finding(impact(**fields)) for fields in (DIRECT, RULE)]
    valid = validate_analysis_result(result(items), snapshot)
    assert {f.resources[0].scope.account_id for f in valid.findings} == {"123456789012"}
    assert all(f.evidence_ids == ["sg"] for f in valid.findings)
    assert [f.impact.label for f in valid.findings] == [ImpactLabel.OBSERVED] * 2


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (result([finding(impact(RTB, **DIRECT))], resource_id=RTB), "resource_not_security_group"),
        (result([finding(impact("absent", **DIRECT))], resource_id="absent"), "resource_unknown"),
    ],
    ids=["not-a-security-group", "unknown-group"],
)
def test_integrity_rejects_bad_selected_groups(data, code):
    snapshot = load_snapshot(FIXTURES_ROOT / "snapshots" / "network.json")
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(data, snapshot)
    assert error.value.details[0].type == code


def test_unsupported_platform_is_a_cited_free_limitation_not_an_impact():
    limitation = AnalysisLimitation(
        code=UNSUPPORTED_PLATFORM_CODE,
        message="Security-group impact is only analyzed for AWS snapshots.",
    )
    model = AnalysisFinding(
        snapshot_id=SNAPSHOT_ID,
        summary="Security-group impact is not supported for this cloud.",
        status=AnalysisStatus.INSUFFICIENT_EVIDENCE,
        resources=[ResourceReference(resource_id=SGW, scope=SCOPE)],
        limitations=[limitation],
    )
    assert model.impact is None
    assert AnalysisFinding.model_validate_json(model.model_dump_json()) == model


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (AnalyzerErrorCode.RESOURCE_NOT_FOUND, "Selected resource was not found in the snapshot."),
        (
            AnalyzerErrorCode.UNSUPPORTED_RESOURCE,
            "Selected resource is not supported by this analyzer.",
        ),
    ],
)
def test_new_analyzer_errors_have_fixed_messages(code, message):
    error = AnalyzerError(code)
    assert (error.code, error.message, str(error)) == (code, message, message)


def test_all_label_kind_pairs_are_covered_by_exactly_the_documented_rule():

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
