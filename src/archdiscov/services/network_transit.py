"""Transit gateways as route targets: one hop through a gateway into another VPC.

A route to a transit gateway leads into a VPC when the gateway is stored, owned by the
subnet's account, and available. The subnet's VPC attachment to it must be available with a
recorded subnet in the source subnet's Availability Zone, since only those zones reach the
gateway. That attachment's associated route table must record its whole route search, and
the table's longest-prefix route for the address must be active and name one available VPC
attachment. The gateway delivers into any zone of the destination VPC, so the destination's
subnets are not checked. A deeper chain (a peering, VPN, or Direct Connect attachment,
another VPC's attachment, several attachments, or a prefix-list route) is unknown.
"""

from __future__ import annotations

from ipaddress import ip_network
from typing import TYPE_CHECKING

from archdiscov.models.architecture import Subnet
from archdiscov.models.cloud.contracts import AwsTargetScope, CloudExternalTarget
from archdiscov.models.enums import AttachmentKind, TransitGatewayRouteType
from archdiscov.models.network_path import NetworkResult
from archdiscov.models.resources.network import (
    TransitGateway,
    TransitGatewayAttachment,
    TransitGatewayRouteTable,
)

from .network_decisions import missing
from .network_matching import (
    IpNetwork,
    Undecided,
    cited_target,
    longest_prefix,
    omitted_rules,
    parse_network,
)

if TYPE_CHECKING:
    from archdiscov.graph import DependencyEdge, GraphIndex
    from archdiscov.models.resource_base import Resource

    from .network_routes import RouteHop

TRANSIT_OPERATIONS = frozenset(
    {
        "DescribeTransitGateways",
        "DescribeTransitGatewayAttachments",
        "DescribeTransitGatewayVpcAttachments",
        "DescribeTransitGatewayRouteTables",
        "GetTransitGatewayRouteTableAssociations",
        "SearchTransitGatewayRoutes",
    }
)
_GATEWAY = "transit_gateway"
_ATTACHMENT = "transit_gateway_attachment"
_TABLE = "transit_gateway_route_table"
# States from the EC2 API reference (TransitGateway, TransitGatewayAttachment,
# TransitGatewayRouteTable, TransitGatewayRoute): True carries traffic, False denies it,
# and None is a transition whose outcome is not yet known.
_GATEWAY_STATES: dict[str, bool | None] = {
    "available": True,
    "pending": None,
    "modifying": None,
    "deleting": False,
    "deleted": False,
}
_ATTACHMENT_STATES: dict[str, bool | None] = {
    "available": True,
    "pending": None,
    "modifying": None,
    **dict.fromkeys(
        (
            "initiating",
            "initiatingRequest",
            "pendingAcceptance",
            "rollingBack",
            "rejecting",
            "rejected",
            "failing",
            "failed",
            "deleting",
            "deleted",
        ),
        False,
    ),
}
_TABLE_STATES: dict[str, bool | None] = {
    "available": True,
    "pending": None,
    "deleting": False,
    "deleted": False,
}
_ROUTE_STATES: dict[str, bool | None] = {
    "active": True,
    "pending": None,
    "blackhole": False,
    "deleting": False,
    "deleted": False,
}

type _Hop[T] = tuple[T, tuple[str, ...]] | NetworkResult


def _facts(resource: Resource, *pointers: str) -> tuple[str, ...]:
    return tuple(e for pointer in pointers for e in resource.fact_evidence.get(pointer, []))


def _outside(label: str, name: str, owner: str | None) -> str:
    where = f" in account {owner}" if owner else ""
    return f"The {label} {name}{where} is outside this snapshot."


def _foreign(hop: RouteHop, resource: Resource, owner: str | None, label: str) -> str | None:
    """Why another account's element cannot be decided; None when it is the subnet's."""
    if owner is None:
        return f"The owner of the {label} {resource.identity.physical_id} is not recorded."
    if owner != hop.subnet.identity.account_id:
        return _outside(label, resource.identity.physical_id or resource.id, owner)
    return None


