"""VPC peering connections as route targets: one hop into the peer VPC.

A route to a peering connection leads into the VPC on its other side when the connection is
active, joins the subnet's VPC to that VPC within one account, and records CIDRs on that side
holding the address. Peering is not transitive, so the hop ends in the peer VPC. Another
account's side is unknown: snapshots of several accounts are not composed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from archdiscov.models.architecture import Vpc
from archdiscov.models.enums import AttachmentKind
from archdiscov.models.network_path import NetworkResult
from archdiscov.models.resources.network import PeeringSide, VpcPeeringConnection

from .network_decisions import missing
from .network_matching import Undecided, cited_target, parse_network, screen_cidr

if TYPE_CHECKING:
    from archdiscov.graph import DependencyEdge

    from .network_routes import RouteHop

PEERING_OPERATIONS = frozenset({"DescribeVpcPeeringConnections"})
_ELEMENT = "vpc_peering_connection"
# Status codes from the VpcPeeringConnectionStateReason API reference: only an active
# connection carries traffic.
_STATES: dict[str, bool | None] = {
    "active": True,
    **dict.fromkeys(
        (
            "initiating-request",
            "pending-acceptance",
            "provisioning",
            "rejected",
            "failed",
            "expired",
            "deleting",
            "deleted",
        ),
        False,
    ),
}
_SIDES = {
    "requester": AttachmentKind.PEERING_REQUESTER_VPC,
    "accepter": AttachmentKind.PEERING_ACCEPTER_VPC,
}


def _side_edges(hop: RouteHop, connection: VpcPeeringConnection) -> dict[str, DependencyEdge]:
    """Each side's recorded VPC edge, by side name."""
    edges = {}
    for name, kind in _SIDES.items():
        found = hop.index.outgoing_edges(connection.id, attachment_kind=kind)
        if len(found) == 1:
            edges[name] = found[0]
    return edges


def _outside(own: PeeringSide, other: PeeringSide, connection: VpcPeeringConnection) -> str:
    """Why the peer side cannot be decided: another account's VPC, or one not recorded."""
    vpc = other.vpc_id or "on the other side"
    if other.owner_id is not None and other.owner_id != own.owner_id:
        return (
            f"Peering connection {connection.identity.physical_id} leads to VPC {vpc} in "
            f"account {other.owner_id}, outside this snapshot."
        )
    return f"VPC {vpc} of peering connection {connection.identity.physical_id} is not recorded."


def _holds_address(hop: RouteHop, side: PeeringSide) -> bool | Undecided:
    """Whether the side's recorded CIDRs hold the whole address, as routes require."""
    if not side.cidr_blocks:
        return Undecided.missing("The peer side's CIDRs are not recorded.")
    network = parse_network(hop.address)
    screens = [
        screen_cidr(cidr, network, "The address is unknown.", bounded=True)
        for cidr in side.cidr_blocks
    ]
    if any(screen is True for screen in screens):
        return True
    return next((s for s in screens if isinstance(s, Undecided)), False)


def peering_evidence(vpc_id: str, hop: RouteHop) -> tuple[str, ...] | NetworkResult:
    """Evidence that the route's peering connection leads the address into `vpc_id`.

    Cites the connection's state and both sides, and its two VPC edges; otherwise the
    result that ends the hop.
    """
    index = hop.index
    connection = cited_target(index, hop.table.id, AttachmentKind.ROUTE_TARGET, hop.citations)
    if not isinstance(connection, VpcPeeringConnection):
        return missing("The route's peering connection is not recorded.", element=hop.where)
    state = connection.state
    if problem := hop.state_result(connection, state, _STATES, "peering connection", _ELEMENT):
        return problem
    edges = _side_edges(hop, connection)
    own_vpc_id = index.parent_id(hop.subnet.id)
    own = next((name for name, edge in edges.items() if edge.target_id == own_vpc_id), None)
    if own is None or len(edges) != 2:
        return missing(
            "The peering connection does not record the subnet's VPC and a peer side.",
            element=_ELEMENT,
            resource_id=connection.id,
        )
    other = "accepter" if own == "requester" else "requester"
    own_side, other_side = getattr(connection, own), getattr(connection, other)
    peer = index.get_resource(edges[other].target_id)
    if not isinstance(peer, Vpc) or other_side.owner_id not in (None, own_side.owner_id):
        return missing(
            _outside(own_side, other_side, connection), element=_ELEMENT, resource_id=connection.id
        )
    if peer.id != vpc_id:
        message = f"The peering connection leads to {peer.identity.physical_id}, not the VPC asked."
        return hop.denied(connection, message, _ELEMENT, f"/{other}")
    holds = _holds_address(hop, other_side)
    if isinstance(holds, Undecided):
        return holds.result(element=_ELEMENT, resource_id=connection.id)
    if not holds:
        message = "The peer VPC's recorded CIDRs do not hold the address."
        return hop.denied(connection, message, _ELEMENT, f"/{other}")
    facts = [
        evidence_id
        for pointer in ("/state", "/requester", "/accepter")
        for evidence_id in connection.fact_evidence.get(pointer, [])
    ]
    return (*facts, *(evidence_id for edge in edges.values() for evidence_id in edge.evidence_ids))
