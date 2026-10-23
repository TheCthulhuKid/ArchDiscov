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
from pydantic import SecretStr

from archdiscov.assistant import openai_provider
from archdiscov.assistant.config import (
    OpenAIProtocol,
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
from archdiscov.assistant.openai_provider import OpenAIProvider
from archdiscov.assistant.provider import (
    CompletionRequest,
    CompletionResponse,
    ProviderCapability,
    ProviderErrorCode,
    ProviderInvalidRequestError,
    ProviderMalformedOutputError,
    ProviderRefusalError,
    ProviderTruncatedOutputError,
    ProviderUnavailableError,
    RequestLimits,
    StopReason,
    UnsupportedCapabilityError,
    complete,
)
from archdiscov.assistant.providers import load_provider
from archdiscov.services.ask import ask_question
from tests.assistant.test_anthropic_provider import EXCHANGE
from tests.assistant.test_config import OPENAI
from tests.assistant.test_continuations import THINKING
from tests.assistant.test_orchestrator import SNAPSHOT
from tests.assistant.test_provider_contract import INSPECT, SEARCH
from tests.assistant.test_secret_leaks import SECRET, _renderings
from tests.support.mock_api import MockApi, unparseable_body

ENDPOINT = "https://gateway.example.com/v1"
QUESTION = "Which databases are reachable?"
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


def _provider(
    api: MockApi, protocol: OpenAIProtocol = OpenAIProtocol.RESPONSES, **fields: Any
) -> OpenAIProvider:
    config = parse_provider_config(
        {**OPENAI, "endpoint": ENDPOINT, "protocol": protocol.value, **fields}
    )
    return OpenAIProvider(config, SecretStr(SECRET), transport=api, async_transport=api)


def _create(provider: OpenAIProvider, mode: str = "sync") -> None:
    """Send the protocol's smallest request; the mocked API refuses it."""
    params = PARAMS[provider.protocol]
    with pytest.raises(openai.BadRequestError):
        if mode == "sync":
            provider._create(**params)
        else:
            asyncio.run(provider._create_async(**params))


def _request(*messages: dict, tools=(SEARCH,), **limits: Any) -> CompletionRequest:
    return CompletionRequest(
        conversation=Conversation.model_validate(
            {"messages": [{"role": "user", "text": QUESTION}, *messages]}
        ),
        tools=tools,
        limits=RequestLimits(**limits),
    )


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
    _create(_provider(api, protocol), mode)
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
        _provider(api)._create(**PARAMS[OpenAIProtocol.RESPONSES])
    assert [request.url.host for request in api.requests] == ["gateway.example.com"]


def test_no_key_or_request_body_reaches_a_rendering_or_log(monkeypatch, caplog) -> None:
    monkeypatch.setenv("OPENAI_LOG", "debug")
    caplog.set_level(logging.DEBUG)
    for name in openai_provider._SDK_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
    provider = _provider(MockApi([_rejected()]))
    _create(provider)
    for name in openai_provider._SDK_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING
    for rendering in (*_renderings(provider), caplog.text):
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
        complete(_provider(api, capabilities=declared), _request(**limits))
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
PARALLEL = ["tool_calling", "parallel_tool_calls"]
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


def _api(*bodies: dict) -> MockApi:
    return MockApi([httpx2.Response(200, json=body) for body in bodies])


def _chat(api: MockApi, **fields: Any) -> OpenAIProvider:
    return _provider(api, CHAT, **fields)


def _send(provider: OpenAIProvider, mode: str = "sync", request=None) -> CompletionResponse:
    request = request or _request()
    if mode == "sync":
        return complete(provider, request)
    return asyncio.run(provider.complete_async(request))


def _sent(api: MockApi, index: int = 0) -> dict[str, Any]:
    return json.loads(api.requests[index].content)


def test_a_chat_request_maps_every_message_tool_and_limit() -> None:
    api = _api(_body())
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
    _send(_chat(api, capabilities=PARALLEL), request=request)
    assert str(api.requests[0].url) == f"{ENDPOINT}/chat/completions"
    assert _sent(api) == {
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
            for tool in (SEARCH, INSPECT)
        ],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }


def test_an_assistant_message_without_text_sends_no_content() -> None:
    api = _api(_body())
    call = {
        "id": "call_0",
        "name": "search_resources",
        "arguments": {"kind": "parsed", "value": {"name": "réseau"}},
    }
    _send(
        _chat(api),
        request=_request(
            {"role": "assistant", "tool_calls": [call]},
            {"role": "tool", "results": [{"call_id": "call_0", "content": "[]"}]},
        ),
    )
    assistant = _sent(api)["messages"][1]
    assert "content" not in assistant
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"name":"réseau"}'


@pytest.mark.parametrize(
    ("declared", "limit", "expected"),
    [(PARALLEL, 4, True), (PARALLEL, 1, False), (["tool_calling"], 1, None)],
    ids=["declared-many", "declared-one", "undeclared"],
)
def test_parallel_tool_calls_is_sent_only_when_declared(declared, limit, expected) -> None:
    api = _api(_body())
    _send(_chat(api, capabilities=declared), request=_request(max_tool_calls_per_turn=limit))
    assert _sent(api).get("parallel_tool_calls") is expected


def test_a_request_without_tools_sends_no_tool_fields() -> None:
    api = _api(_body())
    _send(_chat(api, capabilities=PARALLEL), request=_request(tools=()))
    assert set(_sent(api)) == {"model", "messages", "max_completion_tokens"}


def test_no_key_or_sampling_parameter_is_in_the_body() -> None:
    api = _api(_body())
    _send(_chat(api, capabilities=PARALLEL), request=_request(max_tool_calls_per_turn=4))
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
    api = _api(_body())
    message = {
        "role": "assistant",
        "text": "Thinking.",
        "continuation": {"provider_kind": provider_kind, "blocks": [THINKING]},
    }
    with pytest.raises(ProviderInvalidRequestError):
        _send(_chat(api), request=_request(message, {"role": "user", "text": "Go on."}))
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
    response = _send(_chat(_api(body)), mode)
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
        _send(_chat(_api(body)))
    assert str(raised.value) == error.message
    assert "None of them" not in "".join(_renderings(raised.value))


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
    assert _send(_chat(_api(_body(usage=usage)))).usage == expected


def test_a_response_without_usage_reports_none() -> None:
    body = {key: value for key, value in _body().items() if key != "usage"}
    assert _send(_chat(_api(body))).usage == TokenUsage()


# ---- until the Responses mapping -------------------------------------------------


def test_completing_with_responses_sends_nothing_until_it_is_mapped() -> None:
    api = MockApi([])
    provider = _provider(api, OpenAIProtocol.RESPONSES)
    with pytest.raises(ProviderUnavailableError):
        complete(provider, _request())
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider.complete_async(_request()))
    assert api.requests == []


# ---- until the failure table -----------------------------------------------------


@pytest.mark.parametrize(
    ("served", "error"),
    [
        (httpx2.Response(429, json={"error": {"message": QUESTION}}), ProviderUnavailableError),
        (unparseable_body, ProviderMalformedOutputError),
    ],
    ids=["api-error", "unparseable-body"],
)
def test_failures_are_typed_and_not_retried_until_the_failure_table(served, error) -> None:
    api = MockApi([served, httpx2.Response(200, json=_body())])
    with pytest.raises(error) as raised:
        _send(_chat(api, max_retries=2))
    assert len(api.requests) == 1
    assert QUESTION not in "".join(_renderings(raised.value))
