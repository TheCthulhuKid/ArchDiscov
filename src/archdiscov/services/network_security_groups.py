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

from archdiscov.models.enums import NetworkBlockerCode, RuleDirection
from archdiscov.models.network_path import NetworkResult
from archdiscov.models.resources.network import SecurityGroup, SecurityGroupRule

from .network_decisions import (
    allow,
    any_of,
    blocker,
    deny,
    missing,
    qualify_coverage,
    require_aws,
    unsupported,
)

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

RULE_OPERATIONS = frozenset({"DescribeSecurityGroups", "DescribeSecurityGroupRules"})
_ALL = "-1"
_PROTOCOLS = {"tcp": "tcp", "6": "tcp", "udp": "udp", "17": "udp", "icmp": "icmp", "1": "icmp"}
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


def _protocol(value: str | None) -> str | None:
    return _PROTOCOLS.get(value.lower()) if value is not None else None


def _in_ports(rule: SecurityGroupRule, port: int) -> bool | None:
    """Whether the rule's range holds the port; None when the range is not recorded."""
    if rule.from_port is None or rule.to_port is None:
        return None
    return rule.from_port == -1 or rule.from_port <= port <= rule.to_port


# A screening step either lets matching continue (None) or stops it with the rule's
# outcome wrapped in a 1-tuple, where a bare None inside means the rule surely cannot match.
_Stop = tuple[NetworkResult | None]


@dataclass(frozen=True)
class _Query:
    """One traffic question about one group, shared by every rule it is matched against."""

    group: SecurityGroup
    peer: Peer
    peer_physical_ids: set[str] | None
    account_id: str | None
    protocol: str
    port: int

    def _missing(self, message: str, where: str) -> _Stop:
        return (missing(message, element=f"{_ELEMENT}:{where}", resource_id=self.group.id),)

    def _unsupported(self, message: str, where: str) -> _Stop:
        return (unsupported(message, element=f"{_ELEMENT}:{where}", resource_id=self.group.id),)

    def _screen_protocol(self, rule: SecurityGroupRule, where: str) -> _Stop | None:
        if rule.protocol is None:
            return self._missing("The rule records no protocol.", where)
        if rule.protocol == _ALL:
            return None
        recognized = _protocol(rule.protocol)
        if recognized is None:
            return self._unsupported("The rule uses an unrecognized protocol.", where)
        if recognized != self.protocol:
            return (None,)
        in_ports = _in_ports(rule, self.port)
        if in_ports is None:
            return self._missing("The rule records no port range.", where)
        return None if in_ports else (None,)

    def _screen_group(self, rule: SecurityGroupRule, where: str) -> _Stop | None:
        if rule.referenced_account_id not in (None, self.account_id):
            return self._unsupported("Cross-account group references are unsupported.", where)
        if self.peer_physical_ids is None:
            return self._missing("The peer's group membership is unknown.", where)
        return None if rule.referenced_group_id in self.peer_physical_ids else (None,)

    def _screen_cidr(self, rule: SecurityGroupRule, where: str) -> _Stop | None:
        if not isinstance(rule.cidr, IPv4Network):
            return self._unsupported("IPv6 rules are unsupported.", where)
        if self.peer.network is None:
            return self._missing("The peer address is unknown.", where)
        return None if self.peer.network.subnet_of(rule.cidr) else (None,)

    def _screen_peer(self, rule: SecurityGroupRule, where: str) -> _Stop | None:
        if rule.prefix_list_id is not None:
            return self._unsupported("Prefix-list rules are unsupported.", where)
        if rule.referenced_group_id is not None:
            return self._screen_group(rule, where)
        if rule.cidr is not None:
            return self._screen_cidr(rule, where)
        return self._missing("The rule records no source or destination.", where)

    def match(
        self, rule: SecurityGroupRule, citations: list[str], where: str
    ) -> NetworkResult | None:
        """The rule's result if it could match the query, or None when it surely cannot."""
        for screen in (self._screen_protocol, self._screen_peer):
            if (stop := screen(rule, where)) is not None:
                return stop[0]
        if not citations:
            return self._missing("The matching rule has no citation.", where)[0]
        return allow(citations)


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
    if (unsupported_platform := require_aws(index)) is not None:
        return unsupported_platform
    group = index.get_resource(group_id)
    if not isinstance(group, SecurityGroup):
        return missing("The security group is not recorded.", resource_id=group_id)
    if (normalized := _protocol(protocol)) is None:
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
    elif evidence_ids := index.resource_evidence_ids(group.id):
        outcome = deny(
            [
                blocker(
                    NetworkBlockerCode.DENIED,
                    "No recorded rule allows this traffic; security groups deny by default.",
                    element="implicit_deny",
                    resource_id=group.id,
                    evidence_ids=evidence_ids,
                )
            ]
        )
    else:
        outcome = missing("The group has no recorded rule evidence.", resource_id=group.id)
    return qualify_coverage(index, outcome, [group], RULE_OPERATIONS)
