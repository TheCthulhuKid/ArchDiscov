from dataclasses import FrozenInstanceError

import pytest

from archdiscov.graph import DependencyEdge, GraphIndex, TerminalTarget
from archdiscov.models.architecture import LocalTarget, UnresolvedTarget
from archdiscov.models.cloud.contracts import CloudDependency, CloudEvidence, CloudExternalTarget
from archdiscov.models.cloud.scope import AzureScope
from archdiscov.models.enums import AttachmentKind, DependencyRelationship, ResourceType
from archdiscov.services.snapshot_access import SnapshotAccess, upgrade_snapshot
from archdiscov.services.snapshots import load_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"


@pytest.mark.parametrize("fixture", ["hierarchy", "network", "dependency-targets"])
def test_legacy_and_explicit_schema_three_upgrade_have_identical_edge_handles(fixture):
    legacy = load_snapshot(FIXTURES / f"{fixture}.json")
    old = GraphIndex(legacy)
    modern = GraphIndex(upgrade_snapshot(legacy))
    assert old.dependency_edges == modern.dependency_edges
    assert old.terminal_ids == modern.terminal_ids
    for edge in old.dependency_edges:
        assert old.incoming_edges(edge.target_id) == modern.incoming_edges(edge.target_id)
        assert old.outgoing_edges(edge.source_id) == modern.outgoing_edges(edge.source_id)


@pytest.mark.parametrize(
    "fixture", ["hierarchy", "network", "iam", "load-balancing", "dependency-targets", "azure"]
)
def test_fixture_dependencies_preserve_exact_context_and_reverse_adjacency(fixture):
    snapshot = load_snapshot(FIXTURES / f"{fixture}.json")
    before = snapshot.model_dump(mode="json")
    access = SnapshotAccess.from_snapshot(snapshot)
    expected = list(access.dependencies())
    evidence = access.evidence
    index = GraphIndex(snapshot)
    edges = index.dependency_edges
    assert len(edges) == len(expected)
    assert index.terminal_ids == tuple(sorted(index.terminal_ids))
    for edge in edges:
        assert isinstance(edge, DependencyEdge)
        match = CloudDependency(
            source_id=edge.source_id,
            target=edge.target,
            location=edge.location,
            relationship=edge.relationship,
            attributes=edge.attributes,
            evidence_ids=list(edge.evidence_ids),
        )
        assert match in expected
        expected.remove(match)
        assert edge in index.outgoing_edges(edge.source_id)
        assert edge in index.incoming_edges(edge.target_id)
        assert index.get_dependency_edge(edge.id) == edge
        assert all(evidence[key].resource_id == edge.source_id for key in edge.evidence_ids)
        if isinstance(edge.target, LocalTarget):
            assert index.get_resource(edge.target_id) is not None
            assert index.get_terminal(edge.target_id) is None
        else:
            terminal = index.get_terminal(edge.target_id)
            assert isinstance(terminal, TerminalTarget)
            assert terminal.edge_id == edge.id
            assert terminal.target == match.target
            assert index.get_resource(terminal.id) is None
            assert index.outgoing_edges(terminal.id) == ()
            assert index.incoming_edges(terminal.id) == (edge,)
    assert expected == []
    assert {e.id for e in edges} == {
        e.id for resource_id in index.resource_ids for e in index.outgoing_edges(resource_id)
    }
    assert snapshot.model_dump(mode="json") == before


def test_network_relationship_and_kind_filters_preserve_direction():
    index = GraphIndex(load_snapshot(FIXTURES / "network.json"))
    prefix = "123456789012/eu-west-1/ec2/physical/"
    routes = index.outgoing_edges(
        prefix + "rtb-main",
        relationship=DependencyRelationship.ATTACHED_TO,
        attachment_kind=AttachmentKind.ROUTE_TARGET,
    )
    assert len(routes) == 2
    local = next(edge for edge in routes if isinstance(edge.target, LocalTarget))
    assert local.target_id == prefix + "igw-main"
    assert local.evidence_ids == ("rtb-igw",)
    assert local.attributes == {"kind": "route_target", "target_kind": "internet_gateway"}
    external = next(edge for edge in routes if isinstance(edge.target, CloudExternalTarget))
    assert external.target.identifier == "pcx-1"
    assert external.target.scope.account_id is None
    assert external.target.native_type == "AWS::EC2::VPCPeeringConnection"
    assert index.incoming_edges(local.target_id, attachment_kind=AttachmentKind.ROUTE_TARGET) == (
        local,
    )
    assert index.outgoing_edges(local.target_id) == ()
    assert (
        index.outgoing_edges(prefix + "rtb-main", relationship=DependencyRelationship.FORWARDS_TO)
        == ()
    )
    assert (
        index.outgoing_edges(prefix + "rtb-main", attachment_kind=AttachmentKind.PROFILE_ROLE) == ()
    )
    (group_edge,) = index.outgoing_edges(
        prefix + "sg-web", attachment_kind=AttachmentKind.SECURITY_GROUP_REFERENCE
    )
    assert group_edge.target_id == prefix + "sg-db"
    assert group_edge.attributes["from_port"] == 5432


