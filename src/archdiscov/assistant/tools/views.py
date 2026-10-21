"""Allowlisted views of recorded data, shared by the tools and the run record.

Each view copies only the fields a model needs. Stable IDs, enums, and pattern-validated
identity parts (account, subscription, region, location, service) are returned as
values; anything taken from cloud configuration or source files (names, tags, physical
IDs, native types, attribute and evidence values, source locators, failure text) is
returned as `UntrustedText`.
"""

import json
from collections.abc import Callable, Iterable, Sequence
from typing import Literal, Self

from pydantic import JsonValue, NonNegativeInt, model_validator

from archdiscov.graph import GraphIndex
from archdiscov.graph.containment import ContainmentEdge
from archdiscov.graph.dependencies import DependencyEdge, TerminalTarget
from archdiscov.graph.identity import native_resource_type
from archdiscov.models.analysis.database_access import DatabaseAccess
from archdiscov.models.analysis.exposure import InternetExposure
from archdiscov.models.analysis.impact import SecurityGroupImpact
from archdiscov.models.analysis.references import AnalysisLimitation, PathStep
from archdiscov.models.analysis.results import AnalysisFinding
from archdiscov.models.analysis.trust import CrossAccountTrust
from archdiscov.models.architecture import LocalTarget, UnresolvedTarget
from archdiscov.models.cloud.contracts import (
    AwsTargetScope,
    CloudEvidence,
    CloudExternalTarget,
    CloudOperationCoverage,
    CloudTarget,
)
from archdiscov.models.cloud.scope import AzureScope
from archdiscov.models.enums import (
    AnalysisStatus,
    AttachmentKind,
    CloudPlatform,
    CoverageStatus,
    DependencyRelationship,
    EvidenceValueStatus,
    LifecycleAction,
    QueryDirection,
    QueryLimitationKind,
    SourceType,
)
from archdiscov.models.json_pointer import escape_pointer_token
from archdiscov.models.query.edges import QueryTraversalStep
from archdiscov.models.query.limitations import QueryLimitation
from archdiscov.models.resource_base import Model, Resource
from archdiscov.models.validation_errors import validation_error

from .contracts import (
    BoundedList,
    Truncation,
    TruncationReason,
    UntrustedText,
    sanitized,
    untrusted,
)


def _maybe(text: str | None) -> UntrustedText | None:
    return None if text is None else untrusted(text)


