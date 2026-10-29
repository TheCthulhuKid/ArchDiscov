import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from archdiscov.graph import GraphIndex, PhysicalResourceKey
from archdiscov.models.architecture import Subnet, Vpc
from archdiscov.models.cloud.scope import AwsScope, AzureScope
from archdiscov.models.enums import AwsPartition, ResourceType
from archdiscov.models.resources.azure import AzureSubnet, AzureVirtualMachine, AzureVirtualNetwork
from archdiscov.models.snapshot_access import SnapshotAccess, upgrade_snapshot
from archdiscov.services.snapshots import load_snapshot, validate_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"


def test_aws_lookup_keeps_nested_and_scope_wide_resources():
    index = GraphIndex(load_snapshot(FIXTURES / "hierarchy.json"))
    vpc = index.find_physical(
        PhysicalResourceKey.from_values(index.scope, ResourceType.VPC, "vpc-main")
    )[0]
    assert isinstance(vpc, Vpc)
    subnet, unknown = vpc.children
    assert isinstance(subnet, Subnet)
    assert index.physical_key(unknown.id).native_type == "AWS::EC2::SecurityGroup"
    role = index.find_physical(
        PhysicalResourceKey.from_values(index.scope, "AWS::IAM::Role", "role-app")
    )[0]
    assert role.identity.region == "global"
    assert index.get_resource("missing") is None
    assert index.physical_key("missing") is None
    assert (
        index.find_physical(
            PhysicalResourceKey.from_values(index.scope, ResourceType.VPC, "missing")
        )
        == ()
    )


def test_azure_lookup_reuses_arm_matching_and_preserves_source_spelling():
    index = GraphIndex(load_snapshot(FIXTURES / "azure.json"))
    resources = index.resources()
    assert {type(r) for r in resources} == {AzureVirtualNetwork, AzureSubnet, AzureVirtualMachine}
    for resource in resources:
        key = PhysicalResourceKey.from_values(
            index.scope,
            resource.identity.native_type.upper(),
            resource.identity.physical_id.upper(),
        )
        assert key == index.physical_key(resource.id)
        assert index.find_physical(key) == (resource,)
        assert "Microsoft." in index.find_physical(key)[0].identity.physical_id


def ambiguous_snapshot():
    data = json.loads((FIXTURES / "hierarchy.json").read_text())
    data["evidence"] = {}
    data["dependencies_by_region"] = {}
    data["global_resources"] = []
    data["coverage"]["regions"] = ["eu-west-1", "eu-west-2"]
    data["regions"] = {
        region: {
            "resources": [
                {
                    "identity": {
                        "account_id": data["account_id"],
                        "region": region,
                        "service": "ec2",
                        "physical_id": "shared",
                    },
                    "resource_type": "AWS::EC2::VPC",
                }
            ]
        }
        for region in ("eu-west-2", "eu-west-1")
    }
    return validate_snapshot(data)


def test_ambiguous_aws_lookup_keeps_all_candidates_without_case_folding():
    index = GraphIndex(ambiguous_snapshot())
    key = PhysicalResourceKey.from_values(index.scope, ResourceType.VPC, "shared")
    candidates = index.find_physical(key)
    assert tuple(r.id for r in candidates) == index.resource_ids
    assert [r.identity.region for r in candidates] == ["eu-west-1", "eu-west-2"]
    for other in (
        replace(key, native_type="AWS::EC2::Subnet"),
        replace(key, physical_id="SHARED"),
        PhysicalResourceKey.from_values(
            AwsScope(account_id="999999999999"), key.native_type, "shared"
        ),
        PhysicalResourceKey.from_values(
            AwsScope(account_id="123456789012", partition=AwsPartition.AWS_CN),
            key.native_type,
            "shared",
        ),
    ):
        assert index.find_physical(other) == ()


