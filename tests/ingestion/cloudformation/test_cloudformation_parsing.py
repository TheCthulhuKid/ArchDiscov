import json

import pytest

from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.cloudformation.evaluation_limits import MAX_BYTES, MAX_DEPTH
from archdiscov.ingestion.cloudformation.parsing import load_template, parse_template


def test_json_and_yaml_templates(tmp_path):
    template = {"Resources": {"Bucket": {"Type": "AWS::S3::Bucket", "Properties": {}}}}
    path = tmp_path / "template.json"
    path.write_text(json.dumps(template))
    assert load_template(path) == template
    assert parse_template("Resources:\n  Bucket:\n    Type: AWS::S3::Bucket\n") == {
        "Resources": {"Bucket": {"Type": "AWS::S3::Bucket"}}
    }


def test_explicit_intrinsic_tags():
    result = parse_template(
        "Resources:\n  R:\n    Type: Custom::Thing\n    Properties:\n"
        "      Ref: !Ref Name\n"
        "      GetAtt: !GetAtt Bucket.Arn\n"
        "      Sub: !Sub '${Name}'\n"
        "      Join: !Join ['-', [a, b]]\n"
        "      Condition: !Condition Enabled\n"
    )
    assert result["Resources"]["R"]["Properties"] == {
        "Ref": {"Ref": "Name"},
        "GetAtt": {"Fn::GetAtt": ["Bucket", "Arn"]},
        "Sub": {"Fn::Sub": "${Name}"},
        "Join": {"Fn::Join": ["-", ["a", "b"]]},
        "Condition": {"Condition": "Enabled"},
    }


@pytest.mark.parametrize(
    "text",
    [
        '{"Resources": {}, "Resources": {}}',
        "Resources: {}\nMetadata: !!python/object/apply:os.system [secret-value]",
    ],
    ids=["duplicate-key", "python-object"],
)
def test_unsafe_or_malformed_templates_have_generic_errors(text):
    with pytest.raises(IngestionError) as exc:
        parse_template(text)
    assert exc.value.code == "invalid_request"
    assert str(exc.value) == "Invalid CloudFormation template."
    assert exc.value.__suppress_context__


def test_limits_and_alias_expansion():
    """The smallest input over each limit; the root mapping is the first nesting level, and
    the aliases expand a tiny text past the node limit."""
    prefix = "Resources: {}\nMetadata: "
    for text in (
        prefix + "[" * MAX_DEPTH + "0" + "]" * MAX_DEPTH,
        prefix + "a" * (MAX_BYTES + 1 - len(prefix)),
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


def test_unquoted_template_version_is_a_string_not_a_python_date():
    assert parse_template("AWSTemplateFormatVersion: 2010-09-09\nResources: {}") == {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Resources": {},
    }


def test_template_shapes_accept_optional_fields_and_reject_wrong_types():
    template = {
        "Resources": {
            "Bucket": {"Type": "AWS::S3::Bucket", "DependsOn": "Queue", "Condition": "Prod"}
        },
        "Parameters": {"Name": {"Type": "String", "NoEcho": True, "AllowedValues": ["a"]}},
        "Conditions": {"Prod": True},
        "Transform": "AWS::Serverless-2016-10-31",
    }
    assert parse_template(json.dumps(template)) == template
    with pytest.raises(IngestionError, match="Invalid CloudFormation template"):
        parse_template(json.dumps({"Resources": {"Bucket": "AWS::S3::Bucket"}}))


def test_file_errors_are_generic(tmp_path):
    with pytest.raises(IngestionError, match=r"^Invalid CloudFormation template\.$"):
        load_template(tmp_path / "missing-secret-name")
