"""The OpenAI-compatible adapter: built only from its configuration, mapping each protocol.

Requests go to a mocked API; nothing reaches the network.
"""

import asyncio
import json
import logging
from functools import partial
from pathlib import Path
from typing import Any

import httpx2
import openai
import pytest

from archdiscov.assistant import openai_provider
from archdiscov.assistant.adapters import MAX_RETRY_WAIT_SECONDS, RETRY_BACKOFF_SECONDS
from archdiscov.assistant.config import (
    OpenAIProtocol,
    ProviderConfigError,
    ProviderConfigErrorCode,
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
from archdiscov.assistant.openai_provider import SDK_ERRORS, OpenAIProvider
from archdiscov.assistant.provider import (
    CompletionRequest,
    CompletionResponse,
    ProviderAuthenticationError,
    ProviderCapability,
    ProviderError,
    ProviderErrorCode,
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
    UnsupportedCapabilityError,
    complete,
)
from archdiscov.assistant.providers import load_provider
from archdiscov.services.ask import ask_question
from tests.assistant.provider_harness import ChatCompletionsHarness, ResponsesHarness
from tests.support.mock_api import MockApi, Waits, unparseable_body
from tests.support.openai_exchanges import (
    ENDPOINT,
    PARALLEL,
    QUESTION,
    openai_adapter,
    openai_request,
    responding_api,
    send_request,
    sent_body,
)
from tests.support.provider_data import EXCHANGE, INSPECT_TOOL, SEARCH_TOOL, THINKING
from tests.support.scripted_turns import SNAPSHOT
from tests.support.secrets import SECRET, secret_renderings

PROTOCOLS = list(OpenAIProtocol)
HOSTILE_ENVIRONMENT = {
    "OPENAI_API_KEY": "sk-proj-environmentKEY0123456789abcdef",
    "OPENAI_ADMIN_KEY": "sk-admin-environmentKEY0123456789abcdef",
    "OPENAI_BASE_URL": "https://elsewhere.example.com/v1",
    "OPENAI_ORG_ID": "org-someone-else",
    "OPENAI_PROJECT_ID": "proj-someone-else",
    "OPENAI_CUSTOM_HEADERS": "Authorization: Bearer injected\nX-Injected: 1",
    "OPENAI_LOG": "debug",
    "HTTPS_PROXY": "http://proxy.invalid:3128",
    "ALL_PROXY": "http://proxy.invalid:3128",
}
SDK_HEADERS = {
    "accept",
    "accept-encoding",
    "authorization",
    "connection",
    "content-length",
    "content-type",
    "host",
    "user-agent",
}
# The smallest request each protocol accepts; the mappings (steps 41.3 and 41.4) build more.
PARAMS: dict[OpenAIProtocol, dict[str, Any]] = {
    OpenAIProtocol.CHAT_COMPLETIONS: {
        "model": "gpt-5",
        "messages": [{"role": "user", "content": QUESTION}],
    },
    OpenAIProtocol.RESPONSES: {"model": "gpt-5", "input": QUESTION},
}
PATHS = {
    OpenAIProtocol.CHAT_COMPLETIONS: "/chat/completions",
    OpenAIProtocol.RESPONSES: "/responses",
}


def _rejected() -> httpx2.Response:
    return httpx2.Response(400, json={"error": {"message": "Rejected.", "type": "invalid"}})


def _create(provider: OpenAIProvider, mode: str = "sync") -> None:
    """Send the protocol's smallest request; the mocked API refuses it."""
    params = PARAMS[provider.protocol]
    with pytest.raises(openai.BadRequestError):
        if mode == "sync":
            provider._create(**params)
        else:
            asyncio.run(provider._create_async(**params))


def _config(tmp_path: Path, protocol: OpenAIProtocol, extra: str = "") -> Path:
    path = tmp_path / "provider.toml"
    path.write_text(
        f'kind = "openai_compatible"\nmodel = "gpt-5"\napi_key_env = "ASK_KEY"\n'
        f'endpoint = "{ENDPOINT}"\nprotocol = "{protocol.value}"\n{extra}',
        encoding="utf-8",
    )
    return path


# ---- construction ------------------------------------------------------------------


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=str)
def test_a_configuration_builds_the_adapter_without_a_request(
    tmp_path: Path, protocol: OpenAIProtocol
) -> None:
    config, provider = load_provider(_config(tmp_path, protocol), environ={"ASK_KEY": SECRET})
    assert isinstance(provider, OpenAIProvider)
    assert (provider.model, provider.base_url, provider.protocol) == (
        config.model,
        ENDPOINT,
        protocol,
    )
    assert provider.capabilities.capabilities == {ProviderCapability.TOOL_CALLING}


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("[]", set()),
        ('["tool_calling"]', {ProviderCapability.TOOL_CALLING}),
        (
            '["tool_calling", "parallel_tool_calls"]',
            {ProviderCapability.TOOL_CALLING, ProviderCapability.PARALLEL_TOOL_CALLS},
        ),
    ],
    ids=["none", "tools", "parallel"],
)
def test_the_adapter_declares_the_configured_capabilities(
    tmp_path: Path, declared: str, expected: set
) -> None:
    path = _config(tmp_path, OpenAIProtocol.RESPONSES, f"capabilities = {declared}\n")
    _, provider = load_provider(path, environ={"ASK_KEY": SECRET})
    assert provider.capabilities.capabilities == expected


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

    monkeypatch.setattr(openai_provider, "OpenAIProvider", built)
    with pytest.raises(ProviderConfigError) as raised:
        load_provider(_config(tmp_path, OpenAIProtocol.RESPONSES), environ=environ)
    assert raised.value.code == code and raised.value.variable == "ASK_KEY"


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("protocol", PROTOCOLS, ids=str)
def test_only_the_configuration_chooses_the_target_and_credential(
    monkeypatch, protocol: OpenAIProtocol, mode: str
) -> None:
    for name, value in HOSTILE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    api = MockApi([_rejected()])
    _create(openai_adapter(api, protocol), mode)
    [request] = api.requests
    assert str(request.url) == f"{ENDPOINT}{PATHS[protocol]}"
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    assert {name for name in request.headers if not name.startswith("x-stainless-")} == (
        SDK_HEADERS
    )
    body = request.content.decode()
    assert not any(value in body for value in HOSTILE_ENVIRONMENT.values())


