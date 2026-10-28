"""Subnet route selection by longest prefix, for local and internet-gateway targets.

The subnet's route table is its single observed explicit association, or, only when it has
none, the VPC's single observed main table. The best route is the longest matching prefix of
the query's address family; routes of the other family are not considered. A route whose
destination is a managed prefix list contributes each recorded entry as a prefix. A route to
an internet gateway or egress-only internet gateway is a recorded route, not proof of
internet reachability. A route to a NAT gateway reaches the internet only through the
gateway's own subnet's route to an internet gateway. Transit gateway, peering, and endpoint
targets are unsupported and never allow.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
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
    cited_target,
    implicit_deny,
    parse_network,
    require_recorded,
    sole_associate,
)
from .network_nat import NAT_OPERATIONS, nat_gateway_evidence
from .network_prefix_lists import PREFIX_LIST_OPERATIONS, resolve_prefix_list

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

ROUTE_OPERATIONS = frozenset({"DescribeRouteTables"})
_ELEMENT = "route"
_REACH_KINDS = {
    RouteReach.LOCAL: RouteTargetKind.LOCAL,
    RouteReach.INTERNET_GATEWAY: RouteTargetKind.INTERNET_GATEWAY,
    RouteReach.EGRESS_ONLY_INTERNET_GATEWAY: RouteTargetKind.EGRESS_ONLY_INTERNET_GATEWAY,
    RouteReach.NAT_GATEWAY: RouteTargetKind.NAT_GATEWAY,
}


@dataclass(frozen=True)
class _Gateway:
    """A gateway a route can reach, and the recorded attachment that makes it usable."""

    expected: type[Resource]
    attachment: AttachmentKind
    state: str
    label: str


_GATEWAYS = {
    RouteReach.INTERNET_GATEWAY: _Gateway(
        InternetGateway,
        AttachmentKind.INTERNET_GATEWAY_ATTACHMENT,
        "available",
        "internet gateway",
    ),
    RouteReach.EGRESS_ONLY_INTERNET_GATEWAY: _Gateway(
        EgressOnlyInternetGateway,
        AttachmentKind.EGRESS_ONLY_INTERNET_GATEWAY_ATTACHMENT,
        "attached",
        "egress-only internet gateway",
    ),
}
# The collection each reach reads beyond the route tables.
_REACH_OPERATIONS = {
    RouteReach.LOCAL: frozenset(),
    RouteReach.INTERNET_GATEWAY: frozenset({"DescribeInternetGateways"}),
    RouteReach.EGRESS_ONLY_INTERNET_GATEWAY: frozenset({"DescribeEgressOnlyInternetGateways"}),
    RouteReach.NAT_GATEWAY: NAT_OPERATIONS,
}


@dataclass(frozen=True)
class RouteHop:
    """The selected route whose target a handler checks, and what the handler may need."""

    index: GraphIndex
    subnet: Subnet
    table: RouteTable
    citations: list[str]
    where: str
    address: str

    def follow(self, subnet_id: str, reach: RouteReach) -> NetworkResult:
        """The same address's route from another subnet, for a target that forwards it."""
        return evaluate_subnet_route(self.index, subnet_id, self.address, reach=reach)


# Evidence that the selected route's target is usable, or the result that ends the decision.
type TargetCheck = Callable[[RouteHop], tuple[str, ...] | NetworkResult]


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


@dataclass(frozen=True)
class _Candidate:
    """One prefix a route matches: its CIDR, or one recorded entry of its prefix list."""

    network: IpNetwork
    position: int
    route: Route
    # The prefix-list entry's citations; None for a CIDR route.
    entry_evidence_ids: tuple[str, ...] | None = None


def _prefix_list_id(route: Route) -> str | None:
    """The list a route's destination names; older snapshots record only its destination."""
    if route.prefix_list_id is not None:
        return route.prefix_list_id
    destination = route.destination or ""
    return destination if destination.startswith("pl-") else None


def _route_candidates(
    index: GraphIndex, table: RouteTable, position: int, route: Route, version: int
) -> list[_Candidate] | Undecided:
    """The prefixes of the query's family the route matches; a list's are its entries."""
    if (prefix_list_id := _prefix_list_id(route)) is not None:
        citations = table.fact_evidence.get(f"/routes/{position}", [])
        entries = resolve_prefix_list(index, table.id, citations, prefix_list_id)
        if isinstance(entries, Undecided):
            return entries
        return [
            _Candidate(cidr, position, route, cited)
            for cidr, cited in entries
            if cidr.version == version
        ]
    if route.destination is None:
        return Undecided.missing("The route records no destination.")
    try:
        network = ip_network(route.destination, strict=False)
    except ValueError:
        return Undecided.missing("The route destination is not a CIDR.")
    return [_Candidate(network, position, route)] if network.version == version else []


def _tie(best: list[_Candidate]) -> Undecided:
    """Why routes sharing the longest prefix cannot be ordered.

    AWS prefers a static route, then a prefix-list route, then a propagated one, and picks
    at random between overlapping prefix lists. The collector does not record whether a
    route is static or propagated, so only a tie among CIDR routes has its usual reason.
    """
    lists = {c.position for c in best if c.entry_evidence_ids is not None}
    if not lists:
        return Undecided.missing("Several routes share the longest matching prefix.")
    if len(lists) > 1:
        return Undecided.missing(
            "Several prefix-list routes share the longest matching prefix; AWS chooses "
            "between overlapping prefix lists at random."
        )
    return Undecided.missing(
        "A route and a prefix-list route share the longest matching prefix, and whether the "
        "route is static or propagated is not recorded."
    )


