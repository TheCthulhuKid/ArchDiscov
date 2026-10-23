"""The Bedrock adapter's Converse mapping: requests, responses, and reasoning continuations.

Requests go to a mocked Converse API through botocore, which validates each one against the
service model; nothing reaches AWS. The mocked sessions read the harness's own AWS profile.
"""

import asyncio
import base64
import json
import logging
from typing import Any

import pytest
from botocore.exceptions import ReadTimeoutError
from botocore.stub import Stubber

from archdiscov.assistant.bedrock_provider import BedrockProvider
from archdiscov.assistant.config import parse_provider_config
from archdiscov.assistant.conversation import (
    AssistantMessage,
    Conversation,
    MalformedArgumentsReason,
    MalformedToolArguments,
    ParsedToolArguments,
    TokenUsage,
    ToolCall,
)
from archdiscov.assistant.fake import FakeClock, FakeRespond, load_fake_transcript
from archdiscov.assistant.orchestrator import run_assistant
from archdiscov.assistant.provider import (
    CompletionRequest,
    CompletionResponse,
    ProviderErrorCode,
    ProviderInvalidRequestError,
    ProviderMalformedOutputError,
    ProviderRefusalError,
    ProviderTruncatedOutputError,
    ProviderUnavailableError,
    RequestLimits,
    StopReason,
    complete,
)
from archdiscov.assistant.tools.session import ToolSession
from tests.assistant.provider_harness import HARNESS_PROFILE, BedrockHarness
from tests.assistant.test_anthropic_provider import EXCHANGE
from tests.assistant.test_bedrock_provider import ANSWER, MODEL, QUESTION, _fields
from tests.assistant.test_continuations import SIGNATURE, THINKING
from tests.assistant.test_orchestrator import SNAPSHOT
from tests.assistant.test_provider_contract import INSPECT, SEARCH, _fail
from tests.assistant.test_secret_leaks import _renderings
from tests.paths import FIXTURES_ROOT
from tests.support.mock_api import AwsResponse, BotocoreApi

TEXT = {"text": "None of them."}
LOOKUP = {"toolUse": {"toolUseId": "tooluse_1", "name": "search_resources", "input": {"type": "x"}}}
INSPECTION = {"toolUse": {"toolUseId": "tooluse_2", "name": "inspect_resource", "input": {}}}
REASONING = {"reasoningContent": {"reasoningText": {"text": "Thinking.", "signature": SIGNATURE}}}
REDACTED = {"reasoningContent": {"redactedContent": base64.b64encode(b"encrypted").decode()}}
_ANSWER = {"message": {"role": "assistant", "text": "None of them."}, "stop_reason": "end"}


def _body(*content: dict, stop: str = "end_turn", usage: dict | None = None) -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": list(content)}},
        "stopReason": stop,
        "usage": usage or {"inputTokens": 3, "outputTokens": 1, "totalTokens": 4},
        "metrics": {"latencyMs": 1},
    }


def _api(*bodies: dict) -> BotocoreApi:
    return BotocoreApi([AwsResponse(200, body) for body in bodies], profile=HARNESS_PROFILE)


def _provider(api: BotocoreApi) -> BedrockProvider:
    config = parse_provider_config(_fields(capabilities=["tool_calling", "parallel_tool_calls"]))
    return BedrockProvider(config, session_factory=api.session_factory)


def _request(*messages: dict, tools=(SEARCH, INSPECT), **limits: Any) -> CompletionRequest:
    return CompletionRequest(
        conversation=Conversation.model_validate(
            {"messages": [{"role": "user", "text": QUESTION}, *messages]}
        ),
        tools=tools,
        limits=RequestLimits(**limits),
    )


def _send(provider: BedrockProvider, mode: str = "sync", request=None) -> CompletionResponse:
    request = request or _request()
    if mode == "sync":
        return complete(provider, request)
    return asyncio.run(provider.complete_async(request))


def _sent(api: BotocoreApi, index: int = 0) -> dict[str, Any]:
    return json.loads(api.requests[index].body)


# ---- requests ----------------------------------------------------------------------


def _tool_spec(tool) -> dict[str, Any]:
    return {
        "toolSpec": {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": {"json": tool.input_schema},
        }
    }


