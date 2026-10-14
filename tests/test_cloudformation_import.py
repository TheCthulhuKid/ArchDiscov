import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from archdiscov.ingestion.adapter import AdapterContext, IngestionError
from archdiscov.ingestion.cloudformation import CloudFormationAdapter, CloudFormationRequest
from archdiscov.models.architecture import Ec2Instance, Subnet, Vpc
from archdiscov.models.enums import AwsRegion, AwsService
from archdiscov.services import (
    collect_snapshots,
    collect_snapshots_async,
    load_snapshot,
    save_snapshot,
)
from archdiscov.services.evidence import get_evidence
from archdiscov.services.snapshots import SnapshotConflictError

FIXTURE = Path(__file__).parent / "fixtures/cloudformation/template.yaml"
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


def test_fixture_end_to_end_and_immutable_persistence(tmp_path):
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
    before = output.read_bytes()
    assert b"fixture-secret-must-not-persist" not in before
    assert b"https://example.invalid" not in before
    loaded = load_snapshot(output)
    assert loaded == snapshot
    evidence_id = vpc.fact_evidence["/cidr_blocks/0"][0]
    inspected = get_evidence(loaded, evidence_id)
    assert inspected.evidence.configuration_path == "/Resources/Network/Properties/CidrBlock"
    with pytest.raises(SnapshotConflictError):
        save_snapshot(snapshot, output)
    assert output.read_bytes() == before


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


@pytest.mark.parametrize(
    "change",
    [
        {"region": "global"},
        {"account_id": "wrong"},
        {"sensitive_paths": ("not-a-pointer",)},
        {"sensitive_paths": ("/bad~2path",)},
        {"stack_id": "arn:aws:cloudformation:us-east-1:123456789012:stack/test/id"},
        {"stack_id": "arn:aws:cloudformation:eu-west-1:999999999999:stack/test/id"},
    ],
)
def test_request_rejects_invalid_context(change):
    data = dict(path=FIXTURE, account_id=ACCOUNT, region="eu-west-1") | change
    with pytest.raises(ValidationError):
        CloudFormationRequest(**data)


def test_stack_context_keeps_logical_identity_and_no_physical_ids():
    snapshot = collect_snapshots(request(stack_name="demo"))[0]
    assert "demo/Network" in resources(snapshot)
    assert snapshot.source.stack_name == "demo"


@pytest.mark.parametrize("parameters", [{"Undeclared": "secret"}, {"Stage": ["secret"]}])
def test_invalid_parameters_fail_safely(parameters):
    with pytest.raises(IngestionError) as error:
        collect_snapshots(request(parameters=parameters))
    assert "secret" not in str(error.value)


def test_wrong_adapter_request_rejected():
    with pytest.raises(IngestionError, match="CloudFormationRequest"):
        CloudFormationAdapter().collect(object(), context=AdapterContext())


def test_empty_template_is_successful_zero(tmp_path):
    snapshot = collect_snapshots(request(write(tmp_path, {"Resources": {}})))[0]
    assert snapshot.coverage.operations[0].result_count == 0
    assert snapshot.coverage.operations[0].status == "success"


def test_yaml_malicious_input_has_safe_service_error(tmp_path):
    path = tmp_path / "malicious.yaml"
    path.write_text('!!python/object/apply:os.system ["secret-command"]')
    with pytest.raises(IngestionError) as error:
        collect_snapshots(request(path))
    assert "secret-command" not in str(error.value)


def test_no_value_list_compaction_cites_original_index(tmp_path):
    path = write(
        tmp_path,
        {
            "Resources": {
                "V": {
                    "Type": "AWS::EC2::VPC",
                    "Properties": {
                        "Tags": [{"Ref": "AWS::NoValue"}, {"Key": "Name", "Value": "actual"}]
                    },
                }
            }
        },
    )
    snapshot = collect_snapshots(request(path))[0]
    resource = resources(snapshot)["V"]
    citation = snapshot.evidence[resource.fact_evidence["/name"][0]]
    assert citation.configuration_path == "/Resources/V/Properties/Tags/1/Value"
    assert citation.value == "actual"


def test_intrinsic_generated_collection_cites_source_expression(tmp_path):
    path = write(
        tmp_path,
        {
            "Conditions": {"Yes": True},
            "Resources": {
                "V": {
                    "Type": "AWS::EC2::VPC",
                    "Properties": {
                        "Tags": {"Fn::If": ["Yes", [{"Key": "Name", "Value": "actual"}], []]}
                    },
                }
            },
        },
    )
    snapshot = collect_snapshots(request(path))[0]
    resource = resources(snapshot)["V"]
    citation = snapshot.evidence[resource.fact_evidence["/name"][0]]
    assert citation.configuration_path == "/Resources/V/Properties/Tags"
    assert citation.value == "actual"


def test_redaction_after_no_value_still_cites_original_index(tmp_path):
    path = write(
        tmp_path,
        {
            "Resources": {
                "V": {
                    "Type": "AWS::EC2::VPC",
                    "Properties": {
                        "Tags": [{"Ref": "AWS::NoValue"}, {"Key": "password", "Value": "hidden"}]
                    },
                }
            }
        },
    )
    snapshot = collect_snapshots(request(path))[0]
    citations = [e for e in snapshot.evidence.values() if e.value_status == "redacted"]
    assert citations
    assert {e.configuration_path for e in citations} == {"/Resources/V/Properties/Tags/1/Value"}
    assert "hidden" not in snapshot.model_dump_json()
