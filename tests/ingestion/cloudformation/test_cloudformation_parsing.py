import json

import pytest

from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.cloudformation.parsing import load_template, parse_template


def test_json_and_yaml_templates(tmp_path):
    template = {"Resources": {"Bucket": {"Type": "AWS::S3::Bucket", "Properties": {}}}}
    path = tmp_path / "template.json"
    path.write_text(json.dumps(template))
    assert load_template(path) == template
    assert parse_template("Resources:\n  Bucket:\n    Type: AWS::S3::Bucket\n") == {
        "Resources": {"Bucket": {"Type": "AWS::S3::Bucket"}}
    }


@pytest.mark.parametrize(
    ("tag", "argument", "expected"),
    [
        ("Ref", "Name", {"Ref": "Name"}),
        ("GetAtt", "Bucket.Arn", {"Fn::GetAtt": ["Bucket", "Arn"]}),
        ("Sub", "'${Name}'", {"Fn::Sub": "${Name}"}),
        ("Join", "['-', [a, b]]", {"Fn::Join": ["-", ["a", "b"]]}),
        ("If", "[C, a, b]", {"Fn::If": ["C", "a", "b"]}),
        ("Condition", "Enabled", {"Condition": "Enabled"}),
    ],
)
def test_explicit_intrinsic_tags(tag, argument, expected):
    result = parse_template(
        "Resources:\n  R:\n    Type: Custom::Thing\n    Properties:\n"
        f"      Value: !{tag} {argument}"
    )
    assert result["Resources"]["R"]["Properties"]["Value"] == expected


@pytest.mark.parametrize(
    "text",
    [
        '{"Resources": {}, "Resources": {}}',
        "Resources: {}\nMetadata: !!python/object/apply:os.system [secret-value]",
        "Resources: {}\nMetadata: &a {self: *a}",
        "Resources: {}\nMetadata: {x: .nan}",
        "Resources: {}\nMetadata: {x: [",
    ],
    ids=["duplicate-key", "python-object", "recursive-alias", "non-finite", "yaml-syntax"],
)
def test_unsafe_or_malformed_templates_have_generic_errors(text):
    with pytest.raises(IngestionError) as exc:
        parse_template(text)
    assert exc.value.code == "invalid_request"
    assert str(exc.value) == "Invalid CloudFormation template."
    assert exc.value.__suppress_context__


def test_limits_and_alias_expansion():
    for text in (
        "Resources: {}\nMetadata: " + "[" * 100 + "0" + "]" * 100,
        "Resources: {}\nMetadata: " + "a" * 1_048_577,
        "Resources: {}\nMetadata: [" + "0," * 60_000 + "]",
        "Resources: {}\nMetadata:\n"
        + "  a: &a [x, x, x, x, x, x, x, x, x, x]\n"
        + "\n".join(
            f"  {chr(98 + i)}: &{chr(98 + i)} [{', '.join(['*' + chr(97 + i)] * 10)}]"
            for i in range(5)
        ),
    ):
        with pytest.raises(IngestionError):
            parse_template(text)


def test_safe_alias_and_transform_are_data_only():
    template = parse_template(
        "Resources: {}\nMetadata: {a: &a [x], b: *a}\nTransform: AWS::Serverless-2016-10-31"
    )
    assert template["Metadata"] == {"a": ["x"], "b": ["x"]}
    assert template["Transform"] == "AWS::Serverless-2016-10-31"


def test_json_numeric_semantics_are_preserved():
    assert parse_template('{"Resources": {}, "Metadata": {"x": 1e3}}')["Metadata"]["x"] == 1000


def test_unquoted_template_version_is_a_string_not_a_python_date():
    assert parse_template("AWSTemplateFormatVersion: 2010-09-09\nResources: {}") == {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Resources": {},
    }


def test_template_version_shape_fails_at_parsing_boundary():
    with pytest.raises(IngestionError, match="Invalid CloudFormation template"):
        parse_template(json.dumps({"Resources": {}, "AWSTemplateFormatVersion": 42}))


BUCKET = {"Type": "AWS::S3::Bucket"}
PARAMETER = {"Type": "String"}


@pytest.mark.parametrize(
    "template",
    [
        {"Resources": []},
        {"Resources": {"Bucket": "AWS::S3::Bucket"}},
        {"Resources": {"Bucket": {**BUCKET, "Properties": []}}},
        {"Resources": {"Bucket": {**BUCKET, "DependsOn": ["Queue", 1]}}},
        {"Resources": {}, "Parameters": {"Name": {"Default": "x"}}},
        {"Resources": {}, "Mappings": {"Regions": {"eu": "x"}}},
        {"Resources": {}, "Outputs": {"Name": {"Description": "no value"}}},
    ],
    ids=lambda template: json.dumps(template)[:60],
)
def test_each_template_shape_rule_is_enforced(template):
    with pytest.raises(IngestionError, match="Invalid CloudFormation template"):
        parse_template(json.dumps(template))


def test_valid_optional_shapes_are_accepted():
    template = {
        "Resources": {"Bucket": {**BUCKET, "DependsOn": "Queue", "Condition": "Prod"}},
        "Parameters": {"Name": {**PARAMETER, "NoEcho": True, "AllowedValues": ["a"]}},
        "Conditions": {"Prod": True},
        "Transform": "AWS::Serverless-2016-10-31",
    }
    assert parse_template(json.dumps(template)) == template


def test_file_errors_are_generic(tmp_path):
    with pytest.raises(IngestionError, match=r"^Invalid CloudFormation template\.$"):
        load_template(tmp_path / "missing-secret-name")
    path = tmp_path / "binary"
    path.write_bytes(b"\xff")
    with pytest.raises(IngestionError):
        load_template(path)