def test_a_request_maps_every_message_tool_and_limit() -> None:
    provider = _provider(_api())
    request = CompletionRequest(
        conversation=Conversation.model_validate(
            {
                "messages": [
                    {"role": "system", "text": "Answer only from tool results."},
                    {"role": "user", "text": QUESTION},
                    *EXCHANGE,
                ]
            }
        ),
        tools=(SEARCH, INSPECT),
        limits=RequestLimits(max_output_tokens=512, max_tool_calls_per_turn=4),
    )
    expected = {
        "modelId": MODEL,
        "system": [{"text": "Answer only from tool results."}],
        "messages": [
            {"role": "user", "content": [{"text": QUESTION}]},
            {
                "role": "assistant",
                "content": [
                    {"text": "Searching."},
                    {
                        "toolUse": {
                            "toolUseId": "call_0",
                            "name": "search_resources",
                            "input": {"type": "rds_instance"},
                        }
                    },
                    # A malformed call was never run; it is replayed with no input.
                    {"toolUse": {"toolUseId": "call_1", "name": "inspect_resource", "input": {}}},
                ],
            },
            {
                # Tool results and the prompt after them share one user turn, and only an
                # error sets a status, since some models reject one.
                "role": "user",
                "content": [
                    {"toolResult": {"toolUseId": "call_0", "content": [{"text": "[]"}]}},
                    {
                        "toolResult": {
                            "toolUseId": "call_1",
                            "content": [{"text": "Arguments were not JSON."}],
                            "status": "error",
                        }
                    },
                    {"text": "Submit your answer now."},
                ],
            },
        ],
        "inferenceConfig": {"maxTokens": 512},
        "toolConfig": {"tools": [_tool_spec(SEARCH), _tool_spec(INSPECT)]},
    }
    # The stubber checks the request against both this and the service model.
    with Stubber(provider._client) as stubber:
        stubber.add_response("converse", ANSWER, expected)
        complete(provider, request)
        stubber.assert_no_pending_responses()


def test_no_tools_sends_no_tool_configuration_and_nothing_else_is_sent() -> None:
    api = _api(_body(TEXT), _body(TEXT))
    provider = _provider(api)
    _send(provider, request=_request(max_tool_calls_per_turn=4))
    _send(provider, request=_request(tools=()))
    # The model ID is in the path; no choice, sampling, guardrail, or model field is sent.
    assert set(_sent(api, 0)) == {"messages", "inferenceConfig", "toolConfig"}
    assert set(_sent(api, 0)["toolConfig"]) == {"tools"}
    assert set(_sent(api, 1)) == {"messages", "inferenceConfig"}


# ---- responses ---------------------------------------------------------------------


def _call(block: dict, arguments=None) -> ToolCall:
    use = block["toolUse"]
    value = arguments if arguments is not None else ParsedToolArguments(value=use["input"])
    return ToolCall(id=use["toolUseId"], name=use["name"], arguments=value)


def _malformed(reason: MalformedArgumentsReason) -> MalformedToolArguments:
    return MalformedToolArguments(reason=reason)


def _with_input(block: dict, value: Any) -> dict:
    return {"toolUse": {**block["toolUse"], "input": value}}


