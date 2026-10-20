from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from ..canonical import canonical_json, sha256_json
from ..enums import AnalysisStatus, AnalyzerKind, NetworkDecision
from ..resource_base import Model, NonEmptyString
from .database_access import DatabaseAccess
from .exposure import InternetExposure
from .impact import SecurityGroupImpact
from .parameters import (
    AnalyzerParameters,
    DatabaseAccessParameters,
    SecurityGroupImpactParameters,
)
from .references import AnalysisLimitation, PathStep, ResourceReference
from .trust import CrossAccountTrust

# Analyzer-specific finding blocks and the only analyzer allowed to produce each.
_BLOCK_ANALYZERS = {
    "impact": AnalyzerKind.SECURITY_GROUP_IMPACT,
    "trust": AnalyzerKind.CROSS_ACCOUNT_TRUST,
    "database_access": AnalyzerKind.DATABASE_ACCESS,
    "internet_exposure": AnalyzerKind.INTERNET_EXPOSURE,
}


# Per block: how findings name it, and the resources they must reference, in order.
_BLOCK_RESOURCES = {
    "trust": ("trust", ["the trusting resource"]),
    "database_access": ("database access", ["the source", "the database"]),
    "internet_exposure": ("internet exposure", ["the entry point"]),
}


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
    database_access: DatabaseAccess | None = None
    internet_exposure: InternetExposure | None = None

    @model_validator(mode="after")
    def validate_finding(self) -> Self:
        if self.status != AnalysisStatus.SUPPORTED and not self.limitations:
            raise ValueError("partial and insufficient_evidence results require limitations")
        if self.status != AnalysisStatus.INSUFFICIENT_EVIDENCE and not self.evidence_ids:
            raise ValueError("supported and partial findings require evidence_ids")
        if sum(getattr(self, name) is not None for name in _BLOCK_ANALYZERS) > 1:
            raise ValueError("a finding carries at most one analyzer-specific block")
        for name, (label, required) in _BLOCK_RESOURCES.items():
            if (block := getattr(self, name)) is not None:
                self._validate_block(block, label, required)
        derived_id = deterministic_finding_id(self)
        if self.id and self.id != derived_id:
            raise ValueError("finding id must match deterministic_finding_id")
        self.id = derived_id
        return self

    def _validate_block(self, block: object, label: str, required: list[str]) -> None:
        """Block-carrying findings name their resources, and unknown is never supported."""
        if len(self.resources) < len(required):
            raise ValueError(f"{label} findings must name {' and '.join(required)}")
        decision = getattr(block, "decision", None)
        if decision == NetworkDecision.UNKNOWN and self.status == AnalysisStatus.SUPPORTED:
            raise ValueError(f"an unknown {label} decision cannot be supported")


def deterministic_finding_id(finding: AnalysisFinding) -> str:
    """SHA-256 of validated semantic data, including the conclusion summary.

    Snapshot, status, references, path and limitation codes/context define identity.
    Limitation messages do not. Reference, limitation and citation order
    is immaterial; path order and attribute list order remain meaningful. Analyzer
    kind is envelope-only and therefore is not part of the finding identity.
    """
    exclude: dict[str, object] = {"id": True, "limitations": {"__all__": {"message"}}}
    for name in _BLOCK_ANALYZERS:
        if getattr(finding, name) is None:
            # Absent blocks keep identities of findings from other analyzers unchanged.
            exclude[name] = True
    payload = finding.model_dump(mode="json", exclude=exclude)
    payload["evidence_ids"] = sorted(payload["evidence_ids"])
    payload["resources"] = sorted(payload["resources"], key=canonical_json)
    for step in payload["path"]:
        step["evidence_ids"] = sorted(step["evidence_ids"])
    for limitation in payload["limitations"]:
        limitation["evidence_ids"] = sorted(limitation["evidence_ids"])
        if limitation["unresolved_target"] is not None:
            reference = limitation["unresolved_target"]
            reference["evidence_ids"] = sorted(reference["evidence_ids"])
    payload["limitations"] = sorted(payload["limitations"], key=canonical_json)
    return sha256_json(payload)


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
        if isinstance(self.parameters, DatabaseAccessParameters):
            self._validate_database_findings(self.parameters)
        ids = [finding.id for finding in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("finding IDs must be unique")
        return self

    def _validate_database_findings(self, parameters: DatabaseAccessParameters) -> None:
        for finding in self.findings:
            access = finding.database_access
            if access is None:
                continue
            if finding.resources[1].resource_id != parameters.resource_id:
                raise ValueError("database access must describe the selected database")
            if access.port != parameters.port:
                raise ValueError("database access must describe the selected port")
