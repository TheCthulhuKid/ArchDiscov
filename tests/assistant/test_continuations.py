"""Provider continuations: opaque blocks replayed unchanged, bounded, and never recorded."""

import json

import pytest
from pydantic import ValidationError

from archdiscov.assistant.answer_output import answer_data, answer_text
from archdiscov.assistant.conversation import (
    MAX_CONTINUATION_BYTES,
    AssistantMessage,
    ConversationValidationError,
    ProviderContinuation,
    parse_conversation,
)
from archdiscov.assistant.fake import load_fake_transcript
from archdiscov.assistant.provider import CompletionResponse
from archdiscov.assistant.runs import (
    LimitReached,
    MalformedOutput,
    MalformedOutputReason,
    RunLimitKind,
    RunLimits,
)
from tests.assistant.test_answer_service import SEARCH, _answer, _proposal, _submit
from tests.assistant.test_orchestrator import _calls, _continued, _run, _text
from tests.paths import TESTS_ROOT

# Long and opaque, as real signatures are: the secret checks would reject it as text.
SIGNATURE = "EqQBCkgIBhABGAIiQL" + "x7Kp2QmZ9vR4tW8yB1nC6dF3gH5jL0aS" * 12
THINKING = {"type": "thinking", "thinking": "", "signature": SIGNATURE}


def _continuation(provider_kind: str = "fake", **block) -> dict:
    return {"provider_kind": provider_kind, "blocks": [{**THINKING, **block}]}


def _issues(data: dict) -> list[str]:
    conversation = {"messages": [{"role": "user", "text": "q"}, data]}
    with pytest.raises(ConversationValidationError) as raised:
        parse_conversation(conversation)
    rendered = repr(raised.value.details) + str(raised.value)
    assert SIGNATURE not in rendered
    return [issue.type for issue in raised.value.details]


@pytest.mark.parametrize("continuation", [None, _continuation()], ids=["without", "with"])
def test_messages_and_responses_round_trip(continuation) -> None:
    message = AssistantMessage.model_validate({"text": "Searching.", "continuation": continuation})
    response = CompletionResponse(message=message, stop_reason="end")
    assert CompletionResponse.model_validate_json(response.model_dump_json()) == response
    assert (
        parse_conversation(
            {"messages": [{"role": "user", "text": "q"}, message.model_dump(mode="json")]}
        ).messages[1]
        == message
    )


def test_a_continuation_is_bounded_and_named() -> None:
    within = ProviderContinuation.model_validate(_continuation())
    assert within.size == len(json.dumps([THINKING], separators=(",", ":")))
    padding = "x" * MAX_CONTINUATION_BYTES
    assert _issues(
        {"role": "assistant", "text": "a", "continuation": _continuation(thinking=padding)}
    ) == ["continuation_too_large"]
    for invalid in ({"provider_kind": "fake", "blocks": []}, _continuation("Not A Kind")):
        with pytest.raises(ValidationError):
            ProviderContinuation.model_validate(invalid)


def test_a_continuation_alone_is_not_a_message() -> None:
    assert _issues({"role": "assistant", "continuation": _continuation()}) == [
        "assistant_message_empty"
    ]


def test_the_fake_returns_it_and_the_next_request_replays_it_unchanged() -> None:
    turn = _continued(_calls(SEARCH), "fake", THINKING)
    result, provider, _ = _run([turn, _text()])
    assert result.outcome.kind == "answered"
    scripted = AssistantMessage.model_validate(turn["response"]["message"])
    replayed = provider.requests[1].request.conversation.messages
    assert [message for message in replayed if isinstance(message, AssistantMessage)] == [scripted]
    assert result.transcript.turns[0].continuation_bytes == scripted.continuation.size
    assert result.transcript.turns[1].continuation_bytes is None


def test_another_providers_continuation_ends_the_run_unreplayed() -> None:
    result, provider, _ = _run([_continued(_calls(SEARCH), "anthropic", THINKING)])
    assert result.outcome == MalformedOutput(reason=MalformedOutputReason.CONTINUATION_FOREIGN)
    assert len(provider.requests) == 1


def test_a_continuation_counts_toward_total_output() -> None:
    large = _continued(_text(), "fake", {**THINKING, "thinking": "t" * 2_000})
    result, _, _ = _run([large], limits=RunLimits(max_total_output_chars=1_000))
    assert result.outcome == LimitReached(limit=RunLimitKind.TOTAL_OUTPUT)


def test_transcripts_and_renderings_hold_no_continuation_content() -> None:
    result, provider, _ = _answer(
        lambda session: [
            _continued(_calls(SEARCH), "fake", THINKING),
            _continued(_submit("a", _proposal(session)), "fake", THINKING),
        ]
    )
    assert result.outcome.status == "answered"
    assert SIGNATURE in provider.requests[1].request.model_dump_json()
    for rendered in (
        result.model_dump_json(),
        answer_text(result.rendered),
        json.dumps(answer_data(result.rendered)),
    ):
        assert SIGNATURE not in rendered
    assert [turn.continuation_bytes for turn in result.transcript.turns] == [
        len(json.dumps([THINKING], separators=(",", ":")))
    ] * 2


@pytest.mark.parametrize("name", ["multi_step.json", "failures.json"])
def test_existing_fixtures_load_unchanged(name: str) -> None:
    path = TESTS_ROOT / "fixtures" / "assistant" / name
    transcript = load_fake_transcript(path)
    messages = [
        turn.response.message for turn in transcript.turns if getattr(turn, "response", None)
    ]
    assert messages and all(message.continuation is None for message in messages)
