"""Independent checks of the phase 3 completion criteria over finished results.

The result models already reject some of these shapes at construction. These checks restate
the criteria over a finished result, so the audit also fails for a result built around the
models (for example with `model_copy`) and documents what each analyzer must guarantee.
"""

from archdiscov.models.analysis.results import AnalysisFinding, AnalysisResult
from archdiscov.models.architecture import LocalTarget, UnresolvedTarget
from archdiscov.models.enums import AnalysisStatus, AnalyzerKind, NetworkDecision
from archdiscov.models.network_path import compose_decisions

_PATH_BLOCKS = ("impact", "trust")
_BLOCKS = (*_PATH_BLOCKS, "database_access", "internet_exposure")
CONCLUDED = (AnalysisStatus.SUPPORTED, AnalysisStatus.PARTIAL)


def finding_violations(finding: AnalysisFinding) -> list[str]:
    """What a finding must do whatever the coverage: cite, qualify, and not overreach."""
    found = []
    if finding.status in CONCLUDED and not finding.evidence_ids:
        found.append("a concluded finding cites no evidence")
    if finding.status != AnalysisStatus.SUPPORTED and not finding.limitations:
        found.append("a qualified finding states no limitation")
    block_names = [name for name in _BLOCKS if getattr(finding, name) is not None]
    if finding.status in CONCLUDED and set(block_names) & set(_PATH_BLOCKS) and not finding.path:
        found.append("a path finding has no path")
    for name in block_names:
        block = getattr(finding, name)
        elements = getattr(block, "elements", None)
        if elements is None:
            continue
        decision = block.decision
        if compose_decisions(item.decision for item in elements) != decision:
            found.append("the decision does not compose its elements")
        if (
            decision == NetworkDecision.UNKNOWN
            and finding.status != AnalysisStatus.INSUFFICIENT_EVIDENCE
        ):
            found.append("an unknown decision is not insufficient evidence")
    found += _terminal_violations(finding)
    return found


def _terminal_violations(finding: AnalysisFinding) -> list[str]:
    codes = {limitation.code for limitation in finding.limitations}
    found = []
    for position, step in enumerate(finding.path):
        target = getattr(step, "target", None)
        if target is None or isinstance(target, LocalTarget):
            continue
        required = (
            "unresolved_target" if isinstance(target, UnresolvedTarget) else "external_target"
        )
        if required not in codes:
            found.append(f"a {target.kind} terminal has no {required} limitation")
        if position != len(finding.path) - 1:
            found.append("a path continues past a terminal")
    return found


def result_violations(
    result: AnalysisResult, *, limited: bool = False, unrecorded: bool = False
) -> list[str]:
    """Violations of a result; `limited`/`unrecorded` say what coverage the snapshot recorded.

    With limited coverage no finding may be supported and each states a coverage limitation.
    With no recorded coverage every analyzer except ownership (which does not interpret absent
    records) reports a `coverage_unrecorded_<service>` limitation in a group-level finding.
    """
    found = [
        f"{finding.id[:8]}: {problem}"
        for finding in result.findings
        for problem in finding_violations(finding)
    ]
    if limited:
        for finding in result.findings:
            codes = [limitation.code for limitation in finding.limitations]
            if finding.status == AnalysisStatus.SUPPORTED:
                found.append(f"{finding.id[:8]}: supported despite limited coverage")
            if not any(code.startswith("coverage_") for code in codes):
                found.append(f"{finding.id[:8]}: no coverage limitation under limited coverage")
    if (
        unrecorded
        and result.analyzer != AnalyzerKind.OWNERSHIP
        and not any(
            limitation.code.startswith("coverage_unrecorded_")
            for finding in result.findings
            for limitation in finding.limitations
        )
    ):
        found.append("no coverage_unrecorded limitation without recorded coverage")
    return found