def test_redirects_are_not_followed() -> None:
    elsewhere = httpx2.Response(307, headers={"location": "https://elsewhere.example.com/v1"})
    api = MockApi([elsewhere])
    with pytest.raises(openai.APIStatusError):
        openai_adapter(api)._create(**PARAMS[OpenAIProtocol.RESPONSES])
    assert [request.url.host for request in api.requests] == ["gateway.example.com"]


def test_no_key_or_request_body_reaches_a_rendering_or_log(monkeypatch, caplog) -> None:
    monkeypatch.setenv("OPENAI_LOG", "debug")
    caplog.set_level(logging.DEBUG)
    for name in openai_provider._SDK_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
    provider = openai_adapter(MockApi([_rejected()]))
    _create(provider)
    for name in openai_provider._SDK_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING
    for rendering in (*secret_renderings(provider), caplog.text):
        assert SECRET not in rendering and QUESTION not in rendering


# ---- capabilities ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "limits", "missing"),
    [
        ([], {}, ProviderCapability.TOOL_CALLING),
        (["tool_calling"], {"max_tool_calls_per_turn": 2}, ProviderCapability.PARALLEL_TOOL_CALLS),
    ],
    ids=["no-tools", "no-parallel-calls"],
)
def test_an_undeclared_capability_is_refused_before_any_request(
    declared: list[str], limits: dict, missing: ProviderCapability
) -> None:
    api = MockApi([])
    with pytest.raises(UnsupportedCapabilityError) as raised:
        complete(openai_adapter(api, capabilities=declared), openai_request(**limits))
    assert raised.value.capability is missing
    assert api.requests == []


