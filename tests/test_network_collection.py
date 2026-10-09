import json

import pytest
from botocore.exceptions import ClientError
from typer.testing import CliRunner

from archdiscov.cli import app
from archdiscov.ingestion import aws as aws_module
from archdiscov.ingestion.adapter import AdapterContext, IngestionError
from archdiscov.ingestion.aws import EC2_OPERATIONS, OperationRunner, OperationTask
from archdiscov.ingestion.operations import Ec2Operation as Op
from archdiscov.models.architecture import UnresolvedTarget
from archdiscov.models.enums import AwsRegion, AwsService
from archdiscov.services import collect_and_save, collect_snapshots, load_snapshot
from tests import aws_fakes
from tests.aws_fakes import ACCOUNT, Session, make_context, request
from tests.network_data import FULL
from tests.test_network_relationships import ATTACHED_ENI, INSTANCE

KEYS = {operation: key for operation, key in EC2_OPERATIONS}
ITEMS = {
    **FULL,
    Op.DESCRIBE_INSTANCES: [{"Instances": [INSTANCE]}],
    Op.DESCRIBE_NETWORK_INTERFACES: [ATTACHED_ENI],
}
DENIED = ClientError({"Error": {"Code": "UnauthorizedOperation"}}, "DescribeRouteTables")


def snake(operation: Op) -> str:
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in operation.value).lstrip("_")


def pages(overrides=None):
    def factory(region):
        data = {op: [{KEYS[op]: items}] for op, items in ITEMS.items()}
        data.update(overrides or {})
        return {snake(op): page for op, page in data.items()}

    return factory


def collect(**kwargs):
    (snapshot,) = collect_snapshots(request(ACCOUNT), context=make_context(**kwargs))
    return snapshot


def flatten(resources):
    for resource in resources:
        yield resource
        yield from flatten(resource.children)


def group_of(snapshot, physical_id="sg-1"):
    resources = flatten(snapshot.regions["us-east-1"].resources)
    return next(r for r in resources if r.identity.physical_id == physical_id)


def test_collection_emits_schema_two_with_network_resources_and_relationships():
    snapshot = collect(pages=pages())
    assert snapshot.schema_version == "2"
    kinds = {r.resource_type.value for r in flatten(snapshot.regions["us-east-1"].resources)}
    assert {
        "AWS::EC2::RouteTable",
        "AWS::EC2::SecurityGroup",
        "AWS::EC2::NetworkInterface",
    } <= kinds
    assert {o.operation for o in snapshot.coverage.operations} == {o.value for o in KEYS}
    assert {o.status for o in snapshot.coverage.operations} == {"success"}
    assert snapshot.dependencies_by_region[AwsRegion.US_EAST_1]


def test_failed_route_tables_leave_security_groups_and_unresolve_dependents():
    denied = [DENIED]
    snapshot = collect(
        pages=pages({Op.DESCRIBE_ROUTE_TABLES: denied, Op.DESCRIBE_INTERNET_GATEWAYS: denied})
    )
    status = {o.operation: o.status for o in snapshot.coverage.operations}
    assert status["DescribeRouteTables"] == "failed"
    assert status["DescribeSecurityGroups"] == "success"
    resources = list(flatten(snapshot.regions["us-east-1"].resources))
    kinds = {r.resource_type.value for r in resources}
    assert "AWS::EC2::RouteTable" not in kinds and "AWS::EC2::InternetGateway" not in kinds
    group = next(r for r in resources if r.identity.physical_id == "sg-1")
    assert group.ingress_rules
    assert not any(
        d.attributes["kind"] == "route_target"
        for d in snapshot.dependencies_by_region[AwsRegion.US_EAST_1]
    )


def test_failed_gateway_operation_makes_route_targets_unresolved():
    snapshot = collect(pages=pages({Op.DESCRIBE_INTERNET_GATEWAYS: [DENIED]}))
    deps = snapshot.dependencies_by_region[AwsRegion.US_EAST_1]
    route = next(d for d in deps if d.attributes.get("target_kind") == "internet_gateway")
    assert isinstance(route.target, UnresolvedTarget)
    assert "DescribeInternetGateways did not complete" in route.target.reason


