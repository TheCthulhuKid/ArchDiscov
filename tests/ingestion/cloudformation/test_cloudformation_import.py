import asyncio
import json

import pytest
from pydantic import ValidationError

from archdiscov.ingestion.adapter import IngestionError
from archdiscov.ingestion.cloudformation import CloudFormationRequest
from archdiscov.models.architecture import Ec2Instance, Subnet, Vpc
from archdiscov.models.enums import AwsRegion, AwsService
from archdiscov.services import (
    collect_snapshots,
    collect_snapshots_async,
    load_snapshot,
    save_snapshot,
)
from archdiscov.services.evidence import get_evidence
from tests.paths import FIXTURES_ROOT

FIXTURE = FIXTURES_ROOT / "cloudformation/template.yaml"
ACCOUNT = "123456789012"


def request(path=FIXTURE, **kwargs):
    return CloudFormationRequest(path=path, account_id=ACCOUNT, region="eu-west-1", **kwargs)


def write(tmp_path, template):
    path = tmp_path / "template.json"
    path.write_text(json.dumps(template), encoding="utf-8")
    return path


def resources(snapshot):
    def walk(items):
        for item in items:
            yield item
            yield from walk(item.children)

    return {
        r.identity.logical_address: r
        for r in walk(
            [
                *snapshot.global_resources,
                *(r for region in snapshot.regions.values() for r in region.resources),
            ]
        )
    }


def test_fixture_end_to_end_and_persistence(tmp_path):
    snapshot = collect_snapshots(request())[0]
    stored = resources(snapshot)
    assert set(stored) == {"Network", "Subnet", "Server", "Nested", "Imported"}
    vpc = stored["Network"]
    assert isinstance(vpc, Vpc) and [str(cidr) for cidr in vpc.cidr_blocks] == ["10.20.0.0/16"]
    assert vpc.name == "dev-network"
    subnet = stored["Subnet"]
    assert isinstance(subnet, Subnet) and subnet.availability_zone == "eu-west-1a"
    assert subnet in vpc.children
    assert isinstance(stored["Server"], Ec2Instance)
    assert stored["Server"] in subnet.children
    assert all(r.identity.physical_id is None for r in stored.values())
    assert stored["Nested"].aws_resource_type == "AWS::CloudFormation::Stack"
    assert any(
        d.target.kind == "local" for ds in snapshot.dependencies_by_region.values() for d in ds
    )
    assert any(op.status == "incomplete" for op in snapshot.coverage.operations)
    assert any(e.value_status == "redacted" for e in snapshot.evidence.values())
    assert any(
        "Import" in u.reason or "import" in u.reason
        for op in snapshot.coverage.operations
        for u in op.unresolved_values
    )
    output = save_snapshot(snapshot, tmp_path / "snapshot.json")
    saved = output.read_bytes()
    assert b"fixture-secret-must-not-persist" not in saved
    assert b"https://example.invalid" not in saved
    loaded = load_snapshot(output)
    assert loaded == snapshot
    evidence_id = vpc.fact_evidence["/cidr_blocks/0"][0]
    inspected = get_evidence(loaded, evidence_id)
    assert inspected.evidence.configuration_path == "/Resources/Network/Properties/CidrBlock"


def test_json_yaml_equivalence_with_async_service(tmp_path):
    from archdiscov.ingestion.cloudformation.parsing import load_template

    path = write(tmp_path, load_template(FIXTURE))
    left = collect_snapshots(request())[0]
    right = asyncio.run(collect_snapshots_async(request(path)))[0]
    assert [
        r.model_dump(exclude={"evidence_ids", "fact_evidence", "children"})
        for r in resources(left).values()
    ] == [
        r.model_dump(exclude={"evidence_ids", "fact_evidence", "children"})
        for r in resources(right).values()
    ]
    assert left.dependencies_by_region == right.dependencies_by_region


def test_parameters_noecho_sensitive_paths_and_missing_values(tmp_path):
    path = write(
        tmp_path,
        {
            "Parameters": {
                "Opaque": {"Type": "String", "NoEcho": True},
                "Missing": {"Type": "String"},
            },
            "Resources": {
                "V": {
                    "Type": "AWS::EC2::VPC",
                    "Properties": {
                        "CidrBlock": "10.0.0.0/16",
                        "Tags": [
                            {"Key": "Name", "Value": {"Fn::Sub": "name-${Opaque}"}},
                            {"Key": "other", "Value": {"Ref": "Missing"}},
                            {"Key": "private", "Value": "sensitive-path-secret"},
                            {"Key": "password", "Value": "tag-secret"},
                        ],
                    },
                }
            },
        },
    )
    snapshot = collect_snapshots(
        request(
            path,
            parameters={"Opaque": "noecho-secret"},
            sensitive_paths=("/Resources/V/Properties/Tags/2/Value",),
        )
    )[0]
    text = snapshot.model_dump_json()
    for secret in ("noecho-secret", "sensitive-path-secret", "tag-secret"):
        assert secret not in text
    assert resources(snapshot)["V"].name is None
    assert snapshot.coverage.operations[0].status == "incomplete"


