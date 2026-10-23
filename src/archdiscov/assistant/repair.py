"""The one repair prompt: fixed text that says what to correct, never what the model wrote.

A rejected, malformed, invalid, or mixed `submit_answer` call is answered with a tool result
per call. The `submit_answer` result lists the typed rejection codes, their locations, and
their fixed messages, or the fixed reason the call could not be read, then what to do next;
any other call in a mixed response is answered as not run. `submission_text` builds that
result for any follow-up line, so another caller can report a submission the same way. A
plain-text answer is followed by a user message saying only that no proposal was
submitted; the text is never quoted back.
"""

import json
from collections.abc import Mapping

from .answer_results import needs_repair
from .answers import SUBMIT_ANSWER
from .conversation import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from .rejections import MAX_LISTED_REJECTIONS, Rejection
from .runs import Answered, MalformedOutput, MalformedOutputReason, Proposed, RunOutcome

RESUBMIT = (
    "Call submit_answer once more with a corrected answer. It is the only tool offered now, "
    "and there is no further repair."
)
NO_PROPOSAL = (
    "No answer was submitted: the previous reply was plain text. Answer only by calling "
    "submit_answer once; it is the only tool offered now, and there is no further repair."
)
NOT_RUN = "This call was not run: submit_answer must be called on its own."

# Why a `submit_answer` call could not be read, for the malformed reasons a repair fixes.
_UNREADABLE: Mapping[MalformedOutputReason, str] = {
    MalformedOutputReason.PROPOSAL_MALFORMED: "The submit_answer arguments were not a JSON "
    "object within the size limit.",
    MalformedOutputReason.PROPOSAL_INVALID: "The submit_answer arguments do not match its "
    "input schema.",
    MalformedOutputReason.PROPOSAL_MIXED: "submit_answer was called together with other "
    "tools; call it on its own.",
}


def repair_prompt(
    outcome: RunOutcome, message: AssistantMessage, rejections: tuple[Rejection, ...]
) -> tuple[Message, ...]:
    """The repair for a rejected proposal, an unreadable submission, or a plain-text answer."""
    if not needs_repair(outcome, rejections):
        raise ValueError("only an outcome that needs a repair is repaired")
    if isinstance(outcome, Answered):
        return (UserMessage(text=NO_PROPOSAL),)
    submission = submission_text(outcome, rejections, RESUBMIT)
    return (
        ToolResultMessage(results=tuple(_answer(call, submission) for call in message.tool_calls)),
    )


def _answer(call: ToolCall, submission: str) -> ToolResult:
    if call.name == SUBMIT_ANSWER:
        return ToolResult(call_id=call.id, content=submission, is_error=True)
    return ToolResult(
        call_id=call.id, content=_json({"error": "tool_not_run", "message": NOT_RUN}), is_error=True
    )


def submission_text(
    outcome: Proposed | MalformedOutput, rejections: tuple[Rejection, ...], follow_up: str
) -> str:
    """Why a submission was not accepted, then `follow_up`: the unreadable reason, or up to
    `MAX_LISTED_REJECTIONS` rejections and how many more there were."""
    if isinstance(outcome, MalformedOutput):
        body = {"error": outcome.reason.value, "message": _UNREADABLE[outcome.reason]}
    else:
        listed = rejections[:MAX_LISTED_REJECTIONS]
        body = {
            "error": "answer_rejected",
            "rejections": [
                {"code": item.code.value, "location": item.location, "message": item.message}
                for item in listed
            ],
            "unlisted": len(rejections) - len(listed),
        }
    return _json({**body, "next": follow_up})


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
