import json

from botocore.exceptions import ClientError

from archdiscov.ingestion.elbv2.relationships import walk_resources
from archdiscov.ingestion.requests import AwsCollectionRequest
from archdiscov.models.enums import AwsService
from archdiscov.models.resources.load_balancing import Listener, TargetGroup
from archdiscov.services import collect_and_save, collect_snapshots, load_snapshot
from tests.support.aws_fakes import Session
from tests.support.elbv2_data import (
    LB,
    LISTENER,
    NLB,
    Elbv2Client,
    Elbv2Session,
    config,
    context,
    ec2_pages,
    responses,
)
from tests.support.iam_data import IamClient


def test_combined_services_keep_regional_ec2_and_global_iam_intact(tmp_path):
    session = Elbv2Session(ec2=Session(pages=ec2_pages), iam=IamClient())
    request = AwsCollectionRequest(
        accounts=(
            config(
                services=(
                    AwsService.EC2,
                    AwsService.IAM,
                    AwsService.ELASTIC_LOAD_BALANCING,
                )
            ),
        )
    )
    result = collect_and_save(request, [tmp_path / "all.json"], context=context(session))[0]
    assert result.error is None
    snap = load_snapshot(result.path)
    assert len(snap.coverage.operations) == 23
    assert len(snap.global_resources) == 3
    roots = snap.regions["us-east-1"].resources
    assert len(roots) == 1
    assert roots[0].identity.physical_id == "vpc-a"
    assert len(list(walk_resources(roots))) == 8
    assert sum(method == "list_roles" for method, _ in session.iam.calls) == 1
    assert all(link.target.kind == "local" for link in snap.dependencies_by_region["us-east-1"])


def test_two_elbv2_regions_collect_independently():
    class RegionalSession(Elbv2Session):
        def client(self, name, **kwargs):
            if name == "elbv2":
                self.clients.append((name, kwargs))
                region = kwargs["region_name"]
                return Elbv2Client(json.loads(json.dumps(responses()).replace("us-east-1", region)))
            return super().client(name, **kwargs)

    session = RegionalSession(iam=IamClient())
    request = AwsCollectionRequest(
        accounts=(
            config(
                regions=("us-east-1", "us-west-2"),
                services=(AwsService.IAM, AwsService.ELASTIC_LOAD_BALANCING),
            ),
        )
    )
    snap = collect_snapshots(request, context=context(session))[0]
    assert len(snap.coverage.operations) == 20
    assert sum(method == "list_roles" for method, _ in session.iam.calls) == 1
    assert len(snap.global_resources) == 3
    assert {kwargs["region_name"] for name, kwargs in session.clients if name == "elbv2"} == {
        "us-east-1",
        "us-west-2",
    }
    for region, architecture in snap.regions.items():
        assert all(
            resource.identity.region == region
            for resource in walk_resources(architecture.resources)
        )


def test_failed_health_preserves_inventory_and_successful_listener_details(tmp_path):
    data = responses()
    data["describe_target_health"] = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "protected-secret"}}, "DescribeTargetHealth"
    )
    session = Elbv2Session(Elbv2Client(data))
    request = AwsCollectionRequest(accounts=(config(),))
    outcome = collect_and_save(request, [tmp_path / "partial.json"], context=context(session))[0]
    assert outcome.error is None
    assert not outcome.complete
    snap = load_snapshot(outcome.path)
    resources = list(walk_resources(snap.regions["us-east-1"].resources))
    assert len(resources) == 4
    assert next(r for r in resources if isinstance(r, TargetGroup)).targets is None
    assert next(r for r in resources if isinstance(r, Listener)).actions[0].forward_groups
    assert "protected-secret" not in outcome.path.read_text()


def test_nlb_defaults_forward_without_rule_api():
    data = json.loads(
        json.dumps(responses())
        .replace(LB, NLB)
        .replace(LISTENER, LISTENER.replace("listener/app/web/", "listener/net/tcp/"))
    )
    data["describe_load_balancers"]["LoadBalancers"][0]["Type"] = "network"
    data["describe_listeners"]["Listeners"][0]["Protocol"] = "TCP"
    session = Elbv2Session(Elbv2Client(data))
    snap = collect_snapshots(AwsCollectionRequest(accounts=(config(),)), context=context(session))[
        0
    ]
    assert not any(method == "describe_rules" for method, _ in session.elb.calls)
    listener = next(
        r for r in walk_resources(snap.regions["us-east-1"].resources) if isinstance(r, Listener)
    )
    links = [
        link
        for link in snap.dependencies_by_region["us-east-1"]
        if link.source_id == listener.id and link.relationship == "forwards_to"
    ]
    assert len(links) == 1
    assert links[0].target.kind == "local"
    assert links[0].attributes["weight"] == 100
    assert (
        next(op for op in snap.coverage.operations if op.operation == "DescribeRules").result_count
        == 0
    )