def json_text(value: JsonValue) -> UntrustedText:
    return untrusted(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


def capped[T](items: Sequence[T], requested: int, effective: int) -> BoundedList[T]:
    """Keep `effective` items of `requested`; a smaller `effective` was forced by size."""
    kept = tuple(items[:effective])
    if len(kept) == len(items):
        return BoundedList(items=kept)
    reasons = []
    if len(items) > requested:
        reasons.append(TruncationReason.RESULT_LIMIT)
    if effective < requested:
        reasons.append(TruncationReason.SIZE_LIMIT)
    return BoundedList(
        items=kept,
        truncation=Truncation(reasons=tuple(reasons), returned=len(kept), available=len(items)),
    )


def fit[O: Model](build: Callable[[int], O], requested: int, max_chars: int) -> O:
    """Build with `requested` items per list, halving until the JSON fits `max_chars`."""
    effective = requested
    output = build(effective)
    while effective > 1 and len(output.model_dump_json()) > max_chars:
        effective = max(1, effective // 2)
        output = build(effective)
    return output


class TagView(Model):
    key: UntrustedText
    value: UntrustedText | None


class ResourceRef(Model):
    resource_id: str


class ResourceView(Model):
    resource_id: str
    platform: CloudPlatform
    native_type: UntrustedText
    service: str
    location: str | None
    physical_id: UntrustedText | None
    logical_address: UntrustedText | None
    name: UntrustedText | None
    tags: BoundedList[TagView]
    # Terraform plan actions: planned changes, not deployed state.
    lifecycle_actions: tuple[LifecycleAction, ...] = ()


def resource_view(resource: Resource, tag_limit: int) -> ResourceView:
    identity = resource.identity
    tags = resource.attributes.get("tags")
    tag_views = (
        [
            TagView(key=untrusted(key), value=None if value is None else _tag_value(value))
            for key, value in sorted(tags.items())
        ]
        if isinstance(tags, dict)
        else []
    )
    location = getattr(identity, "region", None) or getattr(identity, "location", None)
    return ResourceView(
        resource_id=resource.id,
        platform=identity.platform,
        native_type=untrusted(native_resource_type(resource)),
        service=str(identity.service),
        location=None if location is None else str(location),
        physical_id=_maybe(identity.physical_id),
        logical_address=_maybe(identity.logical_address),
        name=_maybe(resource.name),
        tags=capped(tag_views, tag_limit, tag_limit),
        lifecycle_actions=() if resource.lifecycle is None else tuple(resource.lifecycle.actions),
    )


def _tag_value(value: JsonValue) -> UntrustedText:
    return untrusted(value) if isinstance(value, str) else json_text(value)


class AttributeView(Model):
    # A resource-relative pointer, as in fact citations (`/cidr_blocks`, `/attributes/x`).
    key: UntrustedText
    # Compact JSON of the sanitized value.
    value: UntrustedText


# Shown elsewhere in the views, or structure rather than configuration.
_NON_ATTRIBUTE_FIELDS = {
    "id",
    "identity",
    "name",
    "resource_type",
    "attributes",
    "evidence_ids",
    "fact_evidence",
    "children",
    "lifecycle",
}


def attribute_views(resource: Resource) -> list[AttributeView]:
    """Typed fields and generic attributes (without tags), sanitized, in pointer order."""
    typed = resource.model_dump(mode="json", exclude=_NON_ATTRIBUTE_FIELDS)
    values = {f"/{escape_pointer_token(key)}": value for key, value in sanitized(typed).items()}
    for key, value in sanitized(resource.attributes).items():
        if key != "tags":
            values[f"/attributes/{escape_pointer_token(key)}"] = value
    return [
        AttributeView(key=untrusted(key), value=json_text(value))
        for key, value in sorted(values.items())
    ]


class FactView(Model):
    fact_path: UntrustedText
    evidence_ids: tuple[str, ...]


def fact_views(resource: Resource) -> list[FactView]:
    return [
        FactView(fact_path=untrusted(path), evidence_ids=tuple(ids))
        for path, ids in sorted(resource.fact_evidence.items())
    ]


class EvidenceView(Model):
    evidence_id: str
    resource_id: str
    source: SourceType
    location: str | None
    service: str
    locator: UntrustedText
    configuration_path: UntrustedText
    value: UntrustedText | None
    value_status: EvidenceValueStatus
    redacted_path_count: NonNegativeInt
    limitation: UntrustedText | None


def evidence_view(evidence_id: str, evidence: CloudEvidence) -> EvidenceView:
    return EvidenceView(
        evidence_id=evidence_id,
        resource_id=evidence.resource_id,
        source=evidence.source,
        location=evidence.location,
        service=evidence.service,
        locator=untrusted(evidence.locator),
        configuration_path=untrusted(evidence.configuration_path),
        value=None
        if evidence.value_status != EvidenceValueStatus.PRESENT
        else json_text(sanitized(evidence.value)),
        value_status=evidence.value_status,
        redacted_path_count=len(evidence.redacted_paths),
        limitation=_maybe(evidence.limitation),
    )


class CoverageView(Model):
    service: str
    operation: str
    location: str | None
    status: CoverageStatus
    result_count: NonNegativeInt | None
    failure_count: NonNegativeInt
    unresolved_count: NonNegativeInt
    reason: UntrustedText | None
    first_failure: UntrustedText | None


def coverage_view(record: CloudOperationCoverage) -> CoverageView:
    first = record.failures[0] if record.failures else None
    return CoverageView(
        service=record.service,
        operation=record.operation,
        location=record.location,
        status=record.status,
        result_count=record.result_count,
        failure_count=len(record.failures),
        unresolved_count=len(record.unresolved_values),
        reason=_maybe(record.reason),
        first_failure=None if first is None else untrusted(f"{first.code}: {first.message}"),
    )


def coverage_order(records: Iterable[CloudOperationCoverage]) -> list[CloudOperationCoverage]:
    return sorted(
        records, key=lambda record: (record.service, record.location or "", record.operation)
    )


def _nonlocal_fields(target: CloudExternalTarget | UnresolvedTarget) -> dict:
    if isinstance(target, UnresolvedTarget):
        return {
            "kind": "unresolved",
            "reason": untrusted(target.reason),
            "source_path": untrusted(target.source_path),
        }
    scope = target.scope
    return {
        "kind": "external",
        "identifier": untrusted(target.identifier),
        "account_id": scope.account_id if isinstance(scope, AwsTargetScope) else None,
        "subscription_id": scope.subscription_id if isinstance(scope, AzureScope) else None,
        "native_type": _maybe(target.native_type),
    }


class TerminalView(Model):
    """A nonlocal dependency target; never a stored resource."""

    terminal_id: str
    kind: Literal["external", "unresolved"]
    identifier: UntrustedText | None = None
    account_id: str | None = None
    subscription_id: str | None = None
    native_type: UntrustedText | None = None
    reason: UntrustedText | None = None
    source_path: UntrustedText | None = None


def terminal_view(terminal: TerminalTarget) -> TerminalView:
    return TerminalView(terminal_id=terminal.id, **_nonlocal_fields(terminal.target))


class TargetView(Model):
    """A dependency target as recorded: a stored resource, external, or unresolved."""

    kind: Literal["local", "external", "unresolved"]
    resource_id: str | None = None
    identifier: UntrustedText | None = None
    account_id: str | None = None
    subscription_id: str | None = None
    native_type: UntrustedText | None = None
    reason: UntrustedText | None = None
    source_path: UntrustedText | None = None


def target_view(target: CloudTarget) -> TargetView:
    if isinstance(target, LocalTarget):
        return TargetView(kind="local", resource_id=target.resource_id)
    return TargetView(**_nonlocal_fields(target))


class NodeRef(Model):
    """A stored resource or a nonlocal terminal, exactly one."""

    resource_id: str | None = None
    terminal: TerminalView | None = None

    @model_validator(mode="after")
    def require_one(self) -> Self:
        if (self.resource_id is None) == (self.terminal is None):
            raise validation_error("tool_node_ref_ambiguous", "exactly one of resource or terminal")
        return self


class EdgeView(Model):
    kind: Literal["dependency", "containment"]
    edge_id: str | None
    direction: Literal[QueryDirection.OUTGOING, QueryDirection.INCOMING]
    source: NodeRef
    target: NodeRef
    relationship: DependencyRelationship | None = None
    attachment_kind: AttachmentKind | None = None
    location: str | None = None
    evidence_ids: tuple[str, ...] = ()


def edge_view(index: GraphIndex, step: QueryTraversalStep) -> EdgeView:
    edge = step.edge.edge
    if isinstance(edge, ContainmentEdge):
        return EdgeView(
            kind="containment",
            edge_id=None,
            direction=step.direction,
            source=NodeRef(resource_id=edge.source_id),
            target=NodeRef(resource_id=edge.target_id),
        )
    assert isinstance(edge, DependencyEdge)
    if isinstance(edge.target, LocalTarget):
        target = NodeRef(resource_id=edge.target.resource_id)
    else:
        terminal = index.get_terminal(edge.target_id)
        assert terminal is not None
        target = NodeRef(terminal=terminal_view(terminal))
    return EdgeView(
        kind="dependency",
        edge_id=edge.id,
        direction=step.direction,
        source=NodeRef(resource_id=edge.source_id),
        target=target,
        relationship=edge.relationship,
        attachment_kind=edge.attachment_kind,
        location=edge.location,
        evidence_ids=edge.evidence_ids,
    )


class LimitationView(Model):
    """A query limitation; messages are fixed service text, references are IDs."""

    kind: QueryLimitationKind
    message: str
    service: str | None = None
    operation: str | None = None
    location: str | None = None
    status: CoverageStatus | None = None
    resource_id: str | None = None
    fact_path: UntrustedText | None = None
    terminal_id: str | None = None
    edge_id: str | None = None


def limitation_view(limitation: QueryLimitation) -> LimitationView:
    updates: dict = {}
    coverage = getattr(limitation, "coverage", None)
    if coverage is not None:
        updates |= {
            "service": coverage.service,
            "operation": coverage.operation,
            "location": coverage.location,
        }
    if hasattr(limitation, "status"):
        updates["status"] = limitation.status
    if hasattr(limitation, "fact_path"):
        updates |= {
            "resource_id": limitation.resource_id,
            "fact_path": untrusted(limitation.fact_path),
        }
    if hasattr(limitation, "terminal_id"):
        updates |= {"terminal_id": limitation.terminal_id, "edge_id": limitation.edge_id}
    return LimitationView(kind=limitation.kind, message=limitation.message, **updates)


class PathStepView(Model):
    kind: str
    source: ResourceRef
    target: TargetView
    relationship: DependencyRelationship | None = None
    evidence_ids: tuple[str, ...]
    attributes: tuple[AttributeView, ...] = ()


def _step_view(step: PathStep) -> PathStepView:
    if step.kind == "containment":
        return PathStepView(
            kind="containment",
            source=ResourceRef(resource_id=step.source_id),
            target=TargetView(kind="local", resource_id=step.target_id),
            evidence_ids=tuple(step.evidence_ids),
        )
    return PathStepView(
        kind="dependency",
        source=ResourceRef(resource_id=step.source_id),
        target=target_view(step.target),
        relationship=step.relationship,
        evidence_ids=tuple(step.evidence_ids),
        attributes=tuple(
            AttributeView(key=untrusted(key), value=json_text(value))
            for key, value in sorted(step.attributes.items())
        ),
    )


class AnalysisLimitationView(Model):
    code: str
    message: UntrustedText
    service: str | None = None
    operation: str | None = None
    location: str | None = None
    unresolved_source: ResourceRef | None = None
    unresolved_target: TargetView | None = None
    evidence_ids: tuple[str, ...] = ()


def _limitation_view(limitation: AnalysisLimitation) -> AnalysisLimitationView:
    coverage = limitation.coverage
    unresolved = limitation.unresolved_target
    return AnalysisLimitationView(
        code=limitation.code,
        message=untrusted(limitation.message),
        service=None if coverage is None else coverage.service,
        operation=None if coverage is None else coverage.operation,
        location=None if coverage is None else coverage.location,
        unresolved_source=None
        if unresolved is None
        else ResourceRef(resource_id=unresolved.source_id),
        unresolved_target=None if unresolved is None else target_view(unresolved.target),
        evidence_ids=tuple(limitation.evidence_ids)
        + (() if unresolved is None else tuple(unresolved.evidence_ids)),
    )


class FindingView(Model):
    finding_id: str
    status: AnalysisStatus
    summary: UntrustedText
    resources: tuple[ResourceRef, ...]
    evidence_ids: tuple[str, ...]
    path: tuple[PathStepView, ...]
    limitations: tuple[AnalysisLimitationView, ...]
    impact: SecurityGroupImpact | None = None
    trust: CrossAccountTrust | None = None
    database_access: DatabaseAccess | None = None
    internet_exposure: InternetExposure | None = None


def finding_view(finding: AnalysisFinding) -> FindingView:
    return FindingView(
        finding_id=finding.id,
        status=finding.status,
        summary=untrusted(finding.summary),
        resources=tuple(ResourceRef(resource_id=ref.resource_id) for ref in finding.resources),
        evidence_ids=tuple(finding.evidence_ids),
        path=tuple(_step_view(step) for step in finding.path),
        limitations=tuple(_limitation_view(item) for item in finding.limitations),
        impact=finding.impact,
        trust=finding.trust,
        database_access=finding.database_access,
        internet_exposure=finding.internet_exposure,
    )