@pytest.mark.parametrize(
    ("body", "message", "stop"),
    [
        (_body(TEXT), AssistantMessage(text=TEXT["text"]), StopReason.END),
        (_body(TEXT, stop="stop_sequence"), AssistantMessage(text=TEXT["text"]), StopReason.END),
        (
            _body(LOOKUP, stop="tool_use"),
            AssistantMessage(tool_calls=(_call(LOOKUP),)),
            StopReason.TOOL_USE,
        ),
        (
            _body(LOOKUP, INSPECTION, stop="tool_use"),
            AssistantMessage(tool_calls=(_call(LOOKUP), _call(INSPECTION))),
            StopReason.TOOL_USE,
        ),
        (
            _body({"text": "Let me "}, {"text": "look."}, LOOKUP, stop="tool_use"),
            AssistantMessage(text="Let me look.", tool_calls=(_call(LOOKUP),)),
            StopReason.TOOL_USE,
        ),
        (
            _body(_with_input(LOOKUP, "{not json"), _with_input(INSPECTION, [1]), stop="tool_use"),
            AssistantMessage(
                tool_calls=(
                    _call(LOOKUP, _malformed(MalformedArgumentsReason.INVALID_JSON)),
                    _call(INSPECTION, _malformed(MalformedArgumentsReason.NOT_OBJECT)),
                )
            ),
            StopReason.TOOL_USE,
        ),
        (_body(TEXT, stop="max_tokens"), AssistantMessage(text=TEXT["text"]), StopReason.LENGTH),
        (
            _body(TEXT, stop="model_context_window_exceeded"),
            AssistantMessage(text=TEXT["text"]),
            StopReason.LENGTH,
        ),
        (
            _body(TEXT, stop="guardrail_intervened"),
            AssistantMessage(text=TEXT["text"]),
            StopReason.REFUSAL,
        ),
        (
            _body(TEXT, stop="content_filtered"),
            AssistantMessage(text=TEXT["text"]),
            StopReason.REFUSAL,
        ),
    ],
    ids=[
        "text",
        "stop-sequence",
        "single-call",
        "parallel-calls",
        "text-and-calls",
        "malformed-inputs",
        "max-tokens-text",
        "context-window-text",
        "guardrail-text",
        "content-filtered-text",
    ],
)
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_responses_map_to_the_shared_contracts(body, message, stop, mode: str) -> None:
    response = _send(_provider(_api(body)), mode)
    assert response == CompletionResponse(
        message=message, stop_reason=stop, usage=TokenUsage(input_tokens=3, output_tokens=1)
    )


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (_body(stop="max_tokens"), ProviderTruncatedOutputError),
        (
            _body(TEXT, _with_input(LOOKUP, {"type": "rd"}), stop="max_tokens"),
            ProviderTruncatedOutputError,
        ),
        (_body(stop="guardrail_intervened"), ProviderRefusalError),
        (_body(TEXT, LOOKUP, stop="guardrail_intervened"), ProviderRefusalError),
        (_body(stop="content_filtered"), ProviderRefusalError),
        (_body(TEXT, LOOKUP, stop="content_filtered"), ProviderRefusalError),
        (_body(TEXT, stop="malformed_model_output"), ProviderMalformedOutputError),
        (_body(LOOKUP, stop="malformed_tool_use"), ProviderMalformedOutputError),
        (_body(TEXT, stop="improvised"), ProviderMalformedOutputError),
        (_body(TEXT, {"hologram": {}}), ProviderMalformedOutputError),
        (_body(TEXT, {"reasoningContent": {"hologram": {}}}), ProviderMalformedOutputError),
        (
            _body(TEXT, {"image": {"format": "png", "source": {"bytes": "aW1n"}}}),
            ProviderMalformedOutputError,
        ),
        (
            _body({"citationsContent": {"content": [TEXT], "citations": []}}),
            ProviderMalformedOutputError,
        ),
        (
            _body({"guardContent": {"text": {"text": "x"}}}, TEXT),
            ProviderMalformedOutputError,
        ),
        (
            _body({"toolUse": {**LOOKUP["toolUse"], "type": "server_tool_use"}}, stop="tool_use"),
            ProviderMalformedOutputError,
        ),
        (
            _body(TEXT, {"toolResult": {"toolUseId": "t", "content": [TEXT]}}),
            ProviderMalformedOutputError,
        ),
        (_body(TEXT, LOOKUP), ProviderMalformedOutputError),
        (_body(LOOKUP, stop="end_turn"), ProviderMalformedOutputError),
        (_body(TEXT, stop="tool_use"), ProviderMalformedOutputError),
        (_body(stop="end_turn"), ProviderMalformedOutputError),
        (_body({"text": 5}), ProviderMalformedOutputError),
        (_body({**TEXT, **LOOKUP}), ProviderMalformedOutputError),
        (
            _body({"toolUse": {**LOOKUP["toolUse"], "name": "not a tool name"}}, stop="tool_use"),
            ProviderMalformedOutputError,
        ),
        (_body(TEXT, usage={"inputTokens": -1, "outputTokens": 1}), ProviderMalformedOutputError),
        ({**_body(TEXT), "output": {"hologram": {}}}, ProviderMalformedOutputError),
        ({"output": _body(TEXT)["output"]}, ProviderMalformedOutputError),
    ],
    ids=[
        "max-tokens-empty",
        "max-tokens-partial-call",
        "guardrail-empty",
        "guardrail-with-call",
        "content-filtered-empty",
        "content-filtered-with-call",
        "malformed-model-output",
        "malformed-tool-use",
        "unknown-stop",
        "unknown-block",
        "unknown-reasoning",
        "image-block",
        "citations-block",
        "guard-content-block",
        "server-tool-use",
        "tool-result-block",
        "calls-at-end-turn",
        "no-calls-at-tool-use",
        "text-at-tool-use",
        "empty",
        "text-not-a-string",
        "two-member-block",
        "invalid-tool-name",
        "negative-usage",
        "unknown-output",
        "no-stop-reason",
    ],
)
def test_unusable_responses_are_typed_errors_without_provider_text(body, error) -> None:
    with pytest.raises(error) as raised:
        _send(_provider(_api(body)))
    assert str(raised.value) == error.message
    assert "None of them" not in "".join(_renderings(raised.value))


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (
            {
                "inputTokens": 10,
                "outputTokens": 5,
                "totalTokens": 20,
                "cacheReadInputTokens": 3,
                "cacheWriteInputTokens": 2,
            },
            TokenUsage(
                input_tokens=10,
                output_tokens=5,
                cache_read_input_tokens=3,
                cache_write_input_tokens=2,
            ),
        ),
        (
            {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
            TokenUsage(input_tokens=10, output_tokens=5),
        ),
    ],
    ids=["with-cache", "without-cache"],
)
def test_usage_maps_with_and_without_cache_fields(usage, expected) -> None:
    assert _send(_provider(_api(_body(TEXT, usage=usage)))).usage == expected


