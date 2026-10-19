"""Security-group impact: recorded attachments and rule references of one AWS group."""

from __future__ import annotations

from typing import TYPE_CHECKING

from archdiscov.models.analysis.parameters import AnalyzerParameters, SecurityGroupImpactParameters
from archdiscov.models.analysis.results import AnalysisFinding, AnalysisResult
from archdiscov.models.enums import (
    AnalysisStatus,
    AnalyzerErrorCode,
    AnalyzerKind,
    AwsService,
    CloudPlatform,
    QueryTruncationReason,
)
from archdiscov.models.resources.network import SecurityGroup

from .analysis_limitations import service_coverage_limitations, unsupported_platform_finding
from .analyzer_errors import AnalyzerError
from .analyzers import build_result
from .security_group_attachments import direct_attachments
from .security_group_downstream import downstream_paths, truncation_limitations
from .security_group_findings import resource_reference
from .security_group_rules import rule_references

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

# Services whose resources can attach to a group; ec2 also holds the group and its rules.
ATTACHING_SERVICES = (
    AwsService.EC2,
    AwsService.ELASTIC_LOAD_BALANCING,
    AwsService.ECS,
    AwsService.LAMBDA,
    AwsService.RDS,
)
UNRECORDED_PREFIX = "coverage_unrecorded_"
_INCOMPLETE_SUMMARY = (
    "Attachment and rule-reference completeness for the selected security group "
    "cannot be established from the recorded coverage."
)


def _unique(findings: list[AnalysisFinding]) -> list[AnalysisFinding]:
    """Drop findings with identical content (such as parallel identical edges), in order."""
    return list({finding.id: finding for finding in findings}.values())


class SecurityGroupImpactAnalyzer:
    """Observed attachments and rule references of one AWS group, plus potential downstream.

    Attachment is only an explicit membership edge, never VPC or subnet containment.
    Observed findings cite the stored edges; potential findings trace dependencies from
    impacted resources to `max_depth`. Everything is bounded by `result_limit`; a
    group-level insufficient-evidence finding reports missing or limited attaching-service
    coverage and truncation. Results are recorded structure, not reachability.
    """

    kind = AnalyzerKind.SECURITY_GROUP_IMPACT
    version = "1"

    def analyze(self, index: GraphIndex, parameters: AnalyzerParameters) -> AnalysisResult:
        if not isinstance(parameters, SecurityGroupImpactParameters):
            raise TypeError("security-group impact analyzer requires its own parameters")
        selected = index.get_resource(parameters.resource_id)
        if selected is None:
            raise AnalyzerError(AnalyzerErrorCode.RESOURCE_NOT_FOUND)
        if index.scope.platform != CloudPlatform.AWS:
            finding = unsupported_platform_finding(
                index, "security groups", [resource_reference(index, selected)]
            )
            return build_result(self, index, parameters, [finding])
        if not isinstance(selected, SecurityGroup):
            raise AnalyzerError(AnalyzerErrorCode.UNSUPPORTED_RESOURCE)

        rules, skipped = rule_references(index, selected)
        observed = _unique([*direct_attachments(index, selected), *rules])
        retained = observed[: parameters.result_limit]
        downstream, truncation = downstream_paths(
            index,
            selected.id,
            (ref.resource_id for finding in retained for ref in finding.resources),
            parameters,
            parameters.result_limit - len(retained),
        )
        candidates = _unique([*retained, *downstream])
        findings = candidates[: parameters.result_limit]
        if len(observed) > len(retained) or len(candidates) > len(findings):
            truncation.add(QueryTruncationReason.RESULT_LIMIT)
        limitations = [
            *service_coverage_limitations(index, ATTACHING_SERVICES, selected.identity.region),
            *skipped,
            *truncation_limitations(truncation),
        ]
        if limitations:
            findings.append(
                AnalysisFinding(
                    snapshot_id=index.snapshot_id,
                    summary=_INCOMPLETE_SUMMARY,
                    status=AnalysisStatus.INSUFFICIENT_EVIDENCE,
                    resources=[resource_reference(index, selected)],
                    limitations=limitations,
                )
            )
        return build_result(self, index, parameters, findings)