def _gateway(hop: RouteHop) -> TransitGateway | NetworkResult:
    """The route's stored gateway of the subnet's account, in a carrying state."""
    index, table = hop.index, hop.table
    gateway = cited_target(index, table.id, AttachmentKind.ROUTE_TARGET, hop.citations)
    if not isinstance(gateway, TransitGateway):
        external = [
            edge.target
            for edge in index.outgoing_edges(table.id, attachment_kind=AttachmentKind.ROUTE_TARGET)
            if set(edge.evidence_ids) & set(hop.citations)
            and isinstance(edge.target, CloudExternalTarget)
        ]
        if len(external) == 1:
            target = external[0]
            owner = target.scope.account_id if isinstance(target.scope, AwsTargetScope) else None
            message = _outside("transit gateway", target.identifier, owner)
            return missing(message, element=hop.where, resource_id=table.id)
        return missing("The route's transit gateway is not recorded.", element=hop.where)
    if problem := _foreign(hop, gateway, gateway.owner_id, "transit gateway"):
        return missing(problem, element=_GATEWAY, resource_id=gateway.id)
    states = _GATEWAY_STATES
    if problem := hop.state_result(gateway, gateway.state, states, "transit gateway", _GATEWAY):
        return problem
    return gateway


def _vpc_edge(index: GraphIndex, attachment: TransitGatewayAttachment) -> DependencyEdge | None:
    edges = index.outgoing_edges(
        attachment.id, attachment_kind=AttachmentKind.TRANSIT_GATEWAY_VPC_ATTACHMENT
    )
    return edges[0] if len(edges) == 1 else None


def _attachments(
    index: GraphIndex, gateway: TransitGateway
) -> list[tuple[TransitGatewayAttachment, DependencyEdge]]:
    """The gateway's stored attachments, each with its edge to the gateway."""
    found = []
    for edge in index.incoming_edges(
        gateway.id, attachment_kind=AttachmentKind.TRANSIT_GATEWAY_ATTACHMENT
    ):
        attachment = index.get_resource(edge.source_id)
        if isinstance(attachment, TransitGatewayAttachment):
            found.append((attachment, edge))
    return found


def _unusable(
    hop: RouteHop, attachment: TransitGatewayAttachment, trail: tuple[str, ...]
) -> NetworkResult | None:
    """Why the attachment cannot carry the hop: another account's, or not carrying."""
    owner = attachment.attached_resource_owner_id
    if problem := _foreign(hop, attachment, owner, "transit gateway attachment"):
        return missing(problem, element=_ATTACHMENT, resource_id=attachment.id)
    return hop.state_result(
        attachment,
        attachment.state,
        _ATTACHMENT_STATES,
        "transit gateway attachment",
        _ATTACHMENT,
        trail=trail,
    )


def _source(
    hop: RouteHop, gateway: TransitGateway, trail: tuple[str, ...]
) -> _Hop[TransitGatewayAttachment]:
    """The subnet VPC's usable attachment to the gateway, with a subnet in the subnet's zone."""
    index = hop.index
    vpc_id = index.parent_id(hop.subnet.id)
    found = [
        (attachment, (*edge.evidence_ids, *vpc_edge.evidence_ids))
        for attachment, edge in _attachments(index, gateway)
        if (vpc_edge := _vpc_edge(index, attachment)) is not None and vpc_edge.target_id == vpc_id
    ]
    if len(found) != 1:
        return missing(
            "The subnet's VPC has no recorded attachment to the transit gateway.",
            element=_ATTACHMENT,
            resource_id=gateway.id,
        )
    attachment, cited = found[0]
    evidence = (*trail, *cited)
    if problem := _unusable(hop, attachment, evidence):
        return problem
    zone = _zone_evidence(hop, attachment, evidence)
    if isinstance(zone, NetworkResult):
        return zone
    return attachment, (*evidence, *_facts(attachment, "/state"), *zone)


def _subnets_recorded(index: GraphIndex, attachment: TransitGatewayAttachment) -> bool:
    """Whether the whole subnet list is recorded: set, and no import left a value out."""
    if attachment.subnet_ids is None:
        return False
    return not any(
        value.resource_id == attachment.id
        for record in index.coverage_records()
        for value in record.unresolved_values
    )


