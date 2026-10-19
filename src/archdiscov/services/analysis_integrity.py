"""Snapshot-backed reference checks over an owned, detached access view."""

from collections.abc import Iterable
from dataclasses import dataclass

from archdiscov.models.analysis.references import (
    AnalysisLimitation,
    ContainmentPathStep,
    PathStep,
)
from archdiscov.models.analysis.results import AnalysisFinding, AnalysisResult
from archdiscov.models.architecture import LocalTarget
from archdiscov.models.cloud.contracts import CloudDependency, CloudEvidence
from archdiscov.models.cloud.scope import SupportedScope
from archdiscov.models.coverage_key import CoverageKey
from archdiscov.models.enums import TrustSource
from archdiscov.models.resource_base import Resource
from archdiscov.models.resources.iam import IamRole
from archdiscov.models.resources.network import SecurityGroup
from archdiscov.models.resources.queues import SqsQueue

from .analysis_diagnostics import reject
from .snapshot_access import SnapshotAccess, _resources_from_validated_snapshot

Location = tuple[str | int, ...]


@dataclass(frozen=True)
class _IndexedDependency:
    edge: CloudDependency
    citations: frozenset[str]


def _has_citation_subset(ids: Iterable[str], candidates: Iterable[_IndexedDependency]) -> bool:
    citations = frozenset(ids)
    return any(citations <= item.citations for item in candidates)


@dataclass(frozen=True)
class _IntegrityContext:
    scope: SupportedScope
    resources: dict[str, Resource]
    evidence: dict[str, CloudEvidence]
    dependencies_by_source: dict[str, list[_IndexedDependency]]
    containment: set[tuple[str, str]]
    coverage: set[CoverageKey]

    @classmethod
    def from_access(cls, access: SnapshotAccess) -> "_IntegrityContext":
        snapshot = access._borrow_snapshot()
        resources = {
            resource.id: resource for resource in _resources_from_validated_snapshot(snapshot)
        }
        dependencies: dict[str, list[_IndexedDependency]] = {}
        for edge in snapshot.dependencies:
            dependencies.setdefault(edge.source_id, []).append(
                _IndexedDependency(edge, frozenset(edge.evidence_ids))
            )
        return cls(
            snapshot.scope,
            resources,
            snapshot.evidence,
            dependencies,
            set(access.containment()),
            {CoverageKey.from_record(record) for record in snapshot.coverage.operations},
        )

    def require_resource(self, resource_id: str, location: Location) -> None:
        if resource_id not in self.resources:
            reject(location, "resource_unknown", "Referenced resource must exist in the snapshot.")

    def require_evidence(
        self, ids: Iterable[str], location: Location, owner: str | None = None
    ) -> None:
        for index, evidence_id in enumerate(ids):
            item_location = (*location, index)
            if evidence_id not in self.evidence:
                reject(
                    item_location,
                    "evidence_unknown",
                    "Referenced evidence must exist in the snapshot.",
                )
            if owner is not None and self.evidence[evidence_id].resource_id != owner:
                reject(
                    item_location,
                    "evidence_owner_mismatch",
                    "Path evidence must belong to its source resource.",
                )


def _validate_path(path: list[PathStep], base: Location, context: _IntegrityContext) -> None:
    previous_target: str | None = None
    for index, step in enumerate(path):
        location = (*base, "path", index)
        context.require_resource(step.source_id, (*location, "source_id"))
        if index and previous_target != step.source_id:
            reject(
                location,
                "path_disconnected",
                "Ordered path steps must connect through local resources.",
            )
        context.require_evidence(step.evidence_ids, (*location, "evidence_ids"), step.source_id)
        if isinstance(step, ContainmentPathStep):
            context.require_resource(step.target_id, (*location, "target_id"))
            if (step.source_id, step.target_id) not in context.containment:
                reject(
                    location,
                    "containment_unknown",
                    "Containment path step must match a stored parent-child edge.",
                )
            previous_target = step.target_id
        else:
            if isinstance(step.target, LocalTarget):
                context.require_resource(step.target.resource_id, (*location, "target"))
            candidates = (
                item
                for item in context.dependencies_by_source.get(step.source_id, ())
                if item.edge.target == step.target
                and item.edge.relationship == step.relationship
                and item.edge.attributes == step.attributes
            )
            if not _has_citation_subset(step.evidence_ids, candidates):
                reject(
                    location,
                    "dependency_unknown",
                    "Dependency path step and citations must match a stored edge.",
                )
            previous_target = (
                step.target.resource_id if isinstance(step.target, LocalTarget) else None
            )