# ---- continuations -----------------------------------------------------------------


def test_reasoning_is_replayed_verbatim_and_in_order_on_the_next_request() -> None:
    first = _body(
        REASONING, {"text": "Searching."}, REDACTED, _with_input(LOOKUP, {}), stop="tool_use"
    )
    api = _api(first, _body(TEXT))
    result = run_assistant(
        QUESTION,
        provider=_provider(api),
        session=ToolSession.from_paths(SNAPSHOT),
        provider_kind="bedrock",
        model=MODEL,
    )
    assert result.outcome.kind == "answered"
    replayed = _sent(api, 1)["messages"][1]
    assert replayed == {"role": "assistant", "content": first["output"]["message"]["content"]}
    assert result.transcript.turns[0].continuation_bytes > 0
    assert SIGNATURE not in result.transcript.model_dump_json()


def test_a_response_without_reasoning_has_no_continuation() -> None:
    assert _send(_provider(_api(_body(TEXT, LOOKUP, stop="tool_use")))).message.continuation is None


def test_another_providers_continuation_is_never_sent() -> None:
    api = _api(_body(TEXT))
    foreign = {
        "role": "assistant",
        "text": "Earlier.",
        "continuation": {"provider_kind": "anthropic", "blocks": [THINKING]},
    }
    with pytest.raises(ProviderInvalidRequestError):
        _send(_provider(api), request=_request(foreign, {"role": "user", "text": "Go on."}))
    assert api.requests == []


# ---- harness -----------------------------------------------------------------------


def test_the_harness_serves_fake_turns_that_map_back_unchanged() -> None:
    transcript = load_fake_transcript(FIXTURES_ROOT / "assistant" / "multi_step.json")
    turns = [turn.model_dump(mode="json") for turn in transcript.turns]
    clock = FakeClock()
    harness = BedrockHarness()
    provider = harness.build(turns, clock=clock)
    for turn in transcript.turns:
        assert isinstance(turn, FakeRespond)
        assert _send(provider) == turn.response
    assert clock.monotonic() == sum(turn.delay_seconds for turn in transcript.turns)
    assert harness.attempts(provider) == len(turns)


def test_a_delay_beyond_the_clients_read_timeout_is_a_read_timeout() -> None:
    respond = {"kind": "respond", "delay_seconds": 90, "response": _ANSWER}
    clock = FakeClock()
    provider = BedrockHarness().build([respond], clock=clock)
    with pytest.raises(ProviderUnavailableError) as raised:
        _send(provider)
    assert isinstance(raised.value.__cause__, ReadTimeoutError)
    assert clock.monotonic() == 60


# ---- failures until step 42.3 ------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "error"),
    [
        (ProviderErrorCode.AUTHENTICATION, ProviderUnavailableError),
        (ProviderErrorCode.RATE_LIMIT, ProviderUnavailableError),
        (ProviderErrorCode.TIMEOUT, ProviderUnavailableError),
        (ProviderErrorCode.TRANSPORT, ProviderUnavailableError),
        (ProviderErrorCode.UNAVAILABLE, ProviderUnavailableError),
        (ProviderErrorCode.MALFORMED_OUTPUT, ProviderMalformedOutputError),
    ],
)
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_failures_are_unavailable_until_mapped_and_never_retried(caplog, code, error, mode) -> None:
    caplog.set_level(logging.DEBUG)
    harness = BedrockHarness()
    provider = harness.build([_fail(code), {"kind": "respond", "response": _ANSWER}], max_retries=2)
    with pytest.raises(error) as raised:
        _send(provider, mode)
    assert str(raised.value) == error.message
    assert harness.attempts(provider) == 1
    rendered = "".join(_renderings(raised.value)) + caplog.text
    assert QUESTION not in rendered
