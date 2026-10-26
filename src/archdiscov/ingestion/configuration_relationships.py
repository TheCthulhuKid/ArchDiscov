"""Observed configuration references, never effective access or inferred attachments."""

from collections.abc import Iterable, Iterator

from ..models.architecture import (
    Dependency,
    DependencyTarget,
    Evidence,
    ExternalTarget,
    LocalTarget,
    Resource,
    UnresolvedTarget,
)
from ..models.enums import (
    AttachmentKind,
    AwsRegion,
    AwsService,
    DependencyRelationship,
    ResourceType,
)
from ..models.resource_tree import walk_resources as walk_resources


def merge_resources(roots: list[Resource], resources: list[Resource]) -> None:
    index = {
        (r.resource_type, r.identity.physical_id): r for r in [*walk_resources(roots), *resources]
    }
    stored = {r.id for r in walk_resources(roots)}
    for resource in resources:
        if resource.id in stored:
            continue
        parent_kind, parent_ref = ResourceType.VPC, getattr(resource, "vpc_id", None)
        if resource.resource_type in {ResourceType.ECS_SERVICE, ResourceType.ECS_TASK}:
            parent_kind, parent_ref = (
                ResourceType.ECS_CLUSTER,
                getattr(resource, "cluster_arn", None),
            )
        parent = index.get((parent_kind, parent_ref)) if parent_ref else None
        if parent is not None:
            parent.children.append(resource)
        else:
            if parent_ref:
                resource.attributes["unresolved_parent"] = {
                    "identifier": parent_ref,
                    "resource_type": parent_kind.value,
                    "reason": "parent was not collected",
                }
            roots.append(resource)
        stored.add(resource.id)


_Reference = tuple[str, ResourceType, AwsService, AttachmentKind]

_SCALAR_REFERENCES: tuple[_Reference, ...] = (
    ("cluster_arn", ResourceType.ECS_CLUSTER, AwsService.ECS, AttachmentKind.CLUSTER_MEMBERSHIP),
    (
        "task_definition_arn",
        ResourceType.ECS_TASK_DEFINITION,
        AwsService.ECS,
        AttachmentKind.TASK_DEFINITION,
    ),
    ("execution_role_arn", ResourceType.IAM_ROLE, AwsService.IAM, AttachmentKind.EXECUTION_ROLE),
    ("task_role_arn", ResourceType.IAM_ROLE, AwsService.IAM, AttachmentKind.TASK_ROLE),
    ("role_arn", ResourceType.IAM_ROLE, AwsService.IAM, AttachmentKind.FUNCTION_ROLE),
    (
        "cluster_identifier",
        ResourceType.RDS_CLUSTER,
        AwsService.RDS,
        AttachmentKind.DATABASE_CLUSTER,
    ),
    (
        "db_subnet_group_name",
        ResourceType.RDS_SUBNET_GROUP,
        AwsService.RDS,
        AttachmentKind.DATABASE_SUBNET_GROUP,
    ),
    (
        "dead_letter_target_arn",
        ResourceType.SQS_QUEUE,
        AwsService.SQS,
        AttachmentKind.DEAD_LETTER_QUEUE,
    ),
)
_LIST_REFERENCES: tuple[_Reference, ...] = (
    ("subnet_ids", ResourceType.SUBNET, AwsService.EC2, AttachmentKind.SUBNET_MEMBERSHIP),
    (
        "security_group_ids",
        ResourceType.SECURITY_GROUP,
        AwsService.EC2,
        AttachmentKind.SECURITY_GROUP_MEMBERSHIP,
    ),
    (
        "network_interface_ids",
        ResourceType.NETWORK_INTERFACE,
        AwsService.EC2,
        AttachmentKind.INSTANCE_INTERFACE,
    ),
    ("associated_role_arns", ResourceType.IAM_ROLE, AwsService.IAM, AttachmentKind.ASSOCIATED_ROLE),
)
_SERVICES = {AwsService.ECS, AwsService.LAMBDA, AwsService.RDS, AwsService.SQS}


def arn_scope(ref: str) -> tuple[str | None, str | None]:
    """The account and region an ARN names; None for a part that is absent or not an ARN."""
    parts = ref.split(":", 5)
    if len(parts) != 6:
        return None, None
    account = parts[4] if len(parts[4]) == 12 and parts[4].isdigit() else None
    return account, parts[3] or None


def _index(resources: list[Resource]) -> dict[tuple[ResourceType, str], list[Resource]]:
    index: dict[tuple[ResourceType, str], list[Resource]] = {}
    for resource in resources:
        identifiers = {
            resource.identity.physical_id,
            getattr(resource, "arn", None),
            getattr(resource, "identifier", None),
            resource.name if resource.resource_type == ResourceType.RDS_SUBNET_GROUP else None,
        }
        for identifier in identifiers - {None}:
            index.setdefault((resource.resource_type, identifier), []).append(resource)
    return index


def _references(
    resource: Resource,
) -> Iterator[tuple[str, str, ResourceType, AwsService, AttachmentKind]]:
    """(pointer, observed identifier, kind, service, attachment) for each reference set."""
    for field, kind, service, attachment in _SCALAR_REFERENCES:
        ref = getattr(resource, field, None)
        if ref:
            yield f"/{field}", ref, kind, service, attachment
    for field, kind, service, attachment in _LIST_REFERENCES:
        for position, ref in enumerate(getattr(resource, field, None) or []):
            yield f"/{field}/{position}", ref, kind, service, attachment


def _target(
    index: dict[tuple[ResourceType, str], list[Resource]],
    resource: Resource,
    ref: str,
    kind: ResourceType,
    service: AwsService,
    path: str,
) -> DependencyTarget:
    matches = [
        match
        for match in index.get((kind, ref), [])
        if match.identity.account_id == resource.identity.account_id
        and (service == AwsService.IAM or match.identity.region == resource.identity.region)
    ]
    if len(matches) == 1:
        return LocalTarget(resource_id=matches[0].id)
    account, region = arn_scope(ref)
    if account and (
        account != resource.identity.account_id
        or (region and region != resource.identity.region.value)
    ):
        return ExternalTarget(
            identifier=ref, account_id=account, service=service, aws_resource_type=kind.value
        )
    return UnresolvedTarget(
        reason="target was not uniquely collected with the expected resource type",
        source_path=path,
    )


def resolve_relationships(
    roots: Iterable[Resource], evidence: dict[str, Evidence]
) -> dict[AwsRegion, list[Dependency]]:
    resources = list(walk_resources(roots))
    index = _index(resources)
    result: dict[AwsRegion, list[Dependency]] = {}
    for resource in resources:
        if resource.identity.service not in _SERVICES:
            continue
        for pointer, ref, kind, service, attachment in _references(resource):
            citations = resource.fact_evidence.get(pointer, [])
            if not citations:
                continue
            path = evidence[citations[0]].configuration_path
            result.setdefault(resource.identity.region, []).append(
                Dependency(
                    source_id=resource.id,
                    target=_target(index, resource, ref, kind, service, path),
                    relationship=DependencyRelationship.ATTACHED_TO,
                    evidence_ids=citations,
                    attributes={"kind": attachment.value, "observed_identifier": ref},
                )
            )
    return result
