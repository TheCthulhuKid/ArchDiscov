import pytest
from pydantic import ValidationError

from archdiscov.models.resources.functions import LambdaFunction, safe_configuration
from tests.support.lambda_data import ARN, SECRET, configuration


def function(**fields):
    return LambdaFunction(
        identity={
            "account_id": "123456789012",
            "region": "us-east-1",
            "service": "lambda",
            "physical_id": ARN,
        },
        arn=ARN,
        **fields,
    )


def test_optional_unknown_fields_are_not_empty_configuration():
    item = function()
    assert item.configuration is None
    assert item.subnet_ids is None and item.security_group_ids is None
    assert function(subnet_ids=[], security_group_ids=[]).subnet_ids == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("memory_size", True),
        ("memory_size", 10241),
        ("runtime", "future"),
        ("role_arn", "https://example.invalid/secret"),
    ],
    ids=["coercible", "out-of-range", "unknown-choice", "pattern"],
)
def test_direct_typed_facts_reject_unknown_malformed_and_coercible_values(field, value):
    with pytest.raises(ValidationError):
        function(**{field: value})


def test_direct_configuration_projection_never_copies_ordinary_named_environment_values():
    source = {
        **configuration(),
        "Environment": {"Variables": {"APP_MODE": SECRET}},
        "Description": SECRET,
        "Code": {"Location": SECRET},
        "Future": SECRET,
    }
    item = function(configuration=source)
    assert SECRET not in item.model_dump_json()
    assert item.configuration == configuration()
    safe, paths = safe_configuration(source)
    assert safe == configuration()
    assert "/Environment" in paths
    assert source["Environment"]["Variables"]["APP_MODE"] == SECRET


def test_arn_identity_must_match():
    item = function()
    data = item.model_dump()
    data["arn"] = ARN + "2"
    with pytest.raises(ValidationError):
        LambdaFunction.model_validate(data)
