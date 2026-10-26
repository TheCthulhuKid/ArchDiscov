"""Containment and observed forwarding references, without reachability inference."""

from collections.abc import Callable
from enum import Enum
from typing import Any

from ...models.architecture import (
    Dependency,
    DependencyTarget,
    Evidence,
    ExternalTarget,
    LocalTarget,
    Resource,
    UnresolvedTarget,
)
from ...models.enums import (
    AttachmentKind,
    AwsService,
    DependencyRelationship,
    ListenerActionType,
    LoadBalancerType,
    ResourceType,
    TargetType,
)
from ...models.resource_tree import walk_resources as walk_resources
from ...models.resources.load_balancing import Listener, ListenerRule, LoadBalancer, TargetGroup
from ...models.resources.load_balancing_values import ListenerAction
from ..configuration_relationships import arn_scope


class _ResolutionMode(Enum):
    LOCAL = "local"
    IP = "ip"
    EXTERNAL_LAMBDA = "external_lambda"
    APPLICATION_LOAD_BALANCER = "application_load_balancer"


def _resolve_target(
    index: dict[tuple[ResourceType, str], list[Resource]],
    kind: ResourceType,
    ref: str,
    path: str,
    mode: _ResolutionMode,
) -> DependencyTarget:
    if mode == _ResolutionMode.IP:
        return UnresolvedTarget(
            reason="IP registration is observed; resource ownership is not inferred",
            source_path=path,
        )
    if mode == _ResolutionMode.EXTERNAL_LAMBDA:
        return ExternalTarget(
            identifier=ref,
            service=AwsService.LAMBDA,
            account_id=arn_scope(ref)[0],
            aws_resource_type=ResourceType.LAMBDA_FUNCTION,
        )
    matches = index.get((kind, ref), [])
    if mode == _ResolutionMode.APPLICATION_LOAD_BALANCER:
        matches = [
            r
            for r in matches
            if isinstance(r, LoadBalancer) and r.lb_type == LoadBalancerType.APPLICATION
        ]
    if len(matches) == 1:
        return LocalTarget(resource_id=matches[0].id)
    return UnresolvedTarget(
        reason="target was not uniquely observed with the expected resource type",
        source_path=path,
    )


def merge_resources(roots: list[Resource], resources: list[Resource]) -> list[Resource]:
    """Place each new resource once beneath an observed parent of the expected type."""
    all_resources = [*walk_resources(roots), *resources]
    index = {(r.resource_type, r.identity.physical_id): r for r in all_resources}
    stored = {r.id for r in walk_resources(roots)}
    for resource in resources:
        if resource.id in stored:
            continue
        parent_type, parent_id = ResourceType.VPC, getattr(resource, "vpc_id", None)
        if isinstance(resource, Listener):
            parent_type, parent_id = ResourceType.ELBV2_LOAD_BALANCER, resource.load_balancer_arn
        elif isinstance(resource, ListenerRule):
            parent_type, parent_id = ResourceType.ELBV2_LISTENER, resource.listener_arn
        parent = index.get((parent_type, parent_id)) if parent_id else None
        if parent is None:
            resource.attributes["unresolved_parent"] = {
                "physical_id": parent_id,
                "resource_type": parent_type.value,
                "reason": "parent configuration was unavailable or not collected",
            }
            roots.append(resource)
        else:
            parent.children.append(resource)
        stored.add(resource.id)
    return roots


_MEMBERSHIPS = (
    ("subnet_ids", ResourceType.SUBNET, AttachmentKind.SUBNET_MEMBERSHIP),
    ("security_group_ids", ResourceType.SECURITY_GROUP, AttachmentKind.SECURITY_GROUP_MEMBERSHIP),
)
_PARENTS = {
    ResourceType.ELBV2_LISTENER: (
        "load_balancer_arn",
        ResourceType.ELBV2_LOAD_BALANCER,
        AttachmentKind.LISTENER_LOAD_BALANCER,
    ),
    ResourceType.ELBV2_LISTENER_RULE: (
        "listener_arn",
        ResourceType.ELBV2_LISTENER,
        AttachmentKind.RULE_LISTENER,
    ),
}
_TARGET_MODES = {
    TargetType.IP: _ResolutionMode.IP,
    TargetType.LAMBDA: _ResolutionMode.EXTERNAL_LAMBDA,
    TargetType.ALB: _ResolutionMode.APPLICATION_LOAD_BALANCER,
}


