import hashlib
import json
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from ..enums import AnalysisStatus, AnalyzerKind
from ..resource_base import Model, NonEmptyString
from .impact import SecurityGroupImpact
from .parameters import AnalyzerParameters, SecurityGroupImpactParameters
from .references import AnalysisLimitation, PathStep, ResourceReference
from .trust import CrossAccountTrust


class AnalysisFinding(Model):
    """Structured finding preserving the legacy Finding evidence and uncertainty rules."""

    snapshot_id: UUID
    id: NonEmptyString = Field(default="")
    summary: NonEmptyString
    status: AnalysisStatus
    evidence_ids: list[NonEmptyString] = Field(default_factory=list)
    resources: list[ResourceReference] = Field(default_factory=list)
    path: list[PathStep] = Field(default_factory=list)
    limitations: list[AnalysisLimitation] = Field(default_factory=list)
    impact: SecurityGroupImpact | None = None
    trust: CrossAccountTrust | None = None

    @model_validator(mode="after")
    def validate_finding(self) -> Self:
        if self.status != AnalysisStatus.SUPPORTED and not self.limitations:
            raise ValueError("partial and insufficient_evidence results require limitations")
        if self.status != AnalysisStatus.INSUFFICIENT_EVIDENCE and not self.evidence_ids:
            raise ValueError("supported and partial findings require evidence_ids")
        if self.impact is not None and self.trust is not None:
            raise ValueError("a finding carries at most one analyzer-specific block")
        if self.trust is not None and not self.resources:
            raise ValueError("trust findings must name the trusting resource")
        derived_id = deterministic_finding_id(self)
        if self.id and self.id != derived_id:
            raise ValueError("finding id must match deterministic_finding_id")
        self.id = derived_id
        return self


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def deterministic_finding_id(finding: AnalysisFinding) -> str:
    """SHA-256 of validated semantic data, including the conclusion summary.

    Snapshot, status, references, path and limitation codes/context define identity.
    Limitation messages do not. Reference, limitation and citation order
    is immaterial; path order and attribute list order remain meaningful. Analyzer
    kind is envelope-only and therefore is not part of the finding identity.
    """
    exclude: dict[str, object] = {"id": True, "limitations": {"__all__": {"message"}}}
    for name in ("impact", "trust"):
        if getattr(finding, name) is None:
            # Absent blocks keep identities of findings from other analyzers unchanged.
            exclude[name] = True
    payload = finding.model_dump(mode="json", exclude=exclude)
    payload["evidence_ids"] = sorted(payload["evidence_ids"])
    payload["resources"] = sorted(payload["resources"], key=_canonical_json)
    for step in payload["path"]:
        step["evidence_ids"] = sorted(step["evidence_ids"])
    for limitation in payload["limitations"]:
        limitation["evidence_ids"] = sorted(limitation["evidence_ids"])
        if limitation["unresolved_target"] is not None:
            reference = limitation["unresolved_target"]
            reference["evidence_ids"] = sorted(reference["evidence_ids"])
    payload["limitations"] = sorted(payload["limitations"], key=_canonical_json)
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


# Analyzer-specific finding blocks and the only analyzer allowed to produce each.
_BLOCK_ANALYZERS = {
    "impact": AnalyzerKind.SECURITY_GROUP_IMPACT,
    "trust": AnalyzerKind.CROSS_ACCOUNT_TRUST,
}


class AnalysisResult(Model):
    schema_version: Literal["1"] = "1"
    snapshot_id: UUID
    analyzer: AnalyzerKind
    analyzer_version: NonEmptyString
    parameters: AnalyzerParameters
    created_at: AwareDatetime
    findings: list[AnalysisFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.parameters.kind != self.analyzer:
            raise ValueError("parameter kind must match analyzer")
        if any(finding.snapshot_id != self.snapshot_id for finding in self.findings):
            raise ValueError("finding snapshot_id must match result snapshot_id")
        for block, kind in _BLOCK_ANALYZERS.items():
            if self.parameters.kind != kind and any(
                getattr(finding, block) is not None for finding in self.findings
            ):
                raise ValueError(f"{block} findings require {kind} parameters")
        if isinstance(self.parameters, SecurityGroupImpactParameters) and any(
            finding.impact.selected_group_id != self.parameters.resource_id
            for finding in self.findings
            if finding.impact is not None
        ):
            raise ValueError("impact must describe the selected security group")
        ids = [finding.id for finding in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("finding IDs must be unique")
        return self
