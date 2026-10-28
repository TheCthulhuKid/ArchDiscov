"""Subnet route selection by longest prefix, for local and internet-gateway targets.

The subnet's route table is its single observed explicit association, or, only when it has
none, the VPC's single observed main table. The best route is the longest matching prefix of
the query's address family; routes of the other family are not considered. A route to an
internet gateway or egress-only internet gateway is a recorded route, not proof of internet
reachability. NAT, transit gateway, peering, endpoint, and prefix-list routes are
unsupported and never allow.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_network
from typing import TYPE_CHECKING

from archdiscov.models.architecture import Subnet, Vpc
from archdiscov.models.enums import AttachmentKind, RouteReach, RouteTargetKind
from archdiscov.models.network_path import NetworkResult
from archdiscov.models.resource_base import Resource
from archdiscov.models.resources.network import (
    EgressOnlyInternetGateway,
    InternetGateway,
    Route,
    RouteTable,
)

from .network_decisions import missing, qualify_coverage, unsupported
from .network_matching import (
    IpNetwork,
    Undecided,
    cited_decision,
    implicit_deny,
    parse_network,
    require_recorded,
    sole_associate,
)

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

ROUTE_OPERATIONS = frozenset({"DescribeRouteTables"})
_ELEMENT = "route"
_REACH_KINDS = {
    RouteReach.LOCAL: RouteTargetKind.LOCAL,
    RouteReach.INTERNET_GATEWAY: RouteTargetKind.INTERNET_GATEWAY,
    RouteReach.EGRESS_ONLY_INTERNET_GATEWAY: RouteTargetKind.EGRESS_ONLY_INTERNET_GATEWAY,
}


@dataclass(frozen=True)
class _Gateway:
    """A gateway a route can reach, and the recorded attachment that makes it usable."""

    expected: type[Resource]
    attachment: AttachmentKind
    state: str
    label: str
    operation: str


_GATEWAYS = {
    RouteReach.INTERNET_GATEWAY: _Gateway(
        InternetGateway,
        AttachmentKind.INTERNET_GATEWAY_ATTACHMENT,
        "available",
        "internet gateway",
        "DescribeInternetGateways",
    ),
    RouteReach.EGRESS_ONLY_INTERNET_GATEWAY: _Gateway(
        EgressOnlyInternetGateway,
        AttachmentKind.EGRESS_ONLY_INTERNET_GATEWAY_ATTACHMENT,
        "attached",
        "egress-only internet gateway",
        "DescribeEgressOnlyInternetGateways",
    ),
}


def _route_table(
    index: GraphIndex, subnet: Subnet
) -> tuple[RouteTable, tuple[str, ...]] | NetworkResult:
    """The subnet's explicit table, else the VPC's main table when none is explicit."""
    kind = AttachmentKind.ROUTE_TABLE_ASSOCIATION
    if index.incoming_edges(subnet.id, attachment_kind=kind):
        return sole_associate(index, subnet, kind, RouteTable, "route_table")
    vpc = index.get_resource(index.parent_id(subnet.id) or "")
    if not isinstance(vpc, Vpc):
        return missing("The subnet's VPC is not recorded.", resource_id=subnet.id)
    return sole_associate(
        index,
        vpc,
        kind,
        RouteTable,
        "main_route_table",
        only=lambda edge: edge.attributes.get("main") is True,
    )


def _network(route: Route, version: int) -> IpNetwork | Undecided | None:
    """The route's destination; None for the other address family, which cannot match."""
    if route.destination is None:
        return Undecided.missing("The route records no destination.")
    if route.destination.startswith("pl-"):
        return Undecided.unsupported("Prefix-list routes are unsupported.")
    try:
        network = ip_network(route.destination, strict=False)
    except ValueError:
        return Undecided.missing("The route destination is not a CIDR.")
    return network if network.version == version else None


def _best_route(table: RouteTable, target: IpNetwork) -> tuple[int, Route] | Undecided | None:
    """The longest-prefix route holding the whole target network; None when none does.

    A target wider than one address only bounds an address not known exactly, so a more
    specific route covering part of it could take some traffic and makes the result undecided.
    """
    containing: list[tuple[int, int, Route]] = []
    partial: list[int] = []
    for position, route in enumerate(table.routes):
        network = _network(route, target.version)
        if isinstance(network, Undecided):
            return network
        if network is None:
            continue
        if target.subnet_of(network):
            containing.append((network.prefixlen, position, route))
        elif target.overlaps(network):
            partial.append(network.prefixlen)
    longest = max((prefix for prefix, _, _ in containing), default=-1)
    if any(prefix > longest for prefix in partial):
        return Undecided.missing("A more specific route covers only part of the address range.")
    if not containing:
        return None
    best = [(position, route) for prefix, position, route in containing if prefix == longest]
    if len(best) > 1:
        return Undecided.missing("Several routes share the longest matching prefix.")
    return best[0]


