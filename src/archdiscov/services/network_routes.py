"""Subnet route selection by longest prefix, for local and internet-gateway targets.

The subnet's route table is its single observed explicit association, or, only when it has
none, the VPC's single observed main table. The best IPv4 route is the longest matching
prefix. A route to an internet gateway is a recorded route, not proof of internet
reachability. NAT, transit gateway, peering, endpoint, and prefix-list routes are
unsupported and never allow; IPv6 routes cannot match an IPv4 address and are not
considered.
"""

from __future__ import annotations

from ipaddress import IPv4Network, ip_network
from typing import TYPE_CHECKING

from archdiscov.models.architecture import Subnet, Vpc
from archdiscov.models.enums import AttachmentKind, RouteReach, RouteTargetKind
from archdiscov.models.network_path import NetworkResult
from archdiscov.models.resources.network import InternetGateway, Route, RouteTable

from .network_decisions import missing, qualify_coverage, unsupported
from .network_matching import (
    Undecided,
    cited_decision,
    implicit_deny,
    parse_ipv4_network,
    require_recorded,
    sole_associate,
)

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

ROUTE_OPERATIONS = frozenset({"DescribeRouteTables"})
GATEWAY_OPERATIONS = frozenset({"DescribeInternetGateways"})
_ELEMENT = "route"
_REACH_KINDS = {
    RouteReach.LOCAL: RouteTargetKind.LOCAL,
    RouteReach.INTERNET_GATEWAY: RouteTargetKind.INTERNET_GATEWAY,
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


def _network(route: Route) -> IPv4Network | Undecided | None:
    """The route's IPv4 destination; None for IPv6, which cannot match an IPv4 address."""
    if route.destination is None:
        return Undecided.missing("The route records no destination.")
    if route.destination.startswith("pl-"):
        return Undecided.unsupported("Prefix-list routes are unsupported.")
    try:
        network = ip_network(route.destination, strict=False)
    except ValueError:
        return Undecided.missing("The route destination is not a CIDR.")
    return network if isinstance(network, IPv4Network) else None


def _best_route(table: RouteTable, target: IPv4Network) -> tuple[int, Route] | Undecided | None:
    """The longest-prefix route holding the whole target network; None when none does.

    A target wider than one address only bounds an address not known exactly, so a more
    specific route covering part of it could take some traffic and makes the result undecided.
    """
    containing: list[tuple[int, int, Route]] = []
    partial: list[int] = []
    for position, route in enumerate(table.routes):
        network = _network(route)
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
    index: GraphIndex, table: RouteTable, route: Route, vpc_id: str | None, where: str
) -> tuple[str, ...] | NetworkResult:
    """Evidence that the route's internet gateway is recorded as attached to the VPC."""
    targets = [
        edge
        for edge in index.outgoing_edges(table.id, attachment_kind=AttachmentKind.ROUTE_TARGET)
        if edge.attributes.get("destination") == route.destination
    ]
    gateway = index.get_resource(targets[0].target_id) if len(targets) == 1 else None
    if not isinstance(gateway, InternetGateway):
        return missing("The route's internet gateway is not recorded.", element=where)
    attachments = [
        edge
        for edge in index.outgoing_edges(
            gateway.id, attachment_kind=AttachmentKind.INTERNET_GATEWAY_ATTACHMENT
        )
        if edge.target_id == vpc_id and edge.attributes.get("state") == "available"
    ]
    if len(attachments) != 1:
        return missing(
            "The internet gateway is not recorded as available and attached to the VPC.",
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
    target: IPv4Network,
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
    if allowed and reach == RouteReach.INTERNET_GATEWAY:
        attachment = _attachment_evidence(index, table, route, index.parent_id(subnet.id), where)
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
    """Decide whether the subnet's best route for the IPv4 address leads where `reach` says.

    `address` may also be a CIDR bounding an address not known exactly.

    Allows when the longest-prefix route is active and leads to `reach`: a local target, or
    an internet gateway recorded as available and attached to the subnet's VPC. Denies
    when no route matches, the best route is a blackhole, or it leads elsewhere among the
    supported targets. Cites the route, the association, and the gateway attachment.
    Limited route-table collection (and gateway collection, for an internet-gateway
    query) downgrades any decision to unknown.
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
            index, subnet, table, association_evidence_ids, parse_ipv4_network(address), reach
        )
    operations = ROUTE_OPERATIONS | (
        GATEWAY_OPERATIONS if reach == RouteReach.INTERNET_GATEWAY else set()
    )
    return qualify_coverage(index, outcome, [subnet], operations)
