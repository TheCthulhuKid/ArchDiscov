"""Database network access: compute sources with a recorded potential path to an RDS port."""

from __future__ import annotations

from typing import TYPE_CHECKING

from archdiscov.models.analysis.database_access import DatabaseAccess
from archdiscov.models.analysis.parameters import AnalyzerParameters, DatabaseAccessParameters
from archdiscov.models.analysis.references import AnalysisLimitation
from archdiscov.models.analysis.results import AnalysisFinding, AnalysisResult
from archdiscov.models.enums import (
    AccessSourceKind,
    AnalyzerErrorCode,
    AnalyzerKind,
    AwsService,
    CloudPlatform,
    NetworkDecision,
)

from .analysis_bounds import DECISION_ORDER, bound_findings, qualify_decision
from .analysis_limitations import (
    distinct_limitations,
    service_coverage_limitations,
    unsupported_platform_finding,
)
from .analyzer_errors import AnalyzerError
from .analyzers import build_result
from .database_access_paths import PathEvaluation, evaluate_candidate
from .database_placement import DatabasePlacement, resolve_database_placement
from .database_sources import CandidateSource, SourceDiscovery, discover_sources
from .security_group_findings import resource_reference

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

# Services whose collection the candidate list and the evaluated path rely on.
COVERAGE_SERVICES = (AwsService.EC2, AwsService.RDS, AwsService.ECS, AwsService.LAMBDA)
_KIND_ORDER = list(AccessSourceKind)
_INCOMPLETE_SUMMARY = (
    "Completeness of the compute sources and network path to the selected database cannot "
    "be established from the recorded evidence."
)
_SUMMARIES = {
    NetworkDecision.ALLOW: (
        "Recorded configuration permits a network path from this {kind} to the database on "
        "port {port}; this is network permission, not authentication or live reachability."
    ),
    NetworkDecision.DENY: (
        "Recorded configuration denies the network path from this {kind} to the database "
        "on port {port}."
    ),
    NetworkDecision.UNKNOWN: (
        "A network path from this {kind} to the database on port {port} cannot be "
        "established from the recorded evidence."
    ),
}


def _finding(
    index: GraphIndex,
    placement: DatabasePlacement,
    candidate: CandidateSource,
    evaluation: PathEvaluation,
    port: int,
) -> AnalysisFinding:
    """Allow and deny are supported conclusions; unknown is insufficient evidence."""
    resources = [candidate.resource, placement.database]
    qualified = qualify_decision(index, resources, evaluation.decision)
    return AnalysisFinding(
        snapshot_id=index.snapshot_id,
        summary=_SUMMARIES[evaluation.decision].format(
            kind=candidate.kind.value.replace("_", " "), port=port
        ),
        status=qualified.status,
        evidence_ids=list(evaluation.evidence_ids),
        resources=[resource_reference(index, resource) for resource in resources],
        limitations=distinct_limitations([*evaluation.limitations, *qualified.limitations]),
        database_access=DatabaseAccess(
            source_kind=candidate.kind,
            port=port,
            decision=evaluation.decision,
            elements=list(evaluation.elements),
        ),
    )


def _order(finding: AnalysisFinding) -> tuple[int, int, str]:
    access = finding.database_access
    return (
        DECISION_ORDER.index(access.decision),
        _KIND_ORDER.index(access.source_kind),
        finding.resources[0].resource_id,
    )


def _candidate_findings(
    index: GraphIndex, placement: DatabasePlacement, discovery: SourceDiscovery, port: int
) -> list[AnalysisFinding]:
    """One finding per candidate source (identical findings collapse to one)."""
    findings = (
        _finding(
            index, placement, candidate, evaluate_candidate(index, placement, candidate, port), port
        )
        for candidate in discovery.candidates
    )
    return list({finding.id: finding for finding in findings}.values())


def _group_limitations(
    index: GraphIndex, placement: DatabasePlacement, discovery: SourceDiscovery
) -> list[AnalysisLimitation]:
    """Collection and placement gaps that apply to the whole result."""
    return [
        *service_coverage_limitations(index, COVERAGE_SERVICES, placement.database.identity.region),
        *placement.limitations,
        *discovery.limitations,
    ]


class DatabaseAccessAnalyzer:
    """Compute sources with a recorded potential network path to a database port.

    For a selected RDS instance or cluster and port, every EC2 instance, awsvpc ECS task,
    and Lambda function in the database's VPC is a candidate whose placement is evaluated
    with the step 29 primitives (security groups, NACLs, and local routes, both directions).
    A finding is network permission from recorded configuration: never authentication,
    credentials, IAM database access, or live reachability, and candidates are placements,
    not usage. Allow and deny are supported conclusions; unknown is insufficient evidence.
    Findings are qualified by the recorded EC2, RDS, ECS, and Lambda coverage, ordered by
    decision, source kind, and identity before the result limit applies, and a group-level
    finding reports placement gaps, limited or unrecorded collection, sources in other VPCs
    (unsupported), and truncation. Azure returns the shared unsupported-platform result.
    """

    kind = AnalyzerKind.DATABASE_ACCESS
    version = "1"

    def analyze(self, index: GraphIndex, parameters: AnalyzerParameters) -> AnalysisResult:
        if not isinstance(parameters, DatabaseAccessParameters):
            raise TypeError("database-access analyzer requires its own parameters")
        selected = index.get_resource(parameters.resource_id)
        if selected is None:
            raise AnalyzerError(AnalyzerErrorCode.RESOURCE_NOT_FOUND)
        if index.scope.platform != CloudPlatform.AWS:
            finding = unsupported_platform_finding(
                index, "database access", [resource_reference(index, selected)]
            )
            return build_result(self, index, parameters, [finding])
        placement = resolve_database_placement(index, parameters.resource_id, parameters.port)
        discovery = discover_sources(index, placement)
        findings = bound_findings(
            index,
            _candidate_findings(index, placement, discovery, parameters.port),
            order=_order,
            limit=parameters.result_limit,
            limitations=_group_limitations(index, placement, discovery),
            summary=_INCOMPLETE_SUMMARY,
            truncation_message="Increase the result limit to see every candidate source.",
            resources=[resource_reference(index, placement.database)],
        )
        return build_result(self, index, parameters, findings)
