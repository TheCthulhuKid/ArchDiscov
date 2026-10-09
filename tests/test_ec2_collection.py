import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from typer.testing import CliRunner

from archdiscov.cli import app
from archdiscov.ingestion import aws as aws_module
from archdiscov.ingestion.adapter import AdapterContext, IngestionError
from archdiscov.ingestion.operations import Ec2Operation
from archdiscov.services import (
    SnapshotConflictError,
    collect_and_save,
    collect_snapshots,
    load_snapshot,
)
from tests import aws_fakes as fakes
from tests.aws_fakes import Clock, Session, request

ACCOUNT = "123456789012"
OTHER = "210987654321"
VPC = "vpc-1"
SUBNET = "subnet-1"
INSTANCE = "i-1"

VPCS = [
    {
        "VpcId": VPC,
        "CidrBlock": "10.0.0.0/16",
        "CidrBlockAssociationSet": [{"CidrBlock": "10.0.0.0/16"}, {"CidrBlock": "10.1.0.0/16"}],
        "State": "available",
        "IsDefault": False,
        "Tags": [{"Key": "Name", "Value": "main"}],
    }
]
SUBNETS = [
    {
        "SubnetId": SUBNET,
        "VpcId": VPC,
        "CidrBlock": "10.0.1.0/24",
        "AvailabilityZone": "us-east-1a",
        "State": "available",
    },
    {"SubnetId": "subnet-orphan", "VpcId": "vpc-missing", "CidrBlock": "10.9.0.0/24"},
]
RESERVATIONS = [
    {
        "Instances": [
            {
                "InstanceId": INSTANCE,
                "InstanceType": "t3.micro",
                "SubnetId": SUBNET,
                "VpcId": VPC,
                "State": {"Name": "running"},
                "LaunchTime": datetime(2025, 1, 1, tzinfo=UTC),
                "SecurityGroups": [{"GroupId": "sg-1", "GroupName": "web"}],
                "Tags": [{"Key": "Name", "Value": "web"}, {"Key": "password", "Value": "hunter2"}],
            },
            {"InstanceId": "i-nosubnet", "InstanceType": "t3.nano", "SubnetId": "subnet-gone"},
        ]
    }
]


def default_pages(region):
    return {
        "describe_vpcs": [{"Vpcs": VPCS}],
        "describe_subnets": [{"Subnets": SUBNETS[:1]}, {"Subnets": SUBNETS[1:]}],
        "describe_instances": [{"Reservations": RESERVATIONS}],
        **fakes.empty_network_pages(),
    }


def make_context(pages=default_pages, account=ACCOUNT):
    return fakes.make_context(pages, account)


def collect(**kwargs):
    (snapshot,) = collect_snapshots(request(ACCOUNT), context=make_context(**kwargs))
    return snapshot


def test_collects_vpc_subnet_instance_hierarchy_with_evidence():
    snapshot = collect()
    roots = snapshot.regions["us-east-1"].resources
    vpc = next(r for r in roots if r.identity.physical_id == VPC)
    assert vpc.name == "main"
    assert [str(c) for c in vpc.cidr_blocks] == ["10.0.0.0/16", "10.1.0.0/16"]
    (subnet,) = vpc.children
    assert subnet.availability_zone == "us-east-1a"
    assert [str(c) for c in subnet.cidr_blocks] == ["10.0.1.0/24"]
    (instance,) = subnet.children
    assert instance.instance_type == "t3.micro"
    assert instance.name == "web"
    assert instance.attributes["security_group_ids"] == ["sg-1"]

    cited = snapshot.evidence[instance.fact_evidence["/instance_type"][0]]
    assert cited.resource_id == instance.id
    assert cited.locator == "ec2:DescribeInstances"
    assert cited.configuration_path == "Reservations[0].Instances[0].InstanceType"
    assert cited.value == "t3.micro"
    whole = snapshot.evidence[instance.evidence_ids[0]]
    assert whole.value["LaunchTime"] == "2025-01-01T00:00:00+00:00"
    assert snapshot.coverage.account_wide is False


def test_sensitive_tag_values_are_redacted_in_evidence_and_attributes():
    snapshot = collect()
    text = snapshot.model_dump_json()
    assert "hunter2" not in text
    whole = snapshot.evidence["us-east-1/DescribeInstances/i-1"]
    assert whole.value_status == "redacted"
    assert whole.redacted_paths == ["/Tags/1/Value"]


def test_unavailable_parents_are_not_synthesized():
    snapshot = collect()
    roots = snapshot.regions["us-east-1"].resources
    by_physical = {r.identity.physical_id: r for r in roots}
    orphan = by_physical["subnet-orphan"]
    assert orphan.attributes["unresolved_parent"]["id"] == "vpc-missing"
    loose = by_physical["i-nosubnet"]
    assert loose.attributes["unresolved_parent"]["kind"] == "subnet"
    assert not any(r.identity.physical_id in ("vpc-missing", "subnet-gone") for r in roots)


def test_pages_are_aggregated_and_zero_results_are_successful():
    def pages(region):
        return {
            "describe_vpcs": [{"Vpcs": []}],
            "describe_subnets": [{"Subnets": SUBNETS[:1]}, {"Subnets": []}],
            "describe_instances": [{"Reservations": []}],
            **fakes.empty_network_pages(),
        }

    snapshot = collect(pages=pages)
    outcomes = {o.operation: o for o in snapshot.coverage.operations}
    assert {o.status for o in outcomes.values()} == {"success"}
    assert outcomes["DescribeVpcs"].result_count == 0
    assert outcomes["DescribeSubnets"].result_count == 1


