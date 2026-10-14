from botocore.exceptions import ClientError, EndpointConnectionError

from archdiscov.ingestion.iam.collector import collect_inventory
from archdiscov.ingestion.operations import IamOperation as Op

from .iam_data import POLICY_ARN, IamClient, IamSession, config, context, responses


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


def test_failed_role_detail_preserves_inventory_and_qualifies_policy_discovery():
    data = responses()
    data["get_role"] = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "secret"}}, "GetRole"
    )
    inventory, _ = collect(data)
    assert len(inventory.results[Op.LIST_ROLES].responses) == 1
    assert inventory.results[Op.GET_ROLE].coverage.status == "failed"
    assert inventory.results[Op.GET_POLICY].coverage.status == "incomplete"
    assert inventory.results[Op.GET_POLICY_VERSION].coverage.status == "incomplete"
    assert "secret" not in inventory.results[Op.GET_ROLE].coverage.model_dump_json()


def test_empty_discovery_is_successful_zero_and_failed_discovery_skips_details():
    data = responses()
    data["list_roles"] = {"Roles": []}
    inventory, calls = collect(data)
    assert all(r.coverage.status == "success" for r in inventory.results.values())
    assert inventory.results[Op.GET_ROLE].coverage.result_count == 0
    assert len(calls) == 2
    data["list_roles"] = EndpointConnectionError(endpoint_url="secret")
    inventory, calls = collect(data)
    assert inventory.results[Op.GET_ROLE].coverage.status == "skipped"
    assert inventory.results[Op.GET_POLICY_VERSION].coverage.status == "skipped"
    assert len(calls) == 2
