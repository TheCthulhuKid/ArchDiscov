import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from archdiscov.graph import GraphIndex, PhysicalResourceKey
from archdiscov.models.architecture import Ec2Instance, Subnet, Vpc
from archdiscov.models.azure import AzureSubnet, AzureVirtualMachine, AzureVirtualNetwork
from archdiscov.models.cloud_scope import AwsScope, AzureScope
from archdiscov.models.enums import AwsPartition, ResourceType
from archdiscov.services.snapshot_access import SnapshotAccess, upgrade_snapshot
from archdiscov.services.snapshots import load_snapshot, validate_snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "snapshots"


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_fixture_resources_preserve_exact_subtypes_and_data(path):
    snapshot = load_snapshot(path)
    before = snapshot.model_dump(mode="json")
    expected = {r.id: r for r in SnapshotAccess.from_snapshot(snapshot).resources()}
    index = GraphIndex(snapshot)
    assert index.snapshot_id == snapshot.snapshot_id
    assert index.resource_ids == tuple(sorted(expected))
    assert [r.id for r in index.resources()] == list(index.resource_ids)
    for resource_id, resource in expected.items():
        found = index.get_resource(resource_id)
        assert type(found) is type(resource)
        assert found == resource
        key = index.physical_key(resource_id)
        if resource.identity.physical_id is None:
            assert key is None
        else:
            assert resource in index.find_physical(key)
    assert snapshot.model_dump(mode="json") == before


@pytest.mark.parametrize("modern", [False, True])
def test_aws_lookup_keeps_nested_and_scope_wide_resources(modern):
    snapshot = load_snapshot(FIXTURES / "hierarchy.json")
    if modern:
        snapshot = upgrade_snapshot(snapshot)
    index = GraphIndex(snapshot)
    vpc = index.find_physical(
        PhysicalResourceKey.from_values(index.scope, ResourceType.VPC, "vpc-main")
    )[0]
    assert isinstance(vpc, Vpc)
    assert str(vpc.cidr_blocks[0]) == "10.0.0.0/16"
    subnet, unknown = vpc.children
    assert isinstance(subnet, Subnet)
    assert subnet.availability_zone == "eu-west-1a"
    assert isinstance(subnet.children[0], Ec2Instance)
    assert subnet.children[0].instance_type == "t3.micro"
    assert unknown.aws_resource_type == "AWS::EC2::SecurityGroup"
    assert index.physical_key(unknown.id).native_type == "AWS::EC2::SecurityGroup"
    role = index.find_physical(
        PhysicalResourceKey.from_values(index.scope, "AWS::IAM::Role", "role-app")
    )[0]
    assert role.identity.region == "global"


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
    vm = next(r for r in resources if isinstance(r, AzureVirtualMachine))
    assert vm.vm_size is None
    assert vm.fact_evidence == {"/vm_size": ["vm-size"]}


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


def test_ambiguous_physical_lookup_retains_all_candidates_in_stable_order():
    snapshot = ambiguous_snapshot()
    index = GraphIndex(snapshot)
    key = PhysicalResourceKey.from_values(index.scope, ResourceType.VPC, "shared")
    candidates = index.find_physical(key)
    assert len(candidates) == 2
    assert tuple(r.id for r in candidates) == index.resource_ids
    assert [r.identity.region for r in candidates] == ["eu-west-1", "eu-west-2"]
    snapshot.regions = dict(reversed(snapshot.regions.items()))
    assert GraphIndex(snapshot).find_physical(key) == candidates


def test_aws_matching_qualifies_scope_and_native_type_without_case_folding():
    index = GraphIndex(ambiguous_snapshot())
    key = index.physical_key(index.resource_ids[0])
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
    azure_scope.subscription_id = "22222222-2222-2222-2222-222222222222"
    assert dict(key.scope)["subscription_id"] == "11111111-1111-1111-1111-111111111111"
    with pytest.raises(FrozenInstanceError):
        key.physical_id = "changed"


@pytest.mark.parametrize("platform", ["aws", "azure"])
def test_logical_only_resources_are_not_physical_candidates(platform):
    if platform == "aws":
        snapshot = load_snapshot(FIXTURES / "terraform-plan.json")
    else:
        data = json.loads((FIXTURES / "azure.json").read_text())
        vm = data["regions"]["eastus"]["resources"][0]["children"][0]["children"][0]
        vm["identity"].pop("physical_id")
        vm["identity"]["logical_address"] = "template.vm"
        vm["evidence_ids"] = []
        vm["fact_evidence"] = {}
        data["regions"]["eastus"]["resources"] = [vm]
        data["evidence"] = {}
        data["dependencies"] = []
        data["coverage"]["operations"] = []
        snapshot = validate_snapshot(data)
    index = GraphIndex(snapshot)
    resource = index.resources()[0]
    assert index.get_resource(resource.id) == resource
    assert index.physical_key(resource.id) is None
    key = PhysicalResourceKey.from_values(
        index.scope, resource.resource_type, resource.identity.logical_address
    )
    assert index.find_physical(key) == ()


@pytest.mark.parametrize("name", ["hierarchy", "azure"])
def test_input_and_all_returned_mutable_data_are_isolated(name):
    snapshot = load_snapshot(FIXTURES / f"{name}.json")
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
    if name == "hierarchy":
        scope.account_id = "999999999999"
    else:
        scope.subscription_id = "22222222-2222-2222-2222-222222222222"
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
        index.scope
    assert calls == [snapshot.snapshot_id]


def test_invalid_mutated_snapshot_is_revalidated_before_indexing():
    snapshot = load_snapshot(FIXTURES / "hierarchy.json")
    snapshot.regions["eu-west-1"].resources[0].children[0].id = "changed"
    with pytest.raises(ValidationError, match="resource id must match"):
        GraphIndex(snapshot)


def test_missing_lookups_return_explicit_empty_results():
    index = GraphIndex(load_snapshot(FIXTURES / "hierarchy.json"))
    assert index.get_resource("missing") is None
    assert index.physical_key("missing") is None
    assert (
        index.find_physical(
            PhysicalResourceKey.from_values(index.scope, ResourceType.VPC, "missing")
        )
        == ()
    )


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


def test_empty_snapshot_has_no_invented_resources():
    snapshot = ambiguous_snapshot()
    for region in snapshot.regions.values():
        region.resources.clear()
    index = GraphIndex(snapshot)
    assert index.resources() == ()
    assert index.resource_ids == ()
    assert index.scope.account_id == snapshot.account_id
