import json
from threading import Lock

import pytest
from typer.testing import CliRunner

from archdiscov.cli import app
from archdiscov.ingestion import aws
from archdiscov.ingestion.configuration_relationships import walk_resources
from archdiscov.ingestion.requests import COLLECTABLE_SERVICES
from archdiscov.models.enums import AttachmentKind, AwsRegion, AwsService, CoverageStatus
from archdiscov.models.resources.load_balancing import Listener
from archdiscov.services import load_snapshot
from tests.ingestion.aws.test_account_assembly import Session as AssemblySession
from tests.ingestion.aws.test_account_assembly import denied, in_region
from tests.ingestion.aws.test_sqs_collection import Client as SqsClient
from tests.support.aws_fakes import Client as Ec2Client
from tests.support.elbv2_data import LB
from tests.support.iam_data import ACCOUNT

ACCOUNTS = {"second": "222222222222", "first": "111111111111"}
REGIONS = (AwsRegion.US_EAST_1, AwsRegion.US_WEST_2)
SECRETS = (
    "protected-context",
    "protected-tag",
    "assembly-private-value",
    "secret-context",
    "secret-generic-tag-condition",
    "secret-redrive",
    "secret-redrive-allow",
    "secret-counter",
    "secret-message",
    "secret-credentials",
    "secret-tag",
    "secret-password",
    "acceptance-lambda-secret",
    "acceptance-rds-secret",
    "acceptance-ecs-secret",
    "acceptance-ec2-secret",
)


def for_account(value, account):
    if isinstance(value, str):
        return value.replace(ACCOUNT, account)
    if isinstance(value, list):
        return [for_account(item, account) for item in value]
    if isinstance(value, dict):
        return {
            for_account(key, account): for_account(item, account) for key, item in value.items()
        }
    return value


class DiscoveryClient(Ec2Client):
    def __init__(self, session, region):
        super().__init__(session.account, session.pages[region])
        self.session = session
        self.region = region

    def describe_regions(self, **params):
        self.session.discovery_calls.append((self.region, params))
        if self.session.failure == "discovery":
            raise denied("DescribeRegions")
        return {
            "Regions": [
                {"RegionName": "us-west-2", "OptInStatus": "opted-in"},
                {"RegionName": "ap-east-1", "OptInStatus": "not-opted-in"},
                {"RegionName": "us-east-1", "OptInStatus": "opt-in-not-required"},
            ]
        }


class Session(AssemblySession):
    region_name = "us-east-1"

    def __init__(self, account, partial=False, failure=None):
        super().__init__(partial)
        self.account = account
        self.failure = failure
        self.identity_calls = 0
        self.discovery_calls = []
        self.client_calls = []
        self.iam.data = for_account(self.iam.data, account)
        for region, clients in self.clients.items():
            clients["sqs"] = SqsClient(in_region(SqsClient().data, region))
            clients["lambda"].pages[0]["Functions"][0]["Environment"] = {
                "Variables": {"PASSWORD": "acceptance-lambda-secret"}
            }
            if isinstance(clients["lambda"].detail, dict):
                clients["lambda"].detail["Environment"] = {
                    "Variables": {"PASSWORD": "acceptance-lambda-secret"}
                }
            clients["rds"].data["describe_db_instances"]["DBInstances"][0]["MasterUserPassword"] = (
                "acceptance-rds-secret"
            )
            clients["ecs"].values["describe_task_definition"]["taskDefinition"][
                "containerDefinitions"
            ][0]["environment"] = [{"name": "PASSWORD", "value": "acceptance-ecs-secret"}]
            self.pages[region]["describe_instances"][0]["Reservations"][0]["Instances"][0][
                "UserData"
            ] = "acceptance-ec2-secret"
            for name in ("elbv2", "rds", "sqs"):
                clients[name].data = for_account(clients[name].data, account)
            clients["ecs"].values = for_account(clients["ecs"].values, account)
            clients["lambda"].pages = for_account(clients["lambda"].pages, account)
            clients["lambda"].detail = for_account(clients["lambda"].detail, account)
            self.pages[region] = for_account(self.pages[region], account)

    def client(self, name, **kwargs):
        self.client_calls.append((name, kwargs))
        if name == "sts":
            return self
        if name == "ec2":
            return DiscoveryClient(self, kwargs["region_name"])
        return super().client(name, **kwargs)

    def get_caller_identity(self):
        self.identity_calls += 1
        if self.failure == "identity":
            raise denied("GetCallerIdentity")
        return {"Account": self.account, "Arn": f"arn:aws:iam::{self.account}:user/offline"}


def install_sessions(monkeypatch, *, partial=False, failure=None):
    sessions = []
    lock = Lock()

    def factory(*, profile_name, region_name):
        assert region_name == "us-east-1"
        session = Session(
            ACCOUNTS[profile_name],
            partial=partial and profile_name == "second",
            failure=failure if profile_name == "second" else None,
        )
        with lock:
            sessions.append((profile_name, session))
        return session

    monkeypatch.setattr(aws, "default_session_factory", factory)
    return sessions