def _validate_limitations(
    limitations: list[AnalysisLimitation], base: Location, context: _IntegrityContext
) -> None:
    for index, limitation in enumerate(limitations):
        location = (*base, "limitations", index)
        context.require_evidence(limitation.evidence_ids, (*location, "evidence_ids"))
        if (
            limitation.coverage is not None
            and CoverageKey.from_record(limitation.coverage) not in context.coverage
        ):
            reject(
                (*location, "coverage"),
                "coverage_unknown",
                "Coverage limitation must reference a recorded operation.",
            )
        reference = limitation.unresolved_target
        if reference is not None:
            context.require_resource(
                reference.source_id, (*location, "unresolved_target", "source_id")
            )
            context.require_evidence(
                reference.evidence_ids,
                (*location, "unresolved_target", "evidence_ids"),
                reference.source_id,
            )
            candidates = (
                item
                for item in context.dependencies_by_source.get(reference.source_id, ())
                if item.edge.target == reference.target
            )
            if not _has_citation_subset(reference.evidence_ids, candidates):
                reject(
                    location,
                    "unresolved_target_unknown",
                    "Limitation must reference a stored unresolved dependency.",
                )


_TRUST_SOURCES = {
    TrustSource.ROLE_TRUST_POLICY: IamRole,
    TrustSource.RESOURCE_POLICY: SqsQueue,
}


def _validate_trust(finding: AnalysisFinding, base: Location, context: _IntegrityContext) -> None:
    """The trusting resource must be of the kind that owns this kind of policy."""
    trusting = context.resources.get(finding.resources[0].resource_id)
    if trusting is not None and not isinstance(trusting, _TRUST_SOURCES[finding.trust.source]):
        reject(
            (*base, "trust", "source"),
            "trust_source_mismatch",
            "Trust source must match the type of the trusting resource.",
        )


def _validate_finding(finding: AnalysisFinding, index: int, context: _IntegrityContext) -> None:
    base = ("findings", index)
    context.require_evidence(finding.evidence_ids, (*base, "evidence_ids"))
    for ref_index, reference in enumerate(finding.resources):
        location = (*base, "resources", ref_index)
        context.require_resource(reference.resource_id, (*location, "resource_id"))
        if reference.scope != context.scope:
            reject(
                (*location, "scope"),
                "resource_scope_mismatch",
                "Resource reference scope must match the snapshot.",
            )
    if finding.trust is not None:
        _validate_trust(finding, base, context)
    _validate_path(finding.path, base, context)
    _validate_limitations(finding.limitations, base, context)


def validate_integrity(result: AnalysisResult, access: SnapshotAccess) -> None:
    snapshot = access._borrow_snapshot()
    if result.snapshot_id != snapshot.snapshot_id:
        reject(
            ("snapshot_id",), "snapshot_mismatch", "Result must reference the selected snapshot."
        )
    context = _IntegrityContext.from_access(access)
    selected = getattr(result.parameters, "resource_id", None)
    if selected is not None:
        context.require_resource(selected, ("parameters", "resource_id"))
        # Results without impact findings keep the earlier, looser selection contract.
        if any(finding.impact is not None for finding in result.findings) and not isinstance(
            context.resources[selected], SecurityGroup
        ):
            reject(
                ("parameters", "resource_id"),
                "resource_not_security_group",
                "Selected resource must be a security group.",
            )
    for index, finding in enumerate(result.findings):
        _validate_finding(finding, index, context)
