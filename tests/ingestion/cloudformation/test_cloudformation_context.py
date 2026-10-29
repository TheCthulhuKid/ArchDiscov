import pytest

from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.cloudformation.context import (
    parameters_for,
    prepare_template,
    pseudo_parameters,
    safe_properties,
)
from archdiscov.ingestion.cloudformation.requests import CloudFormationRequest


def request(**kwargs):
    return CloudFormationRequest(
        path="template.yaml", account_id="123456789012", region="eu-west-1", **kwargs
    )


def test_parameter_types():
    definitions = {
        "Text": ("String", "literal"),
        "Integer": ("Number", "3"),
        "Float": ("Number", "1.5"),
        "Numbers": ("List<Number>", "1, 2"),
        "Subnets": ("List<AWS::EC2::Subnet::Id>", ["subnet-1"]),
    }
    template = {
        "Parameters": {
            name: {"Type": kind, "Default": value} for name, (kind, value) in definitions.items()
        }
    }
    assert parameters_for(template, request()) == {
        "Text": "literal",
        "Integer": 3,
        "Float": 1.5,
        "Numbers": [1, 2],
        "Subnets": ["subnet-1"],
    }


def test_invalid_parameter_type_does_not_leak_value():
    with pytest.raises(IngestionError, match="does not match its type") as error:
        parameters_for({"Parameters": {"P": {"Type": "Number", "Default": "secret"}}}, request())
    assert "secret" not in str(error.value)


def test_sensitive_default_path_stays_protected_when_overridden():
    req = request(
        parameters={"Opaque": "override-secret"}, sensitive_paths=("/Parameters/Opaque/Default",)
    )
    source = {"Parameters": {"Opaque": {"Type": "String", "Default": "default-secret"}}}
    safe = prepare_template(source, req)
    assert parameters_for(safe, req) == {"Opaque": "[REDACTED]"}
    assert source["Parameters"]["Opaque"]["Default"] == "default-secret"


def test_noecho_and_generic_parameter_names():
    source = {
        "Parameters": {
            "Opaque": {"Type": "String", "NoEcho": True, "Default": "secret"},
            "Password": {"Type": "String", "Default": "secret"},
        }
    }
    assert parameters_for(source, request()) == {"Opaque": "[REDACTED]", "Password": "[REDACTED]"}


def test_missing_and_remote_parameters_remain_unavailable():
    source = {
        "Parameters": {
            "Missing": {"Type": "String"},
            "Remote": {"Type": "AWS::SSM::Parameter::Value<String>", "Default": "parameter-path"},
        }
    }
    assert parameters_for(source, request()) == {}


def test_safe_tag_projection():
    safe, paths = safe_properties({"Tags": [{"Key": "password", "Value": "secret"}]})
    assert safe["Tags"][0]["Value"] == "[REDACTED]"
    assert paths == ["/Tags/0/Value"]


def test_sensitive_paths_permitted_cases():
    paths = (
        "/Resources/R/Properties/Values/0",
        "/Parameters/P/Default",
        "/Mappings/M/Region",
        "/Conditions/C",
    )
    source = {
        "Resources": {"R": {"Properties": {"Values": ["secret"]}}},
        "Parameters": {"P": {"Default": {"Values": ["secret"]}}},
        "Mappings": {"M": {"Region": {"Value": "secret"}}},
        "Conditions": {"C": {"Fn::Equals": ["secret", "other"]}},
    }
    safe = prepare_template(source, request(sensitive_paths=paths))
    assert safe["Resources"]["R"]["Properties"]["Values"] == ["[REDACTED]"]
    assert safe["Parameters"]["P"]["Default"] == "[REDACTED]"
    assert safe["Mappings"]["M"]["Region"] == "[REDACTED]"
    assert safe["Conditions"]["C"] == "[REDACTED]"
    assert "[REDACTED]" not in str(source)


def test_sensitive_paths_reject_ineligible_locations_without_disclosing_them():
    source = {"Resources": {}, "Outputs": {"O": {"Value": "secret"}}}
    with pytest.raises(IngestionError, match="does not identify a value") as error:
        prepare_template(source, request(sensitive_paths=("/Outputs/O/Value",)))
    assert "/Outputs/O/Value" not in str(error.value)
    assert "[REDACTED]" not in str(source)


def test_pseudo_parameters_use_supplied_stack_and_partition():
    assert pseudo_parameters(request(stack_name="demo"))["AWS::StackName"] == "demo"
    china = CloudFormationRequest(path="x", account_id="123456789012", region="cn-north-1")
    assert pseudo_parameters(china)["AWS::Partition"] == "aws-cn"
    assert pseudo_parameters(china)["AWS::URLSuffix"] == "amazonaws.com.cn"
