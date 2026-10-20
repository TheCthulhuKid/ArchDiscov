"""Security-group rule matching over recorded rules, one direction per call.

Security groups only allow. A query is allowed when a recorded rule matches, denied
(implicitly) only when the group's rule collection is observed and no rule could match,
and unknown otherwise. IPv6, prefix lists, cross-account references, and unrecognized
protocols are unsupported and never allow. Statefulness is not modelled.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, ip_network
from typing import TYPE_CHECKING

from archdiscov.models.enums import RuleDirection
from archdiscov.models.network_path import NetworkResult
from archdiscov.models.resources.network import SecurityGroup, SecurityGroupRule

from .network_decisions import (
    any_of,
    missing,
    qualify_coverage,
    unsupported,
)
from .network_matching import (
    Screen,
    Undecided,
    cited_decision,
    combine,
    implicit_deny,
    normalize_protocol,
    omitted_rules,
    require_recorded,
    screen_cidr,
    screen_protocol,
)

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

RULE_OPERATIONS = frozenset({"DescribeSecurityGroups", "DescribeSecurityGroupRules"})
_ELEMENT = "security_group_rule"


@dataclass(frozen=True)
class Peer:
    """The other end of the traffic: the source for ingress, the destination for egress.

    `network` is an IPv4 address or CIDR, and `group_ids` the stored security groups the
    peer belongs to. None means unknown, so rules that depend on it become unknown
    rather than non-matching; an empty tuple means the peer belongs to no group.
    """

    network: IPv4Network | None = None
    group_ids: tuple[str, ...] | None = None

    @classmethod
    def address(cls, value: str, group_ids: tuple[str, ...] | None = None) -> Peer:
        return cls(ip_network(IPv4Address(value)), group_ids)

    @classmethod
    def cidr(cls, value: str, group_ids: tuple[str, ...] | None = None) -> Peer:
        network = ip_network(value, strict=False)
        if not isinstance(network, IPv4Network):
            raise ValueError("only IPv4 peers are supported")
        return cls(network, group_ids)


@dataclass(frozen=True)
class _Query:
    """One traffic question about one group, shared by every rule it is matched against."""

    group: SecurityGroup
    peer: Peer
    peer_physical_ids: set[str] | None
    account_id: str | None
    protocol: str
    port: int

    def _screen_group(self, rule: SecurityGroupRule) -> Screen:
        if rule.referenced_account_id not in (None, self.account_id):
            return Undecided.unsupported("Cross-account group references are unsupported.")
        if self.peer_physical_ids is None:
            return Undecided.missing("The peer's group membership is unknown.")
        return rule.referenced_group_id in self.peer_physical_ids

    def _screen_peer(self, rule: SecurityGroupRule) -> Screen:
        if rule.prefix_list_id is not None:
            return Undecided.unsupported("Prefix-list rules are unsupported.")
        if rule.referenced_group_id is not None:
            return self._screen_group(rule)
        if rule.cidr is not None:
            return screen_cidr(rule.cidr, self.peer.network, "The peer address is unknown.")
        return Undecided.missing("The rule records no source or destination.")

    def match(
        self, rule: SecurityGroupRule, citations: list[str], where: str
    ) -> NetworkResult | None:
        """The rule's result if it could match the query, or None when it surely cannot."""
        screen = combine(
            screen_protocol(rule.protocol, rule.from_port, rule.to_port, self.protocol, self.port),
            self._screen_peer(rule),
        )
        if isinstance(screen, Undecided):
            return screen.result(element=f"{_ELEMENT}:{where}", resource_id=self.group.id)
        if not screen:
            return None
        return cited_decision(
            True,
            citations,
            (),
            subject="rule",
            element=f"{_ELEMENT}:{where}",
            resource_id=self.group.id,
        )


def evaluate_security_group(
    index: GraphIndex,
    group_id: str,
    direction: RuleDirection,
    peer: Peer,
    *,
    protocol: str,
    port: int,
) -> NetworkResult:
    """Decide whether the group's recorded rules permit this traffic in one direction.

    `protocol` is tcp, udp, icmp, or their numbers; `port` is the destination port (the
    ICMP type for icmp). Cites the matching rule on allow, and the group's own evidence on
    an implicit deny. Limited rule collection downgrades any decision to unknown.
    """
    group = require_recorded(index, group_id, SecurityGroup, "security group")
    if isinstance(group, NetworkResult):
        return group
    if (normalized := normalize_protocol(protocol)) is None:
        return unsupported("The query protocol is unsupported.", element="protocol")
    members = None if peer.group_ids is None else [index.get_resource(i) for i in peer.group_ids]
    if members is not None and None in members:
        return missing("A peer security group is not recorded.", resource_id=group_id)
    peer_physical_ids = None if members is None else {m.identity.physical_id for m in members}
    ingress = direction == RuleDirection.INGRESS
    rules = group.ingress_rules if ingress else group.egress_rules
    name = "ingress_rules" if ingress else "egress_rules"
    query = _Query(
        group, peer, peer_physical_ids, getattr(index.scope, "account_id", None), normalized, port
    )
    results = [
        result
        for position, rule in enumerate(rules)
        if (
            result := query.match(
                rule,
                list(group.fact_evidence.get(f"/{name}/{position}", [])),
                f"/{name}/{position}",
            )
        )
        is not None
    ]
    if results:
        outcome = any_of(results)
    elif omitted_rules(group):
        outcome = missing("Some rules could not be recorded.", resource_id=group.id)
    else:
        outcome = implicit_deny(
            index, group, "No recorded rule allows this traffic; security groups deny by default."
        )
    return qualify_coverage(index, outcome, [group], RULE_OPERATIONS)
