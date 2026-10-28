"""Security-group rule matching over recorded rules, one direction per call.

Security groups only allow. A query is allowed when a recorded rule matches, denied
(implicitly) only when the group's rule collection is observed and no rule could match,
and unknown otherwise. A rule of the other address family than the peer never matches. A
rule naming a prefix list matches through the list's recorded entries; a list whose entries
are not recorded is unknown. Cross-account references and unrecognized protocols are
unsupported and never allow. Statefulness is not modelled.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address, ip_network
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
    IpNetwork,
    Screen,
    Undecided,
    cited_decision,
    combine,
    implicit_deny,
    normalize_protocol,
    omitted_rules,
    parse_network,
    require_recorded,
    screen_cidr,
    screen_protocol,
)
from .network_prefix_lists import PREFIX_LIST_OPERATIONS, resolve_prefix_list

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

RULE_OPERATIONS = frozenset({"DescribeSecurityGroups", "DescribeSecurityGroupRules"})
_ELEMENT = "security_group_rule"


@dataclass(frozen=True)
class Peer:
    """The other end of the traffic: the source for ingress, the destination for egress.

    `network` is an IPv4 or IPv6 address or CIDR, and `group_ids` the stored security groups
    the peer belongs to. None means unknown, so rules that depend on it become unknown
    rather than non-matching; an empty tuple means the peer belongs to no group. With
    `bounded`, the peer is one unknown address somewhere inside `network` (for example a
    subnet), so a rule covering only part of it is unknown, not a non-match.
    """

    network: IpNetwork | None = None
    group_ids: tuple[str, ...] | None = None
    bounded: bool = False

    @classmethod
    def address(cls, value: str, group_ids: tuple[str, ...] | None = None) -> Peer:
        return cls(ip_network(ip_address(value)), group_ids)

    @classmethod
    def cidr(cls, value: str, group_ids: tuple[str, ...] | None = None) -> Peer:
        return cls(parse_network(value), group_ids)

    @classmethod
    def within(cls, value: str, group_ids: tuple[str, ...] | None = None) -> Peer:
        """One unknown address somewhere inside the CIDR."""
        return cls(parse_network(value), group_ids, bounded=True)


@dataclass(frozen=True)
class _Query:
    """One traffic question about one group, shared by every rule it is matched against."""

    index: GraphIndex
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

    def _screen_cidr(self, cidr: IpNetwork) -> Screen:
        return screen_cidr(
            cidr, self.peer.network, "The peer address is unknown.", bounded=self.peer.bounded
        )

    def _screen_prefix_list(
        self, prefix_list_id: str, citations: list[str]
    ) -> tuple[Screen, tuple[str, ...]]:
        """Whether some entry holds the peer, with the citations of the entries that do."""
        resolved = resolve_prefix_list(self.index, self.group.id, citations, prefix_list_id)
        if isinstance(resolved, Undecided):
            return resolved, ()
        screens = [(self._screen_cidr(cidr), cited) for cidr, cited in resolved]
        hits = [cited for screen, cited in screens if screen is True]
        if hits:
            return True, tuple(evidence_id for cited in hits for evidence_id in cited)
        return next((s for s, _ in screens if isinstance(s, Undecided)), False), ()

    def _screen_peer(
        self, rule: SecurityGroupRule, citations: list[str]
    ) -> tuple[Screen, tuple[str, ...]]:
        """Whether the rule's source or destination holds the peer, and the entries it used."""
        if rule.prefix_list_id is not None:
            return self._screen_prefix_list(rule.prefix_list_id, citations)
        if rule.referenced_group_id is not None:
            return self._screen_group(rule), ()
        if rule.cidr is not None:
            return self._screen_cidr(rule.cidr), ()
        return Undecided.missing("The rule records no source or destination."), ()

    def match(
        self, rule: SecurityGroupRule, citations: list[str], where: str
    ) -> NetworkResult | None:
        """The rule's result if it could match the query, or None when it surely cannot."""
        peer_screen, entry_citations = self._screen_peer(rule, citations)
        screen = combine(
            screen_protocol(rule.protocol, rule.from_port, rule.to_port, self.protocol, self.port),
            peer_screen,
        )
        if isinstance(screen, Undecided):
            return screen.result(element=f"{_ELEMENT}:{where}", resource_id=self.group.id)
        if not screen:
            return None
        return cited_decision(
            True,
            citations,
            entry_citations,
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
        index,
        group,
        peer,
        peer_physical_ids,
        getattr(index.scope, "account_id", None),
        normalized,
        port,
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
    # A prefix-list rule depends on the list's reads too, so a limited read is never taken
    # for an empty list.
    names_list = any(rule.prefix_list_id is not None for rule in rules)
    operations = RULE_OPERATIONS | (PREFIX_LIST_OPERATIONS if names_list else frozenset())
    return qualify_coverage(index, outcome, [group], operations)
