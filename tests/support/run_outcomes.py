"""One example of every run outcome, and a rejection to fail a proposal with."""

from archdiscov.assistant.answers import AnswerProposal
from archdiscov.assistant.conversation import AssistantMessage
from archdiscov.assistant.provider import ProviderErrorCode, StopReason
from archdiscov.assistant.rejections import RejectionCode, reject
from archdiscov.assistant.runs import (
    Answered,
    Cancelled,
    LimitReached,
    MalformedOutput,
    MalformedOutputReason,
    Proposed,
    ProviderFailed,
    RunLimitKind,
    RunOutcome,
)

UNKNOWN_ID_REJECTIONS = (
    reject(RejectionCode.UNKNOWN_ID, "conclusions[0].citations[0].finding_ids[0]"),
)
PROPOSAL = AnswerProposal.model_validate(
    {
        "status": "insufficient_evidence",
        "contexts": [{"side": "primary", "source_kind": "aws"}],
        "insufficient_reason": "no_applicable_findings",
        "summary": "Nothing applicable.",
    }
)


def every_run_outcome() -> list[RunOutcome]:
    """One example of every run outcome, every malformed reason, and every limit."""
    return [
        Proposed(proposal=PROPOSAL),
        Answered(message=AssistantMessage(text="Plain text."), stop_reason=StopReason.END),
        *(MalformedOutput(reason=reason) for reason in MalformedOutputReason),
        *(LimitReached(limit=limit) for limit in RunLimitKind),
        ProviderFailed(code=ProviderErrorCode.TIMEOUT, retryable=False, message="m"),
        Cancelled(),
    ]