def test_an_ask_run_without_declared_tool_calling_fails_before_any_request(
    tmp_path: Path, monkeypatch
) -> None:
    api = MockApi([])
    monkeypatch.setenv("ASK_KEY", SECRET)
    monkeypatch.setattr(
        openai_provider,
        "OpenAIProvider",
        partial(OpenAIProvider, transport=api, async_transport=api),
    )
    result = ask_question(
        QUESTION,
        snapshot_path=SNAPSHOT,
        config_path=_config(tmp_path, OpenAIProtocol.CHAT_COMPLETIONS, "capabilities = []\n"),
    )
    outcome = result.answer.outcome
    assert outcome.provider_error == ProviderErrorCode.UNSUPPORTED_CAPABILITY
    assert outcome.exit_code == 6
    assert api.requests == []


# ---- Chat Completions requests ---------------------------------------------------

CHAT = OpenAIProtocol.CHAT_COMPLETIONS
TEXT = {"role": "assistant", "content": "None of them."}
LOOKUP = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "search_resources", "arguments": '{"type":"x"}'},
}
INSPECTION = {
    "id": "call_2",
    "type": "function",
    "function": {"name": "inspect_resource", "arguments": "{}"},
}


def _choice(message: dict, finish: Any = "stop") -> dict[str, Any]:
    return {"index": 0, "finish_reason": finish, "message": {"role": "assistant", **message}}


def _body(
    message: dict | None = None,
    finish: Any = "stop",
    *,
    usage: dict | None = None,
    choices: list | None = None,
) -> dict[str, Any]:
    return {
        "id": "chatcmpl_1",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-5",
        "choices": [_choice(message or TEXT, finish)] if choices is None else choices,
        "usage": usage or {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }


def _chat(api: MockApi, **fields: Any) -> OpenAIProvider:
    return openai_adapter(api, CHAT, **fields)


def test_a_chat_request_maps_every_message_tool_and_limit() -> None:
    api = responding_api(_body())
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
    send_request(_chat(api, capabilities=PARALLEL), request=request)
    assert str(api.requests[0].url) == f"{ENDPOINT}/chat/completions"
    assert sent_body(api) == {
        "model": "gpt-5",
        "messages": [
            {"role": "system", "content": "Answer only from tool results."},
            {"role": "user", "content": QUESTION},
            {
                "role": "assistant",
                "content": "Searching.",
                "tool_calls": [
                    {
                        "type": "function",
                        "id": "call_0",
                        "function": {
                            "name": "search_resources",
                            "arguments": '{"type":"rds_instance"}',
                        },
                    },
                    # A malformed call was never run; it is replayed with no arguments.
                    {
                        "type": "function",
                        "id": "call_1",
                        "function": {"name": "inspect_resource", "arguments": "{}"},
                    },
                ],
            },
            # One message per result: the protocol has no error flag.
            {"role": "tool", "tool_call_id": "call_0", "content": "[]"},
            {"role": "tool", "tool_call_id": "call_1", "content": "Arguments were not JSON."},
            {"role": "user", "content": "Submit your answer now."},
        ],
        "max_completion_tokens": 512,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in (SEARCH_TOOL, INSPECT_TOOL)
        ],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }


def test_an_assistant_message_without_text_sends_no_content() -> None:
    api = responding_api(_body())
    call = {
        "id": "call_0",
        "name": "search_resources",
        "arguments": {"kind": "parsed", "value": {"name": "réseau"}},
    }
    send_request(
        _chat(api),
        request=openai_request(
            {"role": "assistant", "tool_calls": [call]},
            {"role": "tool", "results": [{"call_id": "call_0", "content": "[]"}]},
        ),
    )
    assistant = sent_body(api)["messages"][1]
    assert "content" not in assistant
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"name":"réseau"}'


@pytest.mark.parametrize(
    ("declared", "limit", "expected"),
    [(PARALLEL, 4, True), (PARALLEL, 1, False), (["tool_calling"], 1, None)],
    ids=["declared-many", "declared-one", "undeclared"],
)
def test_parallel_tool_calls_is_sent_only_when_declared(declared, limit, expected) -> None:
    api = responding_api(_body())
    send_request(
        _chat(api, capabilities=declared), request=openai_request(max_tool_calls_per_turn=limit)
    )
    assert sent_body(api).get("parallel_tool_calls") is expected