def test_conditions_local_getatt_and_conditional_missing_target(tmp_path):
    path = write(
        tmp_path,
        {
            "Conditions": {"Off": {"Fn::Equals": ["yes", "no"]}},
            "Resources": {
                "Omitted": {"Type": "AWS::EC2::VPC", "Condition": "Off"},
                "Uncertain": {"Type": "AWS::EC2::VPC", "Condition": "Missing"},
                "Role": {"Type": "AWS::IAM::Role"},
                "Function": {
                    "Type": "AWS::Lambda::Function",
                    "Properties": {
                        "Role": {"Fn::GetAtt": ["Role", "Arn"]},
                        "VpcId": {"Ref": "Omitted"},
                    },
                },
            },
        },
    )
    snapshot = collect_snapshots(request(path))[0]
    stored = resources(snapshot)
    assert set(stored) == {"Role", "Function"}
    assert stored["Role"].identity.region == AwsRegion.GLOBAL
    assert stored["Role"].identity.service == AwsService.IAM
    links = snapshot.dependencies_by_region[AwsRegion.EU_WEST_1]
    assert any(
        d.target.kind == "local" and d.target.resource_id == stored["Role"].id for d in links
    )
    assert any(d.target.kind == "unresolved" for d in links)
    assert any(
        u.configuration_path == "/Resources/Uncertain/Condition"
        for op in snapshot.coverage.operations
        for u in op.unresolved_values
    )


def test_nested_stack_and_transform_never_fetch_or_deploy(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr("boto3.Session", forbidden)
    path = write(
        tmp_path,
        {
            "Transform": "AWS::Serverless-2016-10-31",
            "Resources": {
                "Nested": {
                    "Type": "AWS::CloudFormation::Stack",
                    "Properties": {
                        "TemplateURL": "https://invalid.example/secret",
                        "Parameters": {"x": "protected"},
                    },
                }
            },
        },
    )
    snapshot = collect_snapshots(request(path))[0]
    text = snapshot.model_dump_json()
    assert "invalid.example" not in text and '"protected"' not in text
    assert any(
        u.configuration_path == "/Transform"
        for op in snapshot.coverage.operations
        for u in op.unresolved_values
    )


def test_request_rejects_a_stack_from_another_region():
    with pytest.raises(ValidationError, match="stack_id must be a stack ARN"):
        request(stack_id="arn:aws:cloudformation:us-east-1:123456789012:stack/test/id")


def test_stack_context_keeps_logical_identity_and_no_physical_ids():
    snapshot = collect_snapshots(request(stack_name="demo"))[0]
    assert "demo/Network" in resources(snapshot)
    assert snapshot.source.stack_name == "demo"


def test_undeclared_parameter_fails_safely():
    with pytest.raises(IngestionError, match="not declared") as error:
        collect_snapshots(request(parameters={"Undeclared": "secret"}))
    assert "secret" not in str(error.value)


def test_empty_template_is_successful_zero(tmp_path):
    snapshot = collect_snapshots(request(write(tmp_path, {"Resources": {}})))[0]
    assert snapshot.coverage.operations[0].result_count == 0
    assert snapshot.coverage.operations[0].status == "success"


def test_no_value_list_compaction_cites_and_redacts_original_indexes(tmp_path):
    tags = [
        {"Ref": "AWS::NoValue"},
        {"Key": "Name", "Value": "actual"},
        {"Key": "password", "Value": "hidden"},
    ]
    path = write(
        tmp_path, {"Resources": {"V": {"Type": "AWS::EC2::VPC", "Properties": {"Tags": tags}}}}
    )
    snapshot = collect_snapshots(request(path))[0]
    resource = resources(snapshot)["V"]
    citation = snapshot.evidence[resource.fact_evidence["/name"][0]]
    assert citation.configuration_path == "/Resources/V/Properties/Tags/1/Value"
    assert citation.value == "actual"
    redacted = [e for e in snapshot.evidence.values() if e.value_status == "redacted"]
    assert {e.configuration_path for e in redacted} == {"/Resources/V/Properties/Tags/2/Value"}
    assert "hidden" not in snapshot.model_dump_json()
