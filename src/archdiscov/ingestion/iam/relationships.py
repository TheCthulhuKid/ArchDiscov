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
    EvidenceValueStatus,
    TrustPrincipalKind,
)
from ...models.iam import IamInstanceProfile, IamManagedPolicy, IamRole, sanitize_document
from ..evidence_builder import EvidenceBuilder


def _walk(resources: Iterable[Resource]) -> Iterable[Resource]:
    for resource in resources:
        yield resource
        yield from _walk(resource.children)


def resolve_relationships(
    snapshot_id: UUID,
    account_id: str,
    global_resources: list[Resource],
    regional_resources: Iterable[Resource],
    evidence: dict[str, Evidence],
) -> dict[AwsRegion, list[Dependency]]:
    builder = EvidenceBuilder(snapshot_id, account_id, AwsRegion.GLOBAL, AwsService.IAM)
    arn_groups: dict[str, list[Resource]] = {}
    for resource in global_resources:
        arn_groups.setdefault(resource.arn, []).append(resource)
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
        matches = arn_groups.get(arn, [])
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

    for resource in _walk(regional_resources):
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
        _trust_links(resource, arn_groups, evidence, builder, emit)
    evidence.update(builder.evidence)
    return dependencies


def _trust_links(role, arn_groups, evidence, builder, emit):
    citations = role.fact_evidence.get("/trust_document")
    if role.trust_document is None or not citations:
        return
    origin = evidence[citations[0]]
    statements = role.trust_document["Statement"]
    entries = enumerate(statements) if isinstance(statements, list) else [(None, statements)]
    for index, statement in entries:
        path = (
            origin.configuration_path + ".Statement" + (f"[{index}]" if index is not None else "")
        )
        safe, paths = sanitize_document({"Statement": statement})
        statement_id = builder.source_evidence(
            role, origin.locator.split(":", 1)[1], path, safe["Statement"]
        )
        if paths:
            item = builder.evidence[statement_id]
            item.redacted_paths = [p.removeprefix("/Statement") for p in paths]
            item.value_status = EvidenceValueStatus.REDACTED
        negative = "NotPrincipal" in statement
        principal = statement.get("NotPrincipal" if negative else "Principal")
        principal_path = path + (".NotPrincipal" if negative else ".Principal")
        if isinstance(principal, dict) and principal:
            values = [(key, value, principal_path + "." + key) for key, value in principal.items()]
        else:
            values = [("Unknown", principal, principal_path)]
        for key, value, source_path in values:
            kind = {
                "AWS": TrustPrincipalKind.AWS,
                "Service": TrustPrincipalKind.SERVICE,
                "Federated": TrustPrincipalKind.FEDERATED,
            }.get(key, TrustPrincipalKind.UNKNOWN)
            entries = enumerate(value) if isinstance(value, list) and value else [(None, value)]
            for offset, identifier in entries:
                entry_path = source_path + (f"[{offset}]" if offset is not None else "")
                principal_id = builder.source_evidence(
                    role, origin.locator.split(":", 1)[1], entry_path, identifier
                )
                target = _principal_target(identifier, kind, negative, arn_groups, entry_path)
                emit(
                    role,
                    AttachmentKind.TRUST_PRINCIPAL,
                    target,
                    [principal_id, statement_id],
                    principal_kind=kind.value,
                    not_principal=negative,
                )


def _principal_target(identifier, kind, negative, arn_groups, path):
    if (
        negative
        or kind == TrustPrincipalKind.UNKNOWN
        or not isinstance(identifier, str)
        or not identifier.strip()
        or any(c in identifier for c in "*?")
    ):
        return UnresolvedTarget(
            reason="Principal form is wildcard, negative, unsupported, or ambiguous",
            source_path=path,
        )
    if kind == TrustPrincipalKind.AWS:
        matches = arn_groups.get(identifier, [])
        if len(matches) == 1 and isinstance(matches[0], IamRole):
            return LocalTarget(resource_id=matches[0].id)
        if len(matches) > 1:
            return UnresolvedTarget(
                reason="Principal ARN matches multiple stored resources", source_path=path
            )
        account = re.fullmatch(r"[0-9]{12}", identifier)
        arn = re.fullmatch(r"arn:aws[a-z-]*:iam::([0-9]{12}):[^\s]+", identifier)
        principal_id = re.fullmatch(r"(?:AROA|AIDA)[A-Z0-9]+", identifier)
        if not (account or arn or principal_id):
            return UnresolvedTarget(
                reason="AWS principal identifier is unsupported", source_path=path
            )
        return ExternalTarget(
            identifier=identifier,
            account_id=identifier if account else arn[1] if arn else None,
            service=AwsService.IAM,
        )
    return ExternalTarget(identifier=identifier, service=AwsService.IAM)
