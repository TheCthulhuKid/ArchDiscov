"""Provider continuations: opaque blocks replayed unchanged, bounded, and never recorded."""

import json

import pytest

from archdiscov.assistant.answer_output import answer_data, answer_text
from archdiscov.assistant.conversation import (
    MAX_CONTINUATION_BYTES,
    AssistantMessage,
    ConversationValidationError,
    ProviderContinuation,
    parse_conversation,
)
from archdiscov.assistant.runs import (
    LimitReached,
    MalformedOutput,
    MalformedOutputReason,
    RunLimitKind,
    RunLimits,
)
from tests.support.provider_data import SIGNATURE, THINKING
from tests.support.scripted_answers import SEARCH, answer_proposal, answer_turns, submit_turn
from tests.support.scripted_turns import calls_turn, continued_turn, run_turns, text_turn


def _continuation(**block) -> dict:
    return {"provider_kind": "fake", "blocks": [{**THINKING, **block}]}


def _issues(data: dict) -> list[str]:
    conversation = {"messages": [{"role": "user", "text": "q"}, data]}
    with pytest.raises(ConversationValidationError) as raised:
        parse_conversation(conversation)
    rendered = repr(raised.value.details) + str(raised.value)
    assert SIGNATURE not in rendered
    return [issue.type for issue in raised.value.details]


def test_a_continuation_is_bounded_and_not_a_message_alone() -> None:
    within = ProviderContinuation.model_validate(_continuation())
    assert within.size == len(json.dumps([THINKING], separators=(",", ":")))
    padding = "x" * MAX_CONTINUATION_BYTES
    assert _issues(
        {"role": "assistant", "text": "a", "continuation": _continuation(thinking=padding)}
    ) == ["continuation_too_large"]
    assert _issues({"role": "assistant", "continuation": _continuation()}) == [
        "assistant_message_empty"
    ]


def test_the_fake_returns_it_and_the_next_request_replays_it_unchanged() -> None:
    turn = continued_turn(calls_turn(SEARCH), "fake", THINKING)
    result, provider, _ = run_turns([turn, text_turn()])
    assert result.outcome.kind == "answered"
    scripted = AssistantMessage.model_validate(turn["response"]["message"])
    replayed = provider.requests[1].request.conversation.messages
    assert [message for message in replayed if isinstance(message, AssistantMessage)] == [scripted]
    assert result.transcript.turns[0].continuation_bytes == scripted.continuation.size
    assert result.transcript.turns[1].continuation_bytes is None


def test_another_providers_continuation_ends_the_run_unreplayed() -> None:
    result, provider, _ = run_turns([continued_turn(calls_turn(SEARCH), "anthropic", THINKING)])
    assert result.outcome == MalformedOutput(reason=MalformedOutputReason.CONTINUATION_FOREIGN)
    assert len(provider.requests) == 1


def test_a_continuation_counts_toward_total_output() -> None:
    large = continued_turn(text_turn(), "fake", {**THINKING, "thinking": "t" * 2_000})
    result, _, _ = run_turns([large], limits=RunLimits(max_total_output_chars=1_000))
    assert result.outcome == LimitReached(limit=RunLimitKind.TOTAL_OUTPUT)


def test_transcripts_and_renderings_hold_no_continuation_content() -> None:
    result, provider, _ = answer_turns(
        lambda session: [
            continued_turn(calls_turn(SEARCH), "fake", THINKING),
            continued_turn(submit_turn("a", answer_proposal(session)), "fake", THINKING),
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
