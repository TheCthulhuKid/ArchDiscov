"""NAT gateways as route targets: outbound internet traffic through a public NAT gateway.

A route to a NAT gateway reaches the internet only when the gateway is recorded as public,
available, and addressed, and its own subnet routes the same address to an attached
internet gateway. A private NAT gateway never reaches the internet, and a NAT gateway never
carries inbound traffic: inbound questions ask for an internet gateway, which a NAT route is
not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from archdiscov.models.enums import (
    AttachmentKind,
    NatConnectivity,
    NetworkBlockerCode,
    NetworkDecision,
    RouteReach,
)
from archdiscov.models.network_path import NetworkResult
from archdiscov.models.resources.network import NatGateway

from .network_decisions import blocker, deny, missing
from .network_matching import cited_target

if TYPE_CHECKING:
    from .network_routes import RouteHop

NAT_OPERATIONS = frozenset({"DescribeNatGateways"})
_ELEMENT = "nat_gateway"
# States from the DescribeNatGateways API reference: True carries traffic, False denies it,
# and None is a transition whose outcome is not yet known.
_STATES: dict[str, bool | None] = {
    "available": True,
    "pending": None,
    "failed": False,
    "deleting": False,
    "deleted": False,
}


def _denied(gateway: NatGateway, hop: RouteHop, message: str, pointer: str) -> NetworkResult:
    return deny(
        [
            blocker(
                NetworkBlockerCode.DENIED,
                message,
                element=_ELEMENT,
                resource_id=gateway.id,
                evidence_ids=(*hop.citations, *gateway.fact_evidence.get(pointer, [])),
            )
        ]
    )


def _unusable(gateway: NatGateway, hop: RouteHop) -> NetworkResult | None:
    """Why the recorded gateway cannot carry internet traffic, or None when it can."""
    carries = _STATES.get(gateway.state or "")
    if carries is False:
        return _denied(gateway, hop, f"The NAT gateway is {gateway.state}.", "/state")
    if carries is None:
        state = gateway.state or "not recorded"
        return missing(
            f"The NAT gateway's state ({state}) does not show whether it carries traffic.",
            element=_ELEMENT,
            resource_id=gateway.id,
        )
    if gateway.connectivity is NatConnectivity.PRIVATE:
        return _denied(
            gateway, hop, "A private NAT gateway does not reach the internet.", "/connectivity"
        )
    if gateway.connectivity is None or not gateway.public_ips:
        return missing(
            "The NAT gateway's connectivity or public address is not recorded.",
            element=_ELEMENT,
            resource_id=gateway.id,
        )
    return None


def nat_gateway_evidence(hop: RouteHop) -> tuple[str, ...] | NetworkResult:
    """Evidence that the route's NAT gateway forwards the address to the internet.

    Cites the gateway's state, connectivity, and public addresses, its subnet, and that
    subnet's route to an attached internet gateway; otherwise the result that ends the hop.
    """
    index = hop.index
    gateway = cited_target(index, hop.table.id, AttachmentKind.ROUTE_TARGET, hop.citations)
    if not isinstance(gateway, NatGateway):
        return missing("The route's NAT gateway is not recorded.", element=hop.where)
    if (problem := _unusable(gateway, hop)) is not None:
        return problem
    placements = index.outgoing_edges(gateway.id, attachment_kind=AttachmentKind.NAT_GATEWAY_SUBNET)
    # An unstored subnet is reported by the onward route itself.
    if len(placements) != 1:
        return missing(
            "The NAT gateway's subnet is not recorded.", element=_ELEMENT, resource_id=gateway.id
        )
    onward = hop.follow(placements[0].target_id, RouteReach.INTERNET_GATEWAY)
    if onward.decision != NetworkDecision.ALLOW:
        return onward
    addresses = (f"/public_ips/{n}" for n in range(len(gateway.public_ips)))
    facts = [
        evidence_id
        for pointer in ("/state", "/connectivity", *addresses)
        for evidence_id in gateway.fact_evidence.get(pointer, [])
    ]
    return (*facts, *placements[0].evidence_ids, *onward.evidence_ids)
