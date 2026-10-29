"""Phase-closing check complementing the multi-account and raw-import journeys: real
Botocore requests, through a Stubber, collected through the CLI."""

import json

import boto3
from botocore.stub import Stubber
from typer.testing import CliRunner

from archdiscov.cli import app
from archdiscov.ingestion import aws
from archdiscov.models.enums import CoverageStatus
from archdiscov.services import load_snapshot

ACCOUNT = "123456789012"
REGION = "us-east-1"


def test_stubber_collection_to_saved_file_and_exact_evidence(monkeypatch, tmp_path):
    session = boto3.Session(
        region_name=REGION,
        aws_access_key_id="offline-key",
        aws_secret_access_key="offline-secret",
    )
    sts = session.client("sts")
    ec2 = session.client("ec2")

    class StubbedSession:
        region_name = REGION

        def client(self, name, **kwargs):
            assert kwargs.get("region_name", REGION) == REGION
            return {"sts": sts, "ec2": ec2}[name]

    monkeypatch.setattr(aws, "default_session_factory", lambda **kwargs: StubbedSession())
    output = tmp_path / "observed.json"
    runner = CliRunner()
    with Stubber(sts) as identity, Stubber(ec2) as inventory:
        identity.add_response(
            "get_caller_identity",
            {
                "Account": ACCOUNT,
                "Arn": f"arn:aws:iam::{ACCOUNT}:user/offline",
                "UserId": "offline",
            },
            {},
        )
        # One runner worker gives Stubber a deterministic request order. Separate
        # synchronization tests verify that real concurrent execution stays bounded.
        for operation, result_key in aws.EC2_OPERATIONS:
            method = ec2.meta.method_to_api_mapping
            method_name = next(name for name, api in method.items() if api == operation)
            if operation == "DescribeRouteTables":
                inventory.add_client_error(
                    method_name,
                    service_error_code="UnauthorizedOperation",
                    service_message="never-persist-diagnostic-secret",
                    expected_params={},
                )
            elif operation == "DescribeVpcs":
                inventory.add_response(
                    method_name,
                    {result_key: [{"VpcId": "vpc-offline", "CidrBlock": "10.0.0.0/16"}]},
                    {},
                )
            else:
                inventory.add_response(method_name, {result_key: []}, {})
        collected = runner.invoke(
            app,
            [
                "collect",
                "--account",
                f"offline:{ACCOUNT}",
                "--region",
                REGION,
                "--max-concurrency",
                "1",
                "--output",
                str(output),
                "--json",
            ],
        )
        assert collected.exit_code == 0, collected.output
        identity.assert_no_pending_responses()
        inventory.assert_no_pending_responses()

    summary = json.loads(collected.stdout)["accounts"][0]
    assert summary["complete"] is False
    saved = load_snapshot(output)
    assert saved.schema_version == "2"
    operations = {op.operation: op for op in saved.coverage.operations}
    assert operations["DescribeRouteTables"].status == CoverageStatus.FAILED
    assert operations["DescribeRouteTables"].failures[0].code == "UnauthorizedOperation"
    assert operations["DescribeInstances"].status == CoverageStatus.SUCCESS
    assert operations["DescribeInstances"].result_count == 0
    vpc = saved.regions[REGION].resources[0]
    assert [str(cidr) for cidr in vpc.cidr_blocks] == ["10.0.0.0/16"]
    evidence_id = vpc.fact_evidence["/cidr_blocks"][0]
    inspected = runner.invoke(app, ["evidence", evidence_id, "--snapshot", str(output), "--json"])
    assert inspected.exit_code == 0, inspected.output
    evidence = json.loads(inspected.stdout)
    assert evidence["evidence"]["resource_id"] == vpc.id
    assert evidence["evidence"]["configuration_path"] == "Vpcs[0].CidrBlock"
    assert evidence["evidence"]["value"] == "10.0.0.0/16"
    assert "/cidr_blocks" in evidence["fact_paths"]
    assert b"never-persist-diagnostic-secret" not in output.read_bytes()
    assert "never-persist-diagnostic-secret" not in collected.output + inspected.output