def _zone_evidence(
    hop: RouteHop, attachment: TransitGatewayAttachment, trail: tuple[str, ...]
) -> tuple[str, ...] | NetworkResult:
    """Evidence that the attachment has a subnet in the source subnet's Availability Zone."""
    index, zone = hop.index, hop.subnet.availability_zone
    if not _subnets_recorded(index, attachment):
        return missing(
            "The attachment's subnets are not all recorded.",
            element=_ATTACHMENT,
            resource_id=attachment.id,
        )
    if zone is None:
        return missing(
            "The subnet's Availability Zone is not recorded.",
            element=_ATTACHMENT,
            resource_id=hop.subnet.id,
        )
    zones: list[str | None] = []
    for edge in index.outgoing_edges(
        attachment.id, attachment_kind=AttachmentKind.TRANSIT_GATEWAY_ATTACHMENT_SUBNET
    ):
        subnet = index.get_resource(edge.target_id)
        if not isinstance(subnet, Subnet):
            zones.append(None)
        elif subnet.availability_zone == zone:
            zone_facts = (_facts(placed, "/availability_zone") for placed in (hop.subnet, subnet))
            return (*edge.evidence_ids, *(e for facts in zone_facts for e in facts))
        else:
            zones.append(subnet.availability_zone)
    if None in zones or len(zones) != len(attachment.subnet_ids or []):
        return missing(
            "The Availability Zone of an attachment subnet is not recorded.",
            element=_ATTACHMENT,
            resource_id=attachment.id,
        )
    pointers = [f"/subnet_ids/{n}" for n in range(len(zones))] or ["/subnet_ids"]
    message = f"The transit gateway attachment has no subnet in {zone}."
    return hop.denied(attachment, message, _ATTACHMENT, *pointers, trail=trail)


def _table(
    hop: RouteHop, attachment: TransitGatewayAttachment, trail: tuple[str, ...]
) -> _Hop[TransitGatewayRouteTable]:
    """The attachment's associated route table, in a carrying state, with every route."""
    index = hop.index
    associations = [
        (table, edge)
        for edge in index.incoming_edges(
            attachment.id, attachment_kind=AttachmentKind.TRANSIT_GATEWAY_ROUTE_TABLE_ASSOCIATION
        )
        if edge.attributes.get("state") == "associated"
        and isinstance(table := index.get_resource(edge.source_id), TransitGatewayRouteTable)
    ]
    if len(associations) != 1:
        return missing(
            "The attachment's route table association is not recorded as associated.",
            element=_ATTACHMENT,
            resource_id=attachment.id,
        )
    table, edge = associations[0]
    label = "transit gateway route table"
    trail = (*trail, *edge.evidence_ids)
    if problem := hop.state_result(table, table.state, _TABLE_STATES, label, _TABLE, trail=trail):
        return problem
    if table.routes_complete is not True or omitted_rules(table):
        return missing(
            "The transit gateway route table's routes are not all recorded.",
            element=_TABLE,
            resource_id=table.id,
        )
    return table, (*edge.evidence_ids, *_facts(table, "/state", "/routes_complete"))


def _destination(
    position: int, destination: str | None, version: int
) -> list[tuple[IpNetwork, int]] | Undecided:
    """The route's prefix as a candidate when of the query's family; a list is out of scope."""
    if destination is None:
        return Undecided.missing("The transit gateway route records no destination.")
    if destination.startswith("pl-"):
        return Undecided.unsupported(
            "A transit gateway route to a prefix list is a deeper chain, out of scope."
        )
    try:
        network = ip_network(destination, strict=False)
    except ValueError:
        return Undecided.missing("The transit gateway route destination is not a CIDR.")
    return [(network, position)] if network.version == version else []


def _preferred(table: TransitGatewayRouteTable, best: list[int]) -> int | Undecided:
    """The route AWS picks among those sharing the longest prefix: a static one first."""
    if len(best) == 1:
        return best[0]
    types = [table.routes[n].route_type for n in best]
    if None not in types and types.count(TransitGatewayRouteType.STATIC) == 1:
        return best[types.index(TransitGatewayRouteType.STATIC)]
    return Undecided.missing("Several transit gateway routes share the longest matching prefix.")


