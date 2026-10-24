"""Configured cross-account trust from recorded role trust principals, never access."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

from archdiscov.models.analysis.parameters import AnalyzerParameters, CrossAccountTrustParameters
from archdiscov.models.analysis.references import AnalysisLimitation
from archdiscov.models.analysis.results import AnalysisFinding, AnalysisResult
from archdiscov.models.analysis.trust import CrossAccountTrust
from archdiscov.models.architecture import LocalTarget, UnresolvedTarget
from archdiscov.models.edges import DependencyEdge
from archdiscov.models.enums import (
    AnalysisStatus,
    AnalyzerKind,
    AttachmentKind,
    AwsService,
    CloudPlatform,
    EvidenceValueStatus,
    TrustClassification,
    TrustEffect,
    TrustPrincipalKind,
    TrustSource,
)
from archdiscov.models.resources.iam import IamRole
from archdiscov.models.resources.queues import SqsQueue

from .analysis_limitations import (
    RESULT_LIMIT_CODE,
    service_coverage_limitations,
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
    "A recorded {effect} {policy} statement names {who}{condition}; this is configured trust, "
    "not effective access."
)
TRUST_DOCUMENT_UNAVAILABLE_CODE = "trust_document_unavailable"
POLICY_UNAVAILABLE_CODE = "policy_unavailable"
_INCOMPLETE_SUMMARY = (
    "Trust completeness cannot be established from the recorded IAM and SQS coverage."
)
# Per trust source: the attachment kind of its principal edges and how summaries name it.


class _SourceInfo(NamedTuple):
    edge_kind: AttachmentKind
    label: str


_SOURCES = {
    TrustSource.ROLE_TRUST_POLICY: _SourceInfo(AttachmentKind.TRUST_PRINCIPAL, "role trust"),
    TrustSource.RESOURCE_POLICY: _SourceInfo(
        AttachmentKind.RESOURCE_POLICY_PRINCIPAL, "queue policy"
    ),
}
_PolicyHolder = IamRole | SqsQueue
_COVERAGE_SERVICES = (AwsService.IAM, AwsService.SQS)
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


def _trust_finding(
    index: GraphIndex, holder: _PolicyHolder, edge: DependencyEdge, source: TrustSource
) -> AnalysisFinding:
    statement = _statement(index, edge)
    effect = statement.get("Effect") if statement else None
    base = {
        "snapshot_id": index.snapshot_id,
        "resources": [resource_reference(index, holder)],
        "path": [edge_step(edge)],
    }
    if effect not in _EFFECTS:
        return _unavailable_statement_finding(base, edge)
    not_principal = bool(edge.attributes.get("not_principal"))
    classification = _classify(
        index, edge, _entry_value(index, edge.evidence_ids[0]), not_principal
    )
    conditional = bool(statement.get("Condition"))
    qualified = qualify_status(index, [holder], AnalysisStatus.SUPPORTED)
    return AnalysisFinding(
        **base,
        summary=_SUMMARY.format(
            effect=effect,
            policy=_SOURCES[source].label,
            who=_WHO[classification],
            condition=", subject to a condition" if conditional else "",
        ),
        status=qualified.status,
        evidence_ids=list(edge.evidence_ids),
        limitations=[*target_limitations(edge, _SUBJECT), *qualified.limitations],
        trust=CrossAccountTrust(
            source=source,
            classification=classification,
            effect=_EFFECTS[effect],
            conditional=conditional,
            not_principal=not_principal,
        ),
    )


def _unavailable_policy_finding(
    index: GraphIndex,
    resource: _PolicyHolder,
    code: str,
    summary: str,
    evidence_ids: list[str],
) -> AnalysisFinding:
    """A missing policy document is insufficient evidence, never evidence of no trust."""
    qualified = qualify_status(index, [resource], AnalysisStatus.INSUFFICIENT_EVIDENCE)
    return AnalysisFinding(
        snapshot_id=index.snapshot_id,
        summary=summary,
        status=AnalysisStatus.INSUFFICIENT_EVIDENCE,
        resources=[resource_reference(index, resource)],
        limitations=[
            AnalysisLimitation(
                code=code,
                message="The policy document is unavailable, so trust cannot be assessed.",
                evidence_ids=evidence_ids,
            ),
            *qualified.limitations,
        ],
    )


def _omitted_policy_evidence(index: GraphIndex, queue: SqsQueue) -> list[str]:
    """Queue-owned evidence recording that its attributes or policy could not be read.

    A queue with no policy attribute is normal; only an explicit omitted record counts.
    """
    return [
        evidence_id
        for evidence_id in index.resource_evidence_ids(queue.id)
        if (record := index.get_evidence(evidence_id)) is not None
        and record.value_status == EvidenceValueStatus.OMITTED
        and record.configuration_path.endswith((".Attributes.Policy", ".Attributes"))
    ]


def _resource_findings(index: GraphIndex, resource: _PolicyHolder) -> list[AnalysisFinding]:
    source = (
        TrustSource.ROLE_TRUST_POLICY
        if isinstance(resource, IamRole)
        else TrustSource.RESOURCE_POLICY
    )
    kind = _SOURCES[source].edge_kind
    findings = [
        _trust_finding(index, resource, edge, source)
        for edge in index.outgoing_edges(resource.id, attachment_kind=kind)
    ]
    if isinstance(resource, IamRole) and resource.trust_document is None:
        findings.append(
            _unavailable_policy_finding(
                index,
                resource,
                TRUST_DOCUMENT_UNAVAILABLE_CODE,
                "This role's trust policy is unavailable, so its trust cannot be assessed.",
                list(index.fact_evidence_ids(resource.id, "/trust_document")),
            )
        )
    elif isinstance(resource, SqsQueue) and resource.policy is None:
        omitted = _omitted_policy_evidence(index, resource)
        if omitted:
            findings.append(
                _unavailable_policy_finding(
                    index,
                    resource,
                    POLICY_UNAVAILABLE_CODE,
                    "This queue's policy is unavailable, so its trust cannot be assessed.",
                    omitted,
                )
            )
    return findings


class CrossAccountTrustAnalyzer:
    """Report configured trust principals of IAM roles and SQS queue policies.

    One finding per recorded principal edge (`trust_principal` for role trust policies,
    `resource_policy_principal` for queue policies), classified by its recorded target,
    principal kind, and cited principal entry (policy documents are not re-parsed).
    `Effect` and `Condition` come from the cited statement evidence; `Deny` and
    `NotPrincipal` context is kept, and conditional trust is labelled conditional.
    External and unresolved principals stay terminals with limitations. Findings describe
    configured policy, never that a principal can assume a role or reach a queue.
    Unavailable role trust documents and queue policies are insufficient evidence. A
    group-level finding reports limited or unrecorded IAM and SQS collection and result
    truncation. Azure RBAC is not IAM trust.
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
        for resource in index.resources():
            if isinstance(resource, IamRole | SqsQueue):
                for finding in _resource_findings(index, resource):
                    candidates.setdefault(finding.id, finding)
        findings = list(candidates.values())[: parameters.result_limit]
        limitations = service_coverage_limitations(index, _COVERAGE_SERVICES)
        if len(candidates) > len(findings):
            limitations.append(
                AnalysisLimitation(
                    code=RESULT_LIMIT_CODE,
                    message="Increase the result limit to see every recorded principal.",
                )
            )
        if limitations:
            findings.append(
                AnalysisFinding(
                    snapshot_id=index.snapshot_id,
                    summary=_INCOMPLETE_SUMMARY,
                    status=AnalysisStatus.INSUFFICIENT_EVIDENCE,
                    limitations=limitations,
                )
            )
        return build_result(self, index, parameters, findings)
