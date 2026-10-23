"""The submission result shared by the repair prompt and other callers: why a submission was
not accepted, and a follow-up line the caller chooses."""

import json

import pytest

from archdiscov.assistant.answers import AnswerProposal
from archdiscov.assistant.conversation import AssistantMessage, ParsedToolArguments, ToolCall
from archdiscov.assistant.rejections import (
    MAX_LISTED_REJECTIONS,
    REJECTION_MESSAGES,
    RejectionCode,
    reject,
)
from archdiscov.assistant.repair import RESUBMIT, repair_prompt, submission_text
from archdiscov.assistant.runs import MalformedOutput, MalformedOutputReason, Proposed

FOLLOW_UP = "Correct the answer and submit it again."
PROPOSED = Proposed(
    proposal=AnswerProposal.model_validate(
        {
            "status": "insufficient_evidence",
            "contexts": [{"side": "primary", "source_kind": "aws"}],
            "insufficient_reason": "no_applicable_findings",
            "summary": "Nothing applicable.",
        }
    )
)


def test_rejections_are_listed_up_to_the_bound_with_the_follow_up() -> None:
    codes = list(RejectionCode)
    rejections = tuple(
        reject(codes[number % len(codes)], f"conclusions[{number}]")
        for number in range(MAX_LISTED_REJECTIONS + 3)
    )
    data = json.loads(submission_text(PROPOSED, rejections, FOLLOW_UP))
    assert list(data) == ["error", "rejections", "unlisted", "next"]
    assert data["error"] == "answer_rejected" and data["next"] == FOLLOW_UP
    assert len(data["rejections"]) == MAX_LISTED_REJECTIONS and data["unlisted"] == 3
    first = data["rejections"][0]
    assert first == {
        "code": codes[0].value,
        "location": "conclusions[0]",
        "message": REJECTION_MESSAGES[codes[0]],
    }


@pytest.mark.parametrize(
    "reason",
    [MalformedOutputReason.PROPOSAL_MALFORMED, MalformedOutputReason.PROPOSAL_INVALID],
    ids=str,
)
def test_an_unreadable_submission_names_its_reason_with_the_follow_up(reason) -> None:
    data = json.loads(submission_text(MalformedOutput(reason=reason), (), FOLLOW_UP))
    assert list(data) == ["error", "message", "next"]
    assert data["error"] == reason.value and data["next"] == FOLLOW_UP


def test_the_repair_prompt_is_the_submission_text_with_resubmit() -> None:
    call = ToolCall(id="a", name="submit_answer", arguments=ParsedToolArguments(value={}))
    rejections = (reject(RejectionCode.UNKNOWN_ID, "conclusions[0]"),)
    [message] = repair_prompt(PROPOSED, AssistantMessage(tool_calls=(call,)), rejections)
    [result] = message.results
    assert result.is_error
    assert result.content == submission_text(PROPOSED, rejections, RESUBMIT)