class _Relationships:
    def __init__(self, resources: list[Resource], evidence: dict[str, Evidence]) -> None:
        self.evidence = evidence
        self.index: dict[tuple[ResourceType, str], list[Resource]] = {}
        for resource in resources:
            if resource.identity.physical_id:
                key = (resource.resource_type, resource.identity.physical_id)
                self.index.setdefault(key, []).append(resource)
        self.dependencies: list[Dependency] = []

    def _link(
        self,
        resource: Resource,
        pointer: str,
        ref: str,
        kind: ResourceType,
        relationship: DependencyRelationship,
        attributes: dict[str, Any],
        *,
        mode: _ResolutionMode = _ResolutionMode.LOCAL,
    ) -> None:
        citations = resource.fact_evidence.get(pointer, [])
        if not citations:
            return
        path = self.evidence[citations[0]].configuration_path
        self.dependencies.append(
            Dependency(
                source_id=resource.id,
                target=_resolve_target(self.index, kind, ref, path, mode),
                relationship=relationship,
                evidence_ids=citations,
                attributes={"observed_identifier": ref, **attributes},
            )
        )

    def _load_balancer(self, resource: LoadBalancer) -> None:
        for field, kind, attachment in _MEMBERSHIPS:
            for position, ref in enumerate(getattr(resource, field) or []):
                self._link(
                    resource,
                    f"/{field}/{position}",
                    ref,
                    kind,
                    DependencyRelationship.ATTACHED_TO,
                    {"kind": attachment},
                )

    def _listener_or_rule(self, resource: Listener | ListenerRule) -> None:
        field, parent_type, attachment = _PARENTS[resource.resource_type]
        parent = getattr(resource, field)
        if parent:
            self._link(
                resource,
                f"/{field}",
                parent,
                parent_type,
                DependencyRelationship.ATTACHED_TO,
                {"kind": attachment},
            )
        for position, action in enumerate(resource.actions or []):
            if action.type == ListenerActionType.FORWARD:
                self._forward(resource, position, action)

    def _forward(
        self, resource: Listener | ListenerRule, position: int, action: ListenerAction
    ) -> None:
        # ForwardConfig is authoritative when present; don't double-count its direct ARN.
        groups = [
            (group.arn, group.weight, f"/actions/{position}/forward_groups/{i}/arn")
            for i, group in enumerate(action.forward_groups)
        ]
        if not groups and action.target_group_arn:
            groups = [(action.target_group_arn, None, f"/actions/{position}/target_group_arn")]
        for ref, weight, pointer in groups:
            self._link(
                resource,
                pointer,
                ref,
                ResourceType.ELBV2_TARGET_GROUP,
                DependencyRelationship.FORWARDS_TO,
                {"action_index": position, "action_order": action.order, "weight": weight},
            )

    def _target_group(self, resource: TargetGroup) -> None:
        mode = _TARGET_MODES.get(resource.target_type, _ResolutionMode.LOCAL)
        kind = (
            ResourceType.ELBV2_LOAD_BALANCER
            if resource.target_type == TargetType.ALB
            else ResourceType.EC2_INSTANCE
        )
        for position, target in enumerate(resource.targets or []):
            self._link(
                resource,
                f"/targets/{position}/id",
                target.id,
                kind,
                DependencyRelationship.FORWARDS_TO,
                {
                    "port": target.port,
                    "availability_zone": target.availability_zone,
                    "health_state": target.health_state,
                    "health_reason": target.health_reason,
                },
                mode=mode,
            )


_HANDLERS: dict[ResourceType, Callable[[_Relationships, Any], None]] = {
    ResourceType.ELBV2_LOAD_BALANCER: _Relationships._load_balancer,
    ResourceType.ELBV2_LISTENER: _Relationships._listener_or_rule,
    ResourceType.ELBV2_LISTENER_RULE: _Relationships._listener_or_rule,
    ResourceType.ELBV2_TARGET_GROUP: _Relationships._target_group,
}


def resolve_relationships(roots: list[Resource], evidence: dict[str, Evidence]) -> list[Dependency]:
    resources = list(walk_resources(roots))
    relationships = _Relationships(resources, evidence)
    for resource in resources:
        handler = _HANDLERS.get(resource.resource_type)
        if handler is not None:
            handler(relationships, resource)
    return relationships.dependencies
