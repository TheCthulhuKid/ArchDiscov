"""Containment and observed forwarding references, without reachability inference."""

from collections.abc import Iterable
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
from ...models.load_balancing import Listener, ListenerRule, LoadBalancer, TargetGroup


def walk_resources(roots: Iterable[Resource]) -> Iterable[Resource]:
    for resource in roots:
        yield resource
        yield from walk_resources(resource.children)


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


def resolve_relationships(roots: list[Resource], evidence: dict[str, Evidence]) -> list[Dependency]:
    resources = list(walk_resources(roots))
    index: dict[tuple[ResourceType, str], list[Resource]] = {}
    for resource in resources:
        if resource.identity.physical_id:
            index.setdefault((resource.resource_type, resource.identity.physical_id), []).append(
                resource
            )
    dependencies: list[Dependency] = []

    def resolve(kind: ResourceType, ref: str, path: str, *, alb: bool = False) -> DependencyTarget:
        matches = index.get((kind, ref), [])
        if alb:
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

    def link(
        resource: Resource,
        pointer: str,
        ref: str,
        kind: ResourceType,
        relationship: DependencyRelationship,
        attributes: dict[str, Any],
        *,
        external: bool = False,
        ip: bool = False,
        alb: bool = False,
    ) -> None:
        citations = resource.fact_evidence.get(pointer, [])
        if not citations:
            return
        path = evidence[citations[0]].configuration_path
        if ip:
            target: DependencyTarget = UnresolvedTarget(
                reason="IP registration is observed; resource ownership is not inferred",
                source_path=path,
            )
        elif external:
            parts = ref.split(":", 5)
            target = ExternalTarget(
                identifier=ref,
                service=AwsService.LAMBDA,
                account_id=parts[4]
                if len(parts) == 6 and len(parts[4]) == 12 and parts[4].isdigit()
                else None,
                aws_resource_type="AWS::Lambda::Function",
            )
        else:
            target = resolve(kind, ref, path, alb=alb)
        dependencies.append(
            Dependency(
                source_id=resource.id,
                target=target,
                relationship=relationship,
                evidence_ids=citations,
                attributes={"observed_identifier": ref, **attributes},
            )
        )

    for resource in resources:
        if isinstance(resource, LoadBalancer):
            for field, kind, attachment in (
                ("subnet_ids", ResourceType.SUBNET, AttachmentKind.SUBNET_MEMBERSHIP),
                (
                    "security_group_ids",
                    ResourceType.SECURITY_GROUP,
                    AttachmentKind.SECURITY_GROUP_MEMBERSHIP,
                ),
            ):
                for position, ref in enumerate(getattr(resource, field) or []):
                    link(
                        resource,
                        f"/{field}/{position}",
                        ref,
                        kind,
                        DependencyRelationship.ATTACHED_TO,
                        {"kind": attachment},
                    )
        if isinstance(resource, (Listener, ListenerRule)):
            parent = (
                resource.load_balancer_arn
                if isinstance(resource, Listener)
                else resource.listener_arn
            )
            pointer = "/load_balancer_arn" if isinstance(resource, Listener) else "/listener_arn"
            parent_type = (
                ResourceType.ELBV2_LOAD_BALANCER
                if isinstance(resource, Listener)
                else ResourceType.ELBV2_LISTENER
            )
            attachment = (
                AttachmentKind.LISTENER_LOAD_BALANCER
                if isinstance(resource, Listener)
                else AttachmentKind.RULE_LISTENER
            )
            if parent:
                link(
                    resource,
                    pointer,
                    parent,
                    parent_type,
                    DependencyRelationship.ATTACHED_TO,
                    {"kind": attachment},
                )
            for position, action in enumerate(resource.actions or []):
                if action.type != ListenerActionType.FORWARD:
                    continue
                # ForwardConfig is authoritative when present; don't double-count its direct ARN.
                groups = [
                    (group.arn, group.weight, f"/actions/{position}/forward_groups/{i}/arn")
                    for i, group in enumerate(action.forward_groups)
                ]
                if not groups and action.target_group_arn:
                    groups = [
                        (action.target_group_arn, None, f"/actions/{position}/target_group_arn")
                    ]
                for ref, weight, pointer in groups:
                    link(
                        resource,
                        pointer,
                        ref,
                        ResourceType.ELBV2_TARGET_GROUP,
                        DependencyRelationship.FORWARDS_TO,
                        {"action_index": position, "action_order": action.order, "weight": weight},
                    )
        if isinstance(resource, TargetGroup):
            for position, target in enumerate(resource.targets or []):
                kind = (
                    ResourceType.ELBV2_LOAD_BALANCER
                    if resource.target_type == TargetType.ALB
                    else ResourceType.EC2_INSTANCE
                )
                link(
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
                    external=resource.target_type == TargetType.LAMBDA,
                    ip=resource.target_type == TargetType.IP,
                    alb=resource.target_type == TargetType.ALB,
                )
    return dependencies
