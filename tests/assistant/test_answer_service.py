"""The answer service over the fake provider: validation, the one repair, and rendering.

Each run checks the provider requests made, the answer outcome, the exit code, and the
transcript, including the repair turn and the outcome it repaired.
"""

import asyncio
import json

import pytest

from archdiscov.assistant.answer_service import (
    AnswerCancelled,
    AnswerResult,
    answer_question,
    answer_question_async,
)
from archdiscov.assistant.conversation import ToolResultMessage, UserMessage
from archdiscov.assistant.fake import FakeProvider, FakeTranscript
from archdiscov.assistant.orchestrator import RunInEventLoopError
from archdiscov.assistant.provider import CompletionResponse
from archdiscov.assistant.repair import NO_PROPOSAL, NOT_RUN, RESUBMIT
from archdiscov.assistant.runs import RunLimits
from archdiscov.assistant.tools.session import ToolSession
from tests.support.scripted_answers import (
    ANSWER_QUESTION,
    PLAIN_TEXT,
    SEARCH,
    answer_proposal,
    answer_turns,
    rejected_proposal,
    submit_turn,
)
from tests.support.scripted_turns import SNAPSHOT, HangingProvider, calls_turn, fail_turn, text_turn


def _repair_request(provider: FakeProvider):
    """The last request and its repair prompt messages, after the last assistant message."""
    request = provider.requests[-1].request
    messages = request.conversation.messages
    last = max(index for index, item in enumerate(messages) if item.role == "assistant")
    return request, messages[last + 1 :]


def _submission(prompt) -> dict:
    (message,) = prompt
    assert isinstance(message, ToolResultMessage)
    (result,) = message.results
    assert result.is_error
    return json.loads(result.content)


# ---- answers -----------------------------------------------------------------------


def test_a_valid_first_proposal_is_answered_without_repair() -> None:
    result, provider, _ = answer_turns(
        lambda session: [calls_turn(SEARCH), submit_turn("a", answer_proposal(session))]
    )
    assert result.outcome.status == "answered" and result.outcome.exit_code == 0
    assert len(provider.requests) == 2
    assert result.transcript.repair is None
    assert not any(turn.repair for turn in result.transcript.turns)
    assert result.rendered.status == "insufficient_evidence"


def test_a_rejected_proposal_is_repaired_once() -> None:
    result, provider, _ = answer_turns(
        lambda session: [
            submit_turn("a", rejected_proposal(session)),
            submit_turn("b", answer_proposal(session)),
        ]
    )
    assert result.outcome.status == "answered_after_repair" and result.outcome.exit_code == 0
    assert len(provider.requests) == 2
    request, prompt = _repair_request(provider)
    assert [tool.name for tool in request.tools] == ["submit_answer"]
    submission = _submission(prompt)
    assert submission["error"] == "answer_rejected" and submission["next"] == RESUBMIT
    assert {item["code"] for item in submission["rejections"]} >= {"answer_unknown_id"}
    assert "Invented reasoning" not in prompt[0].model_dump_json()
    transcript = result.transcript
    assert [turn.repair for turn in transcript.turns] == [False, True]
    assert transcript.repair.outcome.kind == "proposed"
    assert "answer_unknown_id" in transcript.repair.prompt.text


def test_a_plain_text_answer_is_repaired_without_quoting_it() -> None:
    result, provider, _ = answer_turns(
        lambda session: [text_turn(PLAIN_TEXT), submit_turn("a", answer_proposal(session))]
    )
    assert result.outcome.status == "answered_after_repair"
    _, prompt = _repair_request(provider)
    assert prompt == (UserMessage(text=NO_PROPOSAL),)
    assert PLAIN_TEXT not in result.transcript.repair.prompt.text
    assert result.transcript.repair.outcome.kind == "answered"


@pytest.mark.parametrize(
    "first",
    [
        lambda session: submit_turn("a", None),
        lambda session: submit_turn("a", {"status": "supported"}),
    ],
    ids=["proposal_malformed", "proposal_invalid"],
)
def test_an_unreadable_submission_is_repaired(first) -> None:
    result, provider, _ = answer_turns(
        lambda session: [first(session), submit_turn("b", answer_proposal(session))]
    )
    assert result.outcome.status == "answered_after_repair"
    submission = _submission(_repair_request(provider)[1])
    assert submission["error"] == result.transcript.repair.outcome.reason


