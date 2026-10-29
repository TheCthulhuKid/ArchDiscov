from botocore.exceptions import EndpointConnectionError

from archdiscov.ingestion.iam.collector import collect_inventory
from archdiscov.ingestion.operations import IamOperation as Op
from archdiscov.ingestion.requests import AwsCollectionRequest
from archdiscov.models.enums import AwsService
from archdiscov.services import collect_snapshots
from tests.support.aws_fakes import Session
from tests.support.iam_data import (
    POLICY_ARN,
    IamClient,
    IamSession,
    config,
    context,
    ec2_pages,
    responses,
)


def combined_config():
    return config(regions=("us-east-1", "us-west-2")).model_copy(
        update={"services": (AwsService.EC2, AwsService.IAM)}
    )


def collect(data=None):
    session = IamSession(IamClient(data))
    return collect_inventory(session, config(), context(session)), session.iam.calls


def test_referenced_policy_and_default_version_are_fetched_once():
    inventory, calls = collect()
    assert len(inventory.results) == 8
    assert all(r.coverage.status == "success" for r in inventory.results.values())
    assert [p for m, p in calls if m == "get_policy"] == [{"PolicyArn": POLICY_ARN}]
    assert [p for m, p in calls if m == "get_policy_version"] == [
        {"PolicyArn": POLICY_ARN, "VersionId": "v7"}
    ]
    assert inventory.results[Op.GET_ROLE_POLICY].responses[0].call.params == {
        "RoleName": "web",
        "PolicyName": "inline",
    }


def test_failed_discovery_skips_details():
    data = responses()
    data["list_roles"] = EndpointConnectionError(endpoint_url="secret")
    inventory, calls = collect(data)
    assert inventory.results[Op.GET_ROLE].coverage.status == "skipped"
    assert inventory.results[Op.GET_POLICY_VERSION].coverage.status == "skipped"
    assert len(calls) == 2


def test_two_regions_link_instances_to_the_global_iam_inventory():
    session = IamSession(ec2=Session(pages=ec2_pages))
    (snapshot,) = collect_snapshots(
        AwsCollectionRequest(accounts=(combined_config(),)), context=context(session)
    )
    assert len(snapshot.global_resources) == 3
    for region in ("us-east-1", "us-west-2"):
        (link,) = snapshot.dependencies_by_region[region]
        assert link.attributes["kind"] == "instance_profile"
        assert link.target.kind == "local"
        cited = snapshot.evidence[link.evidence_ids[0]]
        assert cited.configuration_path == "Reservations[0].Instances[0].IamInstanceProfile.Arn"
        assert cited.resource_id == link.source_id
    kinds = {link.attributes["kind"] for link in snapshot.dependencies_by_region["global"]}
    assert kinds == {"profile_role", "role_policy", "permissions_boundary", "trust_principal"}


def test_missing_profile_when_iam_not_selected_is_unresolved():
    session = IamSession(ec2=Session(pages=ec2_pages))
    account = combined_config().model_copy(update={"services": (AwsService.EC2,)})
    (snapshot,) = collect_snapshots(
        AwsCollectionRequest(accounts=(account,)), context=context(session)
    )
    assert snapshot.global_resources == []
    assert session.iam.calls == []
    for region in account.regions:
        (link,) = snapshot.dependencies_by_region[region]
        assert link.target.kind == "unresolved"
