"""Observed IAM references, not effective access or proof of role assumption."""

import re
from collections.abc import Iterable
from uuid import UUID

from ...models.architecture import (
    Dependency,
    Evidence,
    ExternalTarget,
    LocalTarget,
    Resource,
    UnresolvedTarget,
)
from ...models.enums import (
    AttachmentKind,
    AwsRegion,
    AwsService,
    DependencyRelationship,
)
from ...models.resource_tree import walk_resources
from ...models.resources.iam import IamInstanceProfile, IamManagedPolicy, IamRole
from ..evidence_builder import EvidenceBuilder
from ..policy_principals import arn_groups, policy_principal_links


def resolve_relationships(
    snapshot_id: UUID,
    account_id: str,
    global_resources: list[Resource],
    regional_resources: Iterable[Resource],
    evidence: dict[str, Evidence],
) -> dict[AwsRegion, list[Dependency]]:
    builder = EvidenceBuilder(snapshot_id, account_id, AwsRegion.GLOBAL, AwsService.IAM)
    groups = arn_groups(global_resources)
    dependencies: dict[AwsRegion, list[Dependency]] = {}

    def emit(source: Resource, kind: AttachmentKind, target, citations: list[str], **attrs):
        dependencies.setdefault(source.identity.region, []).append(
            Dependency(
                source_id=source.id,
                target=target,
                relationship=DependencyRelationship.ATTACHED_TO,
                attributes={"kind": kind.value, **attrs},
                evidence_ids=citations,
            )
        )

    def reference(source: Resource, arn: str, pointer: str, kind: AttachmentKind, cls):
        citations = source.fact_evidence.get(pointer)
        if not citations:
            return
        path = evidence[citations[0]].configuration_path
        matches = groups.get(arn, [])
        if len(matches) == 1 and isinstance(matches[0], cls):
            target = LocalTarget(resource_id=matches[0].id)
        else:
            target = UnresolvedTarget(
                reason="Referenced IAM resource was unavailable or ambiguous in this collection",
                source_path=path,
            )
            match = re.fullmatch(r"arn:aws[a-z-]*:iam::([0-9]{12}|aws):(.+)", arn)
            if match and match[1] not in (account_id, "aws"):
                target = ExternalTarget(identifier=arn, account_id=match[1], service=AwsService.IAM)
        emit(source, kind, target, citations)

    for resource in walk_resources(regional_resources):
        arn = resource.attributes.get("iam_instance_profile_arn")
        if isinstance(arn, str):
            reference(
                resource,
                arn,
                "/attributes/iam_instance_profile_arn",
                AttachmentKind.INSTANCE_PROFILE,
                IamInstanceProfile,
            )
    for resource in global_resources:
        if isinstance(resource, IamInstanceProfile):
            for index, arn in enumerate(resource.role_arns):
                reference(
                    resource, arn, f"/role_arns/{index}", AttachmentKind.PROFILE_ROLE, IamRole
                )
        if not isinstance(resource, IamRole):
            continue
        for index, arn in enumerate(resource.attributes.get("attached_policy_arns", [])):
            reference(
                resource,
                arn,
                f"/attributes/attached_policy_arns/{index}",
                AttachmentKind.ROLE_POLICY,
                IamManagedPolicy,
            )
        if resource.boundary_arn:
            reference(
                resource,
                resource.boundary_arn,
                "/boundary_arn",
                AttachmentKind.PERMISSIONS_BOUNDARY,
                IamManagedPolicy,
            )
        citations = resource.fact_evidence.get("/trust_document")
        policy_principal_links(
            resource,
            resource.trust_document,
            evidence[citations[0]] if citations else None,
            builder,
            groups,
            lambda target, ids, **attrs: emit(
                resource, AttachmentKind.TRUST_PRINCIPAL, target, ids, **attrs
            ),
        )
    evidence.update(builder.evidence)
    return dependencies
