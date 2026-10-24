from dataclasses import FrozenInstanceError

import pytest

from archdiscov.graph import ContainmentEdge, GraphIndex
from archdiscov.models.architecture import LocalTarget
from archdiscov.models.snapshot_access import upgrade_snapshot
from archdiscov.services.snapshots import load_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"


@pytest.mark.parametrize("version", ["1", "2", "3"])
def test_aws_containment_has_exact_edges_and_parentless_roots(version):
    snapshot = load_snapshot(FIXTURES / "hierarchy.json")
    if version == "3":
        snapshot = upgrade_snapshot(snapshot)
    else:
        snapshot.schema_version = version
        if version == "2":
            for dependencies in snapshot.dependencies_by_region.values():
                for dependency in dependencies:
                    dependency.target = LocalTarget(resource_id=dependency.target_id)
                    dependency.target_id = None
    before = snapshot.model_dump(mode="json")
    index = GraphIndex(snapshot)
    prefix = "123456789012/eu-west-1/ec2/physical/"
    vpc, subnet, instance, group = (
        prefix + name for name in ("vpc-main", "subnet-app", "i-web", "sg-app")
    )
    role = "123456789012/global/iam/physical/role-app"
    pairs = sorted([(vpc, subnet), (vpc, group), (subnet, instance)])
    assert index.containment_edges == tuple(ContainmentEdge(*pair) for pair in pairs)
    assert index.root_ids == tuple(sorted((vpc, role)))
    assert index.child_ids(vpc) == tuple(sorted((subnet, group)))
    assert index.child_ids(subnet) == (instance,)
    assert index.child_ids(instance) == ()
    assert index.child_ids(role) == ()
    for source, target in pairs:
        assert index.parent_id(target) == source
        assert target in index.child_ids(source)
    assert index.parent_id(vpc) is None
    assert index.parent_id(role) is None
    assert all(edge.evidence_ids == () for edge in index.containment_edges)
    assert snapshot.model_dump(mode="json") == before


def test_azure_containment_is_structural_not_the_reverse_attachment():
    snapshot = load_snapshot(FIXTURES / "azure.json")
    before = snapshot.model_dump(mode="json")
    network = snapshot.regions["eastus"].resources[0]
    subnet = network.children[0]
    vm = subnet.children[0]
    index = GraphIndex(snapshot)
    assert index.containment_edges == tuple(
        sorted(
            (ContainmentEdge(network.id, subnet.id), ContainmentEdge(subnet.id, vm.id)),
            key=lambda edge: (edge.source_id, edge.target_id),
        )
    )
    assert index.root_ids == (network.id,)
    assert index.parent_id(network.id) is None
    assert index.parent_id(subnet.id) == network.id
    assert index.parent_id(vm.id) == subnet.id
    assert index.child_ids(vm.id) == ()
    assert ContainmentEdge(vm.id, subnet.id) not in index.containment_edges
    assert all(edge.evidence_ids == () for edge in index.containment_edges)
    assert snapshot.model_dump(mode="json") == before


def test_scope_wide_children_are_indexed_without_an_invented_envelope_parent():
    snapshot = load_snapshot(FIXTURES / "hierarchy.json")
    role = snapshot.global_resources[0]
    child = role.model_copy(deep=True)
    child.identity.physical_id = "role-child"
    child.id = child.identity.stable_id
    role.children.append(child)
    index = GraphIndex(snapshot)
    assert index.parent_id(role.id) is None
    assert index.parent_id(child.id) == role.id
    assert index.child_ids(role.id) == (child.id,)
    assert ContainmentEdge(role.id, child.id) in index.containment_edges
    assert role.id in index.root_ids
    assert child.id not in index.root_ids


@pytest.mark.parametrize("reverse_children", [False, True])
def test_missing_parent_context_does_not_create_containment(reverse_children):
    snapshot = load_snapshot(FIXTURES / "hierarchy.json")
    region = next(iter(snapshot.regions.values()))
    vpc = region.resources[0]
    if reverse_children:
        vpc.children.reverse()
    group = next(child for child in vpc.children if child.identity.physical_id == "sg-app")
    vpc.children.remove(group)
    group.attributes["unresolved_parent"] = "vpc-main"
    region.resources.append(group)
    index = GraphIndex(snapshot)
    assert index.parent_id(group.id) is None
    assert group.id in index.root_ids
    assert group.id not in index.child_ids(vpc.id)


def test_containment_order_is_independent_of_stored_child_and_root_order():
    snapshot = load_snapshot(FIXTURES / "hierarchy.json")
    region = next(iter(snapshot.regions.values()))
    region.resources.append(region.resources[0].children.pop())
    expected = GraphIndex(snapshot)
    region.resources.reverse()
    for resource in region.resources:
        resource.children.reverse()
    actual = GraphIndex(snapshot)
    assert actual.containment_edges == expected.containment_edges
    assert actual.root_ids == expected.root_ids
    for resource_id in expected.resource_ids:
        assert actual.parent_id(resource_id) == expected.parent_id(resource_id)
        assert actual.child_ids(resource_id) == expected.child_ids(resource_id)


def test_containment_remains_isolated_from_input_and_returned_resource_mutations():
    snapshot = load_snapshot(FIXTURES / "hierarchy.json")
    vpc = next(iter(snapshot.regions.values())).resources[0]
    index = GraphIndex(snapshot)
    edges, children, roots = index.containment_edges, index.child_ids(vpc.id), index.root_ids
    vpc.children.clear()
    index.get_resource(vpc.id).children.clear()
    for resource in index.resources():
        resource.children.clear()
    assert index.containment_edges == edges
    assert index.child_ids(vpc.id) == children
    assert index.root_ids == roots
    with pytest.raises(FrozenInstanceError):
        edges[0].source_id = "changed"


def test_absent_resource_lookups_are_empty():
    index = GraphIndex(load_snapshot(FIXTURES / "hierarchy.json"))
    assert index.parent_id("missing") is None
    assert index.child_ids("missing") == ()


def test_empty_snapshot_has_no_containment_or_roots():
    snapshot = load_snapshot(FIXTURES / "hierarchy.json")
    snapshot.regions.clear()
    snapshot.global_resources.clear()
    snapshot.evidence.clear()
    snapshot.dependencies_by_region.clear()
    index = GraphIndex(snapshot)
    assert index.containment_edges == ()
    assert index.root_ids == ()