def collect_args(outputs):
    return [
        "collect",
        "--all-services",
        "--bootstrap-region",
        "us-east-1",
        *[
            arg
            for profile, account in ACCOUNTS.items()
            for arg in ("--account", f"{profile}:{account}")
        ],
        *[arg for output in outputs for arg in ("--output", str(output))],
    ]


def assert_inventory(snapshot, account, regions=REGIONS, scope="enabled"):
    assert snapshot.account_id == account
    assert set(snapshot.regions) == set(regions)
    assert snapshot.coverage.account_wide is True
    assert set(snapshot.coverage.regions) == set(regions)
    assert snapshot.metadata["collection_scope"] == {
        "account_id": account,
        "services": [service.value for service in sorted(COLLECTABLE_SERVICES)],
        "regions": [region.value for region in regions],
        "region_scope": scope,
    }
    global_resources = list(walk_resources(snapshot.global_resources))
    assert global_resources
    assert {resource.identity.service for resource in global_resources} == {AwsService.IAM}
    assert all(resource.identity.region == AwsRegion.GLOBAL for resource in global_resources)
    all_resources = list(global_resources)
    for region in regions:
        resources = list(walk_resources(snapshot.regions[region].resources))
        assert {resource.identity.service for resource in resources} == COLLECTABLE_SERVICES - {
            AwsService.IAM
        }
        assert all(resource.identity.region == region for resource in resources)
        all_resources.extend(resources)
    assert all(resource.identity.account_id == account for resource in all_resources)
    assert len({resource.id for resource in all_resources}) == len(all_resources)
    by_id = {resource.id: resource for resource in all_resources}
    for region in regions:
        links = snapshot.dependencies_by_region[region]
        assert links
        assert all(
            snapshot.evidence[eid].resource_id == link.source_id
            for link in links
            for eid in link.evidence_ids
        )
        profiles = [
            link for link in links if link.attributes.get("kind") == AttachmentKind.INSTANCE_PROFILE
        ]
        assert len(profiles) == 1
        profile = profiles[0]
        assert profile.target.kind == "local"
        assert by_id[profile.source_id].identity.service == AwsService.EC2
        assert by_id[profile.target.resource_id].identity.service == AwsService.IAM
        assert by_id[profile.target.resource_id].resource_type == "AWS::IAM::InstanceProfile"
    operations = snapshot.coverage.operations
    assert all(operation.account_id == account for operation in operations)
    assert {operation.service for operation in operations} == COLLECTABLE_SERVICES
    assert len({(op.region, op.service, op.operation) for op in operations}) == len(operations)
    assert all(op.region == AwsRegion.GLOBAL for op in operations if op.service == AwsService.IAM)
    for region in regions:
        queues = next(
            op for op in operations if op.region == region and op.operation == "ListQueues"
        )
        assert queues.status == CoverageStatus.SUCCESS
        assert queues.result_count == 1


