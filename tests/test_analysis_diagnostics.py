from dataclasses import FrozenInstanceError, fields

import pytest

from archdiscov.models.analysis_parameters import (
    InternetExposureParameters,
    SecurityGroupImpactParameters,
)
from archdiscov.models.analysis_results import AnalysisResult
from archdiscov.services.analysis_diagnostics import (
    AnalysisValidationError,
    AnalysisValidationIssue,
    parse_analysis_result,
    reject,
)

SNAPSHOT_ID = "00000000-0000-4000-8000-000000000001"
SECRET = "private-diagnostic-secret"


@pytest.fixture
def data():
    return {
        "snapshot_id": SNAPSHOT_ID,
        "analyzer": "ownership",
        "analyzer_version": "1",
        "parameters": {"kind": "ownership"},
        "created_at": "2026-01-01T00:00:00Z",
        "findings": [
            {
                "snapshot_id": SNAPSHOT_ID,
                "summary": "Recorded relationship.",
                "status": "supported",
                "evidence_ids": ["evidence"],
                "resources": [
                    {
                        "resource_id": "resource",
                        "scope": {"platform": "aws", "account_id": "123456789012"},
                    }
                ],
                "path": [
                    {
                        "kind": "dependency",
                        "source_id": "resource",
                        "target": {"kind": "local", "resource_id": "target"},
                        "relationship": "attached_to",
                        "evidence_ids": ["evidence"],
                    }
                ],
            }
        ],
    }


def test_known_fields_and_error_order_are_preserved(data):
    data["analyzer_version"] = " "
    data["parameters"]["ownership_keys"] = [""]
    data["findings"][0]["resources"][0]["scope"]["account_id"] = SECRET
    data["findings"][0]["path"][0]["target"]["resource_id"] = ""
    with pytest.raises(AnalysisValidationError) as error:
        parse_analysis_result(data)
    assert error.value.details == (
        AnalysisValidationIssue(
            ("analyzer_version",), "string_pattern_mismatch", "Invalid analysis result field."
        ),
        AnalysisValidationIssue(
            ("parameters", "field", "ownership_keys", 0),
            "string_too_short",
            "Invalid analysis result field.",
        ),
        AnalysisValidationIssue(
            ("findings", 0, "resources", 0, "scope", "field", "account_id"),
            "string_pattern_mismatch",
            "Invalid analysis result field.",
        ),
        AnalysisValidationIssue(
            ("findings", 0, "path", 0, "field", "target", "field", "resource_id"),
            "string_too_short",
            "Invalid analysis result field.",
        ),
    )
    assert SECRET not in repr(error.value.details)


@pytest.mark.parametrize("site", ["envelope", "parameters", "scope", "target", "attributes"])
def test_dynamic_keys_and_discriminators_are_hidden(data, site):
    path = data["findings"][0]["path"][0]
    match site:
        case "envelope":
            data[SECRET] = SECRET
        case "parameters":
            data["parameters"]["kind"] = SECRET
        case "scope":
            data["findings"][0]["resources"][0]["scope"]["platform"] = SECRET
        case "target":
            path["target"]["kind"] = SECRET
        case "attributes":
            path["attributes"] = {SECRET: object()}
    with pytest.raises(AnalysisValidationError) as error:
        parse_analysis_result(data)
    assert error.value.code == "invalid_analysis_result"
    assert str(error.value) == "Analysis result validation failed."
    assert SECRET not in repr(error.value.details)
    assert all(issue.message == "Invalid analysis result field." for issue in error.value.details)
    assert [field.name for field in fields(AnalysisValidationIssue)] == [
        "location",
        "type",
        "message",
    ]
    assert error.value.__cause__ is not None


def test_validator_context_and_message_are_hidden(data):
    data["parameters"]["ownership_keys"] = [SECRET, SECRET]
    with pytest.raises(AnalysisValidationError) as error:
        parse_analysis_result(data)
    assert error.value.details == (
        AnalysisValidationIssue(
            ("parameters", "field"), "value_error", "Invalid analysis result field."
        ),
    )
    assert "ownership_keys must be unique" not in repr(error.value.details)


def test_parser_detaches_nested_models_and_revalidates_mutations(data):
    original = AnalysisResult.model_validate(data)
    parsed = parse_analysis_result(original)
    assert parsed == original
    assert parsed is not original
    assert parsed.findings[0] is not original.findings[0]
    assert parsed.findings[0].path[0] is not original.findings[0].path[0]
    assert parsed.parameters.ownership_keys is not original.parameters.ownership_keys
    parsed.parameters.ownership_keys.append("Department")
    assert original.parameters.ownership_keys == ["Owner", "Team"]
    original.findings[0].summary = " "
    with pytest.raises(AnalysisValidationError) as error:
        parse_analysis_result(original)
    assert error.value.details[0].location == ("findings", 0, "summary")


@pytest.mark.parametrize("cyclic", [False, True])
def test_serialization_failures_are_safe(data, cyclic):
    if cyclic:
        data[SECRET] = data
    else:
        data = AnalysisResult.model_validate(data)
        data.parameters.ownership_keys.append(object())
    with pytest.raises(AnalysisValidationError) as error:
        parse_analysis_result(data)
    assert error.value.details == (
        AnalysisValidationIssue((), "serialization_error", "Analysis result is not serializable."),
    )
    assert error.value.__cause__ is not None


def test_reject_preserves_typed_diagnostic_contract():
    with pytest.raises(AnalysisValidationError) as error:
        reject(("findings", 0), "resource_unknown", "Referenced resource must exist.")
    assert error.value.details == (
        AnalysisValidationIssue(
            ("findings", 0), "resource_unknown", "Referenced resource must exist."
        ),
    )
    with pytest.raises(FrozenInstanceError):
        error.value.details[0].message = "changed"


@pytest.mark.parametrize(
    "model,order",
    [
        (SecurityGroupImpactParameters, ["kind", "resource_id", "max_depth", "result_limit"]),
        (InternetExposureParameters, ["kind", "max_depth", "result_limit"]),
    ],
)
def test_shared_parameter_constraints_preserve_schema(model, order):
    schema = model.model_json_schema()
    assert list(schema["properties"]) == order
    assert schema["properties"]["max_depth"] == {
        "default": 3,
        "minimum": 1,
        "maximum": 10,
        "title": "Max Depth",
        "type": "integer",
    }
    assert schema["properties"]["result_limit"] == {
        "default": 100,
        "minimum": 1,
        "maximum": 10000,
        "title": "Result Limit",
        "type": "integer",
    }
