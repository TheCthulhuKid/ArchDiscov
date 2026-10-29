"""The Bedrock adapter's Converse mapping: requests, responses, continuations, and failures.

Requests go to a mocked Converse API through botocore, which validates each one against the
service model; nothing reaches AWS. The mocked sessions read the harness's own AWS profile.
"""

import asyncio
import base64
import json
import logging
from typing import Any

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from botocore.stub import Stubber

from archdiscov.assistant.bedrock_provider import BedrockProvider
from archdiscov.assistant.config import parse_provider_config
from archdiscov.assistant.conversation import (
    AssistantMessage,
    Conversation,
    ParsedToolArguments,
    TokenUsage,
    ToolCall,
)
from archdiscov.assistant.fake import FakeClock
from archdiscov.assistant.orchestrator import run_assistant
from archdiscov.assistant.provider import (
    CompletionRequest,
    CompletionResponse,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderMalformedOutputError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    RequestLimits,
    StopReason,
    complete,
)
from archdiscov.assistant.tools.session import ToolSession
from tests.support.bedrock_exchanges import (
    ANSWER,
    MODEL,
    QUESTION,
    ROLE,
    ROLE_KEY,
    assumed_role_response,
    bedrock_fields,
    signing_key,
)
from tests.support.mock_api import AwsResponse, BotocoreApi
from tests.support.provider_data import EXCHANGE, INSPECT_TOOL, SEARCH_TOOL, SIGNATURE, THINKING
from tests.support.provider_harness import HARNESS_PROFILE
from tests.support.scripted_turns import SNAPSHOT
from tests.support.secrets import secret_renderings

HARNESS_KEY = "AKIAHARNESSKEY000000"

TEXT = {"text": "None of them."}
LOOKUP = {"toolUse": {"toolUseId": "tooluse_1", "name": "search_resources", "input": {"type": "x"}}}
INSPECTION = {"toolUse": {"toolUseId": "tooluse_2", "name": "inspect_resource", "input": {}}}
REASONING = {"reasoningContent": {"reasoningText": {"text": "Thinking.", "signature": SIGNATURE}}}
REDACTED = {"reasoningContent": {"redactedContent": base64.b64encode(b"encrypted").decode()}}


def _body(*content: dict, stop: str = "end_turn", usage: dict | None = None) -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": list(content)}},
        "stopReason": stop,
        "usage": usage or {"inputTokens": 3, "outputTokens": 1, "totalTokens": 4},
        "metrics": {"latencyMs": 1},
    }


def _api(*bodies: dict) -> BotocoreApi:
    return BotocoreApi([AwsResponse(200, body) for body in bodies], profile=HARNESS_PROFILE)


def _provider(api: BotocoreApi, **fields: Any) -> BedrockProvider:
    """The adapter over `api`, without retries."""
    capabilities = ["tool_calling", "parallel_tool_calls"]
    config = parse_provider_config(
        bedrock_fields(capabilities=capabilities, max_retries=0, **fields)
    )
    return BedrockProvider(config, session_factory=api.session_factory)


def _request(
    *messages: dict, tools=(SEARCH_TOOL, INSPECT_TOOL), **limits: Any
) -> CompletionRequest:
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
        tools=(SEARCH_TOOL, INSPECT_TOOL),
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
        "toolConfig": {"tools": [_tool_spec(SEARCH_TOOL), _tool_spec(INSPECT_TOOL)]},
    }
    # The stubber checks the request against both this and the service model.
    with Stubber(provider._client(request.limits.timeout_seconds)) as stubber:
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


def _with_input(block: dict, value: Any) -> dict:
    return {"toolUse": {**block["toolUse"], "input": value}}


@pytest.mark.parametrize(
    ("body", "message", "stop"),
    [
        (_body(TEXT, stop="stop_sequence"), AssistantMessage(text=TEXT["text"]), StopReason.END),
        (
            _body({"text": "Let me "}, {"text": "look."}, LOOKUP, stop="tool_use"),
            AssistantMessage(text="Let me look.", tool_calls=(_call(LOOKUP),)),
            StopReason.TOOL_USE,
        ),
        (
            _body(TEXT, stop="model_context_window_exceeded"),
            AssistantMessage(text=TEXT["text"]),
            StopReason.LENGTH,
        ),
        (
            _body(TEXT, stop="content_filtered"),
            AssistantMessage(text=TEXT["text"]),
            StopReason.REFUSAL,
        ),
    ],
    ids=[
        "stop-sequence",
        "text-and-calls",
        "context-window-text",
        "content-filtered-text",
    ],
)
def test_responses_map_to_the_shared_contracts(body, message, stop) -> None:
    response = _send(_provider(_api(body)), "async")
    assert response == CompletionResponse(
        message=message, stop_reason=stop, usage=TokenUsage(input_tokens=3, output_tokens=1)
    )


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (_body(TEXT, stop="malformed_model_output"), ProviderMalformedOutputError),
        (_body(TEXT, {"hologram": {}}), ProviderMalformedOutputError),
        (_body(TEXT, stop="tool_use"), ProviderMalformedOutputError),
        (_body({"text": 5}), ProviderMalformedOutputError),
    ],
    ids=[
        "malformed-model-output",
        "unknown-block",
        "text-at-tool-use",
        "text-not-a-string",
    ],
)
def test_unusable_responses_are_typed_errors_without_provider_text(body, error) -> None:
    with pytest.raises(error) as raised:
        _send(_provider(_api(body)))
    assert str(raised.value) == error.message
    assert "None of them" not in "".join(secret_renderings(raised.value))


