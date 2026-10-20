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
    NetworkBlockerCode,
    RuleDirection,
)
from archdiscov.models.network_path import NetworkResult
from archdiscov.models.resources.network import NaclEntry, NetworkAcl

from .network_decisions import (
    Match,
    allow,
    blocker,
    deny,
    first_match,
    missing,
    qualify_coverage,
    require_aws,
    unsupported,
)
from .network_matching import (
    Undecided,
    combine,
    implicit_deny,
    normalize_protocol,
    omitted_rules,
    screen_cidr,
    screen_protocol,
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

    def _decide(self, entry: NaclEntry, citations: list[str], where: str) -> NetworkResult:
        """The result of an entry that matches: a cited allow or deny."""
        element = f"{_ELEMENT}:{where}"
        if not citations:
            return missing(
                "The matching entry has no citation.", element=element, resource_id=self.acl.id
            )
        evidence_ids = [*citations, *self.association_evidence_ids]
        if entry.action == NaclAction.ALLOW:
            return allow(evidence_ids)
        return deny(
            [
                blocker(
                    NetworkBlockerCode.DENIED,
                    f"Entry {entry.rule_number} denies this traffic.",
                    element=element,
                    resource_id=self.acl.id,
                    evidence_ids=evidence_ids,
                )
            ]
        )

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
                citations = list(self.acl.fact_evidence.get(where, []))
                yield True, self._decide(entry, citations, where)


def _associated_acl(
    index: GraphIndex, subnet: Subnet
) -> tuple[NetworkAcl, tuple[str, ...]] | NetworkResult:
    """The one observed ACL association for the subnet, with its evidence, or why not."""
    edges = index.incoming_edges(subnet.id, attachment_kind=AttachmentKind.NETWORK_ACL_ASSOCIATION)
    if len(edges) != 1:
        message = (
            "No network ACL association is recorded for the subnet."
            if not edges
            else "Several network ACL associations are recorded for the subnet."
        )
        return missing(message, element="network_acl_association", resource_id=subnet.id)
    acl = index.get_resource(edges[0].source_id)
    if not isinstance(acl, NetworkAcl):
        return missing("The associated network ACL is not recorded.", resource_id=subnet.id)
    if omitted_rules(acl):
        return missing(
            "Some network ACL entries could not be recorded, so their order is incomplete.",
            resource_id=acl.id,
        )
    return acl, edges[0].evidence_ids


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
    if (unsupported_platform := require_aws(index)) is not None:
        return unsupported_platform
    subnet = index.get_resource(subnet_id)
    if not isinstance(subnet, Subnet):
        return missing("The subnet is not recorded.", resource_id=subnet_id)
    if (normalized := normalize_protocol(protocol)) is None:
        return unsupported("The query protocol is unsupported.", element="protocol")
    association = _associated_acl(index, subnet)
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
