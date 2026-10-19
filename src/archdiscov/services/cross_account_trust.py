"""Configured cross-account trust from recorded role trust principals, never access."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from archdiscov.graph.dependencies import DependencyEdge
from archdiscov.models.analysis.parameters import AnalyzerParameters, CrossAccountTrustParameters
from archdiscov.models.analysis.references import AnalysisLimitation
from archdiscov.models.analysis.results import AnalysisFinding, AnalysisResult
from archdiscov.models.analysis.trust import CrossAccountTrust
from archdiscov.models.architecture import LocalTarget, UnresolvedTarget
from archdiscov.models.enums import (
    AnalysisStatus,
    AnalyzerKind,
    AttachmentKind,
    CloudPlatform,
    EvidenceValueStatus,
    TrustClassification,
    TrustEffect,
    TrustPrincipalKind,
    TrustSource,
)
from archdiscov.models.resources.iam import IamRole

from .analysis_limitations import (
    RESULT_LIMIT_CODE,
    target_limitations,
    unsupported_platform_finding,
)
from .analysis_qualification import qualify_status
from .analyzers import build_result
from .security_group_findings import edge_step, resource_reference

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

STATEMENT_UNAVAILABLE_CODE = "trust_statement_unavailable"
_SUBJECT = "trust principal"
_EFFECTS = {item.value: item for item in TrustEffect}
_SUMMARY = (
    "A recorded {effect} trust statement names {who}{condition}; this is configured trust, "
    "not effective access."
)
_WHO = {
    TrustClassification.SAME_ACCOUNT: "a principal in the same account",
    TrustClassification.EXTERNAL_ACCOUNT: "a principal in a named external account",
    TrustClassification.SERVICE: "an AWS service principal",
    TrustClassification.FEDERATED: "a federated principal",
    TrustClassification.WILDCARD: "a wildcard principal",
    TrustClassification.UNRESOLVED: "a principal that could not be resolved",
}


def _entry_value(index: GraphIndex, evidence_id: str) -> Any:
    record = index.get_evidence(evidence_id)
    if record is None or record.value_status == EvidenceValueStatus.OMITTED:
        return None
    return record.value


def _is_wildcard(identifier: Any) -> bool:
    return isinstance(identifier, str) and any(c in identifier for c in "*?")


def _classify(
    index: GraphIndex, edge: DependencyEdge, principal: Any, not_principal: bool
) -> TrustClassification:
    """Classify from the recorded edge target, principal kind, and cited principal entry."""
    if not_principal:
        return TrustClassification.UNRESOLVED
    kind = edge.attributes.get("principal_kind")
    if isinstance(edge.target, LocalTarget):
        return TrustClassification.SAME_ACCOUNT
    if isinstance(edge.target, UnresolvedTarget):
        return (
            TrustClassification.WILDCARD
            if _is_wildcard(principal)
            else TrustClassification.UNRESOLVED
        )
    if kind == TrustPrincipalKind.SERVICE:
        return TrustClassification.SERVICE
    if kind == TrustPrincipalKind.FEDERATED:
        return TrustClassification.FEDERATED
    account = getattr(edge.target.scope, "account_id", None)
    if account is None:
        return TrustClassification.UNRESOLVED
    if account == getattr(index.scope, "account_id", None):
        return TrustClassification.SAME_ACCOUNT
    return TrustClassification.EXTERNAL_ACCOUNT


def _statement(index: GraphIndex, edge: DependencyEdge) -> dict[str, Any] | None:
    """The cited whole statement, the second citation of a principal edge."""
    if len(edge.evidence_ids) < 2:
        return None
    value = _entry_value(index, edge.evidence_ids[1])
    return value if isinstance(value, dict) else None


def _unavailable_statement_finding(base: dict[str, Any], edge: DependencyEdge) -> AnalysisFinding:
    """Without a cited statement and effect, the principal alone says nothing about trust."""
    return AnalysisFinding(
        **base,
        summary="A recorded trust principal has no usable statement; trust cannot be assessed.",
        status=AnalysisStatus.INSUFFICIENT_EVIDENCE,
        limitations=[
            AnalysisLimitation(
                code=STATEMENT_UNAVAILABLE_CODE,
                message="The cited trust statement is unavailable or has no valid Effect.",
                evidence_ids=list(edge.evidence_ids),
            ),
            *target_limitations(edge, _SUBJECT),
        ],
    )


def _trust_finding(index: GraphIndex, role: IamRole, edge: DependencyEdge) -> AnalysisFinding:
    statement = _statement(index, edge)
    effect = statement.get("Effect") if statement else None
    base = {
        "snapshot_id": index.snapshot_id,
        "resources": [resource_reference(index, role)],
        "path": [edge_step(edge)],
    }
    if effect not in _EFFECTS:
        return _unavailable_statement_finding(base, edge)
    not_principal = bool(edge.attributes.get("not_principal"))
    classification = _classify(
        index, edge, _entry_value(index, edge.evidence_ids[0]), not_principal
    )
    conditional = bool(statement.get("Condition"))
    qualified = qualify_status(index, [role], AnalysisStatus.SUPPORTED)
    return AnalysisFinding(
        **base,
        summary=_SUMMARY.format(
            effect=effect,
            who=_WHO[classification],
            condition=", subject to a condition" if conditional else "",
        ),
        status=qualified.status,
        evidence_ids=list(edge.evidence_ids),
        limitations=[*target_limitations(edge, _SUBJECT), *qualified.limitations],
        trust=CrossAccountTrust(
            source=TrustSource.ROLE_TRUST_POLICY,
            classification=classification,
            effect=_EFFECTS[effect],
            conditional=conditional,
            not_principal=not_principal,
        ),
    )


class CrossAccountTrustAnalyzer:
    """Report configured trust principals of IAM roles, classified and cited.

    One finding per recorded `trust_principal` edge, classified by its recorded target,
    principal kind, and cited principal entry (trust documents are not re-parsed).
    `Effect` and `Condition` come from the cited statement evidence; `Deny` and
    `NotPrincipal` context is kept, and conditional trust is labelled conditional.
    External and unresolved principals stay terminals with limitations. Findings describe
    configured policy, never that a principal can assume the role. Results are bounded by
    `result_limit` with a group-level finding when truncated. Azure RBAC is not IAM trust.
    """

    kind = AnalyzerKind.CROSS_ACCOUNT_TRUST
    version = "1"

    def analyze(self, index: GraphIndex, parameters: AnalyzerParameters) -> AnalysisResult:
        if not isinstance(parameters, CrossAccountTrustParameters):
            raise TypeError("cross-account trust analyzer requires its own parameters")
        if index.scope.platform != CloudPlatform.AWS:
            finding = unsupported_platform_finding(index, "IAM trust policies")
            return build_result(self, index, parameters, [finding])
        candidates: dict[str, AnalysisFinding] = {}
        for role in index.resources():
            if not isinstance(role, IamRole):
                continue
            for edge in index.outgoing_edges(
                role.id, attachment_kind=AttachmentKind.TRUST_PRINCIPAL
            ):
                finding = _trust_finding(index, role, edge)
                candidates.setdefault(finding.id, finding)
        findings = list(candidates.values())[: parameters.result_limit]
        if len(candidates) > len(findings):
            findings.append(
                AnalysisFinding(
                    snapshot_id=index.snapshot_id,
                    summary="More trust findings exist than the configured result limit.",
                    status=AnalysisStatus.INSUFFICIENT_EVIDENCE,
                    limitations=[
                        AnalysisLimitation(
                            code=RESULT_LIMIT_CODE,
                            message="Increase the result limit to see every recorded principal.",
                        )
                    ],
                )
            )
        return build_result(self, index, parameters, findings)