@pytest.mark.parametrize(
    ("partial", "json_output"),
    [(False, True), (True, True), (True, False)],
    ids=["complete-json", "partial-json", "partial-readable"],
)
def test_whole_architecture_collect_evidence_and_no_overwrite(
    tmp_path, monkeypatch, partial, json_output
):
    sessions = install_sessions(monkeypatch, partial=partial)
    outputs = [tmp_path / "second.json", tmp_path / "first.json"]
    args = collect_args(outputs)
    flags = ["--json"] if json_output else []
    runner = CliRunner()
    result = runner.invoke(app, [*args, *flags])
    assert result.exit_code == 0, result.output
    if json_output:
        summaries = json.loads(result.output)["accounts"]
        assert [item["account_id"] for item in summaries] == list(ACCOUNTS.values())
        assert [item["path"] for item in summaries] == [str(path) for path in outputs]
        assert [item["complete"] for item in summaries] == [not partial, True]
    else:
        assert result.output.index(ACCOUNTS["second"]) < result.output.index(ACCOUNTS["first"])
        assert "PARTIAL" in result.output
        assert "complete within resolved supported scope" in result.output
        assert "ec2 DescribeSubnets: incomplete" in result.output
        assert "lambda GetFunctionConfiguration: failed" in result.output
    original = [output.read_bytes() for output in outputs]
    for index, (output, account) in enumerate(zip(outputs, ACCOUNTS.values(), strict=True)):
        snapshot = load_snapshot(output)
        assert_inventory(snapshot, account)
        operations = {(op.region, op.operation): op for op in snapshot.coverage.operations}
        if partial and index == 0:
            for region in REGIONS:
                assert operations[region, "DescribeSubnets"].status == CoverageStatus.INCOMPLETE
                assert operations[region, "DescribeSubnets"].result_count == 1
                for name in (
                    "DescribeRouteTables",
                    "DescribeTargetHealth",
                    "GetFunctionConfiguration",
                ):
                    operation = operations[region, name]
                    assert operation.status == CoverageStatus.FAILED
                    assert [failure.code for failure in operation.failures] == ["AccessDenied"]
                assert operations[region, "DescribeListeners"].status == CoverageStatus.SUCCESS
                assert operations[region, "DescribeDBInstances"].status == CoverageStatus.SUCCESS
        else:
            assert all(op.status == CoverageStatus.SUCCESS for op in operations.values())
        listener = next(
            resource
            for resource in walk_resources(snapshot.regions[AwsRegion.US_EAST_1].resources)
            if isinstance(resource, Listener)
        )
        fact_path = "/actions/0/forward_groups/0/arn"
        evidence_id = listener.fact_evidence[fact_path][0]
        inspected = runner.invoke(
            app, ["evidence", evidence_id, "--snapshot", str(output), "--json"]
        )
        assert inspected.exit_code == 0, inspected.output
        evidence = json.loads(inspected.output)
        assert evidence["resource"]["id"] == listener.id
        assert evidence["evidence"] == snapshot.evidence[evidence_id].model_dump(mode="json")
        assert evidence["evidence"]["locator"] == "elasticloadbalancing:DescribeListeners"
        assert evidence["evidence"]["configuration_path"] == (
            f'Request[{{"LoadBalancerArn":"{for_account(LB, account)}"}}].Page[0].'
            "Listeners[0].DefaultActions[0].ForwardConfig.TargetGroups[0].TargetGroupArn"
        )
        assert evidence["evidence"]["value"] == listener.actions[0].forward_groups[0].arn
        assert fact_path in evidence["fact_paths"]
        for secret in SECRETS:
            assert secret not in output.read_text() + result.output + inspected.output
        assert ACCOUNT not in output.read_text()
    assert len(sessions) == 2
    for _, session in sessions:
        assert session.identity_calls == 1
        assert session.discovery_calls == [("us-east-1", {"AllRegions": True})]
        assert sum(method == "list_roles" for method, _ in session.iam.calls) == 1
        assert not any(
            kwargs.get("region_name") == "ap-east-1" for _, kwargs in session.client_calls
        )
    again = runner.invoke(app, [*args, "--json"])
    assert again.exit_code == 3, again.output
    assert [item["error"]["code"] for item in json.loads(again.output)["accounts"]] == [
        "snapshot_conflict",
        "snapshot_conflict",
    ]
    assert [output.read_bytes() for output in outputs] == original
    assert len(sessions) == 4
    assert len({id(session) for _, session in sessions}) == 4


def test_explicit_region_override_bypasses_discovery(tmp_path, monkeypatch):
    sessions = install_sessions(monkeypatch, failure="discovery")
    outputs = [tmp_path / "second.json", tmp_path / "first.json"]
    result = CliRunner().invoke(app, [*collect_args(outputs), "--region", "us-west-2", "--json"])
    assert result.exit_code == 0, result.output
    assert all(item["complete"] for item in json.loads(result.output)["accounts"])
    for output, account in zip(outputs, ACCOUNTS.values(), strict=True):
        assert_inventory(load_snapshot(output), account, (AwsRegion.US_WEST_2,), "explicit")
    assert len(sessions) == 2
    for _, session in sessions:
        assert session.discovery_calls == []
        assert not any(
            kwargs.get("region_name") == "us-east-1"
            for name, kwargs in session.client_calls
            if name != "sts"
        )


@pytest.mark.parametrize("failure", ["identity", "discovery"])
def test_account_preparation_failure_preserves_other_account(tmp_path, monkeypatch, failure):
    sessions = install_sessions(monkeypatch, failure=failure)
    outputs = [tmp_path / "failed.json", tmp_path / "saved.json"]
    result = CliRunner().invoke(app, [*collect_args(outputs), "--json"])
    assert result.exit_code == 5, result.output
    failed, succeeded = json.loads(result.output)["accounts"]
    assert failed["account_id"] == ACCOUNTS["second"]
    assert failed["error"]["code"] == (
        "identity_failed" if failure == "identity" else "region_discovery_failed"
    )
    assert succeeded["account_id"] == ACCOUNTS["first"]
    assert succeeded["complete"] is True
    assert not outputs[0].exists()
    assert_inventory(load_snapshot(outputs[1]), ACCOUNTS["first"])
    failed_session = dict(sessions)["second"]
    assert failed_session.iam.calls == []
    assert [name for name, _ in failed_session.client_calls] == (
        ["sts"] if failure == "identity" else ["sts", "ec2"]
    )
    assert len(failed_session.discovery_calls) == (0 if failure == "identity" else 1)
    assert "assembly-private-value" not in result.output + outputs[1].read_text()
