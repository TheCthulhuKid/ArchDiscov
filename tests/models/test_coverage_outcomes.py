import pytest
from pydantic import ValidationError

from archdiscov.models.architecture import Coverage
from archdiscov.models.cloud.contracts import CloudOperationCoverage

OUTCOME_FIELDS = [
    "operation",
    "status",
    "started_at",
    "completed_at",
    "result_count",
    "failures",
    "unresolved_values",
    "reason",
]


def data(model, **overrides):
    context = (
        {"account_id": "123456789012", "region": "us-east-1", "service": "ec2"}
        if model is Coverage
        else {
            "scope": {"platform": "aws", "account_id": "123456789012"},
            "location": "us-east-1",
            "service": "ec2",
        }
    )
    return {
        **context,
        "operation": "List",
        "status": "success",
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:00:00Z",
        "result_count": 0,
        **overrides,
    }


@pytest.mark.parametrize("model", [Coverage, CloudOperationCoverage])
def test_shared_valid_outcome_and_wire_field_order(model):
    failed = {
        "status": "failed",
        "result_count": None,
        "failures": [{"code": "Denied", "message": "denied"}],
    }
    record = model.model_validate(data(model, **failed))
    context_fields = (
        ["account_id", "region", "service"]
        if model is Coverage
        else ["scope", "location", "service"]
    )
    assert list(record.model_dump(mode="json")) == context_fields + OUTCOME_FIELDS
    assert model.model_validate_json(record.model_dump_json()) == record


@pytest.mark.parametrize(
    "outcome,message",
    [
        (
            {"completed_at": "2025-01-01T00:00:00Z", "result_count": None},
            "coverage completed_at must not precede started_at",
        ),
        (
            {"result_count": None},
            "successful coverage requires a count and no failures/unresolved values",
        ),
        (
            {"status": "incomplete"},
            "incomplete coverage requires a retained count and a limitation",
        ),
        ({"status": "failed", "result_count": None}, "failed coverage requires a failure"),
        (
            {"status": "skipped", "result_count": None},
            "skipped/unsupported coverage requires a reason and no collected data",
        ),
    ],
    ids=["window", "success", "incomplete", "failed", "skipped"],
)
def test_shared_invalid_outcomes_preserve_messages_and_precedence(outcome, message):
    # Both models inherit the one validator; the valid-outcome test proves the sharing.
    model = CloudOperationCoverage
    with pytest.raises(ValidationError) as caught:
        model.model_validate(data(model, **outcome))
    assert [error["msg"] for error in caught.value.errors()] == [f"Value error, {message}"]