def test_a_request_without_tools_sends_no_tool_fields() -> None:
    api = responding_api(_body())
    send_request(_chat(api, capabilities=PARALLEL), request=openai_request(tools=()))
    assert set(sent_body(api)) == {"model", "messages", "max_completion_tokens"}


def test_no_key_or_sampling_parameter_is_in_the_body() -> None:
    api = responding_api(_body())
    send_request(
        _chat(api, capabilities=PARALLEL), request=openai_request(max_tool_calls_per_turn=4)
    )
    body = api.requests[0].content.decode()
    assert SECRET not in body
    assert set(json.loads(body)) == {
        "model",
        "messages",
        "max_completion_tokens",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
    }


@pytest.mark.parametrize("provider_kind", ["openai_compatible", "anthropic"])
def test_a_continuation_is_refused_before_any_request(provider_kind: str) -> None:
    api = responding_api(_body())
    message = {
        "role": "assistant",
        "text": "Thinking.",
        "continuation": {"provider_kind": provider_kind, "blocks": [THINKING]},
    }
    with pytest.raises(ProviderInvalidRequestError):
        send_request(
            _chat(api), request=openai_request(message, {"role": "user", "text": "Go on."})
        )
    assert api.requests == []


# ---- Chat Completions responses --------------------------------------------------


def _call(call: dict, arguments=None) -> ToolCall:
    function = call["function"]
    value = arguments or ParsedToolArguments(value=json.loads(function["arguments"]))
    return ToolCall(id=call["id"], name=function["name"], arguments=value)


def _malformed(reason: MalformedArgumentsReason) -> MalformedToolArguments:
    return MalformedToolArguments(reason=reason)


def _calls_body(*calls: dict) -> dict[str, Any]:
    return _body({"content": None, "tool_calls": list(calls)}, "tool_calls")


def _with_arguments(call: dict, arguments: Any) -> dict:
    return {**call, "function": {**call["function"], "arguments": arguments}}


