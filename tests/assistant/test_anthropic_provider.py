"""The Anthropic adapter: built only from its configuration, mapping requests and responses.

Requests go to a mocked Messages API; nothing reaches the network.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import SecretStr

from archdiscov.assistant import anthropic_provider
from archdiscov.assistant.adapters import MAX_RETRY_WAIT_SECONDS, RETRY_BACKOFF_SECONDS
from archdiscov.assistant.anthropic_provider import DEFAULT_BASE_URL, SDK_ERRORS, AnthropicProvider
from archdiscov.assistant.config import (
    ProviderConfigError,
    ProviderConfigErrorCode,
    parse_provider_config,
)
from archdiscov.assistant.conversation import (
    AssistantMessage,
    Conversation,
    MalformedArgumentsReason,
    MalformedToolArguments,
    ParsedToolArguments,
    TokenUsage,
    ToolCall,
)
from archdiscov.assistant.orchestrator import run_assistant
from archdiscov.assistant.provider import (
    CompletionRequest,
    CompletionResponse,
    ProviderAuthenticationError,
    ProviderCapability,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderMalformedOutputError,
    ProviderRateLimitError,
    ProviderRefusalError,
    ProviderTimeoutError,
    ProviderTransportError,
    ProviderTruncatedOutputError,
    ProviderUnavailableError,
    RequestLimits,
    StopReason,
    complete,
)
from archdiscov.assistant.providers import load_provider
from archdiscov.assistant.tools.session import ToolSession
from tests.support.mock_api import MockApi, Waits
from tests.support.provider_data import ANTHROPIC, EXCHANGE, INSPECT_TOOL, SEARCH_TOOL, THINKING
from tests.support.scripted_turns import SNAPSHOT
from tests.support.secrets import SECRET, secret_renderings

ENDPOINT = "https://gateway.example.com/anthropic"
QUESTION = "Which databases are reachable?"
HOSTILE_ENVIRONMENT = {
    "ANTHROPIC_API_KEY": "sk-ant-api03-environmentKEY0123456789abcdef",
    "ANTHROPIC_AUTH_TOKEN": "environment-token",
    "ANTHROPIC_BASE_URL": "https://elsewhere.example.com",
    "ANTHROPIC_CUSTOM_HEADERS": "Authorization: Bearer injected\nX-Injected: 1",
    "ANTHROPIC_PROFILE": "someone-else",
    "HTTPS_PROXY": "http://proxy.invalid:3128",
    "ALL_PROXY": "http://proxy.invalid:3128",
}
SDK_HEADERS = {
    "accept",
    "accept-encoding",
    "anthropic-version",
    "authorization",
    "connection",
    "content-length",
    "content-type",
    "host",
    "user-agent",
}
TEXT = {"type": "text", "text": "None of them."}
LOOKUP = {"type": "tool_use", "id": "toolu_1", "name": "search_resources", "input": {"type": "x"}}
INSPECTION = {"type": "tool_use", "id": "toolu_2", "name": "inspect_resource", "input": {}}


def _body(
    *content: dict, stop: str = "end_turn", usage: dict | None = None, **fields: Any
) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": list(content),
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": usage or {"input_tokens": 3, "output_tokens": 1},
        **fields,
    }


def _api(*bodies: dict) -> MockApi:
    return MockApi([httpx2.Response(200, json=body) for body in bodies])


def _provider(
    api: MockApi,
    endpoint: str | None = ENDPOINT,
    *,
    max_retries: int = 0,
    waits: Waits | None = None,
) -> AnthropicProvider:
    data = {**ANTHROPIC, "max_retries": max_retries}
    if endpoint is not None:
        data["endpoint"] = endpoint
    waits = Waits() if waits is None else waits
    return AnthropicProvider(
        parse_provider_config(data),
        SecretStr(SECRET),
        transport=api,
        async_transport=api,
        sleep=waits.sleep,
        async_sleep=waits.sleep_async,
    )


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


def _send(provider: AnthropicProvider, mode: str = "sync", request=None) -> CompletionResponse:
    request = request or _request()
    if mode == "sync":
        return complete(provider, request)
    return asyncio.run(provider.complete_async(request))


def _sent(api: MockApi, index: int = 0) -> dict[str, Any]:
    return json.loads(api.requests[index].content)


def _config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "provider.toml"
    path.write_text(
        f'kind = "anthropic"\nmodel = "claude-sonnet-5-5"\napi_key_env = "ASK_KEY"\n{extra}',
        encoding="utf-8",
    )
    return path


# ---- construction ------------------------------------------------------------------


def test_a_configuration_builds_the_adapter_without_a_request(tmp_path: Path) -> None:
    config, provider = load_provider(
        _config(tmp_path, f'endpoint = "{ENDPOINT}"\n'), environ={"ASK_KEY": SECRET}
    )
    assert isinstance(provider, AnthropicProvider)
    assert (provider.model, provider.base_url) == (config.model, ENDPOINT)
    assert provider.capabilities.capabilities == {
        ProviderCapability.TOOL_CALLING,
        ProviderCapability.PARALLEL_TOOL_CALLS,
    }
    _, default = load_provider(_config(tmp_path), environ={"ASK_KEY": SECRET})
    assert default.base_url == DEFAULT_BASE_URL


@pytest.mark.parametrize(
    ("environ", "code"),
    [
        ({}, ProviderConfigErrorCode.CREDENTIAL_MISSING),
        ({"ASK_KEY": " "}, ProviderConfigErrorCode.CREDENTIAL_EMPTY),
    ],
    ids=["missing", "empty"],
)
def test_an_unusable_key_fails_before_the_adapter_exists(
    tmp_path: Path, monkeypatch, environ, code
) -> None:
    def built(*args, **kwargs):
        raise AssertionError("the adapter was built")

    monkeypatch.setattr(anthropic_provider, "AnthropicProvider", built)
    with pytest.raises(ProviderConfigError) as raised:
        load_provider(_config(tmp_path), environ=environ)
    assert raised.value.code == code and raised.value.variable == "ASK_KEY"


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("endpoint", [ENDPOINT, None], ids=["endpoint", "default"])
def test_only_the_configuration_chooses_the_target_and_credential(
    monkeypatch, mode: str, endpoint: str | None
) -> None:
    for name, value in HOSTILE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    api = _api(_body(TEXT))
    _send(_provider(api, endpoint), mode)
    [request] = api.requests
    assert str(request.url) == f"{endpoint or DEFAULT_BASE_URL}/v1/messages"
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    assert {name for name in request.headers if not name.startswith("x-stainless-")} == (
        SDK_HEADERS
    )
    body = request.content.decode()
    assert not any(value in body for value in HOSTILE_ENVIRONMENT.values())


def test_redirects_are_not_followed() -> None:
    elsewhere = httpx2.Response(307, headers={"location": "https://elsewhere.example.com/v1"})
    api = MockApi([elsewhere])
    with pytest.raises(ProviderInvalidRequestError):
        _send(_provider(api))
    assert [request.url.host for request in api.requests] == ["gateway.example.com"]


def test_no_key_or_request_body_reaches_a_rendering_or_log(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    for name in anthropic_provider._SDK_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
    api = MockApi([httpx2.Response(400, json={"type": "error", "error": {"type": "x"}})])
    provider = _provider(api)
    with pytest.raises(ProviderError) as raised:
        _send(provider)
    for name in anthropic_provider._SDK_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING
    for rendering in (*secret_renderings(provider), *secret_renderings(raised.value), caplog.text):
        assert SECRET not in rendering and QUESTION not in rendering


# ---- requests ----------------------------------------------------------------------


def test_a_request_maps_every_message_tool_and_limit() -> None:
    api = _api(_body(TEXT))
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
    _send(_provider(api), request=request)
    assert _sent(api) == {
        "model": "claude-sonnet-5-5",
        "max_tokens": 512,
        "system": [{"type": "text", "text": "Answer only from tool results."}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": QUESTION}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Searching."},
                    {
                        "type": "tool_use",
                        "id": "call_0",
                        "name": "search_resources",
                        "input": {"type": "rds_instance"},
                    },
                    # A malformed call was never run; it is replayed with no input.
                    {"type": "tool_use", "id": "call_1", "name": "inspect_resource", "input": {}},
                ],
            },
            {
                # Tool results and the prompt after them share one user turn.
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_0",
                        "content": "[]",
                        "is_error": False,
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_1",
                        "content": "Arguments were not JSON.",
                        "is_error": True,
                    },
                    {"type": "text", "text": "Submit your answer now."},
                ],
            },
        ],
        "tools": [
            {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
            for tool in (SEARCH_TOOL, INSPECT_TOOL)
        ],
        "tool_choice": {"type": "auto", "disable_parallel_tool_use": False},
    }


def test_one_call_per_turn_disables_parallel_tool_use_and_no_tools_sends_none() -> None:
    api = _api(_body(TEXT), _body(TEXT))
    provider = _provider(api)
    _send(provider, request=_request(max_tool_calls_per_turn=1))
    _send(provider, request=_request(tools=()))
    assert _sent(api, 0)["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert set(_sent(api, 1)) == {"model", "max_tokens", "messages"}


def test_no_key_or_sampling_parameter_is_in_the_body() -> None:
    api = _api(_body(TEXT))
    _send(_provider(api), request=_request(max_tool_calls_per_turn=4))
    body = api.requests[0].content.decode()
    assert SECRET not in body
    assert set(json.loads(body)) == {"model", "max_tokens", "messages", "tools", "tool_choice"}


# ---- responses ---------------------------------------------------------------------


def _call(block: dict, arguments=None) -> ToolCall:
    value = arguments if arguments is not None else ParsedToolArguments(value=block["input"])
    return ToolCall(id=block["id"], name=block["name"], arguments=value)


def _malformed(reason: MalformedArgumentsReason) -> MalformedToolArguments:
    return MalformedToolArguments(reason=reason)


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
            _body(
                {"type": "text", "text": "Let me "},
                {"type": "text", "text": "look."},
                LOOKUP,
                stop="tool_use",
            ),
            AssistantMessage(text="Let me look.", tool_calls=(_call(LOOKUP),)),
            StopReason.TOOL_USE,
        ),
        (
            _body({**LOOKUP, "input": "{not json"}, {**INSPECTION, "input": [1]}, stop="tool_use"),
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
        (_body(TEXT, stop="refusal"), AssistantMessage(text=TEXT["text"]), StopReason.REFUSAL),
    ],
    ids=[
        "text",
        "stop-sequence",
        "single-call",
        "parallel-calls",
        "text-and-calls",
        "malformed-arguments",
        "max-tokens-text",
        "context-window-text",
        "refusal-text",
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
        (_body(stop="max_tokens"), ProviderTruncatedOutputError),
        (_body(stop="refusal"), ProviderRefusalError),
        (
            _body(TEXT, {"type": "server_tool_use", "id": "s", "name": "web_search", "input": {}}),
            ProviderMalformedOutputError,
        ),
        (_body(TEXT, stop="improvised"), ProviderMalformedOutputError),
        (_body(LOOKUP, stop="end_turn"), ProviderMalformedOutputError),
        (
            _body({**LOOKUP, "name": "not a tool name"}, stop="tool_use"),
            ProviderMalformedOutputError,
        ),
    ],
    ids=[
        "max-tokens-empty",
        "refusal-empty",
        "server-tool-block",
        "unknown-stop",
        "no-calls-at-tool-use",
        "invalid-tool-name",
    ],
)
def test_unusable_responses_are_typed_errors_without_provider_text(body, error) -> None:
    with pytest.raises(error) as raised:
        _send(_provider(_api(body)))
    assert str(raised.value) == error.message
    assert "None of them" not in "".join(secret_renderings(raised.value))


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (
            {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 2,
            },
            TokenUsage(
                input_tokens=10,
                output_tokens=5,
                cache_read_input_tokens=3,
                cache_write_input_tokens=2,
            ),
        ),
        ({"input_tokens": 10, "output_tokens": 5}, TokenUsage(input_tokens=10, output_tokens=5)),
    ],
    ids=["with-cache", "without-cache"],
)
def test_usage_maps_cache_creation_to_cache_write(usage, expected) -> None:
    assert _send(_provider(_api(_body(TEXT, usage=usage)))).usage == expected


# ---- continuations -----------------------------------------------------------------


def test_reasoning_is_replayed_verbatim_and_in_order_on_the_next_request() -> None:
    first = _body(
        THINKING,
        {"type": "text", "text": "Searching."},
        {"type": "redacted_thinking", "data": "ZW5jcnlwdGVk"},
        {**LOOKUP, "input": {}},
        stop="tool_use",
    )
    api = _api(first, _body(TEXT))
    result = run_assistant(
        QUESTION,
        provider=_provider(api),
        session=ToolSession.from_paths(SNAPSHOT),
        provider_kind="anthropic",
        model="claude-sonnet-5-5",
    )
    assert result.outcome.kind == "answered"
    replayed = _sent(api, 1)["messages"][1]
    assert replayed == {"role": "assistant", "content": first["content"]}
    assert result.transcript.turns[0].continuation_bytes > 0
    assert THINKING["signature"] not in result.transcript.model_dump_json()


def test_a_response_without_reasoning_has_no_continuation() -> None:
    assert _send(_provider(_api(_body(TEXT)))).message.continuation is None


def test_another_providers_continuation_is_never_sent() -> None:
    api = _api(_body(TEXT))
    foreign = {
        "role": "assistant",
        "text": "Earlier.",
        "continuation": {"provider_kind": "fake", "blocks": [THINKING]},
    }
    with pytest.raises(ProviderInvalidRequestError):
        _send(_provider(api), request=_request(foreign, {"role": "user", "text": "Go on."}))
    assert api.requests == []


# ---- failures ----------------------------------------------------------------------


def _error(status: int, headers: dict | None = None) -> httpx2.Response:
    """An API error whose message holds the key, a tool result, and the question."""
    message = f"{SECRET} tool result: db-a reachable; question: {QUESTION}"
    body = {"type": "error", "error": {"type": "api_error", "message": message}}
    return httpx2.Response(status, json=body, headers=headers)


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, ProviderInvalidRequestError),
        (401, ProviderAuthenticationError),
        (429, ProviderRateLimitError),
        (500, ProviderUnavailableError),
        (529, ProviderRateLimitError),
    ],
)
def test_statuses_map_to_fixed_safe_errors(caplog, status: int, error) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ProviderError) as raised:
        _send(_provider(MockApi([_error(status)])))
    assert type(raised.value) is error
    assert raised.value.retryable is (error is ProviderRateLimitError)
    rendered = "".join(secret_renderings(raised.value)) + caplog.text
    for leaked in (SECRET, QUESTION, "db-a reachable"):
        assert leaked not in rendered


@pytest.mark.parametrize(
    ("served", "error"),
    [
        (lambda request: httpx2.ReadTimeout("slow", request=request), ProviderTimeoutError),
        (lambda request: httpx2.ConnectError("refused", request=request), ProviderTransportError),
        (
            lambda request: httpx2.Response(
                200, content=b"{not json", headers={"content-type": "application/json"}
            ),
            ProviderMalformedOutputError,
        ),
    ],
    ids=["timeout", "connection", "unparseable"],
)
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_transport_failures_map_to_typed_errors(served, error, mode: str) -> None:
    with pytest.raises(error):
        _send(_provider(MockApi([served])), mode)


def test_every_sdk_failure_class_has_a_provider_error() -> None:
    sdk = {
        cls
        for cls in vars(anthropic).values()
        if isinstance(cls, type) and issubclass(cls, anthropic.APIError)
    }
    for cls in sdk:
        assert any(base in SDK_ERRORS for base in cls.__mro__), cls


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("status", [429, 529])
def test_rate_limits_are_retried_with_exponential_backoff(mode: str, status: int) -> None:
    waits = Waits()
    api = MockApi([_error(status), _error(status), httpx2.Response(200, json=_body(TEXT))])
    response = _send(_provider(api, max_retries=2, waits=waits), mode)
    assert response.message.text == TEXT["text"]
    assert len(api.requests) == 3
    assert waits == [RETRY_BACKOFF_SECONDS, RETRY_BACKOFF_SECONDS * 2]


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_retries_stop_at_the_configured_count(mode: str) -> None:
    waits = Waits()
    api = MockApi([lambda request: httpx2.ConnectError("refused", request=request)] * 3)
    with pytest.raises(ProviderTransportError):
        _send(_provider(api, max_retries=2, waits=waits), mode)
    assert len(api.requests) == 3 and len(waits) == 2


@pytest.mark.parametrize(
    "served",
    [
        _error(503),
        _error(400),
        lambda request: httpx2.ReadTimeout("slow", request=request),
    ],
    ids=["unavailable", "invalid-request", "timeout"],
)
def test_other_failures_are_never_retried(served) -> None:
    waits = Waits()
    api = MockApi([served, httpx2.Response(200, json=_body(TEXT))])
    with pytest.raises(ProviderError):
        _send(_provider(api, max_retries=3, waits=waits))
    assert len(api.requests) == 1 and waits == []


@pytest.mark.parametrize(
    ("retry_after", "timeout", "wait"),
    [
        ("2", 60, 2.0),
        ("0", 60, 0.0),
        ("20", 10, 10.0),
        ("120", 60, MAX_RETRY_WAIT_SECONDS),
        ("soon", 60, RETRY_BACKOFF_SECONDS),
        ("-1", 60, RETRY_BACKOFF_SECONDS),
        ("nan", 60, RETRY_BACKOFF_SECONDS),
    ],
    ids=["honoured", "immediate", "request-timeout", "capped", "not-a-number", "negative", "nan"],
)
def test_retry_after_is_honoured_within_bounds(retry_after: str, timeout: int, wait) -> None:
    waits = Waits()
    api = MockApi(
        [_error(429, {"retry-after": retry_after}), httpx2.Response(200, json=_body(TEXT))]
    )
    _send(_provider(api, max_retries=1, waits=waits), request=_request(timeout_seconds=timeout))
    assert waits == [wait]
