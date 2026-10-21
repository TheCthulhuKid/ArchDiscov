"""The rejection vocabulary for answer proposals: stable codes with fixed messages.

Kept apart from the validator so contracts that report rejections (such as answer outcomes)
do not import the tool session, graph index, or services.
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from archdiscov.models.validation_errors import validation_error

from .conversation import Contract

MAX_LISTED_REJECTIONS = 40


class RejectionCode(StrEnum):
    SIDE_UNAVAILABLE = "answer_side_unavailable"
    SOURCE_KIND_MISMATCH = "answer_source_kind_mismatch"
    UNKNOWN_ID = "answer_unknown_id"
    OTHER_SNAPSHOT_ID = "answer_other_snapshot_id"
    NOT_RETURNED = "answer_id_not_returned"
    EVIDENCE_DETACHED = "answer_evidence_detached"
    PLANNED_AS_DEPLOYED = "answer_planned_as_deployed"
    STATUS_UPGRADE = "answer_status_upgrade"
    LIMITATION_UNACKNOWLEDGED = "answer_limitation_unacknowledged"
    LIMITATION_UNKNOWN = "answer_limitation_unknown"
    CONTRADICTION_OMITTED = "answer_contradiction_omitted"
    NARRATIVE_IMPORTED_TEXT = "answer_narrative_imported_text"
    NARRATIVE_UNCITED_ID = "answer_narrative_uncited_id"


REJECTION_MESSAGES: Mapping[RejectionCode, str] = {
    RejectionCode.SIDE_UNAVAILABLE: "No comparison snapshot was selected for this session.",
    RejectionCode.SOURCE_KIND_MISMATCH: (
        "The source kind differs from the one the tools reported for this snapshot."
    ),
    RejectionCode.UNKNOWN_ID: "This ID is not recorded in the snapshot for this side.",
    RejectionCode.OTHER_SNAPSHOT_ID: (
        "This ID belongs to the other selected snapshot; cite it on that side."
    ),
    RejectionCode.NOT_RETURNED: "No tool returned this ID in this conversation.",
    RejectionCode.EVIDENCE_DETACHED: (
        "This evidence belongs to neither a finding nor a resource cited alongside it."
    ),
    RejectionCode.PLANNED_AS_DEPLOYED: (
        "This snapshot records planned or declared configuration; do not describe it as observed."
    ),
    RejectionCode.STATUS_UPGRADE: "The status is stronger than the cited findings support.",
    RejectionCode.LIMITATION_UNACKNOWLEDGED: (
        "This finding has limitations that the answer does not acknowledge."
    ),
    RejectionCode.LIMITATION_UNKNOWN: "This finding has no limitation with this code.",
    RejectionCode.CONTRADICTION_OMITTED: (
        "A returned finding about the same resource from the same result reaches a "
        "different conclusion and is not cited."
    ),
    RejectionCode.NARRATIVE_IMPORTED_TEXT: (
        "This narrative repeats text imported from cloud configuration or source files."
    ),
    RejectionCode.NARRATIVE_UNCITED_ID: (
        "This narrative names a recorded ID that no conclusion cites."
    ),
}

ProposalLocation = Annotated[str, Field(pattern=r"^[a-z_]+(?:\[\d+\]|\.[a-z_]+)*$")]


class Rejection(Contract):
    """One reason a proposal fails; the message is fixed by the code."""

    code: RejectionCode
    location: ProposalLocation
    message: str

    @model_validator(mode="after")
    def require_fixed_message(self) -> Self:
        if self.message != REJECTION_MESSAGES[self.code]:
            raise validation_error("answer_rejection_message", "message is fixed by code")
        return self


def reject(code: RejectionCode, location: str) -> Rejection:
    return Rejection(code=code, location=location, message=REJECTION_MESSAGES[code])
