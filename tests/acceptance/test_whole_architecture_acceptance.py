import json
from threading import Lock

from typer.testing import CliRunner

from archdiscov.cli import app
from archdiscov.ingestion import aws
from archdiscov.ingestion.configuration_relationships import walk_resources
from archdiscov.ingestion.requests import COLLECTABLE_SERVICES
from archdiscov.models.enums import AwsRegion, AwsService, CoverageStatus
from archdiscov.models.resources.load_balancing import Listener
from archdiscov.services import load_snapshot
from tests.support.account_assembly import Session as AssemblySession
from tests.support.account_assembly import denied, in_region
from tests.support.aws_fakes import Client as Ec2Client
from tests.support.iam_data import ACCOUNT
from tests.support.sqs_data import Client as SqsClient

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
    assert snapshot.metadata["collection_scope"] == {
        "account_id": account,
        "services": [service.value for service in sorted(COLLECTABLE_SERVICES)],
        "regions": [region.value for region in regions],
        "region_scope": scope,
    }
    global_resources = list(walk_resources(snapshot.global_resources))
    assert {resource.identity.service for resource in global_resources} == {AwsService.IAM}
    all_resources = list(global_resources)
    for region in regions:
        resources = list(walk_resources(snapshot.regions[region].resources))
        assert {resource.identity.service for resource in resources} == COLLECTABLE_SERVICES - {
            AwsService.IAM
        }
        all_resources.extend(resources)
    assert all(resource.identity.account_id == account for resource in all_resources)
    assert {op.service for op in snapshot.coverage.operations} == COLLECTABLE_SERVICES


def test_whole_architecture_collect_evidence_and_no_overwrite(tmp_path, monkeypatch):
    # The first account's collection is partial, the second's complete.
    sessions = install_sessions(monkeypatch, partial=True)
    outputs = [tmp_path / "second.json", tmp_path / "first.json"]
    args = collect_args(outputs)
    runner = CliRunner()
    result = runner.invoke(app, [*args, "--json"])
    assert result.exit_code == 0, result.output
    summaries = json.loads(result.output)["accounts"]
    assert [item["account_id"] for item in summaries] == list(ACCOUNTS.values())
    assert [item["path"] for item in summaries] == [str(path) for path in outputs]
    assert [item["complete"] for item in summaries] == [False, True]
    original = [output.read_bytes() for output in outputs]
    for index, (output, account) in enumerate(zip(outputs, ACCOUNTS.values(), strict=True)):
        snapshot = load_snapshot(output)
        assert_inventory(snapshot, account)
        operations = {(op.region, op.operation): op for op in snapshot.coverage.operations}
        if index == 0:
            for region in REGIONS:
                assert operations[region, "DescribeSubnets"].status == CoverageStatus.INCOMPLETE
                failed = operations[region, "GetFunctionConfiguration"]
                assert failed.status == CoverageStatus.FAILED
                assert [failure.code for failure in failed.failures] == ["AccessDenied"]
        else:
            assert all(op.status == CoverageStatus.SUCCESS for op in operations.values())
        listener = next(
            resource
            for resource in walk_resources(snapshot.regions[AwsRegion.US_EAST_1].resources)
            if isinstance(resource, Listener)
        )
        evidence_id = listener.fact_evidence["/actions/0/forward_groups/0/arn"][0]
        inspected = runner.invoke(
            app, ["evidence", evidence_id, "--snapshot", str(output), "--json"]
        )
        assert inspected.exit_code == 0, inspected.output
        evidence = json.loads(inspected.output)
        assert evidence["resource"]["id"] == listener.id
        assert evidence["evidence"] == snapshot.evidence[evidence_id].model_dump(mode="json")
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


def test_account_preparation_failure_preserves_other_account(tmp_path, monkeypatch):
    sessions = install_sessions(monkeypatch, failure="discovery")
    outputs = [tmp_path / "failed.json", tmp_path / "saved.json"]
    result = CliRunner().invoke(app, [*collect_args(outputs), "--json"])
    assert result.exit_code == 5, result.output
    failed, succeeded = json.loads(result.output)["accounts"]
    assert failed["account_id"] == ACCOUNTS["second"]
    assert failed["error"]["code"] == "region_discovery_failed"
    assert succeeded["account_id"] == ACCOUNTS["first"]
    assert succeeded["complete"] is True
    assert not outputs[0].exists()
    assert_inventory(load_snapshot(outputs[1]), ACCOUNTS["first"])
    failed_session = dict(sessions)["second"]
    assert failed_session.iam.calls == []
    assert [name for name, _ in failed_session.client_calls] == ["sts", "ec2"]
    assert "assembly-private-value" not in result.output + outputs[1].read_text()