def _attachment_evidence(
    index: GraphIndex,
    table: RouteTable,
    citations: list[str],
    vpc_id: str | None,
    where: str,
    gateway_kind: _Gateway,
) -> tuple[str, ...] | NetworkResult:
    """Evidence that the route's gateway is recorded as attached to the VPC.

    The route's target edge is the one citing the same source entry as the route, so routes
    sharing a destination form, such as a prefix list, are never confused.
    """
    targets = [
        edge
        for edge in index.outgoing_edges(table.id, attachment_kind=AttachmentKind.ROUTE_TARGET)
        if set(edge.evidence_ids) & set(citations)
    ]
    gateway = index.get_resource(targets[0].target_id) if len(targets) == 1 else None
    if not isinstance(gateway, gateway_kind.expected):
        return missing(f"The route's {gateway_kind.label} is not recorded.", element=where)
    attachments = [
        edge
        for edge in index.outgoing_edges(gateway.id, attachment_kind=gateway_kind.attachment)
        if edge.target_id == vpc_id and edge.attributes.get("state") == gateway_kind.state
    ]
    if len(attachments) != 1:
        return missing(
            f"The {gateway_kind.label} is not recorded as {gateway_kind.state} on the VPC.",
            element=where,
            resource_id=gateway.id,
        )
    return attachments[0].evidence_ids


def _inactive_route(
    route: Route,
    citations: list[str],
    association_evidence_ids: tuple[str, ...],
    where: str,
    table: RouteTable,
) -> NetworkResult | None:
    """The result for a blackhole or unrecorded state; None for an active route."""
    if route.state == "active":
        return None
    if route.state == "blackhole":
        return cited_decision(
            False,
            citations,
            association_evidence_ids,
            subject="route",
            element=where,
            resource_id=table.id,
        )
    return missing(
        "The best route does not record an active or blackhole state.",
        element=where,
        resource_id=table.id,
    )


def _decide(
    index: GraphIndex,
    subnet: Subnet,
    table: RouteTable,
    association_evidence_ids: tuple[str, ...],
    target: IpNetwork,
    reach: RouteReach,
) -> NetworkResult:
    best = _best_route(table, target)
    if isinstance(best, Undecided):
        return best.result(element=_ELEMENT, resource_id=table.id)
    if best is None:
        return implicit_deny(
            index,
            table,
            "No recorded route matches this address.",
            association_evidence_ids,
        )
    position, route = best
    where = f"{_ELEMENT}:/routes/{position}"
    citations = list(table.fact_evidence.get(f"/routes/{position}", []))
    if inactive := _inactive_route(route, citations, association_evidence_ids, where, table):
        return inactive
    if route.target_kind not in _REACH_KINDS.values():
        return unsupported(
            f"A {route.target_kind} route target is unsupported.",
            element=where,
            resource_id=table.id,
        )
    extra = association_evidence_ids
    allowed = route.target_kind == _REACH_KINDS[reach]
    if allowed and reach in _GATEWAYS:
        attachment = _attachment_evidence(
            index, table, citations, index.parent_id(subnet.id), where, _GATEWAYS[reach]
        )
        if isinstance(attachment, NetworkResult):
            return attachment
        extra = (*extra, *attachment)
    return cited_decision(
        allowed,
        citations,
        extra,
        subject="route",
        element=where,
        resource_id=table.id,
    )


def evaluate_subnet_route(
    index: GraphIndex, subnet_id: str, address: str, *, reach: RouteReach
) -> NetworkResult:
    """Decide whether the subnet's best route for the address leads where `reach` says.

    `address` is an IPv4 or IPv6 address, or a CIDR bounding an address not known exactly;
    only routes of its address family are considered.

    Allows when the longest-prefix route is active and leads to `reach`: a local target, an
    internet gateway recorded as available and attached to the subnet's VPC, or an
    egress-only internet gateway recorded as attached to it. An egress-only gateway carries
    outbound traffic only, so callers ask for it only for outbound traffic. Denies when no
    route matches, the best route is a blackhole, or it leads elsewhere among the supported
    targets. Cites the route, the association, and the gateway attachment. Limited
    route-table collection (and collection of the queried gateway kind) downgrades any
    decision to unknown.
    """
    subnet = require_recorded(index, subnet_id, Subnet, "subnet")
    if isinstance(subnet, NetworkResult):
        return subnet
    selection = _route_table(index, subnet)
    if isinstance(selection, NetworkResult):
        outcome = selection
    else:
        table, association_evidence_ids = selection
        outcome = _decide(
            index, subnet, table, association_evidence_ids, parse_network(address), reach
        )
    gateway_kind = _GATEWAYS.get(reach)
    operations = ROUTE_OPERATIONS | ({gateway_kind.operation} if gateway_kind else set())
    return qualify_coverage(index, outcome, [subnet], operations)
