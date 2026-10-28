"""VPC endpoints: gateway endpoints as route targets, interface endpoints as their ENIs.

A route to a gateway endpoint reaches it when the endpoint is stored, of the `gateway` type,
in a carrying state, associated with the subnet's route table, and the route's prefix list is
the endpoint service's list. A route to any other endpoint type (Gateway Load Balancer, or a
type not modelled, such as `Resource` or `ServiceNetwork`) is unsupported for every
question, since its routing depends on what lies behind it. An interface endpoint is reached
through its network interfaces, which callers decide with the local route, NACL, and
security-group primitives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from archdiscov.models.enums import AttachmentKind, NetworkBlockerCode, VpcEndpointType
from archdiscov.models.network_path import NetworkResult
from archdiscov.models.resource_base import Resource
from archdiscov.models.resources.network import ManagedPrefixList, VpcEndpoint

from .network_decisions import blocker, deny, missing, unsupported
from .network_matching import cited_target, require_recorded

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

    from .network_routes import RouteHop

ENDPOINT_OPERATIONS = frozenset({"DescribeVpcEndpoints"})
_ELEMENT = "vpc_endpoint"
# States from the VpcEndpoint API reference, compared in lower case because responses are
# lower case where the reference capitalizes: True carries traffic, False denies it, and
# None is a transition or partial state whose outcome is not known.
_STATES: dict[str, bool | None] = {
    "available": True,
    "pending": None,
    "partial": None,
    **dict.fromkeys(
        ("pendingacceptance", "deleting", "deleted", "rejected", "failed", "expired"), False
    ),
}


def _lowered(state: str | None) -> str | None:
    return state.lower() if state is not None else None


def route_endpoint(hop: RouteHop) -> VpcEndpoint | NetworkResult:
    """The route's stored gateway endpoint, else the result that ends any question.

    Only a gateway endpoint's routing is decided, so a route to another type, or to an
    endpoint whose type is not recorded, is unsupported whatever the question asks.
    """
    endpoint = cited_target(hop.index, hop.table.id, AttachmentKind.ROUTE_TARGET, hop.citations)
    if not isinstance(endpoint, VpcEndpoint):
        return missing("The route's VPC endpoint is not recorded.", element=hop.where)
    if endpoint.endpoint_type is not VpcEndpointType.GATEWAY:
        kind = endpoint.endpoint_type or "unrecorded"
        return unsupported(
            f"A route to a {kind} type VPC endpoint is unsupported.",
            element=hop.where,
            resource_id=endpoint.id,
        )
    return endpoint


def _service_list(hop: RouteHop, endpoint: VpcEndpoint) -> tuple[str, ...] | NetworkResult:
    """The route's prefix list's evidence when it is the endpoint service's list."""
    index = hop.index
    listed = cited_target(index, hop.table.id, AttachmentKind.PREFIX_LIST_REFERENCE, hop.citations)
    if not isinstance(listed, ManagedPrefixList):
        return missing(
            "The endpoint route names no recorded prefix list.",
            element=hop.where,
            resource_id=endpoint.id,
        )
    if endpoint.service_name is None or listed.name != endpoint.service_name:
        return missing(
            f"Prefix list {listed.identity.physical_id} is not recorded as the list of the "
            f"endpoint's service ({endpoint.service_name or 'not recorded'}).",
            element=_ELEMENT,
            resource_id=endpoint.id,
        )
    # The list's name is cited by its whole recorded entry.
    return tuple(listed.evidence_ids)


