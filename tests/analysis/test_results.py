from uuid import UUID

import pytest
from pydantic import ValidationError

from archdiscov.models.enums import AnalysisStatus
from archdiscov.models.results import Answer, Finding

SNAPSHOT_ID = UUID("00000000-0000-4000-8000-000000000001")


def result_data(model: type[Finding] | type[Answer], status: str = "supported") -> dict:
    data = {
        "snapshot_id": str(SNAPSHOT_ID),
        "status": status,
        "evidence_ids": ["sg-rule"],
        "limitations": [] if status == "supported" else ["Route-table collection failed."],
    }
    if model is Finding:
        data.update(id="ingress-rule", summary="The recorded rule permits TCP port 443.")
        data["resource_ids"] = ["123456789012/eu-west-1/ec2/physical/sg-example"]
    else:
        data["finding_ids"] = ["ingress-rule"]
    return data


@pytest.mark.parametrize("model", [Finding, Answer])
@pytest.mark.parametrize("status", list(AnalysisStatus))
def test_result_contract_round_trip(model: type[Finding] | type[Answer], status: str) -> None:
    data = result_data(model, status)
    result = model.model_validate(data)
    for restored in (result, model.model_validate_json(result.model_dump_json())):
        assert restored.snapshot_id == SNAPSHOT_ID
        assert restored.status == status
        assert restored.evidence_ids == ["sg-rule"]
        assert restored.limitations == data["limitations"]
        if model is Finding:
            assert restored.id == data["id"]
            assert restored.summary == data["summary"]
            assert restored.resource_ids == data["resource_ids"]
        else:
            assert restored.finding_ids == ["ingress-rule"]


@pytest.mark.parametrize("model", [Finding, Answer])
@pytest.mark.parametrize("status", ["partial", "insufficient_evidence"])
def test_uncertainty_requires_explanation(model: type[Finding] | type[Answer], status: str) -> None:
    data = result_data(model, status)
    data["limitations"] = []
    with pytest.raises(ValidationError, match="require limitations"):
        model.model_validate(data)


@pytest.mark.parametrize("model,field", [(Finding, "evidence_ids"), (Answer, "finding_ids")])
@pytest.mark.parametrize("status", ["supported", "partial"])
def test_conclusions_require_citations(
    model: type[Finding] | type[Answer], field: str, status: str
) -> None:
    data = result_data(model, status)
    data[field] = []
    with pytest.raises(ValidationError, match=f"require {field}"):
        model.model_validate(data)


@pytest.mark.parametrize("model", [Finding, Answer])
def test_insufficient_result_can_have_no_citations(model: type[Finding] | type[Answer]) -> None:
    data = result_data(model, "insufficient_evidence")
    data["evidence_ids"] = []
    if model is Answer:
        data["finding_ids"] = []
    result = model.model_validate(data)
    assert result.evidence_ids == []
    assert result.limitations == ["Route-table collection failed."]


def test_answer_can_cite_evidence_through_findings() -> None:
    data = result_data(Answer)
    del data["evidence_ids"]
    assert Answer.model_validate(data).evidence_ids == []


def test_supported_finding_can_explain_scope_limits() -> None:
    data = result_data(Finding)
    data["limitations"] = ["A rule alone does not prove end-to-end reachability."]
    assert Finding.model_validate(data).limitations == data["limitations"]


@pytest.mark.parametrize(
    "model,field,value,error_type",
    [
        (Finding, "id", " ", "string_pattern_mismatch"),
        (Finding, "summary", "", "string_too_short"),
        (Finding, "resource_ids", [" "], "string_pattern_mismatch"),
        (Finding, "evidence_ids", [""], "string_too_short"),
        (Answer, "finding_ids", [" "], "string_pattern_mismatch"),
        (Answer, "limitations", [" "], "string_pattern_mismatch"),
        (Answer, "snapshot_id", "not-a-uuid", "uuid_parsing"),
        (Answer, "status", "certain", "enum"),
        (Answer, "narrative", "An unsupported conclusion.", "extra_forbidden"),
    ],
)
def test_invalid_result_fields(model, field, value, error_type) -> None:
    data = result_data(model)
    data[field] = value
    with pytest.raises(ValidationError) as error:
        model.model_validate(data)
    diagnostic = error.value.errors()[0]
    assert diagnostic["loc"][0] == field
    assert diagnostic["type"] == error_type