@pytest.mark.parametrize(
    ("body", "message", "stop"),
    [
        (_body(), AssistantMessage(text=TEXT["content"]), StopReason.END),
        (
            _body({"content": None, "tool_calls": [LOOKUP]}, "tool_calls"),
            AssistantMessage(tool_calls=(_call(LOOKUP),)),
            StopReason.TOOL_USE,
        ),
        (
            _body({"content": "", "tool_calls": [LOOKUP, INSPECTION]}, "tool_calls"),
            AssistantMessage(tool_calls=(_call(LOOKUP), _call(INSPECTION))),
            StopReason.TOOL_USE,
        ),
        (
            _body({"content": "Let me look.", "tool_calls": [LOOKUP]}, "tool_calls"),
            AssistantMessage(text="Let me look.", tool_calls=(_call(LOOKUP),)),
            StopReason.TOOL_USE,
        ),
        (
            _body(
                {
                    "tool_calls": [
                        _with_arguments(LOOKUP, "{not json"),
                        _with_arguments(INSPECTION, "[1]"),
                    ]
                },
                "tool_calls",
            ),
            AssistantMessage(
                tool_calls=(
                    _call(LOOKUP, _malformed(MalformedArgumentsReason.INVALID_JSON)),
                    _call(INSPECTION, _malformed(MalformedArgumentsReason.NOT_OBJECT)),
                )
            ),
            StopReason.TOOL_USE,
        ),
        (
            _body({"tool_calls": [_with_arguments(LOOKUP, {"type": "x"})]}, "tool_calls"),
            AssistantMessage(tool_calls=(_call(LOOKUP),)),
            StopReason.TOOL_USE,
        ),
        (_body(TEXT, "length"), AssistantMessage(text=TEXT["content"]), StopReason.LENGTH),
        (
            _body({"content": None, "refusal": "I cannot help."}),
            AssistantMessage(text="I cannot help."),
            StopReason.REFUSAL,
        ),
    ],
    ids=[
        "text",
        "single-call",
        "parallel-calls",
        "text-and-calls",
        "malformed-arguments",
        "decoded-arguments",
        "length-text",
        "refusal-text",
    ],
)
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_chat_responses_map_to_the_shared_contracts(body, message, stop, mode: str) -> None:
    response = send_request(_chat(responding_api(body)), mode)
    assert response == CompletionResponse(
        message=message, stop_reason=stop, usage=TokenUsage(input_tokens=3, output_tokens=1)
    )


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (_body({"content": None}, "length"), ProviderTruncatedOutputError),
        (
            _body({"content": "Part", "tool_calls": [_with_arguments(LOOKUP, '{"ty')]}, "length"),
            ProviderTruncatedOutputError,
        ),
        (_body(TEXT, "content_filter"), ProviderRefusalError),
        (_body({"content": None}, "content_filter"), ProviderRefusalError),
        (_body({"refusal": "No.", "tool_calls": [LOOKUP]}, "tool_calls"), ProviderRefusalError),
        (_body(choices=[_choice(TEXT), _choice(TEXT)]), ProviderMalformedOutputError),
        (_body(choices=[]), ProviderMalformedOutputError),
        (_body(choices="None of them."), ProviderMalformedOutputError),
        ({**_body(), "choices": None}, ProviderMalformedOutputError),
        (_body(TEXT, "function_call"), ProviderMalformedOutputError),
        (_body(TEXT, "improvised"), ProviderMalformedOutputError),
        (_body(TEXT, ["stop"]), ProviderMalformedOutputError),
        (
            _calls_body({"id": "c", "type": "custom", "custom": {"name": "x", "input": ""}}),
            ProviderMalformedOutputError,
        ),
        (_calls_body({**LOOKUP, "function": "oops"}), ProviderMalformedOutputError),
        (_body({**TEXT, "tool_calls": [LOOKUP]}), ProviderMalformedOutputError),
        (_body(TEXT, "tool_calls"), ProviderMalformedOutputError),
        (_body({"content": None}), ProviderMalformedOutputError),
        (_body({"content": 5}), ProviderMalformedOutputError),
        (_body({"content": [{"type": "text", "text": "x"}]}), ProviderMalformedOutputError),
        (
            _calls_body({**INSPECTION, "function": {"name": "not a tool", "arguments": "{}"}}),
            ProviderMalformedOutputError,
        ),
        (_body(choices=[{"index": 0, "finish_reason": "stop"}]), ProviderMalformedOutputError),
        (_body(usage={"prompt_tokens": -1, "completion_tokens": 1}), ProviderMalformedOutputError),
    ],
    ids=[
        "length-empty",
        "length-partial-call",
        "content-filter-text",
        "content-filter-empty",
        "refusal-with-call",
        "two-choices",
        "no-choices",
        "choices-not-a-list",
        "choices-null",
        "function-call-finish",
        "unknown-finish",
        "finish-not-a-string",
        "custom-tool-call",
        "function-not-an-object",
        "calls-at-stop",
        "no-calls-at-tool-calls",
        "empty",
        "content-not-a-string",
        "content-parts",
        "invalid-tool-name",
        "no-message",
        "negative-usage",
    ],
)
def test_unusable_chat_responses_are_typed_errors_without_provider_text(body, error) -> None:
    with pytest.raises(error) as raised:
        send_request(_chat(responding_api(body)))
    assert str(raised.value) == error.message
    assert "None of them" not in "".join(secret_renderings(raised.value))


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "prompt_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 2},
            },
            TokenUsage(
                input_tokens=10,
                output_tokens=5,
                cache_read_input_tokens=3,
                cache_write_input_tokens=2,
            ),
        ),
        (
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            TokenUsage(input_tokens=10, output_tokens=5),
        ),
    ],
    ids=["with-cache", "without-cache"],
)
def test_chat_usage_maps_cached_tokens(usage, expected) -> None:
    assert send_request(_chat(responding_api(_body(usage=usage)))).usage == expected


def test_a_response_without_usage_reports_none() -> None:
    body = {key: value for key, value in _body().items() if key != "usage"}
    assert send_request(_chat(responding_api(body))).usage == TokenUsage()


# ---- failures ----------------------------------------------------------------------


