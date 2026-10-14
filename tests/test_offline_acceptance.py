"""Offline installed-CLI journeys, also runnable with Docker networking disabled."""

import json
import os
import subprocess
import sys
from ipaddress import ip_network
from pathlib import Path

import pytest

from archdiscov.models.architecture import Ec2Instance, Subnet, UnknownResource, Vpc
from archdiscov.services import load_snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "snapshots"


@pytest.mark.parametrize(
    "fixture_name,evidence_id",
    [("hierarchy", "instance"), ("partial-coverage", "sg-rule")],
    ids=["hierarchy", "partial-coverage"],
)
def test_offline_acceptance(tmp_path: Path, fixture_name: str, evidence_id: str) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "PYTHONPATH"
        and not key.startswith(
            ("AWS_", "AZURE_", "GOOGLE_", "GCLOUD_", "OPENAI_", "ANTHROPIC_", "BOTO_")
        )
    }
    env.update(
        AWS_EC2_METADATA_DISABLED="true",
        AWS_SHARED_CREDENTIALS_FILE=str(tmp_path / "no-aws-credentials"),
        AWS_CONFIG_FILE=str(tmp_path / "no-aws-config"),
        BOTO_CONFIG=str(tmp_path / "no-boto-config"),
    )
    executable = Path(sys.executable).with_name("archdiscov")

    def run(*arguments: str, exit_code: int = 0) -> dict:
        result = subprocess.run(
            [str(executable), *arguments, "--json"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == exit_code, (result.stdout, result.stderr)
        assert result.stderr == ""
        assert "Traceback" not in result.stdout
        return json.loads(result.stdout)

    fixture = FIXTURES / f"{fixture_name}.json"
    source = tmp_path / "source.json"
    source.write_bytes(fixture.read_bytes())
    source_bytes = source.read_bytes()
    output = tmp_path / "snapshot.json"
    imported = run("ingest", str(source), "--output", str(output))
    saved = load_snapshot(output)
    assert imported["snapshot_id"] == str(saved.snapshot_id)
    assert imported["path"] == str(output)
    assert imported["source_kind"] == saved.source.kind
    assert imported["complete"] == all(op.status == "success" for op in saved.coverage.operations)
    assert saved == load_snapshot(fixture)
    saved_bytes = output.read_bytes()

    inspected = run("evidence", evidence_id, "--snapshot", str(output))
    assert inspected["snapshot_id"] == str(saved.snapshot_id)
    assert inspected["snapshot_path"] == str(output)
    assert inspected["snapshot_source"] == saved.source.model_dump(mode="json")
    assert inspected["evidence_id"] == evidence_id
    assert inspected["resource"]["id"] == inspected["evidence"]["resource_id"]
    assert inspected["evidence"] == saved.evidence[evidence_id].model_dump(mode="json")

    if fixture_name == "hierarchy":
        vpc = saved.regions["eu-west-1"].resources[0]
        assert isinstance(vpc, Vpc)
        assert vpc.resource_type == "AWS::EC2::VPC"
        assert vpc.identity.physical_id == "vpc-main"
        assert vpc.cidr_blocks == [ip_network("10.0.0.0/16")]
        subnet, unknown = vpc.children
        assert isinstance(subnet, Subnet)
        assert subnet.resource_type == "AWS::EC2::Subnet"
        assert subnet.identity.physical_id == "subnet-app"
        assert subnet.availability_zone == "eu-west-1a"
        assert subnet.cidr_blocks == [ip_network("10.0.1.0/24")]
        (instance,) = subnet.children
        assert isinstance(instance, Ec2Instance)
        assert instance.resource_type == "AWS::EC2::Instance"
        assert instance.identity.physical_id == "i-web"
        assert instance.instance_type == "t3.micro"
        assert instance.fact_evidence == {"/instance_type": [evidence_id]}
        assert isinstance(unknown, UnknownResource)
        assert unknown.resource_type == "unknown"
        assert unknown.identity.physical_id == "sg-app"
        assert unknown.aws_resource_type == "AWS::EC2::SecurityGroup"
        assert unknown.attributes == {"description": "Application security group"}
        assert inspected["resource"]["instance_type"] == "t3.micro"
        assert inspected["fact_paths"] == ["/instance_type"]
        assert inspected["evidence"]["value"]["InstanceType"] == "t3.micro"
    else:
        resource = saved.regions["eu-west-1"].resources[0]
        assert resource.fact_evidence == {"/attributes/ingress/0": [evidence_id]}
        assert inspected["fact_paths"] == ["/attributes/ingress/0"]
        assert inspected["resource"]["fact_evidence"] == resource.fact_evidence
        assert inspected["evidence"]["locator"] == "DescribeSecurityGroups"
        assert inspected["evidence"]["configuration_path"] == "SecurityGroups[0].IpPermissions[0]"
        assert inspected["evidence"]["value"]["FromPort"] == 443
        operations = {operation.operation: operation for operation in saved.coverage.operations}
        failed = operations["DescribeRouteTables"]
        assert failed.status == "failed"
        assert failed.result_count is None
        assert failed.failures[0].code == "UnauthorizedOperation"
        assert failed.failures[0].message == "Route-table collection denied"
        empty = operations["DescribeInstances"]
        assert empty.status == "success"
        assert empty.result_count == 0
        coverage = {operation["operation"]: operation for operation in inspected["coverage"]}
        assert coverage["DescribeRouteTables"] == failed.model_dump(mode="json")
        assert coverage["DescribeInstances"] == empty.model_dump(mode="json")

    assert output.read_bytes() == saved_bytes
    conflict = run("ingest", str(source), "--output", str(output), exit_code=3)
    assert conflict["error"]["code"] == "snapshot_conflict"
    assert output.read_bytes() == saved_bytes
    assert source.read_bytes() == source_bytes

    invalid = json.loads(source_bytes)
    invalid["evidence"][evidence_id]["resource_id"] = "never-disclose-unknown-owner"
    invalid_source = tmp_path / "invalid-reference.json"
    invalid_source.write_text(json.dumps(invalid))
    invalid_output = tmp_path / "invalid-output.json"
    rejected = run("ingest", str(invalid_source), "--output", str(invalid_output), exit_code=2)
    error = rejected["error"]
    assert error["path"] == str(invalid_source)
    assert any(
        issue["type"] == "snapshot_evidence_owner_unknown"
        and issue["message"] == "Snapshot constraint failed: unknown resource owner."
        for issue in error["details"]
    )
    assert "never-disclose-unknown-owner" not in json.dumps(rejected)
    assert not invalid_output.exists()
    assert output.read_bytes() == saved_bytes
    assert source.read_bytes() == source_bytes
    assert fixture.read_bytes() == source_bytes
    assert error["code"] == "invalid_snapshot"