def test_iam_global_and_listener_forwarding_edges_are_only_explicit_dependencies():
    iam = upgrade_snapshot(load_snapshot(FIXTURES / "iam.json"))
    role, profile, policy = iam.scope_resources
    assert GraphIndex(iam).dependency_edges == ()
    iam.evidence["profile-role"] = CloudEvidence(
        snapshot_id=iam.snapshot_id,
        resource_id=profile.id,
        scope=iam.scope,
        service="iam",
        source="mock",
        locator="iam.json",
        configuration_path="InstanceProfiles[0].Roles[0].Arn",
        value=role.arn,
    )
    iam.dependencies.append(
        CloudDependency(
            source_id=profile.id,
            target=LocalTarget(resource_id=role.id),
            relationship=DependencyRelationship.ATTACHED_TO,
            attributes={"kind": AttachmentKind.PROFILE_ROLE},
            evidence_ids=["profile-role"],
        )
    )
    index = GraphIndex(iam)
    (edge,) = index.outgoing_edges(profile.id, attachment_kind=AttachmentKind.PROFILE_ROLE)
    assert edge.location is None
    assert edge.target_id == role.id
    assert index.incoming_edges(role.id) == (edge,)
    assert index.incoming_edges(policy.id) == ()

    snapshot = upgrade_snapshot(load_snapshot(FIXTURES / "load-balancing.json"))
    lb, group = snapshot.regions["eu-west-1"].resources
    listener = lb.children[0]
    assert GraphIndex(snapshot).dependency_edges == ()
    snapshot.dependencies.append(
        CloudDependency(
            source_id=listener.id,
            target=LocalTarget(resource_id=group.id),
            location="eu-west-1",
            relationship=DependencyRelationship.FORWARDS_TO,
            attributes={"weight": 100, "action_order": 1},
            evidence_ids=["listener-action"],
        )
    )
    index = GraphIndex(snapshot)
    (edge,) = index.outgoing_edges(listener.id, relationship=DependencyRelationship.FORWARDS_TO)
    assert edge.target_id == group.id
    assert edge.attributes == {"weight": 100, "action_order": 1}
    assert edge.evidence_ids == ("listener-action",)
    assert index.incoming_edges(group.id) == (edge,)
    assert index.outgoing_edges(lb.id) == ()


def test_parallel_edges_and_identical_occurrences_are_not_collapsed():
    snapshot = upgrade_snapshot(load_snapshot(FIXTURES / "network.json"))
    route = next(item for item in snapshot.dependencies if item.evidence_ids == ["rtb-igw"])
    parallel = route.model_copy(deep=True)
    parallel.evidence_ids = ["rtb"]
    parallel.attributes["ports"] = [80, 443]
    snapshot.dependencies.extend([parallel, route.model_copy(deep=True)])
    index = GraphIndex(snapshot)
    edges = index.incoming_edges(route.target.resource_id)
    assert len(edges) == 3
    assert len({edge.id for edge in edges}) == 3
    assert [edge.evidence_ids for edge in edges].count(("rtb-igw",)) == 2
    assert [edge.evidence_ids for edge in edges].count(("rtb",)) == 1
    snapshot.dependencies.reverse()
    reordered = GraphIndex(snapshot)
    assert reordered.dependency_edges == index.dependency_edges
    assert reordered.terminal_ids == index.terminal_ids
    assert reordered.incoming_edges(route.target.resource_id) == edges


def test_repeated_external_references_keep_edge_specific_terminals():
    snapshot = upgrade_snapshot(load_snapshot(FIXTURES / "dependency-targets.json"))
    external = next(item for item in snapshot.dependencies if item.target.kind == "external")
    snapshot.dependencies.append(external.model_copy(deep=True))
    index = GraphIndex(snapshot)
    edges = [edge for edge in index.dependency_edges if edge.target.kind == "external"]
    assert len(edges) == 2
    assert edges[0].target == edges[1].target
    assert edges[0].target_id != edges[1].target_id
    for edge in edges:
        terminal = index.get_terminal(edge.target_id)
        assert terminal.edge_id == edge.id
        assert terminal.target.scope.account_id == "210987654321"
        assert terminal.target.service == "iam"
        assert index.incoming_edges(terminal.id) == (edge,)
    unresolved = next(edge for edge in index.dependency_edges if edge.target.kind == "unresolved")
    assert isinstance(index.get_terminal(unresolved.target_id).target, UnresolvedTarget)
    assert unresolved.target.source_path == "Instance.SecurityGroupIds[0]"
    assert "could not be evaluated" in unresolved.target.reason