def _error(status: int, headers: dict | None = None) -> httpx2.Response:
    """An API error whose message holds the key, a tool result, and the question."""
    message = f"{SECRET} tool result: db-a reachable; question: {QUESTION}"
    body = {"error": {"message": message, "type": "server_error", "code": None}}
    return httpx2.Response(status, json=body, headers=headers)


_ANSWER = CompletionResponse(message=AssistantMessage(text="Done."), stop_reason=StopReason.END)
# A success body in each protocol's shape.
_BODIES = {
    harness.protocol: harness.body(_ANSWER)
    for harness in (ChatCompletionsHarness, ResponsesHarness)
}


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=str)
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, ProviderInvalidRequestError),
        (401, ProviderAuthenticationError),
        (403, ProviderAuthenticationError),
        (404, ProviderInvalidRequestError),
        (409, ProviderInvalidRequestError),
        (413, ProviderInvalidRequestError),
        (422, ProviderInvalidRequestError),
        (429, ProviderRateLimitError),
        (500, ProviderUnavailableError),
        (502, ProviderUnavailableError),
        (503, ProviderUnavailableError),
        (504, ProviderUnavailableError),
    ],
)
def test_statuses_map_to_fixed_safe_errors(caplog, protocol, status: int, error) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ProviderError) as raised:
        send_request(openai_adapter(MockApi([_error(status)]), protocol))
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
        (unparseable_body, ProviderMalformedOutputError),
    ],
    ids=["timeout", "connection", "unparseable"],
)
@pytest.mark.parametrize("protocol", PROTOCOLS, ids=str)
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_transport_failures_map_to_typed_errors(served, error, protocol, mode: str) -> None:
    with pytest.raises(error):
        send_request(openai_adapter(MockApi([served]), protocol), mode)


def test_every_sdk_failure_class_has_a_provider_error() -> None:
    sdk = {
        cls
        for cls in list(vars(openai).values())
        if isinstance(cls, type) and issubclass(cls, openai.APIError)
    }
    for cls in sdk:
        assert any(base in SDK_ERRORS for base in cls.__mro__), cls


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=str)
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_rate_limits_are_retried_with_exponential_backoff(mode: str, protocol) -> None:
    waits = Waits()
    api = MockApi([_error(429), _error(429), httpx2.Response(200, json=_BODIES[protocol])])
    send_request(openai_adapter(api, protocol, waits=waits, max_retries=2), mode)
    assert len(api.requests) == 3
    assert waits == [RETRY_BACKOFF_SECONDS, RETRY_BACKOFF_SECONDS * 2]


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_retries_stop_at_the_configured_count(mode: str) -> None:
    waits = Waits()
    api = MockApi([lambda request: httpx2.ConnectError("refused", request=request)] * 3)
    with pytest.raises(ProviderTransportError):
        send_request(openai_adapter(api, CHAT, waits=waits, max_retries=2), mode)
    assert len(api.requests) == 3 and len(waits) == 2


@pytest.mark.parametrize(
    "served",
    [_error(503), _error(400), lambda request: httpx2.ReadTimeout("slow", request=request)],
    ids=["unavailable", "invalid-request", "timeout"],
)
def test_other_failures_are_never_retried(served) -> None:
    waits = Waits()
    api = MockApi([served, httpx2.Response(200, json=_body())])
    with pytest.raises(ProviderError):
        send_request(openai_adapter(api, CHAT, waits=waits, max_retries=3))
    assert len(api.requests) == 1 and waits == []


@pytest.mark.parametrize(
    ("retry_after", "timeout", "wait"),
    [("2", 60, 2.0), ("20", 10, 10.0), ("120", 60, MAX_RETRY_WAIT_SECONDS)],
    ids=["honoured", "request-timeout", "capped"],
)
def test_retry_after_is_honoured_within_bounds(retry_after: str, timeout: int, wait) -> None:
    waits = Waits()
    api = MockApi([_error(429, {"retry-after": retry_after}), httpx2.Response(200, json=_body())])
    send_request(
        openai_adapter(api, CHAT, waits=waits, max_retries=1),
        request=openai_request(timeout_seconds=timeout),
    )
    assert waits == [wait]
