"""Observed IAM references, not effective access or proof of role assumption."""

import re
from collections.abc import Callable, Iterable
from functools import partial
from typing import Any
from uuid import UUID

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
    AwsRegion,
    AwsService,
    DependencyRelationship,
    ResourceType,
)
from ...models.resource_tree import walk_resources
from ...models.resources.iam import IamInstanceProfile, IamManagedPolicy, IamRole
from ..evidence_builder import EvidenceBuilder
from ..policy_principals import arn_groups, policy_principal_links


class _Relationships:
    def __init__(
        self,
        builder: EvidenceBuilder,
        global_resources: list[Resource],
        evidence: dict[str, Evidence],
    ) -> None:
        self.builder = builder
        self.evidence = evidence
        self.groups = arn_groups(global_resources)
        self.dependencies: dict[AwsRegion, list[Dependency]] = {}

    def _emit(
        self,
        source: Resource,
        kind: AttachmentKind,
        target: DependencyTarget,
        citations: list[str],
        **attrs: Any,
    ) -> None:
        self.dependencies.setdefault(source.identity.region, []).append(
            Dependency(
                source_id=source.id,
                target=target,
                relationship=DependencyRelationship.ATTACHED_TO,
                attributes={"kind": kind.value, **attrs},
                evidence_ids=citations,
            )
        )

    def _target(self, arn: str, path: str, cls: type[Resource]) -> DependencyTarget:
        matches = self.groups.get(arn, [])
        if len(matches) == 1 and isinstance(matches[0], cls):
            return LocalTarget(resource_id=matches[0].id)
        match = re.fullmatch(r"arn:aws[a-z-]*:iam::([0-9]{12}|aws):(.+)", arn)
        if match and match[1] not in (self.builder.account_id, "aws"):
            return ExternalTarget(identifier=arn, account_id=match[1], service=AwsService.IAM)
        return UnresolvedTarget(
            reason="Referenced IAM resource was unavailable or ambiguous in this collection",
            source_path=path,
        )

    def _reference(
        self,
        source: Resource,
        arn: str,
        pointer: str,
        kind: AttachmentKind,
        cls: type[Resource],
    ) -> None:
        citations = source.fact_evidence.get(pointer)
        if not citations:
            return
        path = self.evidence[citations[0]].configuration_path
        self._emit(source, kind, self._target(arn, path, cls), citations)

    def _regional(self, resource: Resource) -> None:
        arn = resource.attributes.get("iam_instance_profile_arn")
        if isinstance(arn, str):
            self._reference(
                resource,
                arn,
                "/attributes/iam_instance_profile_arn",
                AttachmentKind.INSTANCE_PROFILE,
                IamInstanceProfile,
            )

    def _instance_profile(self, profile: IamInstanceProfile) -> None:
        for index, arn in enumerate(profile.role_arns):
            self._reference(
                profile, arn, f"/role_arns/{index}", AttachmentKind.PROFILE_ROLE, IamRole
            )

    def _role(self, role: IamRole) -> None:
        for index, arn in enumerate(role.attributes.get("attached_policy_arns", [])):
            self._reference(
                role,
                arn,
                f"/attributes/attached_policy_arns/{index}",
                AttachmentKind.ROLE_POLICY,
                IamManagedPolicy,
            )
        if role.boundary_arn:
            self._reference(
                role,
                role.boundary_arn,
                "/boundary_arn",
                AttachmentKind.PERMISSIONS_BOUNDARY,
                IamManagedPolicy,
            )
        citations = role.fact_evidence.get("/trust_document")
        policy_principal_links(
            role,
            role.trust_document,
            self.evidence[citations[0]] if citations else None,
            self.builder,
            self.groups,
            partial(self._emit, role, AttachmentKind.TRUST_PRINCIPAL),
        )


_GLOBAL_HANDLERS: dict[ResourceType, Callable[[_Relationships, Any], None]] = {
    ResourceType.INSTANCE_PROFILE: _Relationships._instance_profile,
    ResourceType.IAM_ROLE: _Relationships._role,
}


def resolve_relationships(
    snapshot_id: UUID,
    account_id: str,
    global_resources: list[Resource],
    regional_resources: Iterable[Resource],
    evidence: dict[str, Evidence],
) -> dict[AwsRegion, list[Dependency]]:
    builder = EvidenceBuilder(snapshot_id, account_id, AwsRegion.GLOBAL, AwsService.IAM)
    relationships = _Relationships(builder, global_resources, evidence)
    for resource in walk_resources(regional_resources):
        relationships._regional(resource)
    for resource in global_resources:
        handler = _GLOBAL_HANDLERS.get(resource.resource_type)
        if handler is not None:
            handler(relationships, resource)
    evidence.update(builder.evidence)
    return relationships.dependencies