def test_failed_rule_operation_falls_back_to_group_permissions():
    snapshot = collect(pages=pages({Op.DESCRIBE_SECURITY_GROUP_RULES: [DENIED]}))
    group = next(
        r
        for r in flatten(snapshot.regions["us-east-1"].resources)
        if r.identity.physical_id == "sg-1"
    )
    assert group.attributes["rules_source"] == "DescribeSecurityGroups"
    assert group.attributes["limitations"]
    assert len(group.ingress_rules) == 2


def test_zero_results_remain_successful():
    empty = {op: [{key: []}] for op, key in KEYS.items()}
    snapshot = collect(pages=lambda region: {snake(op): page for op, page in empty.items()})
    assert {o.status for o in snapshot.coverage.operations} == {"success"}
    assert snapshot.regions["us-east-1"].resources == []
    assert snapshot.dependencies_by_region == {}


def test_new_operations_are_allowlisted_and_others_rejected_before_any_call():
    calls = []

    class Spy(Session):
        def client(self, name, region_name=None, **kwargs):
            calls.append(name)
            return super().client(name, region_name=region_name, **kwargs)

    account = request(ACCOUNT).accounts[0]
    runner = OperationRunner(Spy(ACCOUNT, pages()), account, make_context(pages()))
    with pytest.raises(IngestionError) as exc:
        runner.run(OperationTask(AwsRegion.US_EAST_1, AwsService.EC2, "TerminateInstances"))
    assert exc.value.code == "operation_not_allowed" and calls == []
    for operation in Op:
        runner.run(OperationTask(AwsRegion.US_EAST_1, AwsService.EC2, operation.value))


def test_secrets_in_descriptions_and_tags_never_reach_the_saved_file(tmp_path):
    tagged = {
        **ITEMS[Op.DESCRIBE_SECURITY_GROUPS][0],
        "Tags": [{"Key": "api_key", "Value": "tag-secret"}],
    }
    context = make_context(pages({Op.DESCRIBE_SECURITY_GROUPS: [{"SecurityGroups": [tagged]}]}))
    (outcome,) = collect_and_save(request(ACCOUNT), [tmp_path / "s.json"], context=context)
    text = outcome.path.read_text()
    assert "tag-secret" not in text and "hunter2" not in text and "super secret note" not in text


def test_multi_account_collection_stays_isolated(tmp_path):
    other = "210987654321"

    def factory(**kwargs):
        account = other if kwargs.get("profile_name") == f"p{other}" else ACCOUNT
        return Session(account, pages())

    context = AdapterContext(clock=aws_fakes.Clock(), session_factory=factory)
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    outcomes = collect_and_save(request(ACCOUNT, other), [first, second], context=context)
    assert all(o.error is None for o in outcomes)
    assert load_snapshot(first).account_id == ACCOUNT
    assert load_snapshot(second).account_id == other
    for path, account in ((first, ACCOUNT), (second, other)):
        snapshot = load_snapshot(path)
        resources = flatten(snapshot.regions["us-east-1"].resources)
        assert all(r.id.startswith(f"{account}/") for r in resources)
        deps = snapshot.dependencies_by_region[AwsRegion.US_EAST_1]
        assert all(d.source_id.startswith(f"{account}/") for d in deps)


def test_collect_then_evidence_on_a_rule_citation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        aws_module, "default_session_factory", lambda **kwargs: Session(ACCOUNT, pages())
    )
    runner = CliRunner()
    output = tmp_path / "snap.json"
    result = runner.invoke(
        app,
        ["collect", "--account", f"dev:{ACCOUNT}", "--region", "us-east-1", "-o", str(output)],
    )
    assert result.exit_code == 0, result.output
    snapshot = load_snapshot(output)
    group = next(
        r
        for r in flatten(snapshot.regions["us-east-1"].resources)
        if r.identity.physical_id == "sg-1"
    )
    evidence_id = group.fact_evidence["/ingress_rules/0"][0]
    inspected = runner.invoke(app, ["evidence", evidence_id, "--snapshot", str(output), "--json"])
    assert inspected.exit_code == 0, inspected.output
    payload = json.loads(inspected.output)
    assert "SecurityGroupRules[0]" in json.dumps(payload)

    again = runner.invoke(
        app,
        ["collect", "--account", f"dev:{ACCOUNT}", "--region", "us-east-1", "-o", str(output)],
    )
    assert again.exit_code == 3
