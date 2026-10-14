from copy import deepcopy
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from archdiscov.models.analysis_parameters import (
    AnalyzerParameters,
    CrossAccountTrustParameters,
    DatabaseAccessParameters,
    InternetExposureParameters,
    OwnershipParameters,
    SecurityGroupImpactParameters,
)
from archdiscov.models.analysis_references import (
    AnalysisLimitation,
    ContainmentPathStep,
    CoverageReference,
    DependencyPathStep,
    PathStep,
    ResourceReference,
    UnresolvedTargetReference,
)
from archdiscov.models.analysis_results import (
    AnalysisFinding,
    AnalysisResult,
    deterministic_finding_id,
)

SNAPSHOT_ID = UUID("00000000-0000-4000-8000-000000000001")
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
                "attributes": {"kind": "security_group_membership"},
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


@pytest.mark.parametrize(
    "data,model",
    [
        ({"kind": "ownership"}, OwnershipParameters),
        ({"kind": "security_group_impact", "resource_id": "sg"}, SecurityGroupImpactParameters),
        ({"kind": "cross_account_trust"}, CrossAccountTrustParameters),
        ({"kind": "database_access", "resource_id": "db", "port": 443}, DatabaseAccessParameters),
        ({"kind": "internet_exposure"}, InternetExposureParameters),
    ],
)
def test_parameter_variants(data, model) -> None:
    adapter = TypeAdapter(AnalyzerParameters)
    parameters = adapter.validate_python(data)
    assert isinstance(parameters, model)
    assert adapter.validate_json(parameters.model_dump_json()) == parameters
    if isinstance(parameters, OwnershipParameters):
        assert parameters.ownership_keys == ["Owner", "Team"]
    if isinstance(parameters, (SecurityGroupImpactParameters, InternetExposureParameters)):
        assert parameters.max_depth == 3
        assert parameters.result_limit == 100


@pytest.mark.parametrize(
    "model,base,field,values",
    [
        (OwnershipParameters, {}, "ownership_keys", [[], ["Owner", "Owner"], [" "]]),
        (SecurityGroupImpactParameters, {"resource_id": "sg"}, "max_depth", [0, 11]),
        (InternetExposureParameters, {}, "max_depth", [0, 11]),
        (SecurityGroupImpactParameters, {"resource_id": "sg"}, "result_limit", [0, 10001]),
        (InternetExposureParameters, {}, "result_limit", [0, 10001]),
        (DatabaseAccessParameters, {"resource_id": "db"}, "port", [0, 65536]),
        (DatabaseAccessParameters, {"port": 443}, "resource_id", ["", " "]),
        (CrossAccountTrustParameters, {}, "resource_id", ["unexpected"]),
    ],
)
def test_parameter_constraints(model, base, field, values) -> None:
    for value in values:
        with pytest.raises(ValidationError):
            model.model_validate({**base, field: value})


@pytest.mark.parametrize("kind", ["unknown", None])
def test_parameter_discriminator(kind) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(AnalyzerParameters).validate_python({"kind": kind})


@pytest.mark.parametrize("scope", [AWS_SCOPE, AZURE_SCOPE])
def test_scoped_references(scope) -> None:
    reference = ResourceReference(resource_id="resource", scope=scope)
    assert ResourceReference.model_validate_json(reference.model_dump_json()) == reference
    coverage = CoverageReference(scope=scope, service="network", operation="collect")
    assert coverage.location is None
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ResourceReference.model_validate({**reference.model_dump(), "platform": scope["platform"]})


@pytest.mark.parametrize(
    "target",
    [
        {"kind": "local", "resource_id": "target"},
        {"kind": "external", "scope": AWS_SCOPE, "identifier": "external"},
        {"kind": "external", "scope": AZURE_SCOPE, "identifier": "external"},
        {"kind": "unresolved", "reason": "Unknown", "source_path": "/target"},
    ],
)
def test_dependency_target_variants_and_sanitization(target) -> None:
    step = DependencyPathStep(
        source_id="source",
        target=target,
        relationship="attached_to",
        evidence_ids=["citation"],
        attributes={"password": "private", "port": 443},
    )
    assert step.attributes == {"password": "[REDACTED]", "port": 443}
    assert TypeAdapter(PathStep).validate_json(step.model_dump_json()) == step


