"""Candidate internet exposure of internet-facing load balancers and their targets.

Per listener and client protocol, the path is composed from: the load balancer's scheme, the
route to an attached internet gateway and the NACLs of its subnets (any one subnet's node may
carry the traffic), its security groups, the listener, and forwarding to registered targets.
Within `max_depth` the targets are followed to recorded backend instances, whose security
groups must admit the balancer's groups on the target port. A private backend is reached
through the balancer and is never publicly addressed. Observed target health is recorded as an
observation only: it is never required and never changes a decision. Everything is recorded
configuration, not proof of reachability.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from archdiscov.models.architecture import Ec2Instance
from archdiscov.models.enums import (
    AttachmentKind,
    ExposureElement,
    ExposureEntryKind,
    ExposureProtocol,
    LoadBalancerType,
    NetworkDecision,
    RouteReach,
    RuleDirection,
    TargetHealthState,
)
from archdiscov.models.network_path import NetworkResult
from archdiscov.models.resource_base import Resource
from archdiscov.models.resources.load_balancing import (
    Listener,
    LoadBalancer,
    TargetGroup,
)
from archdiscov.models.resources.load_balancing_values import RegisteredTarget

from .database_sources import instance_placement
from .exposure_evaluation import ExposureEvaluation, evaluate_elements, merge_adjacent
from .exposure_ports import MAX_PORT, MIN_PORT
from .network_composition import decision_rank, groups_allow, nacl_round_trip, subnet_network
from .network_decisions import any_of, missing, qualify_coverage, unsupported
from .network_load_balancers import (
    CLIENT_PROTOCOLS,
    FORWARDING_OPERATIONS,
    TargetWalk,
    evaluate_listener,
    evaluate_load_balancer_forwarding,
    evaluate_load_balancer_scheme,
)
from .network_matching import implicit_deny
from .network_probes import INTERNET_CIDR, PROBE_ADDRESS
from .network_routes import evaluate_subnet_route
from .network_security_groups import Peer
from .placement_collection import PlacementCollector

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

_WHOLE_RANGE = (MIN_PORT, MAX_PORT)
_INGRESS = RuleDirection.INGRESS
_E = ExposureElement
_UNNEEDED = frozenset({"source_address_unrecorded"})  # the private address is not needed
BACKEND_DEPTH = 2  # entry -> target group -> backend instance


def load_balancer_entries(index: GraphIndex) -> list[LoadBalancer]:
    """Every recorded load balancer, in identity order: internal ones are reported as denied."""
    return sorted((r for r in index.resources() if isinstance(r, LoadBalancer)), key=lambda r: r.id)


@dataclass(frozen=True)
class _Context:
    """What every listener evaluation of one balancer shares."""

    index: GraphIndex
    balancer: LoadBalancer
    scheme: NetworkResult
    collector: PlacementCollector
    subnets: set[str]
    groups: set[str]
    max_depth: int

    def whole_range(self, results: dict[ExposureElement, NetworkResult]) -> ExposureEvaluation:
        """One evaluation for a balancer whose ports cannot be enumerated."""
        return evaluate_elements(
            ExposureEntryKind.LOAD_BALANCER,
            self.balancer,
            ExposureProtocol.TCP,
            _WHOLE_RANGE,
            results,
            evidence=self.collector.evidence,
            limitations=self.collector.sorted_limitations(),
        )


def _subnet_paths(
    index: GraphIndex, subnets: set[str], protocol: ExposureProtocol, port: int
) -> tuple[NetworkResult, NetworkResult]:
    """Route and NACL results of the best subnet: any one node may carry the traffic."""
    if not subnets:
        none = missing("No subnet of the load balancer is recorded.", element="subnet")
        return none, none
    paths = [
        (
            evaluate_subnet_route(index, subnet, PROBE_ADDRESS, reach=RouteReach.INTERNET_GATEWAY),
            nacl_round_trip(index, subnet, _INGRESS, INTERNET_CIDR, protocol=protocol, port=port),
        )
        for subnet in sorted(subnets)
    ]
    return min(paths, key=decision_rank)


class _Backends:
    """Backend security-group check for the targets reached; records what was observed."""

    def __init__(self, ctx: _Context) -> None:
        self.index = ctx.index
        self.groups = tuple(sorted(ctx.groups))
        networks = [subnet_network(ctx.index, subnet) for subnet in sorted(ctx.subnets)]
        self.networks = networks or [None]
        self.collector = PlacementCollector(ctx.index)  # the backends' own placement citations
        self.health: set[TargetHealthState] = set()
        self.targets: dict[str, Resource] = {}

    def check(
        self, group: TargetGroup, target: RegisteredTarget, resource: Resource
    ) -> NetworkResult:
        if target.health_state is not None:
            self.health.add(target.health_state)
        if not isinstance(resource, Ec2Instance):
            return unsupported(
                "Only instance targets are followed to their security groups.",
                element="target",
                resource_id=group.id,
            )
        self.targets[resource.id] = resource
        port = target.port or group.port
        if port is None:
            return missing("The target records no port.", element="target", resource_id=group.id)
        protocol = _backend_protocol(group)
        interfaces = instance_placement(self.index, resource, self.collector)
        if not interfaces:
            return missing(
                "The backend instance's security groups are not recorded.",
                element="target",
                resource_id=resource.id,
            )
        return any_of(
            groups_allow(
                self.index,
                interface.security_group_ids,
                _INGRESS,
                Peer(network, self.groups, bounded=True),
                protocol=protocol,
                port=port,
            )
            for interface in interfaces
            for network in self.networks
        )


def _backend_protocol(group: TargetGroup) -> str:
    protocols = CLIENT_PROTOCOLS.get(group.protocol) if group.protocol else None
    return sorted(protocols)[0] if protocols else "tcp"


def _security_group_result(
    ctx: _Context, protocol: ExposureProtocol, port: int
) -> NetworkResult | None:
    """The balancer's security-group ingress; None for a network balancer without groups."""
    if ctx.balancer.lb_type == LoadBalancerType.NETWORK and ctx.balancer.security_group_ids == []:
        return None  # such a balancer filters nothing itself; the targets' groups do
    if not ctx.groups:
        return missing(
            "No security group of the load balancer is recorded.", element="security_group"
        )
    return groups_allow(
        ctx.index,
        sorted(ctx.groups),
        _INGRESS,
        Peer.within(INTERNET_CIDR, ()),
        protocol=protocol,
        port=port,
    )


