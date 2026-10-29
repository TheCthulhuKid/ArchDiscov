"""The Anthropic adapter: built only from its configuration, mapping requests and responses.

Requests go to a mocked Messages API; nothing reaches the network.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from archdiscov.assistant.anthropic_provider import DEFAULT_BASE_URL, AnthropicProvider
from archdiscov.assistant.config import (
    parse_provider_config,
)
from archdiscov.assistant.conversation import (
    AssistantMessage,
    Conversation,
    ParsedToolArguments,
    TokenUsage,
    ToolCall,
)
from archdiscov.assistant.orchestrator import run_assistant
from archdiscov.assistant.provider import (
    CompletionRequest,
    CompletionResponse,
    ProviderCapability,
    ProviderMalformedOutputError,
    RequestLimits,
    StopReason,
    complete,
)
from archdiscov.assistant.providers import load_provider
from archdiscov.assistant.tools.session import ToolSession
from tests.support.mock_api import MockApi
from tests.support.provider_data import ANTHROPIC, EXCHANGE, INSPECT_TOOL, SEARCH_TOOL, THINKING
from tests.support.scripted_turns import SNAPSHOT
from tests.support.secrets import SECRET, secret_renderings

ENDPOINT = "https://gateway.example.com/anthropic"
QUESTION = "Which databases are reachable?"
TEXT = {"type": "text", "text": "None of them."}
LOOKUP = {"type": "tool_use", "id": "toolu_1", "name": "search_resources", "input": {"type": "x"}}


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


def _provider(api: MockApi) -> AnthropicProvider:
    """The adapter over `api`, without retries."""
    config = parse_provider_config({**ANTHROPIC, "max_retries": 0, "endpoint": ENDPOINT})
    return AnthropicProvider(config, SecretStr(SECRET), transport=api, async_transport=api)


def _request(tools=(SEARCH_TOOL, INSPECT_TOOL), **limits: Any) -> CompletionRequest:
    return CompletionRequest(
        conversation=Conversation.model_validate(
            {"messages": [{"role": "user", "text": QUESTION}]}
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


# ---- responses ---------------------------------------------------------------------


def _call(block: dict, arguments=None) -> ToolCall:
    value = arguments if arguments is not None else ParsedToolArguments(value=block["input"])
    return ToolCall(id=block["id"], name=block["name"], arguments=value)


def test_several_text_blocks_are_joined_into_one_text() -> None:
    parts = ({"type": "text", "text": "Let me "}, {"type": "text", "text": "look."})
    body = _body(*parts, LOOKUP, stop="tool_use")
    assert _send(_provider(_api(body)), "async") == CompletionResponse(
        message=AssistantMessage(text="Let me look.", tool_calls=(_call(LOOKUP),)),
        stop_reason=StopReason.TOOL_USE,
        usage=TokenUsage(input_tokens=3, output_tokens=1),
    )


def test_a_server_tool_block_is_malformed_without_provider_text() -> None:
    server_tool = {"type": "server_tool_use", "id": "s", "name": "web_search", "input": {}}
    with pytest.raises(ProviderMalformedOutputError) as raised:
        _send(_provider(_api(_body(TEXT, server_tool))))
    assert str(raised.value) == ProviderMalformedOutputError.message
    assert "None of them" not in "".join(secret_renderings(raised.value))


def test_usage_maps_cache_creation_to_cache_write() -> None:
    usage = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 2,
    }
    expected = TokenUsage(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=3,
        cache_write_input_tokens=2,
    )
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