def test_mid_collection_failure_is_distinct_and_keeps_partial_results():
    denied = ClientError({"Error": {"Code": "UnauthorizedOperation"}}, "DescribeInstances")

    def pages(region):
        data = default_pages(region)
        data["describe_instances"] = [denied]
        data["describe_subnets"] = [{"Subnets": SUBNETS[:1]}, denied]
        return data

    snapshot = collect(pages=pages)
    outcomes = {o.operation: o for o in snapshot.coverage.operations}
    assert outcomes["DescribeInstances"].status == "failed"
    assert outcomes["DescribeInstances"].result_count is None
    assert outcomes["DescribeSubnets"].status == "incomplete"
    assert outcomes["DescribeSubnets"].result_count == 1
    assert outcomes["DescribeVpcs"].status == "success"
    vpc = snapshot.regions["us-east-1"].resources[0]
    assert vpc.children[0].children == []  # instances were never collected


def test_identity_mismatch_collects_nothing():
    with pytest.raises(IngestionError) as exc:
        collect_snapshots(request(ACCOUNT), context=make_context(account=OTHER))
    assert exc.value.code == "account_mismatch"


def test_multi_region_collection_has_separate_scopes():
    (snapshot,) = collect_snapshots(
        request(ACCOUNT, regions=("us-east-1", "eu-west-1")), context=make_context()
    )
    assert set(snapshot.regions) == {"us-east-1", "eu-west-1"}
    assert len(snapshot.coverage.operations) == 2 * len(aws_module.EC2_OPERATIONS)
    assert all(op.operation != Ec2Operation.DESCRIBE_REGIONS for op in snapshot.coverage.operations)
    ids = {r.id for region in snapshot.regions.values() for r in region.resources}
    assert len(ids) == len([r for region in snapshot.regions.values() for r in region.resources])


def test_collect_and_save_isolates_accounts_and_never_overwrites(tmp_path):
    def factory(**kwargs):
        return Session(
            ACCOUNT if kwargs.get("profile_name") == f"p{ACCOUNT}" else "000000000000",
            default_pages,
        )

    context = AdapterContext(clock=Clock(), session_factory=factory)
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    outcomes = collect_and_save(request(ACCOUNT, OTHER), [first, second], context=context)
    assert outcomes[0].path == first and outcomes[0].error is None
    assert outcomes[1].path is None and outcomes[1].error.code == "account_mismatch"
    assert not second.exists()
    assert load_snapshot(first).account_id == ACCOUNT

    before = first.read_bytes()
    again = collect_and_save(request(ACCOUNT), [first], context=context)
    assert isinstance(again[0].error, SnapshotConflictError)
    assert first.read_bytes() == before


def test_collect_and_save_requires_distinct_outputs(tmp_path):
    with pytest.raises(IngestionError):
        collect_and_save(request(ACCOUNT, OTHER), [tmp_path / "a.json"])
    with pytest.raises(IngestionError):
        collect_and_save(request(ACCOUNT, OTHER), [tmp_path / "a.json", tmp_path / "a.json"])


def test_partial_outcome_is_not_complete(tmp_path):
    denied = ClientError({"Error": {"Code": "UnauthorizedOperation"}}, "DescribeVpcs")

    def pages(region):
        return {**default_pages(region), "describe_vpcs": [denied]}

    (outcome,) = collect_and_save(
        request(ACCOUNT), [tmp_path / "s.json"], context=make_context(pages=pages)
    )
    assert outcome.path is not None and not outcome.complete


def test_cli_collect_then_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(
        aws_module, "default_session_factory", lambda **kwargs: Session(ACCOUNT, default_pages)
    )
    runner = CliRunner()
    output = tmp_path / "snap.json"
    result = runner.invoke(
        app,
        [
            "collect",
            "--account",
            f"dev:{ACCOUNT}",
            "--region",
            "us-east-1",
            "-o",
            str(output),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["accounts"][0]
    assert data["complete"] is True and data["path"] == str(output)
    snapshot = load_snapshot(output)
    evidence_id = next(iter(snapshot.evidence))
    inspected = runner.invoke(app, ["evidence", evidence_id, "--snapshot", str(output), "--json"])
    assert inspected.exit_code == 0, inspected.output

    again = runner.invoke(
        app,
        ["collect", "--account", f"dev:{ACCOUNT}", "--region", "us-east-1", "-o", str(output)],
    )
    assert again.exit_code == 3


def test_cli_collect_rejects_bad_requests(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "collect",
            "--account",
            "dev:123",
            "--region",
            "us-east-1",
            "-o",
            str(tmp_path / "x.json"),
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert json.loads(result.output)["error"]["code"] == "invalid_request"
    result = runner.invoke(
        app,
        [
            "collect",
            "--account",
            f"dev:{ACCOUNT}",
            "--region",
            "nowhere",
            "-o",
            str(Path(tmp_path) / "x.json"),
        ],
    )
    assert result.exit_code == 2
    assert not (tmp_path / "x.json").exists()


def test_cli_reports_identity_failure_without_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(
        aws_module, "default_session_factory", lambda **kwargs: Session(OTHER, default_pages)
    )
    output = tmp_path / "snap.json"
    result = CliRunner().invoke(
        app,
        [
            "collect",
            "--account",
            f"dev:{ACCOUNT}",
            "--region",
            "us-east-1",
            "-o",
            str(output),
            "--json",
        ],
    )
    assert result.exit_code == 5
    assert json.loads(result.output)["accounts"][0]["error"]["code"] == "account_mismatch"
    assert not output.exists()