def _listener_evaluation(
    ctx: _Context, protocol: ExposureProtocol, port: int
) -> ExposureEvaluation:
    index, balancer = ctx.index, ctx.balancer
    route, nacl = _subnet_paths(index, ctx.subnets, protocol, port)
    backends = _Backends(ctx)
    walk = TargetWalk(
        check=backends.check if ctx.max_depth >= BACKEND_DEPTH else None, require_health=False
    )
    candidates = {
        _E.SCHEME: ctx.scheme,
        _E.ROUTE: route,
        _E.NACL: nacl,
        _E.SECURITY_GROUP: _security_group_result(ctx, protocol, port),
        _E.LISTENER: evaluate_listener(index, balancer.id, protocol=protocol, port=port),
        _E.TARGET: evaluate_load_balancer_forwarding(
            index, balancer.id, protocol=protocol, port=port, walk=walk
        ),
    }
    return evaluate_elements(
        ExposureEntryKind.LOAD_BALANCER,
        balancer,
        protocol,
        (port, port),
        {element: result for element, result in candidates.items() if result is not None},
        evidence={*ctx.collector.evidence, *backends.collector.evidence},
        limitations=(
            *ctx.collector.sorted_limitations(),
            *backends.collector.sorted_limitations(ignore=_UNNEEDED),
        ),
        targets=tuple(backends.targets[key] for key in sorted(backends.targets)),
        health=tuple(sorted(backends.health)),
    )


def _listeners(index: GraphIndex, balancer: LoadBalancer) -> list[Listener]:
    found = (
        index.get_resource(edge.source_id)
        for edge in index.incoming_edges(
            balancer.id, attachment_kind=AttachmentKind.LISTENER_LOAD_BALANCER
        )
    )
    listeners = [item for item in found if isinstance(item, Listener)]
    return sorted(listeners, key=lambda item: (item.port or 0, item.id))


def _evaluate_listeners(ctx: _Context) -> list[ExposureEvaluation]:
    listeners = _listeners(ctx.index, ctx.balancer)
    if not listeners:
        none = qualify_coverage(
            ctx.index,
            implicit_deny(ctx.index, ctx.balancer, "The load balancer has no recorded listener."),
            [ctx.balancer],
            FORWARDING_OPERATIONS,
        )
        return [ctx.whole_range({_E.SCHEME: ctx.scheme, _E.LISTENER: none})]
    evaluations: list[ExposureEvaluation] = []
    for listener in listeners:
        protocols = CLIENT_PROTOCOLS.get(listener.protocol) if listener.protocol else None
        if listener.port is None or not protocols:
            undecided = unsupported(
                "The listener's port or protocol is not recorded or not supported.",
                element="listener",
                resource_id=listener.id,
            )
            evaluations.append(ctx.whole_range({_E.SCHEME: ctx.scheme, _E.LISTENER: undecided}))
        else:
            evaluations.extend(
                _listener_evaluation(ctx, ExposureProtocol(protocol), listener.port)
                for protocol in sorted(protocols)
            )
    return evaluations


def evaluate_load_balancer_entry(
    index: GraphIndex, balancer: LoadBalancer, max_depth: int
) -> list[ExposureEvaluation]:
    """The candidate exposure of the balancer, one evaluation per listener and protocol."""
    collector = PlacementCollector(index)
    scheme = evaluate_load_balancer_scheme(index, balancer.id)
    if scheme.decision != NetworkDecision.ALLOW:
        context = _Context(index, balancer, scheme, collector, set(), set(), max_depth)
        return [context.whole_range({_E.SCHEME: scheme})]
    context = _Context(
        index,
        balancer,
        scheme,
        collector,
        collector.memberships([balancer], AttachmentKind.SUBNET_MEMBERSHIP, "subnet"),
        collector.memberships(
            [balancer], AttachmentKind.SECURITY_GROUP_MEMBERSHIP, "security group"
        ),
        max_depth,
    )
    ordered = sorted(_evaluate_listeners(context), key=lambda e: (e.protocol, e.start, e.end))
    return merge_adjacent(ordered)
