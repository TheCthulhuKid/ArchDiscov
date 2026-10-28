"""Compositions of the network primitives shared by the path-composing analyzers."""

from __future__ import annotations

from collections.abc import Iterable
from ipaddress import IPv4Network
from typing import TYPE_CHECKING

from archdiscov.models.architecture import Subnet
from archdiscov.models.enums import NetworkDecision, RuleDirection
from archdiscov.models.network_path import NetworkResult, compose_decisions

from .network_acls import evaluate_network_acl
from .network_decisions import all_of, any_of, missing
from .network_probes import EPHEMERAL_PORT
from .network_routes import evaluate_vpc_route
from .network_security_groups import Peer, evaluate_security_group

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex


def subnet_network(index: GraphIndex, subnet_id: str) -> IPv4Network | None:
    """The one IPv4 CIDR of a stored subnet; None when it is not recorded."""
    subnet = index.get_resource(subnet_id)
    if not isinstance(subnet, Subnet):
        return None
    blocks = [block for block in subnet.cidr_blocks or [] if isinstance(block, IPv4Network)]
    return blocks[0] if len(blocks) == 1 else None


def groups_allow(
    index: GraphIndex,
    group_ids: Iterable[str],
    direction: RuleDirection,
    peer: Peer,
    *,
    protocol: str,
    port: int,
) -> NetworkResult:
    """The traffic is allowed when some one of the security groups allows it."""
    return any_of(
        evaluate_security_group(index, group, direction, peer, protocol=protocol, port=port)
        for group in group_ids
    )


def nacl_round_trip(
    index: GraphIndex,
    subnet_id: str,
    first: RuleDirection,
    address: str,
    *,
    protocol: str,
    port: int,
) -> NetworkResult:
    """The request in direction `first` on `port`, and its reply on the ephemeral port.

    NACLs are stateless, so each direction of the subnet's ACL is evaluated separately.
    """
    reply = RuleDirection.EGRESS if first == RuleDirection.INGRESS else RuleDirection.INGRESS
    return all_of(
        evaluate_network_acl(index, subnet_id, direction, address, protocol=protocol, port=p)
        for direction, p in ((first, port), (reply, EPHEMERAL_PORT))
    )


def subnet_routes(
    index: GraphIndex,
    source_subnet_id: str,
    destination_subnet_id: str,
    *,
    source_address: str | None = None,
    destination_address: str | None = None,
) -> NetworkResult:
    """Whether each subnet's route to the other's address leads into the other's VPC.

    The request needs the source subnet's route to `destination_address` to lead into the
    destination's VPC, and the reply the destination subnet's route back to `source_address`
    to lead into the source's VPC. An address not given is bounded by its subnet's IPv4
    CIDR. Within one VPC that is the local route each way; across VPCs it is one peering
    connection or one transit gateway hop each way, every hop cited, and a path needing a
    second hop is unknown. Security groups and NACLs are the caller's to compose.
    """
    source_vpc = index.parent_id(source_subnet_id)
    destination_vpc = index.parent_id(destination_subnet_id)
    source = source_address or _bound(index, source_subnet_id)
    destination = destination_address or _bound(index, destination_subnet_id)
    if source_vpc is None or destination_vpc is None or source is None or destination is None:
        return missing("The subnets' VPCs or addresses are not recorded.")
    return all_of(
        [
            evaluate_vpc_route(index, source_subnet_id, destination, destination_vpc),
            evaluate_vpc_route(index, destination_subnet_id, source, source_vpc),
        ]
    )


def _bound(index: GraphIndex, subnet_id: str) -> str | None:
    network = subnet_network(index, subnet_id)
    return str(network) if network is not None else None


_RANK = {NetworkDecision.ALLOW: 0, NetworkDecision.UNKNOWN: 1, NetworkDecision.DENY: 2}


def decision_rank(results: Iterable[NetworkResult]) -> int:
    """How good the composed results are for an alternative: 0 allow, 1 unknown, 2 deny."""
    return _RANK[compose_decisions(result.decision for result in results)]
