"""Compositions of the step 29 primitives shared by the path-composing analyzers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from archdiscov.models.enums import RuleDirection
from archdiscov.models.network_path import NetworkResult

from .network_acls import evaluate_network_acl
from .network_decisions import all_of, any_of
from .network_probes import EPHEMERAL_PORT
from .network_security_groups import Peer, evaluate_security_group

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex


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