def test_cross_platform_terminal_retains_its_own_scope():
    snapshot = upgrade_snapshot(load_snapshot(FIXTURES / "dependency-targets.json"))
    external = next(item for item in snapshot.dependencies if item.target.kind == "external")
    target_scope = AzureScope(subscription_id="11111111-1111-1111-1111-111111111111")
    external.target = CloudExternalTarget(
        scope=target_scope,
        identifier="declared-azure-reference",
        location="eastus",
        service="Microsoft.Compute",
        native_type=ResourceType.AZURE_VIRTUAL_MACHINE,
    )
    index = GraphIndex(snapshot)
    terminal = next(
        terminal
        for terminal in (index.get_terminal(key) for key in index.terminal_ids)
        if terminal.target.kind == "external"
    )
    assert terminal.target.scope == target_scope
    assert terminal.target.location == "eastus"
    assert terminal.target.service == "Microsoft.Compute"
    assert terminal.target.native_type == ResourceType.AZURE_VIRTUAL_MACHINE
    assert index.get_resource(terminal.id) is None


def test_dependency_cycles_and_self_edges_are_indexed_without_traversal():
    snapshot = upgrade_snapshot(load_snapshot(FIXTURES / "dependency-targets.json"))
    local = next(item for item in snapshot.dependencies if item.target.kind == "local")
    snapshot.dependencies.extend(
        [
            CloudDependency(
                source_id=local.target.resource_id,
                target=LocalTarget(resource_id=local.source_id),
                relationship=DependencyRelationship.ATTACHED_TO,
                evidence_ids=["role"],
            ),
            CloudDependency(
                source_id=local.source_id,
                target=LocalTarget(resource_id=local.source_id),
                location="eu-west-1",
                relationship=DependencyRelationship.ATTACHED_TO,
                evidence_ids=["instance"],
            ),
        ]
    )
    index = GraphIndex(snapshot)
    (reverse,) = index.outgoing_edges(local.target.resource_id)
    assert reverse.target_id == local.source_id
    assert reverse in index.incoming_edges(local.source_id)
    assert len(index.incoming_edges(local.source_id)) == 2
    assert len(index.dependency_edges) == 5


@pytest.mark.parametrize(
    "kind", [None, "fixture_attachment", ["route_target"], {"detail": "custom"}]
)
def test_non_enum_attachment_details_are_retained_without_inferred_kind(kind):
    snapshot = upgrade_snapshot(load_snapshot(FIXTURES / "hierarchy.json"))
    snapshot.dependencies[0].attributes = {"kind": kind}
    index = GraphIndex(snapshot)
    (edge,) = index.dependency_edges
    assert edge.attributes == {"kind": kind}
    assert edge.attachment_kind is None
    assert index.outgoing_edges(edge.source_id) == (edge,)
    assert index.outgoing_edges(edge.source_id, attachment_kind=AttachmentKind.PROFILE_ROLE) == ()


def test_dependency_and_terminal_results_are_deeply_isolated():
    snapshot = upgrade_snapshot(load_snapshot(FIXTURES / "dependency-targets.json"))
    for item in snapshot.dependencies:
        item.attributes["detail"] = {"ports": [80]}
    index = GraphIndex(snapshot)
    expected = index.dependency_edges
    terminals = tuple(index.get_terminal(key) for key in index.terminal_ids)
    snapshot.dependencies.clear()
    for edge in index.dependency_edges:
        with pytest.raises(FrozenInstanceError):
            edge.source_id = "changed"
        for copy in (
            edge,
            index.get_dependency_edge(edge.id),
            *index.outgoing_edges(edge.source_id),
            *index.incoming_edges(edge.target_id),
        ):
            copy.attributes["detail"]["ports"].append(443)
            if isinstance(copy.target, LocalTarget):
                copy.target.resource_id = "changed"
            elif isinstance(copy.target, CloudExternalTarget):
                copy.target.scope.account_id = "999999999999"
            else:
                copy.target.reason = "changed"
    for key in index.terminal_ids:
        terminal = index.get_terminal(key)
        with pytest.raises(FrozenInstanceError):
            terminal.edge_id = "changed"
        if isinstance(terminal.target, CloudExternalTarget):
            terminal.target.identifier = "changed"
        else:
            terminal.target.source_path = "changed"
    assert index.dependency_edges == expected
    assert tuple(index.get_terminal(key) for key in index.terminal_ids) == terminals


def test_empty_and_missing_lookups_do_not_invent_edges_or_terminals():
    index = GraphIndex(load_snapshot(FIXTURES / "iam.json"))
    assert index.dependency_edges == ()
    assert index.terminal_ids == ()
    assert index.outgoing_edges("missing") == ()
    assert index.incoming_edges("missing") == ()
    assert index.get_dependency_edge("missing") is None
    assert index.get_terminal("missing") is None