def _route(hop: RouteHop, table: TransitGatewayRouteTable, trail: tuple[str, ...]) -> _Hop[str]:
    """The attachment the table's best active route names for the address."""
    target = parse_network(hop.address)
    candidates: list[tuple[IpNetwork, int]] = []
    for position, route in enumerate(table.routes):
        found = _destination(position, route.destination, target.version)
        if isinstance(found, Undecided):
            return found.result(element=f"{_TABLE}:/routes/{position}", resource_id=table.id)
        candidates.extend(found)
    best = longest_prefix(candidates, target)
    if isinstance(best, Undecided):
        return best.result(element=_TABLE, resource_id=table.id)
    if not best:
        message = "No transit gateway route matches this address."
        return hop.denied(table, message, _TABLE, "/routes_complete", trail=trail)
    position = _preferred(table, best)
    if isinstance(position, Undecided):
        return position.result(element=_TABLE, resource_id=table.id)
    route, pointer = table.routes[position], f"/routes/{position}"
    where = f"{_TABLE}:{pointer}"
    label = "transit gateway route"
    states = _ROUTE_STATES
    if problem := hop.state_result(
        table, route.state, states, label, where, pointer=pointer, trail=trail
    ):
        return problem
    if len(route.attachment_ids) != 1:
        return Undecided.unsupported(
            "The transit gateway route names several attachments (ECMP), a deeper chain out "
            "of scope."
        ).result(element=where, resource_id=table.id)
    return route.attachment_ids[0], _facts(table, pointer)


def _arrival(
    hop: RouteHop, gateway: TransitGateway, name: str, vpc_id: str, trail: tuple[str, ...]
) -> tuple[str, ...] | NetworkResult:
    """Evidence that the attachment `name` is a usable attachment of the VPC `vpc_id`."""
    index = hop.index
    found = [pair for pair in _attachments(index, gateway) if pair[0].identity.physical_id == name]
    if len(found) != 1:
        return missing(
            f"Transit gateway attachment {name} is not recorded.",
            element=_ATTACHMENT,
            resource_id=gateway.id,
        )
    attachment, edge = found[0]
    if attachment.attached_resource_type != "vpc":
        kind = attachment.attached_resource_type or "unrecorded"
        return Undecided.unsupported(
            f"The transit gateway route leads to a {kind} attachment, a deeper chain out of scope."
        ).result(element=_ATTACHMENT, resource_id=attachment.id)
    if problem := _unusable(hop, attachment, trail):
        return problem
    vpc_edge = _vpc_edge(index, attachment)
    if vpc_edge is None:
        return missing(
            "The attachment's VPC is not recorded.", element=_ATTACHMENT, resource_id=attachment.id
        )
    if vpc_edge.target_id == index.parent_id(hop.subnet.id):
        message = "The transit gateway route leads back to the subnet's own VPC."
        return hop.denied(attachment, message, _ATTACHMENT, "/attached_resource_id", trail=trail)
    # A third VPC could forward the traffic on, through its own routes: a second hop.
    if vpc_edge.target_id != vpc_id:
        return Undecided.unsupported(
            f"The transit gateway route leads to {attachment.attached_resource_id}; reaching "
            "the VPC asked from there needs another hop, a deeper chain out of scope."
        ).result(element=_ATTACHMENT, resource_id=attachment.id)
    return (*edge.evidence_ids, *vpc_edge.evidence_ids, *_facts(attachment, "/state"))


def transit_evidence(vpc_id: str, hop: RouteHop) -> tuple[str, ...] | NetworkResult:
    """Evidence that the route's transit gateway leads the address into `vpc_id`.

    Cites the gateway's state and owner, the source attachment with its gateway, VPC, and
    zone subnet, the route table association, the table's route search and selected route,
    and the destination attachment with its gateway and VPC edges; otherwise the result that
    ends the hop.
    """
    gateway = _gateway(hop)
    if isinstance(gateway, NetworkResult):
        return gateway
    source = _source(hop, gateway, _facts(gateway, "/state", "/owner_id"))
    if isinstance(source, NetworkResult):
        return source
    attachment, evidence = source
    associated = _table(hop, attachment, evidence)
    if isinstance(associated, NetworkResult):
        return associated
    table, cited = associated
    evidence = (*evidence, *cited)
    route = _route(hop, table, evidence)
    if isinstance(route, NetworkResult):
        return route
    name, cited = route
    evidence = (*evidence, *cited)
    arrival = _arrival(hop, gateway, name, vpc_id, evidence)
    if isinstance(arrival, NetworkResult):
        return arrival
    return (*evidence, *arrival)