def gateway_endpoint_evidence(hop: RouteHop) -> tuple[str, ...] | NetworkResult:
    """Evidence that the route reaches its gateway endpoint for the selected prefix list.

    Cites the endpoint's type, service, and state, its edge to the subnet's route table, and
    the prefix list whose name is that service; otherwise the result that ends the hop.
    """
    endpoint = route_endpoint(hop)
    if isinstance(endpoint, NetworkResult):
        return endpoint
    state = _lowered(endpoint.state)
    if problem := hop.state_result(endpoint, state, _STATES, "VPC endpoint", _ELEMENT):
        return problem
    tables = [
        edge
        for edge in hop.index.outgoing_edges(
            endpoint.id, attachment_kind=AttachmentKind.ENDPOINT_ROUTE_TABLE
        )
        if edge.target_id == hop.table.id
    ]
    if len(tables) != 1:
        return missing(
            "The endpoint is not recorded as associated with the subnet's route table.",
            element=_ELEMENT,
            resource_id=endpoint.id,
        )
    service = _service_list(hop, endpoint)
    if isinstance(service, NetworkResult):
        return service
    facts = [
        evidence_id
        for pointer in ("/endpoint_type", "/service_name", "/state")
        for evidence_id in endpoint.fact_evidence.get(pointer, [])
    ]
    return (*facts, *tables[0].evidence_ids, *service)


@dataclass(frozen=True)
class EndpointInterfaces:
    """An interface endpoint's network interfaces and security groups, by resource ID."""

    network_interface_ids: tuple[str, ...]
    security_group_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]


def _members(
    index: GraphIndex, endpoint: VpcEndpoint, kind: AttachmentKind, ids: list[str]
) -> list[tuple[str, tuple[str, ...]]] | None:
    """Each recorded ID's stored resource with its edge's evidence; None when any is not stored."""
    stored = [
        (edge.target_id, edge.evidence_ids)
        for edge in index.outgoing_edges(endpoint.id, attachment_kind=kind)
        if isinstance(index.get_resource(edge.target_id), Resource)
    ]
    return stored if len(stored) == len(ids) else None


def endpoint_interfaces(index: GraphIndex, endpoint_id: str) -> EndpointInterfaces | NetworkResult:
    """An interface endpoint's stored network interfaces and security groups, with citations.

    Callers decide the endpoint through them with the local route, NACL, and security-group
    primitives. The endpoint must be an interface endpoint in a carrying state whose every recorded
    interface and group is stored; a state that carries no traffic denies, citing it, and
    anything else missing is unknown. `DescribeVpcEndpoints` coverage is the caller's to
    qualify, with the primitives it composes.
    """
    endpoint = require_recorded(index, endpoint_id, VpcEndpoint, "VPC endpoint")
    if isinstance(endpoint, NetworkResult):
        return endpoint
    if endpoint.endpoint_type is not VpcEndpointType.INTERFACE:
        return missing(
            "The VPC endpoint is not recorded as an interface endpoint.",
            element=_ELEMENT,
            resource_id=endpoint.id,
        )
    state = _lowered(endpoint.state)
    carries = _STATES.get(state or "")
    if carries is False:
        evidence_ids = endpoint.fact_evidence.get("/state", [])
        message = f"The VPC endpoint is {state}."
        return deny(
            [
                blocker(
                    NetworkBlockerCode.DENIED,
                    message,
                    element=_ELEMENT,
                    resource_id=endpoint.id,
                    evidence_ids=evidence_ids,
                )
            ]
        )
    if not carries:
        return missing(
            f"The VPC endpoint's state ({state or 'not recorded'}) does not show whether it "
            "carries traffic.",
            element=_ELEMENT,
            resource_id=endpoint.id,
        )
    interfaces = _members(
        index, endpoint, AttachmentKind.ENDPOINT_INTERFACE, endpoint.network_interface_ids
    )
    groups = _members(
        index, endpoint, AttachmentKind.ENDPOINT_SECURITY_GROUP, endpoint.security_group_ids
    )
    if not interfaces or groups is None:
        return missing(
            "The interface endpoint's network interfaces or security groups are not all recorded.",
            element=_ELEMENT,
            resource_id=endpoint.id,
        )
    facts = [
        evidence_id
        for pointer in ("/endpoint_type", "/state")
        for evidence_id in endpoint.fact_evidence.get(pointer, [])
    ]
    cited = [e for _, evidence_ids in (*interfaces, *groups) for e in evidence_ids]
    return EndpointInterfaces(
        network_interface_ids=tuple(target for target, _ in interfaces),
        security_group_ids=tuple(target for target, _ in groups),
        evidence_ids=tuple(sorted({*facts, *cited})),
    )
