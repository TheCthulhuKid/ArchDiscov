"""Public IPv4 addressing recorded on network interfaces and instances.

A recorded public IPv4 address allows, a recorded absence denies, and anything else is
unknown. IPv6 addresses are ignored. This is configuration, not proof of reachability.
"""

from __future__ import annotations

from ipaddress import IPv4Address, ip_address
from typing import TYPE_CHECKING

from archdiscov.models.architecture import Ec2Instance
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


def evaluate_public_address(index: GraphIndex, resource_id: str) -> NetworkResult:
    """Decide whether the interface or instance has a recorded public IPv4 address.

    Allows citing the address fact, denies citing the resource's own evidence when no
    address is recorded, and is unknown when the resource, its evidence, or complete
    collection is missing. Limited collection of the resource's operation gives unknown.
    """
    resource = require_recorded(
        index, resource_id, (NetworkInterface, Ec2Instance), "interface or instance"
    )
    if isinstance(resource, NetworkResult):
        return resource
    values, pointer = recorded_public_addresses(resource)
    try:
        parsed = [ip_address(value) for value in values]
    except ValueError:
        return unsupported(
            "A recorded public address is not an IP address.",
            element="public_ip",
            resource_id=resource.id,
        )
    ipv4 = [address for address in parsed if isinstance(address, IPv4Address)]
    if ipv4:
        outcome = cited_decision(
            True,
            list(resource.fact_evidence.get(pointer, [])),
            (),
            subject="public address",
            element="public_ip",
            resource_id=resource.id,
        )
    elif omitted_rules(resource):
        outcome = missing(
            "Some addresses could not be recorded.", element="public_ip", resource_id=resource.id
        )
    else:
        outcome = implicit_deny(index, resource, "No public IPv4 address is recorded.")
    return qualify_coverage(index, outcome, [resource], ADDRESS_OPERATIONS[type(resource)])