def test_structural_containment_is_not_a_dependency() -> None:
    step = ContainmentPathStep(source_id="parent", target_id="child")
    assert step.evidence_ids == []
    assert isinstance(
        TypeAdapter(PathStep).validate_json(step.model_dump_json()), ContainmentPathStep
    )
    with pytest.raises(ValidationError):
        ContainmentPathStep.model_validate({**step.model_dump(), "relationship": "attached_to"})


@pytest.mark.parametrize(
    "model,data",
    [
        (DependencyPathStep, finding_data()["path"][0]),
        (UnresolvedTargetReference, limitation_data()["unresolved_target"]),
    ],
)
def test_dependency_and_unresolved_references_require_citations(model, data) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**data, "evidence_ids": []})


@pytest.mark.parametrize("field", ["code", "message"])
def test_structured_limitation_fields(field) -> None:
    with pytest.raises(ValidationError):
        AnalysisLimitation.model_validate({**limitation_data(), field: " "})


@pytest.mark.parametrize("status", ["supported", "partial", "insufficient_evidence"])
def test_finding_round_trip_preserves_legacy_invariants(status) -> None:
    data = finding_data(status)
    if status == "insufficient_evidence":
        data["evidence_ids"] = []
    finding = AnalysisFinding.model_validate(data)
    assert finding.id == deterministic_finding_id(finding)
    assert len(finding.id) == 64
    assert AnalysisFinding.model_validate_json(finding.model_dump_json()) == finding
    if status != "supported":
        with pytest.raises(ValidationError, match="require limitations"):
            AnalysisFinding.model_validate({**data, "limitations": []})
    if status != "insufficient_evidence":
        with pytest.raises(ValidationError, match="require evidence_ids"):
            AnalysisFinding.model_validate({**data, "evidence_ids": []})


def test_id_canonicalizes_set_like_order_but_preserves_conclusion() -> None:
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
    assert (
        AnalysisFinding.model_validate({**data, "summary": "A different conclusion."}).id
        != original.id
    )


@pytest.mark.parametrize(
    "change", ["snapshot", "status", "resource", "path", "code", "context", "evidence"]
)
def test_id_tracks_semantic_changes(change) -> None:
    data = finding_data("partial")
    original = AnalysisFinding.model_validate(data)
    changed = deepcopy(data)
    if change == "snapshot":
        changed["snapshot_id"] = "00000000-0000-4000-8000-000000000002"
    elif change == "status":
        changed["status"] = "insufficient_evidence"
    elif change == "resource":
        changed["resources"][0]["resource_id"] = "another"
    elif change == "path":
        changed["path"].reverse()
    elif change == "code":
        changed["limitations"][0]["code"] = "another"
    elif change == "context":
        changed["limitations"][0]["coverage"]["operation"] = "AnotherOperation"
    else:
        changed["evidence_ids"] = ["another"]
    assert AnalysisFinding.model_validate(changed).id != original.id


@pytest.mark.parametrize("id_value", [None, "", " ", "tampered"])
def test_persisted_id_is_nonempty_nonnull_and_verified(id_value) -> None:
    with pytest.raises(ValidationError):
        AnalysisFinding.model_validate({**finding_data(), "id": id_value})


def test_detached_revalidation_rejects_changed_semantics() -> None:
    finding = AnalysisFinding.model_validate(finding_data())
    data = finding.model_dump(mode="json")
    data["summary"] = "Changed conclusion."
    with pytest.raises(ValidationError, match="deterministic_finding_id"):
        AnalysisFinding.model_validate(data)


def test_result_round_trip_and_timestamp_exclusion() -> None:
    data = result_data()
    result = AnalysisResult.model_validate(data)
    assert result.schema_version == "1"
    assert AnalysisResult.model_validate_json(result.model_dump_json()) == result
    other = AnalysisResult.model_validate({**data, "created_at": "2026-02-01T00:00:00Z"})
    assert other.findings[0].id == result.findings[0].id


@pytest.mark.parametrize("change", ["kind", "snapshot", "duplicate", "timezone", "schema", "extra"])
def test_result_envelope_constraints(change) -> None:
    data = result_data()
    if change == "kind":
        data["analyzer"] = "internet_exposure"
    elif change == "snapshot":
        data["findings"][0]["snapshot_id"] = "00000000-0000-4000-8000-000000000002"
    elif change == "duplicate":
        data["findings"].append(deepcopy(data["findings"][0]))
    elif change == "timezone":
        data["created_at"] = "2026-01-01T00:00:00"
    elif change == "schema":
        data["schema_version"] = "2"
    else:
        data["raw_context"] = {"secret": "private"}
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(data)