def test_a_mixed_submission_is_repaired_and_its_other_calls_are_not_run() -> None:
    def turns(session: ToolSession) -> list[dict]:
        mixed = calls_turn(
            ("a", "submit_answer", answer_proposal(session)), SEARCH, ignore_request_limits=True
        )
        return [mixed, submit_turn("b", answer_proposal(session))]

    result, provider, session = answer_turns(turns)
    assert result.outcome.status == "answered_after_repair"
    _, prompt = _repair_request(provider)
    (message,) = prompt
    by_call = {item.call_id: json.loads(item.content) for item in message.results}
    assert by_call["a"]["error"] == "proposal_mixed"
    assert by_call["c1"] == {"error": "tool_not_run", "message": NOT_RUN}
    assert session.record.returned().snapshots == ()


# ---- limits and errors -------------------------------------------------------------


def test_the_repair_is_requested_after_every_allowed_request_was_used() -> None:
    result, provider, _ = answer_turns(
        lambda session: [
            calls_turn(SEARCH),
            submit_turn("a", rejected_proposal(session)),
            submit_turn("b", answer_proposal(session)),
        ],
        RunLimits(max_provider_requests=2),
    )
    assert result.outcome.status == "answered_after_repair"
    assert len(provider.requests) == 3
    assert [turn.repair for turn in result.transcript.turns] == [False, False, True]


def test_no_repair_is_requested_past_the_overall_deadline() -> None:
    result, provider, _ = answer_turns(
        lambda _: [
            calls_turn(SEARCH, delay_seconds=9),
            {**text_turn(PLAIN_TEXT), "delay_seconds": 6},
        ],
        RunLimits(request_timeout_seconds=10, overall_timeout_seconds=15),
    )
    assert result.outcome.failure == "limit_reached" and result.outcome.limit == "overall_deadline"
    assert result.outcome.exit_code == 6
    assert len(provider.requests) == 2
    assert result.transcript.repair is not None
    assert not any(turn.repair for turn in result.transcript.turns)


def test_no_repair_is_requested_without_room_for_output() -> None:
    result, provider, _ = answer_turns(
        lambda _: [text_turn("x" * 990)], RunLimits(max_total_output_chars=1_000)
    )
    assert result.outcome.failure == "limit_reached" and result.outcome.limit == "total_output"
    assert len(provider.requests) == 1
    assert result.transcript.repair is not None


def test_a_provider_error_during_the_repair_fails_the_answer() -> None:
    result, _provider, _ = answer_turns(
        lambda _: [text_turn(PLAIN_TEXT), fail_turn("provider_unavailable")]
    )
    assert result.outcome.failure == "provider_error"
    assert result.outcome.provider_error == "provider_unavailable"
    assert result.outcome.exit_code == 6
    (_, repair) = result.transcript.turns
    assert repair.repair and repair.error == "provider_unavailable"


def test_a_tool_call_during_the_repair_is_not_run() -> None:
    result, _, session = answer_turns(lambda _: [text_turn(PLAIN_TEXT), calls_turn(SEARCH)])
    assert result.outcome.failure == "malformed_output"
    assert result.outcome.malformed == "tool_not_offered"
    assert session.record.returned().snapshots == ()


def test_unrepairable_outcomes_get_no_repair() -> None:
    result, provider, _ = answer_turns(lambda _: [fail_turn("provider_rate_limit")])
    assert result.outcome.failure == "provider_error"
    assert len(provider.requests) == 1
    assert result.transcript.repair is None


# ---- cancellation and the sync wrapper ---------------------------------------------


class _TextThenHang(HangingProvider):
    """Answers in plain text, then hangs on the repair request."""

    async def complete_async(self, request):
        if self.calls == 0:
            self.calls += 1
            return CompletionResponse.model_validate(text_turn(PLAIN_TEXT)["response"])
        return await super().complete_async(request)


def test_cancelling_the_repair_explains_the_cancelled_answer() -> None:
    provider = _TextThenHang()

    async def scenario() -> AnswerResult:
        task = asyncio.create_task(
            answer_question_async(
                ANSWER_QUESTION,
                provider=provider,
                session=ToolSession.from_paths(SNAPSHOT),
                provider_kind="fake",
                model="fake",
            )
        )
        await provider.entered.wait()
        task.cancel()
        with pytest.raises(AnswerCancelled) as raised:
            await task
        return raised.value.result

    result = asyncio.run(scenario())
    assert result.outcome.failure == "cancelled" and result.outcome.exit_code == 130
    assert result.transcript.turns[-1].repair
    assert provider.settled


def test_the_sync_wrapper_is_refused_inside_a_running_loop() -> None:
    async def inside() -> None:
        with pytest.raises(RunInEventLoopError, match="await answer_question_async"):
            answer_question(
                ANSWER_QUESTION,
                provider=FakeProvider(FakeTranscript.model_validate({"turns": [text_turn()]})),
                session=ToolSession.from_paths(SNAPSHOT),
                provider_kind="fake",
                model="fake",
            )

    asyncio.run(inside())
