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


@pytest.mark.parametrize(
    "kind,value,expected",
    [
        ("String", "literal", "literal"),
        ("Number", "3", 3),
        ("Number", "1.5", 1.5),
        ("List<Number>", "1, 2", [1, 2]),
        ("List<AWS::EC2::Subnet::Id>", ["subnet-1"], ["subnet-1"]),
    ],
    ids=["string", "integer", "float", "split-numbers", "list-value"],
)
def test_parameter_types(kind, value, expected):
    assert parameters_for({"Parameters": {"P": {"Type": kind, "Default": value}}}, request()) == {
        "P": expected
    }


@pytest.mark.parametrize(
    "kind,value",
    [
        ("String", []),
        ("Number", True),
        ("Number", "nan"),
        ("CommaDelimitedList", {}),
        ("List<Number>", ["bad"]),
    ],
    ids=["not-string", "boolean-number", "non-finite", "not-list", "not-number"],
)
def test_invalid_parameter_types_do_not_leak_values(kind, value):
    with pytest.raises(IngestionError, match="does not match its type"):
        parameters_for({"Parameters": {"P": {"Type": kind, "Default": value}}}, request())


def test_sensitive_default_path_stays_protected_when_overridden():
    req = request(
        parameters={"Opaque": "override-secret"}, sensitive_paths=("/Parameters/Opaque/Default",)
    )
    source = {"Parameters": {"Opaque": {"Type": "String", "Default": "default-secret"}}}
    safe = prepare_template(source, req)
    assert parameters_for(safe, req) == {"Opaque": "[REDACTED]"}
    assert source["Parameters"]["Opaque"]["Default"] == "default-secret"


@pytest.mark.parametrize("name,noecho", [("Opaque", True), ("Password", False)])
def test_noecho_and_generic_parameter_names(name, noecho):
    source = {"Parameters": {name: {"Type": "String", "NoEcho": noecho, "Default": "secret"}}}
    assert parameters_for(source, request())[name] == "[REDACTED]"


def test_missing_and_remote_parameters_remain_unavailable():
    source = {
        "Parameters": {
            "Missing": {"Type": "String"},
            "Remote": {"Type": "AWS::SSM::Parameter::Value<String>", "Default": "parameter-path"},
        }
    }
    assert parameters_for(source, request()) == {}


def test_sensitive_paths_escape_and_safe_tag_projection():
    source = {"Resources": {"R": {"Properties": {"a/b": {"~x": "secret"}}}}}
    req = request(sensitive_paths=("/Resources/R/Properties/a~1b/~0x",))
    assert (
        prepare_template(source, req)["Resources"]["R"]["Properties"]["a/b"]["~x"] == "[REDACTED]"
    )
    safe, paths = safe_properties({"Tags": [{"Key": "password", "Value": "secret"}]})
    assert safe["Tags"][0]["Value"] == "[REDACTED]"
    assert paths == ["/Tags/0/Value"]


def test_missing_sensitive_path_fails_without_disclosing_it():
    with pytest.raises(IngestionError, match="does not identify a value") as error:
        prepare_template({}, request(sensitive_paths=("/secret",)))
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "path",
    [
        "/Resources/R/Properties/Values/0",
        "/Parameters/P/Default",
        "/Mappings/M/Region",
        "/Conditions/C",
    ],
    ids=["resource-properties", "parameter-default", "mapping", "condition"],
)
def test_sensitive_paths_permitted_cases(path):
    source = {
        "Resources": {"R": {"Properties": {"Values": ["secret"]}}},
        "Parameters": {"P": {"Default": {"Values": ["secret"]}}},
        "Mappings": {"M": {"Region": {"Value": "secret"}}},
        "Conditions": {"C": {"Fn::Equals": ["secret", "other"]}},
    }
    safe = prepare_template(source, request(sensitive_paths=(path,)))
    node = safe
    for part in path.split("/")[1:]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    assert node == "[REDACTED]"
    assert "[REDACTED]" not in str(source)


@pytest.mark.parametrize(
    "path",
    [
        "/Resources/R",
        "/Outputs/O/Value",
        "/Resources/R/Properties/Values/01",
    ],
    ids=["too-short", "ineligible-section", "non-canonical-index"],
)
def test_sensitive_paths_reject_ineligible_or_invalid_locations(path):
    source = {
        "Resources": {"R": {"Type": "AWS::EC2::VPC", "Properties": {"Values": ["secret"]}}},
        "Parameters": {"P": {"Type": "String"}},
        "Mappings": {"M": {}},
        "Conditions": {"C": True},
        "Outputs": {"O": {"Value": "secret"}},
    }
    with pytest.raises(IngestionError, match="does not identify a value") as error:
        prepare_template(source, request(sensitive_paths=(path,)))
    assert path not in str(error.value)
    assert "[REDACTED]" not in str(source)


def test_pseudo_parameters_use_supplied_stack_and_partition():
    assert pseudo_parameters(request(stack_name="demo"))["AWS::StackName"] == "demo"
    china = CloudFormationRequest(path="x", account_id="123456789012", region="cn-north-1")
    assert pseudo_parameters(china)["AWS::Partition"] == "aws-cn"
    assert pseudo_parameters(china)["AWS::URLSuffix"] == "amazonaws.com.cn"
