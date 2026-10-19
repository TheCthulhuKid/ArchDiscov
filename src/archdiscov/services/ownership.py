"""Cloud-neutral ownership signals from recorded tags, never from collection assumptions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from archdiscov.models.analysis.parameters import AnalyzerParameters, OwnershipParameters
from archdiscov.models.analysis.references import AnalysisLimitation, ResourceReference
from archdiscov.models.analysis.results import AnalysisFinding, AnalysisResult
from archdiscov.models.enums import AnalysisStatus, AnalyzerKind, EvidenceValueStatus
from archdiscov.models.resource_base import Resource

from .analysis_qualification import qualify_status
from .analyzers import build_result
from .query_context import TAGS_POINTER, tag_provenance

if TYPE_CHECKING:
    from archdiscov.graph import GraphIndex

_SUMMARY = (
    "Recorded tags have no non-empty value for {keys}; this is an ownership signal, "
    "not proof that nobody owns the resource."
)
_INSUFFICIENT_SUMMARY = "Ownership tags cannot be assessed from the recorded data."
_UNAVAILABLE = (
    "Tag provenance is unavailable or incomplete; missing ownership tags are not proof of absence."
)


def _has_owner_tag(resource: Resource, keys: list[str]) -> bool:
    """An ownership key counts when recorded exactly (case-sensitive) with a nonblank string."""
    tags = resource.attributes.get("tags")
    return isinstance(tags, dict) and any(
        isinstance(tags.get(key), str) and tags[key].strip() for key in keys
    )


def _finding(index: GraphIndex, resource: Resource, keys: list[str]) -> AnalysisFinding:
    cited = index.fact_evidence_ids(resource.id, TAGS_POINTER)
    limitations: list[AnalysisLimitation] = []
    if tag_provenance(index, resource) is None:
        status = AnalysisStatus.SUPPORTED
        evidence_ids = [
            evidence_id
            for evidence_id in cited
            if index.get_evidence(evidence_id).value_status == EvidenceValueStatus.PRESENT
        ]
        summary = _SUMMARY.format(keys=" or ".join(keys))
    else:
        status = AnalysisStatus.INSUFFICIENT_EVIDENCE
        evidence_ids = []
        summary = _INSUFFICIENT_SUMMARY
        limitations.append(
            AnalysisLimitation(
                code="tags_unavailable", message=_UNAVAILABLE, evidence_ids=list(cited)
            )
        )
    qualified = qualify_status(index, [resource], status)
    return AnalysisFinding(
        snapshot_id=index.snapshot_id,
        summary=summary,
        status=qualified.status,
        evidence_ids=evidence_ids,
        resources=[ResourceReference(resource_id=resource.id, scope=index.scope)],
        limitations=[*limitations, *qualified.limitations],
    )


class OwnershipAnalyzer:
    """Report resources whose recorded tags lack every configured ownership key.

    Resources with an ownership tag produce no finding. Missing tags are reported
    only when the whole tag map is cited as present; otherwise the result is
    insufficient evidence. Coverage gaps downgrade results through the shared
    qualification helper. The analyzer never branches on cloud platform.
    """

    kind = AnalyzerKind.OWNERSHIP
    version = "1"

    def analyze(self, index: GraphIndex, parameters: AnalyzerParameters) -> AnalysisResult:
        if not isinstance(parameters, OwnershipParameters):
            raise TypeError("ownership analyzer requires ownership parameters")
        keys = parameters.ownership_keys
        findings = [
            _finding(index, resource, keys)
            for resource in index.resources()
            if not _has_owner_tag(resource, keys)
        ]
        return build_result(self, index, parameters, findings)
