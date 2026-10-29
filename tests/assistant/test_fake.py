import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from archdiscov.assistant.conversation import (
    AssistantMessage,
    Conversation,
    MalformedToolArguments,
    ParsedToolArguments,
    ToolCall,
    ToolDeclaration,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from archdiscov.assistant.fake import (
    MAX_TRANSCRIPT_BYTES,
    FakeClock,
    FakeProvider,
    FakeScriptError,
    FakeScriptErrorCode,
    FakeTranscript,
    load_fake_transcript,
)
from archdiscov.assistant.provider import (
    CompletionRequest,
    Provider,
    ProviderCapabilities,
    ProviderError,
    RequestLimits,
    StopReason,
    UnsupportedCapabilityError,
    complete,
)
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "assistant"
TOOLS = tuple(
    ToolDeclaration(name=name, description=f"{name}.", input_schema={"type": "object"})
    for name in ("search_resources", "inspect_resource")
)
QUESTION = UserMessage(text="Which databases are reachable?")
PARALLEL = RequestLimits(max_tool_calls_per_turn=4)


def _text(text: str = "Done.") -> dict:
    return {
        "kind": "respond",
        "response": {"message": {"role": "assistant", "text": text}, "stop_reason": "end"},
    }


def _tool_use(*call_ids: str) -> dict:
    calls = [
        {"id": cid, "name": "search_resources", "arguments": {"kind": "parsed", "value": {}}}
        for cid in call_ids
    ]
    return {
        "kind": "respond",
        "response": {
            "message": {"role": "assistant", "tool_calls": calls},
            "stop_reason": "tool_use",
        },
    }


def _request(*messages, tools=TOOLS, limits=PARALLEL) -> CompletionRequest:
    return CompletionRequest(
        conversation=Conversation(messages=(QUESTION, *messages)), tools=tools, limits=limits
    )


def _after_tools(*call_ids: str) -> tuple:
    calls = tuple(
        ToolCall(id=cid, name="inspect_resource", arguments=ParsedToolArguments(value={}))
        for cid in call_ids
    )
    return (
        AssistantMessage(tool_calls=calls),
        ToolResultMessage(results=tuple(ToolResult(call_id=cid, content="{}") for cid in call_ids)),
    )


MULTI_STEP = [
    _request(),
    _request(*_after_tools("call_search")),
    _request(*_after_tools("call_search"), *_after_tools("call_inspect_a", "call_inspect_b")),
]


def _replay_multi_step(provider: FakeProvider, *, use_async: bool) -> list:
    if use_async:

        async def run() -> list:
            return [await provider.complete_async(request) for request in MULTI_STEP]

        return asyncio.run(run())
    return [provider.complete(request) for request in MULTI_STEP]


def test_scripted_transcript_replays_identically_sync_and_async() -> None:
    transcript = load_fake_transcript(FIXTURES / "multi_step.json")
    runs = []
    for use_async in (False, False, True):
        clock = FakeClock()
        provider = FakeProvider(transcript, clock=clock)
        responses = _replay_multi_step(provider, use_async=use_async)
        runs.append((responses, provider.requests, clock.monotonic()))
        assert provider.remaining_turns == 0
    assert runs[0] == runs[1] == runs[2]

    responses, records, elapsed = runs[0]
    assert [response.stop_reason for response in responses] == [
        StopReason.TOOL_USE,
        StopReason.TOOL_USE,
        StopReason.END,
    ]
    parallel = responses[1].message.tool_calls
    assert [call.id for call in parallel] == ["call_inspect_a", "call_inspect_b"]
    assert isinstance(parallel[1].arguments, MalformedToolArguments)
    assert responses[2].usage.cache_read_input_tokens == 100
    # Every attempt is recorded with the injected clock's reading; delays advance it.
    assert [record.request for record in records] == MULTI_STEP
    assert [record.received_at for record in records] == [0.0, 0.0, 1.5]
    assert elapsed == 4.0


def test_failure_transcript_replays_each_outcome() -> None:
    clock = FakeClock()
    provider = FakeProvider(load_fake_transcript(FIXTURES / "failures.json"), clock=clock)
    outcomes = []
    for _ in range(8):
        try:
            response = provider.complete(_request(limits=RequestLimits(timeout_seconds=30)))
            outcomes.append(response.stop_reason.value)
        except ProviderError as error:
            outcomes.append(error.code.value)
    assert outcomes == [
        "provider_transport",
        "provider_rate_limit",
        "end",
        "length",
        "refusal",
        "provider_malformed_output",
        "provider_timeout",
        "provider_authentication",
    ]
    assert clock.monotonic() == 30.5


def test_exhausted_script_fails_clearly() -> None:
    provider = FakeProvider(FakeTranscript.model_validate({"turns": [_text()]}))
    provider.complete(_request())
    with pytest.raises(FakeScriptError) as raised:
        provider.complete(_request())
    assert raised.value.code == FakeScriptErrorCode.EXHAUSTED
    assert raised.value.turn == 1
    assert not isinstance(raised.value, ProviderError)


def test_expectations_are_checked_against_each_request() -> None:
    matching = {"tool_names": [], "last_message_role": "user"}
    provider = FakeProvider(
        FakeTranscript.model_validate({"turns": [{**_text(), "expect": matching}]})
    )
    assert provider.complete(_request(tools=(), limits=RequestLimits())).message.text == "Done."
    expect = {"tool_names": ["search_resources"]}
    provider = FakeProvider(
        FakeTranscript.model_validate({"turns": [{**_text(), "expect": expect}]})
    )
    with pytest.raises(FakeScriptError) as raised:
        provider.complete(_request())
    assert raised.value.code == FakeScriptErrorCode.EXPECTATION_MISMATCH
    assert raised.value.turn == 0


def test_scripted_turns_must_fit_request_limits() -> None:
    provider = FakeProvider(FakeTranscript.model_validate({"turns": [_tool_use("a", "b")]}))
    with pytest.raises(FakeScriptError) as raised:
        provider.complete(_request(limits=RequestLimits(max_tool_calls_per_turn=1)))
    assert raised.value.code == FakeScriptErrorCode.LIMIT_EXCEEDED


def test_capabilities_are_checked_before_consuming_a_turn() -> None:
    transcript = FakeTranscript.model_validate(
        {"capabilities": {"capabilities": []}, "turns": [_text()]}
    )
    provider = FakeProvider(transcript)
    assert isinstance(provider, Provider)
    assert provider.capabilities == ProviderCapabilities()
    with pytest.raises(UnsupportedCapabilityError):
        provider.complete(_request())
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(provider.complete_async(_request()))
    assert provider.requests == ()
    assert provider.remaining_turns == 1
    assert complete(provider, _request(tools=(), limits=RequestLimits())).message.text == "Done."


@pytest.mark.parametrize(
    "data,code",
    [
        (
            {"capabilities": {"capabilities": []}, "turns": [_tool_use("a")]},
            "fake_transcript_capability_mismatch",
        ),
        (
            {"turns": [{"kind": "fail", "error": "provider_unsupported_capability"}]},
            "fake_transcript_error_unscriptable",
        ),
    ],
)
def test_invalid_transcripts_are_rejected(data: dict, code: str) -> None:
    with pytest.raises(ValidationError) as raised:
        FakeTranscript.model_validate(data)
    assert code in {error["type"] for error in raised.value.errors()}


@pytest.mark.parametrize(
    "content,code",
    [
        (b"{not json", FakeScriptErrorCode.INVALID),
        (b" " * (MAX_TRANSCRIPT_BYTES + 1), FakeScriptErrorCode.TOO_LARGE),
    ],
    ids=["invalid", "too-large"],
)
def test_bad_transcript_files_fail_clearly(
    tmp_path: Path, content: bytes, code: FakeScriptErrorCode
) -> None:
    # A missing file is reached through the configuration, in `test_providers.py`.
    path = tmp_path / "transcript.json"
    path.write_bytes(content)
    with pytest.raises(FakeScriptError) as raised:
        load_fake_transcript(path)
    assert raised.value.code == code
    assert raised.value.path == path
