"""Principal references from stored policy documents, never effective access.

Shared by IAM role trust policies and SQS queue policies so both classify principals
by the same rules. Each principal becomes one dependency citing the principal entry and
its whole sanitized statement; `Effect` and `Condition` stay in that statement evidence.
"""

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from ..models.architecture import (
    Evidence,
    ExternalTarget,
    LocalTarget,
    Resource,
    UnresolvedTarget,
)
from ..models.enums import AwsService, EvidenceValueStatus, TrustPrincipalKind
from ..models.resources.iam import IamRole, sanitize_document
from .evidence_builder import EvidenceBuilder

Emit = Callable[..., None]


def principal_target(
    identifier: Any,
    kind: TrustPrincipalKind,
    negative: bool,
    arn_groups: Mapping[str, list[Resource]],
    path: str,
):
    """The recorded target for one principal entry; ambiguous forms stay unresolved."""
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


def arn_groups(resources: Iterable[Resource]) -> dict[str, list[Resource]]:
    """Resources grouped by ARN, so ambiguous principal ARNs stay detectable."""
    groups: dict[str, list[Resource]] = {}
    for resource in resources:
        arn = getattr(resource, "arn", None)
        if arn is not None:
            groups.setdefault(arn, []).append(resource)
    return groups


_PRINCIPAL_KINDS = {
    "AWS": TrustPrincipalKind.AWS,
    "Service": TrustPrincipalKind.SERVICE,
    "Federated": TrustPrincipalKind.FEDERATED,
}


def _principal_entries(statement: Mapping[str, Any], path: str, negative: bool):
    """Yield (kind, identifier, path) for every principal entry of one statement."""
    principal = statement.get("NotPrincipal" if negative else "Principal")
    principal_path = path + (".NotPrincipal" if negative else ".Principal")
    if isinstance(principal, dict) and principal:
        groups = [(key, value, f"{principal_path}.{key}") for key, value in principal.items()]
    else:
        groups = [("Unknown", principal, principal_path)]
    for key, value, source_path in groups:
        kind = _PRINCIPAL_KINDS.get(key, TrustPrincipalKind.UNKNOWN)
        items = enumerate(value) if isinstance(value, list) and value else [(None, value)]
        for offset, identifier in items:
            yield kind, identifier, source_path + (f"[{offset}]" if offset is not None else "")


def _statement_evidence(
    resource: Resource, operation: str, path: str, statement: Any, builder: EvidenceBuilder
) -> str:
    """Record the whole sanitized statement, keeping redaction paths relative to it."""
    safe, paths = sanitize_document({"Statement": statement})
    statement_id = builder.source_evidence(resource, operation, path, safe["Statement"])
    if paths:
        item = builder.evidence[statement_id]
        item.redacted_paths = [p.removeprefix("/Statement") for p in paths]
        item.value_status = EvidenceValueStatus.REDACTED
    return statement_id


def policy_principal_links(
    resource: Resource,
    document: Mapping[str, Any] | None,
    origin: Evidence | None,
    builder: EvidenceBuilder,
    groups: Mapping[str, list[Resource]],
    emit: Emit,
) -> None:
    """Emit one dependency per principal entry of each statement in `document`.

    `origin` is the cited evidence of the whole document; statement and principal paths
    extend its configuration path. Nothing is emitted without a document and a citation.
    """
    if document is None or origin is None:
        return
    operation = origin.locator.split(":", 1)[1]
    statements = document["Statement"]
    entries = enumerate(statements) if isinstance(statements, list) else [(None, statements)]
    for index, statement in entries:
        suffix = f"[{index}]" if index is not None else ""
        path = f"{origin.configuration_path}.Statement{suffix}"
        statement_id = _statement_evidence(resource, operation, path, statement, builder)
        negative = "NotPrincipal" in statement
        for kind, identifier, entry_path in _principal_entries(statement, path, negative):
            principal_id = builder.source_evidence(resource, operation, entry_path, identifier)
            emit(
                principal_target(identifier, kind, negative, groups, entry_path),
                [principal_id, statement_id],
                principal_kind=kind.value,
                not_principal=negative,
            )
