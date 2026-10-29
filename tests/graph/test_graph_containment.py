from archdiscov.graph import ContainmentEdge, GraphIndex
from archdiscov.services.snapshots import load_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"


def test_aws_containment_has_exact_edges_and_parentless_roots():
    index = GraphIndex(load_snapshot(FIXTURES / "hierarchy.json"))
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
    assert index.parent_id("missing") is None
    assert index.child_ids("missing") == ()


def test_azure_containment_is_structural_not_the_reverse_attachment():
    snapshot = load_snapshot(FIXTURES / "azure.json")
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


def test_missing_parent_context_does_not_create_containment():
    snapshot = load_snapshot(FIXTURES / "hierarchy.json")
    region = next(iter(snapshot.regions.values()))
    vpc = region.resources[0]
    group = next(child for child in vpc.children if child.identity.physical_id == "sg-app")
    vpc.children.remove(group)
    group.attributes["unresolved_parent"] = "vpc-main"
    region.resources.append(group)
    index = GraphIndex(snapshot)
    assert index.parent_id(group.id) is None
    assert group.id in index.root_ids
    assert group.id not in index.child_ids(vpc.id)