def test_azure_keys_keep_subscription_tenant_and_platform_context():
    azure_scope = AzureScope(subscription_id="11111111-1111-1111-1111-111111111111")
    key = PhysicalResourceKey.from_values(
        azure_scope, "Microsoft.Compute/virtualMachines", "shared"
    )
    assert key != PhysicalResourceKey.from_values(
        AzureScope(subscription_id="22222222-2222-2222-2222-222222222222"),
        key.native_type,
        "shared",
    )
    assert key != PhysicalResourceKey.from_values(
        AzureScope(
            subscription_id=azure_scope.subscription_id, tenant_id=azure_scope.subscription_id
        ),
        key.native_type,
        "shared",
    )
    assert key != PhysicalResourceKey.from_values(
        AwsScope(account_id="123456789012"), key.native_type, "shared"
    )


def test_logical_only_resources_are_not_physical_candidates():
    index = GraphIndex(load_snapshot(FIXTURES / "terraform-plan.json"))
    resource = index.resources()[0]
    assert index.get_resource(resource.id) == resource
    assert index.physical_key(resource.id) is None
    key = PhysicalResourceKey.from_values(
        index.scope, resource.resource_type, resource.identity.logical_address
    )
    assert index.find_physical(key) == ()


def test_input_and_all_returned_mutable_data_are_isolated():
    snapshot = load_snapshot(FIXTURES / "hierarchy.json")
    index = GraphIndex(snapshot)
    expected = index.resources()
    root = next(r for r in expected if r.children)
    resource_id = root.id
    key = index.physical_key(resource_id)
    input_root = next(iter(snapshot.regions.values())).resources[0]
    input_root.children[0].attributes["input_changed"] = {"nested": [1]}
    input_root.identity.physical_id = "changed"
    snapshot.regions.clear()
    for returned in (
        index.get_resource(resource_id),
        index.find_physical(key)[0],
        next(r for r in index.resources() if r.id == resource_id),
    ):
        returned.children[0].attributes["changed"] = [1]
        returned.children.clear()
        returned.attributes["changed"] = {"nested": True}
        returned.fact_evidence.clear()
        returned.identity.physical_id = "changed"
        returned.id = "changed"
    scope = index.scope
    scope.account_id = "999999999999"
    assert index.resources() == expected
    assert index.get_resource(resource_id) == root
    assert index.find_physical(key) == (root,)
    assert index.scope != scope


def test_lookup_never_revalidates_or_rebuilds_access(monkeypatch):
    snapshot = load_snapshot(FIXTURES / "hierarchy.json")
    original = SnapshotAccess.from_snapshot
    calls = []

    def counted(snapshot):
        calls.append(snapshot.snapshot_id)
        return original(snapshot)

    monkeypatch.setattr(SnapshotAccess, "from_snapshot", counted)
    index = GraphIndex(snapshot)
    for _ in range(3):
        for resource_id in index.resource_ids:
            index.get_resource(resource_id)
            index.find_physical(index.physical_key(resource_id))
        index.resources()
        _ = index.scope
    assert calls == [snapshot.snapshot_id]


def test_invalid_mutated_snapshot_is_revalidated_before_indexing():
    snapshot = load_snapshot(FIXTURES / "hierarchy.json")
    snapshot.regions["eu-west-1"].resources[0].children[0].id = "changed"
    with pytest.raises(ValidationError, match="resource id must match"):
        GraphIndex(snapshot)


def test_schema3_unknown_resources_are_indexed_by_native_type():
    snapshot = upgrade_snapshot(load_snapshot(FIXTURES / "hierarchy.json"))
    data = snapshot.model_dump(mode="json")
    role = data["scope_resources"][0]
    role["resource_type"] = "cloud_unknown"
    role["platform"] = "aws"
    role["native_type"] = role.pop("aws_resource_type")
    index = GraphIndex(validate_snapshot(data))
    key = PhysicalResourceKey.from_values(index.scope, ResourceType.IAM_ROLE, "role-app")
    assert index.find_physical(key)[0].native_type == ResourceType.IAM_ROLE
    assert index.find_physical(replace(key, native_type="cloud_unknown")) == ()
