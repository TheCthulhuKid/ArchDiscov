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

    @model_validator(mode="after")
    def validate_finding(self) -> Self:
        if self.status != AnalysisStatus.SUPPORTED and not self.limitations:
            raise ValueError("partial and insufficient_evidence results require limitations")
        if self.status != AnalysisStatus.INSUFFICIENT_EVIDENCE and not self.evidence_ids:
            raise ValueError("supported and partial findings require evidence_ids")
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
    if finding.impact is None:
        # Absent impact keeps identities of findings from other analyzers unchanged.
        exclude["impact"] = True
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
        impacts = [finding.impact for finding in self.findings if finding.impact is not None]
        if impacts and not isinstance(self.parameters, SecurityGroupImpactParameters):
            raise ValueError("impact findings require security_group_impact parameters")
        if any(impact.selected_group_id != self.parameters.resource_id for impact in impacts):
            raise ValueError("impact must describe the selected security group")
        ids = [finding.id for finding in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("finding IDs must be unique")
        return self
