"""The OpenAI-compatible adapter: built only from its configuration, mapping each protocol.

Requests go to a mocked API; nothing reaches the network.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from archdiscov.assistant.config import (
    OpenAIProtocol,
)
from archdiscov.assistant.conversation import (
    AssistantMessage,
    Conversation,
    ParsedToolArguments,
    TokenUsage,
    ToolCall,
)
from archdiscov.assistant.openai_provider import OpenAIProvider
from archdiscov.assistant.provider import (
    CompletionRequest,
    CompletionResponse,
    ProviderMalformedOutputError,
    RequestLimits,
    StopReason,
)
from archdiscov.assistant.providers import load_provider
from tests.support.mock_api import MockApi
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
from tests.support.provider_data import EXCHANGE, INSPECT_TOOL, SEARCH_TOOL
from tests.support.secrets import SECRET, secret_renderings

# ---- construction ------------------------------------------------------------------


def test_a_configuration_builds_the_adapter_without_a_request(tmp_path: Path) -> None:
    path = tmp_path / "provider.toml"
    path.write_text(
        f'kind = "openai_compatible"\nmodel = "gpt-5"\napi_key_env = "ASK_KEY"\n'
        f'endpoint = "{ENDPOINT}"\nprotocol = "chat_completions"\ncapabilities = []\n',
        encoding="utf-8",
    )
    config, provider = load_provider(path, environ={"ASK_KEY": SECRET})
    assert isinstance(provider, OpenAIProvider)
    assert (provider.model, provider.base_url, provider.protocol) == (
        config.model,
        ENDPOINT,
        OpenAIProtocol.CHAT_COMPLETIONS,
    )
    # Only the declared capabilities, here none: a compatible server cannot be asked.
    assert provider.capabilities.capabilities == set()


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


# Both protocols share these tool fields; the full request above sends `true`.
@pytest.mark.parametrize(
    ("declared", "expected"),
    [(PARALLEL, False), (["tool_calling"], None)],
    ids=["declared-one-call", "undeclared"],
)
def test_parallel_tool_calls_is_sent_only_when_declared(declared, expected) -> None:
    api = responding_api(_body())
    send_request(_chat(api, capabilities=declared), request=openai_request())
    assert sent_body(api).get("parallel_tool_calls") is expected


# ---- Chat Completions responses --------------------------------------------------


def _call(call: dict, arguments=None) -> ToolCall:
    function = call["function"]
    value = arguments or ParsedToolArguments(value=json.loads(function["arguments"]))
    return ToolCall(id=call["id"], name=function["name"], arguments=value)


def _with_arguments(call: dict, arguments: Any) -> dict:
    return {**call, "function": {**call["function"], "arguments": arguments}}


@pytest.mark.parametrize(
    ("body", "message", "stop"),
    [
        (
            _body({"content": "", "tool_calls": [LOOKUP, INSPECTION]}, "tool_calls"),
            AssistantMessage(tool_calls=(_call(LOOKUP), _call(INSPECTION))),
            StopReason.TOOL_USE,
        ),
        (
            _body({"tool_calls": [_with_arguments(LOOKUP, {"type": "x"})]}, "tool_calls"),
            AssistantMessage(tool_calls=(_call(LOOKUP),)),
            StopReason.TOOL_USE,
        ),
    ],
    ids=["parallel-calls", "decoded-arguments"],
)
def test_chat_responses_map_to_the_shared_contracts(body, message, stop) -> None:
    response = send_request(_chat(responding_api(body)), "async")
    assert response == CompletionResponse(
        message=message, stop_reason=stop, usage=TokenUsage(input_tokens=3, output_tokens=1)
    )


def test_a_chat_response_without_one_choice_is_malformed_without_provider_text() -> None:
    with pytest.raises(ProviderMalformedOutputError) as raised:
        send_request(_chat(responding_api(_body(choices=[_choice(TEXT), _choice(TEXT)]))))
    assert str(raised.value) == ProviderMalformedOutputError.message
    assert "None of them" not in "".join(secret_renderings(raised.value))


def test_chat_usage_maps_cached_tokens() -> None:
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "prompt_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 2},
    }
    expected = TokenUsage(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=3,
        cache_write_input_tokens=2,
    )
    assert send_request(_chat(responding_api(_body(usage=usage)))).usage == expected
