"""Minimal result shapes, not analyzers or tool-returned citation validation."""

from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from .architecture import Model, NonEmptyString
from .enums import AnalysisStatus


class _Result(Model):
    snapshot_id: UUID
    status: AnalysisStatus
    evidence_ids: list[NonEmptyString] = Field(default_factory=list)
    limitations: list[NonEmptyString] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_limitations(self) -> Self:
        if self.status != AnalysisStatus.SUPPORTED and not self.limitations:
            raise ValueError("partial and insufficient_evidence results require limitations")
        return self


class Finding(_Result):
    """One deterministic conclusion; IDs refer to the selected snapshot."""

    id: NonEmptyString
    summary: NonEmptyString
    resource_ids: list[NonEmptyString] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_evidence(self) -> Self:
        if self.status != AnalysisStatus.INSUFFICIENT_EVIDENCE and not self.evidence_ids:
            raise ValueError("supported and partial findings require evidence_ids")
        return self


class Answer(_Result):
    """References to findings rather than provider-authored factual conclusions."""

    finding_ids: list[NonEmptyString] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_findings(self) -> Self:
        if self.status != AnalysisStatus.INSUFFICIENT_EVIDENCE and not self.finding_ids:
            raise ValueError("supported and partial answers require finding_ids")
        return self
