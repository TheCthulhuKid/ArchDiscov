"""Load-balancer scheme and listener-to-target forwarding from recorded structure.

Forwarding follows a listener that accepts the port and protocol, through its default and
conditional actions, to target groups whose registered targets resolve to recorded
resources. Conditional rules are alternatives: an allow means some request can be
forwarded. Other listener actions, unresolved targets, and unrecorded targets or health
are unknown. Target health is required to be recorded but never interpreted, and security
groups are not considered; callers compose those.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from archdiscov.models.enums import (
    AttachmentKind,
    DependencyRelationship,
    ListenerActionType,
    LoadBalancerProtocol,
    LoadBalancerScheme,
)
from archdiscov.models.network_path import NetworkResult
from archdiscov.models.resource_base import Resource
from archdiscov.models.resources.load_balancing import (
    Listener,
    ListenerRule,
    LoadBalancer,
    TargetGroup,
)
from archdiscov.models.resources.load_balancing_values import RegisteredTarget

from .network_decisions import (
    all_of,
    any_of,
    missing,
    qualify_coverage,
    unsupported,
)
from .network_matching import (
    Screen,
    Undecided,
    cited_decision,
    implicit_deny,
    normalize_protocol,
    require_recorded,
)

if TYPE_CHECKING:
    from archdiscov.graph import DependencyEdge, GraphIndex

SCHEME_OPERATIONS = frozenset({"DescribeLoadBalancers"})
FORWARDING_OPERATIONS = frozenset(
    {"DescribeListeners", "DescribeRules", "DescribeTargetGroups", "DescribeTargetHealth"}
)
_P = LoadBalancerProtocol
CLIENT_PROTOCOLS = {
    _P.HTTP: {"tcp"},
    _P.HTTPS: {"tcp"},
    _P.TCP: {"tcp"},
    _P.TLS: {"tcp"},
    _P.UDP: {"udp"},
    _P.TCP_UDP: {"tcp", "udp"},
    _P.QUIC: {"udp"},
    _P.TCP_QUIC: {"tcp", "udp"},
}


def _scheme_outcome(balancer: LoadBalancer) -> NetworkResult:
    internet_facing = balancer.scheme == LoadBalancerScheme.INTERNET_FACING
    if balancer.scheme is None:
        return missing(
            "The load balancer records no scheme.", element="scheme", resource_id=balancer.id
        )
    if internet_facing and balancer.ip_address_type == "dualstack-without-public-ipv4":
        return unsupported(
            "The load balancer has no public IPv4 address.",
            element="ip_address_type",
            resource_id=balancer.id,
        )
    return cited_decision(
        internet_facing,
        list(balancer.fact_evidence.get("/scheme", [])),
        (),
        subject="scheme",
        element="scheme",
        resource_id=balancer.id,
    )


def evaluate_load_balancer_scheme(index: GraphIndex, load_balancer_id: str) -> NetworkResult:
    """Allow for an internet-facing load balancer, deny for an internal one.

    An unrecorded scheme, or an internet-facing balancer without a public IPv4 address
    (dualstack-without-public-ipv4), is unknown. Cites the scheme fact.
    """
    balancer = require_recorded(index, load_balancer_id, LoadBalancer, "load balancer")
    if isinstance(balancer, NetworkResult):
        return balancer
    return qualify_coverage(index, _scheme_outcome(balancer), [balancer], SCHEME_OPERATIONS)


def _screen_listener(listener: Listener, protocol: str, port: int) -> Screen:
    if listener.port is None or listener.protocol is None:
        return Undecided.missing("The listener records no port or protocol.")
    if listener.port != port:
        return False
    if listener.protocol not in CLIENT_PROTOCOLS:
        return Undecided.unsupported("The listener protocol is unsupported.")
    return protocol in CLIENT_PROTOCOLS[listener.protocol]


TargetCheck = Callable[[TargetGroup, RegisteredTarget, Resource], NetworkResult]


@dataclass(frozen=True)
class TargetWalk:
    """How forwarding is walked to the registered targets.

    `check` is evaluated for each target that resolves to a recorded resource and must also
    allow; `require_health` makes an unrecorded health state unknown (health is only ever
    required to be recorded, never interpreted).
    """

    check: TargetCheck | None = None
    require_health: bool = True


def _registered_target_result(
    index: GraphIndex,
    group: TargetGroup,
    position: int,
    edge: DependencyEdge | None,
    extra: tuple[str, ...],
    walk: TargetWalk,
) -> NetworkResult:
    """One registered target: resolved to a recorded resource, with recorded health."""
    pointer = f"/targets/{position}"
    element = f"target:{pointer}"
    resource = index.get_resource(edge.target_id) if edge is not None else None
    if resource is None:
        return missing(
            "The registered target does not resolve to a recorded resource.",
            element=element,
            resource_id=group.id,
        )
    target = group.targets[position]
    if walk.require_health and target.health_state is None:
        return missing(
            "The registered target's health is not recorded.",
            element=element,
            resource_id=group.id,
        )
    reached = cited_decision(
        True,
        list(group.fact_evidence.get(f"{pointer}/id", [])),
        (*extra, *group.fact_evidence.get(f"{pointer}/health_state", [])),
        subject="target",
        element=element,
        resource_id=group.id,
    )
    return reached if walk.check is None else all_of([reached, walk.check(group, target, resource)])


def _target_group_result(
    index: GraphIndex, group: TargetGroup, extra: tuple[str, ...], walk: TargetWalk
) -> NetworkResult:
    """Allow when a registered target resolves to a recorded resource with recorded health."""
    if group.targets is None:
        return missing(
            "The target group's registered targets and health are not recorded.",
            element="targets",
            resource_id=group.id,
        )
    if not group.targets:
        return implicit_deny(index, group, "The target group has no registered targets.", extra)
    edges = {
        frozenset(edge.evidence_ids): edge
        for edge in index.outgoing_edges(group.id, relationship=DependencyRelationship.FORWARDS_TO)
    }
    return any_of(
        _registered_target_result(
            index,
            group,
            position,
            edges.get(frozenset(group.fact_evidence.get(f"/targets/{position}/id", []))),
            extra,
            walk,
        )
        for position in range(len(group.targets))
    )


def _forward_result(
    index: GraphIndex,
    source: Listener | ListenerRule,
    position: int,
    extra: tuple[str, ...],
    walk: TargetWalk,
) -> NetworkResult:
    element = f"action:/actions/{position}"
    edges = [
        edge
        for edge in index.outgoing_edges(source.id, relationship=DependencyRelationship.FORWARDS_TO)
        if edge.attributes.get("action_index") == position and edge.attributes.get("weight") != 0
    ]
    if not edges:
        return missing(
            "The forward action records no target group that receives traffic.",
            element=element,
            resource_id=source.id,
        )

    def group_result(edge: DependencyEdge) -> NetworkResult:
        group = index.get_resource(edge.target_id)
        if not isinstance(group, TargetGroup):
            return missing(
                "The forwarded target group is not recorded.",
                element=element,
                resource_id=source.id,
            )
        return _target_group_result(index, group, (*extra, *edge.evidence_ids), walk)

    return any_of(group_result(edge) for edge in edges)


def _source_result(
    index: GraphIndex, source: Listener | ListenerRule, extra: tuple[str, ...], walk: TargetWalk
) -> NetworkResult:
    """The result of one ordered action list: it must consist of forward actions only."""
    if not source.actions:
        return missing(
            "The listener or rule records no actions.", element="actions", resource_id=source.id
        )
    if any(action.type != ListenerActionType.FORWARD for action in source.actions):
        return unsupported(
            "Only forward actions are evaluated; other listener actions are unsupported.",
            element="actions",
            resource_id=source.id,
        )
    return any_of(
        _forward_result(index, source, position, extra, walk)
        for position in range(len(source.actions))
    )


def _listener_result(index: GraphIndex, listener: Listener, walk: TargetWalk) -> NetworkResult:
    extra = tuple(
        evidence
        for pointer in ("/port", "/protocol")
        for evidence in listener.fact_evidence.get(pointer, [])
    )
    rules = [
        rule
        for edge in index.incoming_edges(listener.id, attachment_kind=AttachmentKind.RULE_LISTENER)
        if isinstance(rule := index.get_resource(edge.source_id), ListenerRule)
        and not rule.is_default
    ]
    return any_of(_source_result(index, source, extra, walk) for source in (listener, *rules))


def _evaluate_listeners(
    index: GraphIndex,
    load_balancer_id: str,
    protocol: str,
    port: int,
    decide: Callable[[Listener], NetworkResult],
) -> NetworkResult:
    """The result over the listeners accepting the port and protocol, qualified by coverage."""
    balancer = require_recorded(index, load_balancer_id, LoadBalancer, "load balancer")
    if isinstance(balancer, NetworkResult):
        return balancer
    if (normalized := normalize_protocol(protocol)) is None:
        return unsupported("The query protocol is unsupported.", element="protocol")
    results = []
    for edge in index.incoming_edges(
        balancer.id, attachment_kind=AttachmentKind.LISTENER_LOAD_BALANCER
    ):
        listener = index.get_resource(edge.source_id)
        if not isinstance(listener, Listener):
            continue
        screen = _screen_listener(listener, normalized, port)
        if isinstance(screen, Undecided):
            results.append(screen.result(element="listener", resource_id=listener.id))
        elif screen:
            results.append(decide(listener))
    if results:
        outcome = any_of(results)
    else:
        outcome = implicit_deny(
            index, balancer, "No recorded listener accepts this port and protocol."
        )
    return qualify_coverage(index, outcome, [balancer], FORWARDING_OPERATIONS)


def evaluate_listener(
    index: GraphIndex, load_balancer_id: str, *, protocol: str, port: int
) -> NetworkResult:
    """Allow when a recorded listener accepts the port and protocol, citing its port and protocol.

    Denies when none does. What the listener does with the traffic is
    `evaluate_load_balancer_forwarding`'s question.
    """

    def accepts(listener: Listener) -> NetworkResult:
        return cited_decision(
            True,
            [
                *listener.fact_evidence.get("/port", []),
                *listener.fact_evidence.get("/protocol", []),
            ],
            (),
            subject="listener",
            element="listener",
            resource_id=listener.id,
        )

    return _evaluate_listeners(index, load_balancer_id, protocol, port, accepts)


def evaluate_load_balancer_forwarding(
    index: GraphIndex,
    load_balancer_id: str,
    *,
    protocol: str,
    port: int,
    walk: TargetWalk = TargetWalk(),
) -> NetworkResult:
    """Decide whether a listener accepts the traffic and forwards it to recorded targets.

    `protocol` is tcp or udp (or their numbers) as the client sends it. Denies when no
    recorded listener accepts the port and protocol, or the forwarded group has no
    registered targets. Cites the listener, forwarding, and target records. Limited listener,
    rule, target-group, or target-health collection gives unknown. `walk` adds a per-target
    check or relaxes the recorded-health requirement.
    """
    return _evaluate_listeners(
        index,
        load_balancer_id,
        protocol,
        port,
        lambda listener: _listener_result(index, listener, walk),
    )
