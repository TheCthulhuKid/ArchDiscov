import pytest
from pydantic import ValidationError

from archdiscov.models.architecture import CollectionFailure, Coverage, UnresolvedValue
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
@pytest.mark.parametrize(
    "outcome",
    [
        {},
        {"status": "incomplete", "reason": "limited"},
        {"status": "incomplete", "failures": [{"code": "Denied", "message": "denied"}]},
        {
            "status": "incomplete",
            "unresolved_values": [{"configuration_path": "/value", "reason": "unknown"}],
        },
        {
            "status": "failed",
            "result_count": None,
            "failures": [{"code": "Denied", "message": "denied"}],
        },
        {"status": "skipped", "result_count": None, "reason": "not requested"},
        {"status": "unsupported", "result_count": None, "reason": "not supported"},
    ],
)
def test_shared_valid_outcomes_and_wire_field_order(model, outcome):
    record = model.model_validate(data(model, **outcome))
    context_fields = (
        ["account_id", "region", "service"]
        if model is Coverage
        else ["scope", "location", "service"]
    )
    assert list(record.model_dump(mode="json")) == context_fields + OUTCOME_FIELDS
    assert model.model_validate_json(record.model_dump_json()) == record


@pytest.mark.parametrize("model", [Coverage, CloudOperationCoverage])
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
            {"failures": [{"code": "Denied", "message": "denied"}]},
            "successful coverage requires a count and no failures/unresolved values",
        ),
        (
            {"status": "incomplete"},
            "incomplete coverage requires a retained count and a limitation",
        ),
        (
            {"status": "incomplete", "result_count": None, "reason": "limited"},
            "incomplete coverage requires a retained count and a limitation",
        ),
        ({"status": "failed"}, "only successful or incomplete coverage can have a result_count"),
        ({"status": "failed", "result_count": None}, "failed coverage requires a failure"),
        (
            {"status": "skipped", "result_count": None},
            "skipped/unsupported coverage requires a reason and no collected data",
        ),
        (
            {
                "status": "unsupported",
                "result_count": None,
                "reason": "unsupported",
                "unresolved_values": [{"configuration_path": "/value", "reason": "unknown"}],
            },
            "skipped/unsupported coverage requires a reason and no collected data",
        ),
    ],
)
def test_shared_invalid_outcomes_preserve_messages_and_precedence(model, outcome, message):
    with pytest.raises(ValidationError) as caught:
        model.model_validate(data(model, **outcome))
    assert [error["msg"] for error in caught.value.errors()] == [f"Value error, {message}"]


@pytest.mark.parametrize("model", [Coverage, CloudOperationCoverage])
@pytest.mark.parametrize("count", [-1, True, "0", 1.5])
def test_result_counts_remain_strict_nonnegative_integers(model, count):
    with pytest.raises(ValidationError) as caught:
        model.model_validate(data(model, result_count=count))
    assert [error["loc"] for error in caught.value.errors()] == [("result_count",)]


@pytest.mark.parametrize("model", [Coverage, CloudOperationCoverage])
def test_outcome_defaults_are_independent_and_timestamps_are_aware(model):
    first = model.model_validate(data(model))
    second = model.model_validate(data(model))
    first.failures.append(CollectionFailure(code="Denied", message="denied"))
    assert second.failures == []
    assert second.unresolved_values == []
    assert second.reason is None
    with pytest.raises(ValidationError) as caught:
        model.model_validate(data(model, started_at="2026-01-01T00:00:00"))
    assert caught.value.errors()[0]["type"] == "timezone_aware"


@pytest.mark.parametrize("model", [Coverage, CloudOperationCoverage])
def test_nested_outcome_types_and_extra_field_policy_are_preserved(model):
    record = model.model_validate(
        data(
            model,
            status="incomplete",
            failures=[{"code": "Denied", "message": "denied"}],
            unresolved_values=[{"configuration_path": "/value", "reason": "unknown"}],
        )
    )
    assert isinstance(record.failures[0], CollectionFailure)
    assert isinstance(record.unresolved_values[0], UnresolvedValue)
    with pytest.raises(ValidationError) as caught:
        model.model_validate(data(model, unexpected=True))
    assert caught.value.errors()[0]["type"] == "extra_forbidden"
