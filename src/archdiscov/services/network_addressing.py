"""Public addressing recorded on network interfaces and instances, one address family per call.

A recorded public address allows, a recorded absence denies, and anything else is unknown.
Over IPv4, the public addresses of interfaces and instances count. Over IPv6, an interface's
global unicast addresses count, because AWS IPv6 addresses need no separate public address;
instances record no IPv6 addresses. This is configuration, not proof of reachability.
"""

from __future__ import annotations

from ipaddress import IPv4Address, ip_address
from typing import TYPE_CHECKING

from archdiscov.models.architecture import Ec2Instance
from archdiscov.models.enums import AddressFamily
from archdiscov.models.network_path import NetworkResult
from archdiscov.models.resource_base import Resource
from archdiscov.models.resources.network import NetworkInterface

from .network_decisions import missing, qualify_coverage, unsupported
from .network_matching import cited_decision, implicit_deny, omitted_rules, require_recorded

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

ADDRESS_OPERATIONS = {
    NetworkInterface: frozenset({"DescribeNetworkInterfaces"}),
    Ec2Instance: frozenset({"DescribeInstances"}),
}


def recorded_public_addresses(resource: Resource) -> tuple[list[str], str]:
    """The recorded public address values and the fact pointer that cites them."""
    if isinstance(resource, NetworkInterface):
        associated = [a.public_ip for a in resource.private_ip_addresses if a.public_ip]
        pointer = "/public_ip" if resource.public_ip else "/private_ip_addresses"
        return [*([resource.public_ip] if resource.public_ip else []), *associated], pointer
    value = resource.attributes.get("public_ip_address")
    return [value] if isinstance(value, str) and value else [], "/attributes/public_ip_address"


def _absence(index: GraphIndex, resource: Resource, element: str, reason: str) -> NetworkResult:
    """A cited deny, or unknown when normalization dropped some of the resource's addresses."""
    if omitted_rules(resource):
        return missing(
            "Some addresses could not be recorded.", element=element, resource_id=resource.id
        )
    return implicit_deny(index, resource, reason)


def _public_ipv4(index: GraphIndex, resource: NetworkInterface | Ec2Instance) -> NetworkResult:
    values, pointer = recorded_public_addresses(resource)
    try:
        parsed = [ip_address(value) for value in values]
    except ValueError:
        return unsupported(
            "A recorded public address is not an IP address.",
            element="public_ip",
            resource_id=resource.id,
        )
    if not any(isinstance(address, IPv4Address) for address in parsed):
        return _absence(index, resource, "public_ip", "No public IPv4 address is recorded.")
    return cited_decision(
        True,
        list(resource.fact_evidence.get(pointer, [])),
        (),
        subject="public address",
        element="public_ip",
        resource_id=resource.id,
    )


def _public_ipv6(index: GraphIndex, resource: NetworkInterface | Ec2Instance) -> NetworkResult:
    element = "ipv6_address"
    if not isinstance(resource, NetworkInterface):
        return missing(
            "Instances record no IPv6 addresses; their network interfaces do.",
            element=element,
            resource_id=resource.id,
        )
    if resource.ipv6_addresses is None:
        return missing(
            "The interface's IPv6 addresses are not recorded.",
            element=element,
            resource_id=resource.id,
        )
    pointers = [
        f"/ipv6_addresses/{n}" for n, a in enumerate(resource.ipv6_addresses) if a.is_global
    ]
    if not pointers:
        return _absence(index, resource, element, "No global IPv6 address is recorded.")
    citations = [e for pointer in pointers for e in resource.fact_evidence.get(pointer, [])]
    return cited_decision(
        True,
        citations,
        (),
        subject="public address",
        element=element,
        resource_id=resource.id,
    )


_BY_FAMILY = {AddressFamily.IPV4: _public_ipv4, AddressFamily.IPV6: _public_ipv6}


def evaluate_public_address(
    index: GraphIndex, resource_id: str, *, family: AddressFamily = AddressFamily.IPV4
) -> NetworkResult:
    """Decide whether the interface or instance has a recorded public address of the family.

    Over IPv4, a recorded public IPv4 address counts. Over IPv6, a recorded global unicast
    address of a network interface counts; a documentation, unique-local, or link-local
    address does not. Allows citing the address facts, denies citing the resource's own
    evidence when no such address is recorded, and is unknown when the resource, its
    evidence, its IPv6 addresses, or complete collection is missing. Limited collection of
    the resource's operation gives unknown.
    """
    resource = require_recorded(
        index, resource_id, (NetworkInterface, Ec2Instance), "interface or instance"
    )
    if isinstance(resource, NetworkResult):
        return resource
    outcome = _BY_FAMILY[family](index, resource)
    return qualify_coverage(index, outcome, [resource], ADDRESS_OPERATIONS[type(resource)])
