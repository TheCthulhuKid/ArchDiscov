"""Stateless, ordered network ACL evaluation for one subnet and one direction per call.

A subnet's associated ACL entries are evaluated in rule-number order and the first match
decides. Traffic matching no entry is denied by the implicit final rule. The ACL must be an
observed association: no default ACL is assumed. IPv6 entries, unrecognized protocols, and
entries that could not be recorded give unknown. Both directions of a flow are evaluated
by separate calls that the caller composes.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from ipaddress import IPv4Address, ip_network
from typing import TYPE_CHECKING

from archdiscov.models.architecture import Subnet
from archdiscov.models.enums import (
    AttachmentKind,
    NaclAction,
    RuleDirection,
)
from archdiscov.models.network_path import NetworkResult
from archdiscov.models.resources.network import NetworkAcl

from .network_decisions import (
    Match,
    first_match,
    qualify_coverage,
    unsupported,
)
from .network_matching import (
    Undecided,
    cited_decision,
    combine,
    implicit_deny,
    normalize_protocol,
    require_recorded,
    screen_cidr,
    screen_protocol,
    sole_associate,
)

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

NACL_OPERATIONS = frozenset({"DescribeNetworkAcls"})
_ELEMENT = "network_acl_entry"


@dataclass(frozen=True)
class _Query:
    """One traffic question about one ACL, shared by every entry it is matched against."""

    acl: NetworkAcl
    association_evidence_ids: tuple[str, ...]
    address: IPv4Address
    protocol: str
    port: int

    def candidates(self, direction: RuleDirection) -> Iterator[tuple[Match, NetworkResult]]:
        """Entries for the direction in rule-number order that could still match."""
        positions = sorted(
            (
                (entry.rule_number, position)
                for position, entry in enumerate(self.acl.entries)
                if entry.direction == direction
            )
        )
        network = ip_network(self.address)
        for _, position in positions:
            entry, where = self.acl.entries[position], f"/entries/{position}"
            screen = combine(
                screen_protocol(
                    entry.protocol, entry.from_port, entry.to_port, self.protocol, self.port
                ),
                screen_cidr(entry.cidr, network, "The address is unknown."),
            )
            if isinstance(screen, Undecided):
                yield None, screen.result(element=f"{_ELEMENT}:{where}", resource_id=self.acl.id)
            elif screen:
                yield (
                    True,
                    cited_decision(
                        entry.action == NaclAction.ALLOW,
                        list(self.acl.fact_evidence.get(where, [])),
                        self.association_evidence_ids,
                        subject="entry",
                        element=f"{_ELEMENT}:{where}",
                        resource_id=self.acl.id,
                    ),
                )


def evaluate_network_acl(
    index: GraphIndex,
    subnet_id: str,
    direction: RuleDirection,
    address: str,
    *,
    protocol: str,
    port: int,
) -> NetworkResult:
    """Decide whether the subnet's associated ACL permits this traffic in one direction.

    `address` is the IPv4 address on the other end (the source for ingress, the
    destination for egress). `protocol` is tcp, udp, icmp, or their numbers, and `port`
    the destination port. Cites the matched entry and the association; an implicit deny
    cites the ACL's own evidence. Limited ACL collection downgrades any decision to unknown.
    """
    subnet = require_recorded(index, subnet_id, Subnet, "subnet")
    if isinstance(subnet, NetworkResult):
        return subnet
    if (normalized := normalize_protocol(protocol)) is None:
        return unsupported("The query protocol is unsupported.", element="protocol")
    association = sole_associate(
        index, subnet, AttachmentKind.NETWORK_ACL_ASSOCIATION, NetworkAcl, "network_acl"
    )
    if isinstance(association, NetworkResult):
        outcome = association
    else:
        acl, association_evidence_ids = association
        query = _Query(acl, association_evidence_ids, IPv4Address(address), normalized, port)
        outcome = first_match(
            query.candidates(direction),
            implicit_deny(
                index,
                acl,
                "No entry allows this traffic; the implicit final rule denies it.",
                association_evidence_ids,
            ),
        )
    return qualify_coverage(index, outcome, [subnet], NACL_OPERATIONS)
