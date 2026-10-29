from archdiscov.graph import GraphIndex
from archdiscov.models.architecture import LocalTarget, UnresolvedTarget
from archdiscov.models.cloud.contracts import CloudExternalTarget
from archdiscov.models.cloud.scope import AzureScope
from archdiscov.models.enums import AttachmentKind, DependencyRelationship, ResourceType
from archdiscov.models.snapshot_access import upgrade_snapshot
from archdiscov.services.snapshots import load_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"


def test_network_relationship_and_kind_filters_preserve_direction():
    index = GraphIndex(load_snapshot(FIXTURES / "network.json"))
    prefix = "123456789012/eu-west-1/ec2/physical/"
    rtb = prefix + "rtb-main"
    routes = index.outgoing_edges(
        rtb,
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
    assert index.outgoing_edges(rtb, relationship=DependencyRelationship.FORWARDS_TO) == ()
    assert index.outgoing_edges(rtb, attachment_kind=AttachmentKind.PROFILE_ROLE) == ()
    (group_edge,) = index.outgoing_edges(
        prefix + "sg-web", attachment_kind=AttachmentKind.SECURITY_GROUP_REFERENCE
    )
    assert group_edge.target_id == prefix + "sg-db"
    assert group_edge.attributes["from_port"] == 5432
    assert index.outgoing_edges("missing") == ()
    assert index.incoming_edges("missing") == ()
    assert index.get_dependency_edge("missing") is None
    assert index.get_terminal("missing") is None


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


def test_external_references_keep_edge_specific_terminals_in_their_own_scope():
    snapshot = upgrade_snapshot(load_snapshot(FIXTURES / "dependency-targets.json"))
    external = next(item for item in snapshot.dependencies if item.target.kind == "external")
    azure = external.model_copy(deep=True)
    target_scope = AzureScope(subscription_id="11111111-1111-1111-1111-111111111111")
    azure.target = CloudExternalTarget(
        scope=target_scope,
        identifier="declared-azure-reference",
        location="eastus",
        service="Microsoft.Compute",
        native_type=ResourceType.AZURE_VIRTUAL_MACHINE,
    )
    snapshot.dependencies.extend([external.model_copy(deep=True), azure])
    index = GraphIndex(snapshot)
    edges = [edge for edge in index.dependency_edges if edge.target == external.target]
    assert len(edges) == 2
    for edge in edges:
        terminal = index.get_terminal(edge.target_id)
        assert terminal.edge_id == edge.id
        assert terminal.target.scope.account_id == "210987654321"
        assert terminal.target.service == "iam"
        assert index.incoming_edges(terminal.id) == (edge,)
    (cross_platform,) = [edge for edge in index.dependency_edges if edge.target == azure.target]
    terminal = index.get_terminal(cross_platform.target_id)
    assert terminal.target.scope == target_scope
    assert terminal.target.location == "eastus"
    assert terminal.target.service == "Microsoft.Compute"
    assert terminal.target.native_type == ResourceType.AZURE_VIRTUAL_MACHINE
    unresolved = next(edge for edge in index.dependency_edges if edge.target.kind == "unresolved")
    assert isinstance(index.get_terminal(unresolved.target_id).target, UnresolvedTarget)


def test_non_enum_attachment_details_are_retained_without_inferred_kind():
    snapshot = upgrade_snapshot(load_snapshot(FIXTURES / "hierarchy.json"))
    snapshot.dependencies[0].attributes = {"kind": "fixture_attachment"}
    index = GraphIndex(snapshot)
    (edge,) = index.dependency_edges
    assert edge.attributes == {"kind": "fixture_attachment"}
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
        assert index.get_dependency_edge(edge.id) == edge
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
        if isinstance(terminal.target, CloudExternalTarget):
            terminal.target.identifier = "changed"
        else:
            terminal.target.source_path = "changed"
    assert index.dependency_edges == expected
    assert tuple(index.get_terminal(key) for key in index.terminal_ids) == terminals