def test_usage_maps_with_and_without_cache_fields() -> None:
    usage = {
        "inputTokens": 10,
        "outputTokens": 5,
        "totalTokens": 20,
        "cacheReadInputTokens": 3,
        "cacheWriteInputTokens": 2,
    }
    expected = TokenUsage(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=3,
        cache_write_input_tokens=2,
    )
    assert _send(_provider(_api(_body(TEXT, usage=usage)))).usage == expected


# ---- continuations -----------------------------------------------------------------


def test_only_reasoning_makes_a_continuation_replayed_verbatim_and_in_order() -> None:
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
    # A response without reasoning has no continuation.
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


# ---- failures ----------------------------------------------------------------------

REQUEST_ID = "bedrock-request-id-0001"


def _api_error(code: str | None, status: int = 400, headers: dict | None = None) -> AwsResponse:
    """An API error whose message holds the question and an ARN, and a request ID."""
    sent = {"x-amzn-RequestId": REQUEST_ID, **(headers or {})}
    if code is not None:
        sent["x-amzn-ErrorType"] = code
    return AwsResponse(status, {"message": f"{QUESTION} on {ROLE}"}, sent)


def _failing(*served, clock: FakeClock | None = None) -> tuple[BedrockProvider, BotocoreApi]:
    """The adapter over an API serving `served`, and the API."""
    api = BotocoreApi(list(served), clock, HARNESS_PROFILE)
    return _provider(api), api


# The codes beyond those the contract's harness serves: one failure class, mapped by its code.
@pytest.mark.parametrize(
    ("code", "error"),
    [
        ("ServiceQuotaExceededException", ProviderRateLimitError),
        ("ModelTimeoutException", ProviderTimeoutError),
        ("SomethingNewException", ProviderUnavailableError),
    ],
    ids=str,
)
def test_api_errors_map_by_code_to_fixed_safe_errors(caplog, code, error) -> None:
    caplog.set_level(logging.DEBUG)
    provider, api = _failing(_api_error(code))
    with pytest.raises(ProviderError) as raised:
        _send(provider)
    assert type(raised.value) is error
    assert raised.value.retryable is (error is ProviderRateLimitError)
    assert isinstance(raised.value.__cause__, ClientError)
    rendered = "".join(secret_renderings(raised.value)) + caplog.text
    for leaked in (QUESTION, ROLE, REQUEST_ID):
        assert leaked not in rendered
    assert len(api.requests) == 1


def test_a_gateway_page_is_an_unavailable_service() -> None:
    provider, _ = _failing(AwsResponse(502, b"<html>Bad gateway</html>"))
    with pytest.raises(ProviderUnavailableError):
        _send(provider)


# ---- timeouts ----------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_each_request_is_read_with_its_own_timeout(mode: str) -> None:
    clock = FakeClock()
    slow = AwsResponse(200, _body(TEXT), delay=15)
    provider, _ = _failing(slow, slow, slow, slow, clock=clock)
    with pytest.raises(ProviderTimeoutError) as raised:
        _send(provider, mode, _request(timeout_seconds=10))
    assert isinstance(raised.value.__cause__, ReadTimeoutError)
    assert clock.monotonic() == 10
    for timeout in (60, 20, 30):
        assert _send(provider, mode, _request(timeout_seconds=timeout)).message.text
    assert clock.monotonic() == 10 + 15 * 3
    # The configured timeout's client is kept, and only the latest other one.
    assert set(provider._clients) == {60, 30}


def test_clients_for_other_timeouts_share_the_assumed_role() -> None:
    answer = AwsResponse(200, _body(TEXT))
    api = BotocoreApi([assumed_role_response(), answer, answer], profile=HARNESS_PROFILE)
    provider = _provider(api, role_arn=ROLE)
    for timeout in (60, 20):
        _send(provider, request=_request(timeout_seconds=timeout))
    assert [signing_key(request) for request in api.requests] == [HARNESS_KEY, ROLE_KEY, ROLE_KEY]
