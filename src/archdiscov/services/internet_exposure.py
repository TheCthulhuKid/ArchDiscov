"""Candidate internet exposure of public-address resources and internet-facing balancers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from archdiscov.models.analysis.exposure import InternetExposure
from archdiscov.models.analysis.parameters import AnalyzerParameters, InternetExposureParameters
from archdiscov.models.analysis.results import AnalysisFinding, AnalysisResult
from archdiscov.models.enums import (
    AnalyzerKind,
    AwsService,
    CloudPlatform,
    ExposureEntryKind,
    NetworkDecision,
)

from .analysis_bounds import DECISION_ORDER, bound_findings, qualify_decision
from .analysis_limitations import (
    distinct_limitations,
    service_coverage_limitations,
    unsupported_platform_finding,
)
from .analyzers import build_result
from .exposure_addresses import evaluate_public_entry, public_entries
from .exposure_evaluation import ExposureEvaluation
from .exposure_load_balancers import evaluate_load_balancer_entry, load_balancer_entries
from .security_group_findings import resource_reference

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

# Services whose collection the entry points and their paths rely on.
COVERAGE_SERVICES = (AwsService.EC2, AwsService.ELASTIC_LOAD_BALANCING)
_KIND_ORDER = list(ExposureEntryKind)
_INCOMPLETE_SUMMARY = (
    "Completeness of the internet-exposure entry points and their paths cannot be "
    "established from the recorded evidence."
)
_ENTRY_LABELS = {
    ExposureEntryKind.PUBLIC_ADDRESS: "resource with a public address",
    ExposureEntryKind.LOAD_BALANCER: "load balancer",
}
_SUMMARIES = {
    NetworkDecision.ALLOW: (
        "Recorded configuration could expose this {entry} to the internet on {protocol} "
        "{ports}; this is candidate exposure, not proof of reachability."
    ),
    NetworkDecision.DENY: (
        "Recorded configuration blocks internet access to this {entry} on {protocol} {ports}."
    ),
    NetworkDecision.UNKNOWN: (
        "Internet exposure of this {entry} on {protocol} {ports} cannot be established from "
        "the recorded evidence."
    ),
}


def _ports(evaluation: ExposureEvaluation) -> str:
    if evaluation.start == evaluation.end:
        return f"port {evaluation.start}"
    return f"ports {evaluation.start}-{evaluation.end}"


def _finding(index: GraphIndex, evaluation: ExposureEvaluation) -> AnalysisFinding:
    """Allow and deny are supported conclusions; unknown is insufficient evidence."""
    resources = [evaluation.entry, *evaluation.targets]
    qualified = qualify_decision(index, resources, evaluation.decision)
    return AnalysisFinding(
        snapshot_id=index.snapshot_id,
        summary=_SUMMARIES[evaluation.decision].format(
            entry=_ENTRY_LABELS[evaluation.kind],
            protocol=evaluation.protocol.value,
            ports=_ports(evaluation),
        ),
        status=qualified.status,
        evidence_ids=list(evaluation.evidence_ids),
        resources=[resource_reference(index, resource) for resource in resources],
        limitations=distinct_limitations([*evaluation.limitations, *qualified.limitations]),
        internet_exposure=InternetExposure(
            entry_kind=evaluation.kind,
            protocol=evaluation.protocol,
            port_start=evaluation.start,
            port_end=evaluation.end,
            decision=evaluation.decision,
            elements=list(evaluation.elements),
            observed_target_health=list(evaluation.observed_target_health),
        ),
    )


def _order(finding: AnalysisFinding) -> tuple[int, int, str, int]:
    exposure = finding.internet_exposure
    return (
        DECISION_ORDER.index(exposure.decision),
        _KIND_ORDER.index(exposure.entry_kind),
        finding.resources[0].resource_id,
        exposure.port_start,
    )


def _evaluations(index: GraphIndex, max_depth: int) -> list[ExposureEvaluation]:
    return [
        *(item for entry in public_entries(index) for item in evaluate_public_entry(index, entry)),
        *(
            item
            for balancer in load_balancer_entries(index)
            for item in evaluate_load_balancer_entry(index, balancer, max_depth)
        ),
    ]


class InternetExposureAnalyzer:
    """Candidate internet exposure of recorded entry points, never proof of reachability.

    Two kinds of entry point are evaluated with the step 29 primitives: resources (network
    interfaces, and instances without stored interfaces) with a recorded public IPv4 address,
    through their subnet's route to an attached internet gateway, NACLs, and security groups
    for the ports open to `0.0.0.0/0`; and load balancers, through scheme, routes, NACLs,
    security groups, listeners, and forwarding to registered targets, which are followed to
    backend instances within `max_depth`. Allow and deny are supported conclusions; unknown is
    insufficient evidence, and any unresolved or unsupported element (NAT, transit gateway,
    peering, endpoint, prefix-list, and IPv6 elements) keeps the result unknown. Observed
    target health is an observation, never part of a decision. Findings are qualified by the
    recorded EC2 and load-balancing coverage, ordered by decision, entry kind, and identity
    before the result limit applies, and a group-level finding reports limited or unrecorded
    collection and truncation. Azure returns the shared unsupported-platform result.
    """

    kind = AnalyzerKind.INTERNET_EXPOSURE
    version = "1"

    def analyze(self, index: GraphIndex, parameters: AnalyzerParameters) -> AnalysisResult:
        if not isinstance(parameters, InternetExposureParameters):
            raise TypeError("internet-exposure analyzer requires its own parameters")
        if index.scope.platform != CloudPlatform.AWS:
            finding = unsupported_platform_finding(index, "internet exposure")
            return build_result(self, index, parameters, [finding])
        findings = bound_findings(
            index,
            (_finding(index, item) for item in _evaluations(index, parameters.max_depth)),
            order=_order,
            limit=parameters.result_limit,
            limitations=service_coverage_limitations(index, COVERAGE_SERVICES),
            summary=_INCOMPLETE_SUMMARY,
            truncation_message="Increase the result limit to see every exposure candidate.",
        )
        return build_result(self, index, parameters, findings)
