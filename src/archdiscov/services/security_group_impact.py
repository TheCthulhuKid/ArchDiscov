"""Security-group impact: recorded attachments and rule references of one AWS group."""

from __future__ import annotations

from typing import TYPE_CHECKING

from archdiscov.models.analysis.parameters import AnalyzerParameters, SecurityGroupImpactParameters
from archdiscov.models.analysis.references import UNSUPPORTED_PLATFORM_CODE, AnalysisLimitation
from archdiscov.models.analysis.results import AnalysisFinding, AnalysisResult
from archdiscov.models.cloud.context import canonical_service_key
from archdiscov.models.enums import (
    AnalysisStatus,
    AnalyzerErrorCode,
    AnalyzerKind,
    AwsService,
    CloudPlatform,
    CoverageStatus,
    QueryTruncationReason,
)
from archdiscov.models.resource_base import Resource
from archdiscov.models.resources.network import SecurityGroup

from .analysis_qualification import coverage_limitation
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


def _coverage_limitations(index: GraphIndex, group: Resource) -> list[AnalysisLimitation]:
    """Limited or unrecorded collection for every service that could attach to the group.

    Only recorded operations are referenced; an absent service gets a per-service
    code and no invented operation name.
    """
    region = group.identity.region
    records = index.coverage_records()
    limitations = []
    for service in ATTACHING_SERVICES:
        matching = [
            record
            for record in records
            if canonical_service_key(index.scope.platform, record.service) == service
            and record.location in (None, region)
        ]
        if not matching:
            limitations.append(
                AnalysisLimitation(
                    code=f"{UNRECORDED_PREFIX}{service}",
                    message=f"No {service} collection is recorded for {region}.",
                )
            )
        limitations.extend(
            coverage_limitation(record)
            for record in matching
            if record.status != CoverageStatus.SUCCESS
        )
    return limitations


def _unsupported(index: GraphIndex, selected: Resource) -> AnalysisFinding:
    platform = index.scope.platform
    return AnalysisFinding(
        snapshot_id=index.snapshot_id,
        summary=f"Security-group impact is not analyzed for {platform} snapshots.",
        status=AnalysisStatus.INSUFFICIENT_EVIDENCE,
        resources=[resource_reference(index, selected)],
        limitations=[
            AnalysisLimitation(
                code=UNSUPPORTED_PLATFORM_CODE,
                message=(
                    f"Only AWS security groups are supported; {platform} semantics are not "
                    "reused. Analyze an AWS snapshot instead."
                ),
            )
        ],
    )


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
            return build_result(self, index, parameters, [_unsupported(index, selected)])
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
            *_coverage_limitations(index, selected),
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
