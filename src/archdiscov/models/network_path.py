"""Typed results shared by every network-path primitive."""

from typing import Self

from pydantic import Field, model_validator

from .analysis.references import CoverageReference
from .enums import NetworkBlockerCode, NetworkDecision
from .resource_base import Model, NonEmptyString


class NetworkBlocker(Model):
    """Why a decision is not an allow: a recorded denial, missing or unsupported input.

    Blockers are structured so callers never parse messages. `element` names the
    primitive element involved (for example a rule or route) and `coverage` identifies a
    recorded limited operation. Messages are descriptive and not part of any identity.
    """

    code: NetworkBlockerCode
    message: NonEmptyString
    element: NonEmptyString | None = None
    resource_id: NonEmptyString | None = None
    evidence_ids: list[NonEmptyString] = Field(default_factory=list)
    coverage: CoverageReference | None = None

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        if (self.coverage is not None) != (self.code == NetworkBlockerCode.COVERAGE_LIMITED):
            raise ValueError("only coverage_limited blockers carry a coverage reference")
        return self


class NetworkResult(Model):
    """One primitive's, or a composition's, decision with its evidence and blockers.

    An allow is always cited and has no blockers, so missing input can never allow.
    A deny is explained only by recorded `denied` blockers. Unknown has at least one
    blocker explaining the uncertainty and none of its blockers is a bare denial.
    Describes recorded configuration, not live reachability.
    """

    decision: NetworkDecision
    evidence_ids: list[NonEmptyString] = Field(default_factory=list)
    blockers: list[NetworkBlocker] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        codes = {blocker.code for blocker in self.blockers}
        denied = NetworkBlockerCode.DENIED
        if self.decision == NetworkDecision.ALLOW:
            if self.blockers or not self.evidence_ids:
                raise ValueError("an allow must be cited and have no blockers")
        elif self.decision == NetworkDecision.DENY:
            if denied not in codes or codes != {denied}:
                raise ValueError("a deny requires denied blockers only")
        elif not self.blockers or codes == {denied}:
            raise ValueError("unknown requires a blocker that explains the uncertainty")
        return self
