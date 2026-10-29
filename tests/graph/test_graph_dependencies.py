from dataclasses import FrozenInstanceError

import pytest

from archdiscov.graph import DependencyEdge, GraphIndex, TerminalTarget
from archdiscov.models.architecture import LocalTarget, UnresolvedTarget
from archdiscov.models.cloud.contracts import CloudDependency, CloudEvidence, CloudExternalTarget
from archdiscov.models.cloud.scope import AzureScope
from archdiscov.models.enums import AttachmentKind, DependencyRelationship, ResourceType
from archdiscov.models.snapshot_access import SnapshotAccess, upgrade_snapshot
from archdiscov.services.snapshots import load_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"
K = AttachmentKind
# (fixture, source, kind, stored target or None for a terminal, cited evidence)
NETWORK_RELATIONSHIPS = [
    ("egress-gateways", "nat-public", K.NAT_GATEWAY_SUBNET, "subnet-public", "nat"),
    ("egress-gateways", "nat-public", K.NAT_GATEWAY_INTERFACE, None, "nat-address"),
    (
        "egress-gateways",
        "eigw-main",
        K.EGRESS_ONLY_INTERNET_GATEWAY_ATTACHMENT,
        "vpc-egress",
        "eigw-attachment",
    ),
    ("vpc-peering", "pcx-app-data", K.PEERING_REQUESTER_VPC, "vpc-app", "pcx-requester"),
    ("vpc-peering", "pcx-app-data", K.PEERING_ACCEPTER_VPC, "vpc-data", "pcx-accepter"),
    (
        "transit-gateway",
        "tgw-attach-app",
        K.TRANSIT_GATEWAY_ATTACHMENT,
        "tgw-main",
        "tgw-attach-app",
    ),
    (
        "transit-gateway",
        "tgw-attach-app",
        K.TRANSIT_GATEWAY_VPC_ATTACHMENT,
        "vpc-app",
        "tgw-attach-app",
    ),
    (
        "transit-gateway",
        "tgw-rtb-main",
        K.TRANSIT_GATEWAY_ROUTE_TABLE_ASSOCIATION,
        "tgw-attach-app",
        "tgw-rtb-association",
    ),
    (
        "transit-gateway",
        "tgw-rtb-main",
        K.TRANSIT_GATEWAY_ROUTE_TABLE_PROPAGATION,
        "tgw-attach-data",
        "tgw-rtb-propagation",
    ),
    ("endpoints-and-prefix-lists", "rtb-svc", K.ROUTE_TARGET, "vpce-s3", "rtb-s3"),
    (
        "endpoints-and-prefix-lists",
        "sg-endpoint",
        K.PREFIX_LIST_REFERENCE,
        "pl-office",
        "sg-office",
    ),
    (
        "endpoints-and-prefix-lists",
        "vpce-s3",
        K.ENDPOINT_ROUTE_TABLE,
        "rtb-svc",
        "vpce-s3-route-table",
    ),
    ("endpoints-and-prefix-lists", "vpce-sqs", K.ENDPOINT_SUBNET, "subnet-svc", "vpce-sqs-subnet"),
    (
        "endpoints-and-prefix-lists",
        "vpce-sqs",
        K.ENDPOINT_INTERFACE,
        "eni-vpce",
        "vpce-sqs-interface",
    ),
    (
        "endpoints-and-prefix-lists",
        "vpce-sqs",
        K.ENDPOINT_SECURITY_GROUP,
        "sg-endpoint",
        "vpce-sqs-group",
    ),
]


@pytest.mark.parametrize(
    "fixture",
    ["hierarchy", "network", "iam", "load-balancing", "dependency-targets", "azure"],
)
def test_fixture_dependencies_preserve_context_adjacency_and_upgrade_handles(fixture):
    snapshot = load_snapshot(FIXTURES / f"{fixture}.json")
    before = snapshot.model_dump(mode="json")
    access = SnapshotAccess.from_snapshot(snapshot)
    expected = list(access.dependencies())
    evidence = access.evidence
    index = GraphIndex(snapshot)
    # Azure snapshots are already in the cloud schema; only AWS ones have a legacy form.
    upgraded = index if fixture == "azure" else GraphIndex(upgrade_snapshot(snapshot))
    edges = index.dependency_edges
    assert len(edges) == len(expected)
    assert upgraded.dependency_edges == edges
    assert upgraded.terminal_ids == index.terminal_ids
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
        assert upgraded.outgoing_edges(edge.source_id) == index.outgoing_edges(edge.source_id)
        assert upgraded.incoming_edges(edge.target_id) == index.incoming_edges(edge.target_id)
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


def test_network_element_relationships_are_followed_both_ways_with_their_evidence():
    names = {row[0] for row in NETWORK_RELATIONSHIPS}
    indexes = {name: GraphIndex(load_snapshot(FIXTURES / f"{name}.json")) for name in names}
    prefix = "123456789012/eu-west-1/ec2/physical/"
    for fixture, source, kind, target, evidence_id in NETWORK_RELATIONSHIPS:
        index = indexes[fixture]
        (edge,) = (
            edge
            for edge in index.outgoing_edges(prefix + source, attachment_kind=kind)
            if edge.evidence_ids == (evidence_id,)
        )
        assert index.get_evidence(evidence_id).resource_id == prefix + source
        if target is None:
            assert index.get_resource(edge.target_id) is None
        else:
            assert edge.target_id == prefix + target
        assert edge in index.incoming_edges(edge.target_id, attachment_kind=kind)

    endpoints = indexes["endpoints-and-prefix-lists"]
    (office,) = (
        edge
        for edge in endpoints.outgoing_edges(prefix + "rtb-svc", attachment_kind=K.ROUTE_TARGET)
        if edge.evidence_ids == ("rtb-office",)
    )
    transit_gateway = endpoints.get_terminal(office.target_id).target
    assert isinstance(transit_gateway, CloudExternalTarget)
    assert transit_gateway.identifier == "tgw-main"
    assert transit_gateway.native_type == "AWS::EC2::TransitGateway"
    assert office.attributes["destination"] == "pl-office"

    nat = indexes["egress-gateways"]
    (interface,) = nat.outgoing_edges(
        prefix + "nat-public", attachment_kind=K.NAT_GATEWAY_INTERFACE
    )
    dangling = nat.get_terminal(interface.target_id).target
    assert isinstance(dangling, UnresolvedTarget)
    assert dangling.source_path == "NatGateways[0].NatGatewayAddresses[0].NetworkInterfaceId"
    assert "eni-nat was not present" in dangling.reason


def test_iam_and_listener_edges_are_only_explicit_and_missing_lookups_invent_none():
    iam = upgrade_snapshot(load_snapshot(FIXTURES / "iam.json"))
    role, profile, policy = iam.scope_resources
    empty = GraphIndex(iam)
    assert empty.dependency_edges == ()
    assert empty.terminal_ids == ()
    assert empty.outgoing_edges("missing") == ()
    assert empty.incoming_edges("missing") == ()
    assert empty.get_dependency_edge("missing") is None
    assert empty.get_terminal("missing") is None
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
    assert unresolved.target.source_path == "Instance.SecurityGroupIds[0]"
    assert "could not be evaluated" in unresolved.target.reason


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
    assert reverse in index.incoming_edges(local.source_id)
    assert len(index.incoming_edges(local.source_id)) == 2
    assert len(index.dependency_edges) == 5


@pytest.mark.parametrize("kind", ["fixture_attachment", ["route_target"]])
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
