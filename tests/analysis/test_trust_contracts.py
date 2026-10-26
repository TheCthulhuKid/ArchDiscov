import json
from itertools import product

import pytest
from pydantic import ValidationError

from archdiscov.graph import GraphIndex
from archdiscov.models.analysis.references import UNSUPPORTED_PLATFORM_CODE
from archdiscov.models.analysis.results import AnalysisFinding, AnalysisResult
from archdiscov.models.analysis.trust import CrossAccountTrust
from archdiscov.models.enums import TrustClassification, TrustEffect, TrustSource
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


def finding(trust_data=None, resource=ROLE, **overrides):
    data = {
        "snapshot_id": SNAPSHOT_ID,
        "summary": "A recorded trust statement names this principal.",
        "status": "supported",
        "evidence_ids": [EVIDENCE],
        "resources": [{"resource_id": resource, "scope": SCOPE}],
        **overrides,
    }
    if trust_data is not None:
        data["trust"] = trust_data
    return data


def result(findings, kind="cross_account_trust"):
    return {
        "snapshot_id": SNAPSHOT_ID,
        "analyzer": kind,
        "analyzer_version": "1",
        "parameters": {"kind": kind},
        "created_at": "2026-10-13T09:00:00Z",
        "findings": findings,
    }


def test_every_classification_effect_and_conditionality_round_trips():
    for classification, effect, conditional in product(
        TrustClassification, TrustEffect, [False, True]
    ):
        model = CrossAccountTrust(
            source=TrustSource.ROLE_TRUST_POLICY,
            classification=classification,
            effect=effect,
            conditional=conditional,
        )
        assert CrossAccountTrust.model_validate_json(model.model_dump_json()) == model
        assert model.not_principal is False


@pytest.mark.parametrize("source", TrustSource)
def test_both_trust_sources_are_representable(source):
    assert CrossAccountTrust.model_validate(trust(source=source.value)).source == source


def test_not_principal_must_be_unresolved():
    ok = CrossAccountTrust.model_validate(trust(classification="unresolved", not_principal=True))
    assert ok.not_principal is True
    for classification in TrustClassification:
        if classification != TrustClassification.UNRESOLVED:
            with pytest.raises(ValidationError, match="unresolved"):
                CrossAccountTrust.model_validate(
                    trust(classification=classification.value, not_principal=True)
                )


@pytest.mark.parametrize(
    "fields",
    [
        {"classification": "nonsense"},
        {"effect": "allow"},
        {"effect": "Maybe"},
        {"source": "somewhere"},
        {"conditional": "yes"},
        {"conditional": 1},
        {"not_principal": "no"},
    ],
    ids=[
        "classification",
        "lowercase-effect",
        "effect",
        "source",
        "str-bool",
        "int-bool",
        "np-bool",
    ],
)
def test_invalid_values_are_rejected(fields):
    with pytest.raises(ValidationError):
        CrossAccountTrust.model_validate(trust(**fields))


def test_every_trust_field_is_part_of_finding_identity():
    ids = {AnalysisFinding.model_validate(finding(trust())).id}
    variants = [
        {"classification": "same_account"},
        {"effect": "Deny"},
        {"conditional": True},
        {"source": "resource_policy"},
        {"classification": "unresolved", "not_principal": True},
    ]
    for fields in variants:
        ids.add(AnalysisFinding.model_validate(finding(trust(**fields))).id)
    assert len(ids) == 1 + len(variants)
    assert AnalysisFinding.model_validate(finding()).id not in ids


def test_finding_shape_rules():
    with pytest.raises(ValidationError, match="at most one"):
        AnalysisFinding.model_validate(
            finding(
                trust(),
                impact={
                    "selected_group_id": ROLE,
                    "kind": "direct_attachment",
                    "label": "observed",
                },
            )
        )
    with pytest.raises(ValidationError, match="trusting resource"):
        AnalysisFinding.model_validate(finding(trust(), resources=[]))


def test_trust_findings_require_cross_account_trust_parameters():
    valid = AnalysisResult.model_validate(result([finding(trust())]))
    assert AnalysisResult.model_validate_json(valid.model_dump_json()) == valid
    with pytest.raises(ValidationError, match="cross_account_trust parameters"):
        AnalysisResult.model_validate(result([finding(trust())], kind="ownership"))


def test_results_validate_against_a_snapshot_and_keep_scope_and_citations():
    checked = validate_analysis_result(result([finding(trust(conditional=True))]), SNAPSHOT)
    [item] = checked.findings
    assert item.resources[0].scope.account_id == "123456789012"
    assert item.evidence_ids == [EVIDENCE]
    assert item.trust.conditional is True


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (result([finding(trust(), resource=PROFILE)]), "trust_source_mismatch"),
        (result([finding(trust(source="resource_policy"))]), "trust_source_mismatch"),
        (result([finding(trust(), resource="absent")]), "resource_unknown"),
    ],
    ids=["role-policy-on-profile", "resource-policy-on-role", "unknown-resource"],
)
def test_integrity_rejects_trust_on_the_wrong_resource(data, code):
    with pytest.raises(AnalysisValidationError) as error:
        validate_analysis_result(data, SNAPSHOT)
    assert error.value.details[0].type == code


def test_unsupported_platform_code_is_shared_and_needs_no_trust_block():
    from archdiscov.models.analysis.references import AnalysisLimitation

    model = AnalysisFinding(
        snapshot_id=SNAPSHOT_ID,
        summary="Cross-account trust is not analyzed for this cloud.",
        status="insufficient_evidence",
        limitations=[AnalysisLimitation(code=UNSUPPORTED_PLATFORM_CODE, message="AWS only.")],
    )
    assert model.trust is None