def _best_route(
    index: GraphIndex, table: RouteTable, target: IpNetwork
) -> _Candidate | Undecided | None:
    """The longest-prefix candidate holding the whole target network; None when none does.

    A target wider than one address only bounds an address not known exactly, so a more
    specific prefix covering part of it could take some traffic and makes the result
    undecided. A prefix-list route whose entries are not recorded could cover anything, so
    it makes the result undecided too.
    """
    candidates: list[_Candidate] = []
    for position, route in enumerate(table.routes):
        found = _route_candidates(index, table, position, route, target.version)
        if isinstance(found, Undecided):
            return found
        candidates.extend(found)
    containing: list[_Candidate] = []
    partial: list[int] = []
    for candidate in candidates:
        if target.subnet_of(candidate.network):
            containing.append(candidate)
        elif target.overlaps(candidate.network):
            partial.append(candidate.network.prefixlen)
    longest = max((c.network.prefixlen for c in containing), default=-1)
    if any(prefix > longest for prefix in partial):
        return Undecided.missing("A more specific route covers only part of the address range.")
    if not containing:
        return None
    best = [c for c in containing if c.network.prefixlen == longest]
    if len({c.position for c in best}) > 1:
        return _tie(best)
    return best[0]


def _attachment_evidence(gateway_kind: _Gateway, hop: RouteHop) -> tuple[str, ...] | NetworkResult:
    """Evidence that the route's gateway is recorded as attached to the subnet's VPC."""
    index = hop.index
    gateway = cited_target(index, hop.table.id, AttachmentKind.ROUTE_TARGET, hop.citations)
    if not isinstance(gateway, gateway_kind.expected):
        return missing(f"The route's {gateway_kind.label} is not recorded.", element=hop.where)
    vpc_id = index.parent_id(hop.subnet.id)
    attachments = [
        edge
        for edge in index.outgoing_edges(gateway.id, attachment_kind=gateway_kind.attachment)
        if edge.target_id == vpc_id and edge.attributes.get("state") == gateway_kind.state
    ]
    if len(attachments) != 1:
        return missing(
            f"The {gateway_kind.label} is not recorded as {gateway_kind.state} on the VPC.",
            element=hop.where,
            resource_id=gateway.id,
        )
    return attachments[0].evidence_ids


# How each supported target kind is confirmed; a kind not listed is unsupported.
_TARGETS: dict[RouteTargetKind, TargetCheck] = {
    RouteTargetKind.LOCAL: lambda hop: (),
    RouteTargetKind.INTERNET_GATEWAY: partial(
        _attachment_evidence, _GATEWAYS[RouteReach.INTERNET_GATEWAY]
    ),
    RouteTargetKind.EGRESS_ONLY_INTERNET_GATEWAY: partial(
        _attachment_evidence, _GATEWAYS[RouteReach.EGRESS_ONLY_INTERNET_GATEWAY]
    ),
    RouteTargetKind.NAT_GATEWAY: nat_gateway_evidence,
}


def _inactive_route(
    route: Route,
    citations: list[str],
    extra_evidence_ids: tuple[str, ...],
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
            extra_evidence_ids,
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
    address: str,
    reach: RouteReach,
) -> NetworkResult:
    best = _best_route(index, table, parse_network(address))
    if isinstance(best, Undecided):
        return best.result(element=_ELEMENT, resource_id=table.id)
    if best is None:
        return implicit_deny(
            index,
            table,
            "No recorded route matches this address.",
            association_evidence_ids,
        )
    position, route = best.position, best.route
    where = f"{_ELEMENT}:/routes/{position}"
    citations = list(table.fact_evidence.get(f"/routes/{position}", []))
    # A prefix-list route also cites the entry that matched.
    extra = (*association_evidence_ids, *(best.entry_evidence_ids or ()))
    if inactive := _inactive_route(route, citations, extra, where, table):
        return inactive
    check = _TARGETS.get(route.target_kind)
    if check is None:
        return unsupported(
            f"A {route.target_kind} route target is unsupported.",
            element=where,
            resource_id=table.id,
        )
    allowed = route.target_kind == _REACH_KINDS[reach]
    if allowed:
        confirmed = check(RouteHop(index, subnet, table, citations, where, address))
        if isinstance(confirmed, NetworkResult):
            return confirmed
        extra = (*extra, *confirmed)
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
    egress-only internet gateway recorded as attached to it, or a public NAT gateway whose
    own subnet routes the address to an internet gateway. Egress-only and NAT gateways carry
    outbound traffic only, so callers ask for them only for outbound traffic. Denies when no
    route matches, the best route is a blackhole, or it leads elsewhere among the supported
    targets. Cites the route, the association, and the gateway attachment. Limited
    route-table collection (and collection of the queried gateway kind, and of prefix lists
    when the table names one) downgrades any decision to unknown.

    A prefix-list route matches through each recorded entry of the address's family and
    cites the entry it matched. At equal prefix length AWS prefers static routes, then
    prefix-list routes, then propagated ones; the route origin is not recorded, so a tie
    involving a prefix-list route is unknown, as is a list whose entries are not recorded.
    """
    subnet = require_recorded(index, subnet_id, Subnet, "subnet")
    if isinstance(subnet, NetworkResult):
        return subnet
    operations = ROUTE_OPERATIONS | _REACH_OPERATIONS[reach]
    selection = _route_table(index, subnet)
    if isinstance(selection, NetworkResult):
        outcome = selection
    else:
        table, association_evidence_ids = selection
        outcome = _decide(index, subnet, table, association_evidence_ids, address, reach)
        # A prefix-list route depends on the list's reads, so a limited read is never taken
        # for an empty list.
        if any(_prefix_list_id(route) is not None for route in table.routes):
            operations |= PREFIX_LIST_OPERATIONS
    return qualify_coverage(index, outcome, [subnet], operations)
