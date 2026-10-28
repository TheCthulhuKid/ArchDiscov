"""The OpenAI-compatible adapter's Responses mapping: stateless requests and their output.

Requests go to a mocked API; nothing reaches the network.
"""

import json
from typing import Any

import pytest

from archdiscov.assistant.config import OpenAIProtocol
from archdiscov.assistant.conversation import (
    AssistantMessage,
    Conversation,
    ParsedToolArguments,
    TokenUsage,
    ToolCall,
)
from archdiscov.assistant.openai_provider import OpenAIProvider
from archdiscov.assistant.orchestrator import run_assistant
from archdiscov.assistant.provider import (
    CompletionRequest,
    CompletionResponse,
    ProviderInvalidRequestError,
    ProviderMalformedOutputError,
    ProviderRefusalError,
    RequestLimits,
    StopReason,
)
from archdiscov.assistant.tools.session import ToolSession
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
from tests.support.provider_data import EXCHANGE, INSPECT_TOOL, SEARCH_TOOL, SIGNATURE, THINKING
from tests.support.scripted_turns import SNAPSHOT
from tests.support.secrets import SECRET, secret_renderings

TEXT = {
    "type": "message",
    "id": "msg_1",
    "role": "assistant",
    "status": "completed",
    "content": [{"type": "output_text", "text": "None of them.", "annotations": []}],
}
LOOKUP = {
    "type": "function_call",
    "id": "fc_1",
    "call_id": "call_1",
    "name": "search_resources",
    "arguments": '{"type":"x"}',
    "status": "completed",
}
INSPECTION = {
    "type": "function_call",
    "id": "fc_2",
    "call_id": "call_2",
    "name": "inspect_resource",
    "arguments": "{}",
    "status": "completed",
}
# Encrypted reasoning, as a stateless request asks for it; long and opaque, like a signature.
REASONING = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": SIGNATURE}


def _message(*parts: dict) -> dict[str, Any]:
    return {**TEXT, "content": list(parts)}


def _text(text: str) -> dict[str, Any]:
    return {"type": "output_text", "text": text, "annotations": []}


def _body(
    *output: dict,
    status: Any = "completed",
    reason: Any = None,
    usage: dict | None = None,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "model": "gpt-5",
        "status": status,
        "incomplete_details": None if reason is None else {"reason": reason},
        "output": list(output),
        "usage": usage or {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
        **fields,
    }


def _responses(api, **fields: Any) -> OpenAIProvider:
    return openai_adapter(api, OpenAIProtocol.RESPONSES, **fields)


# ---- requests ----------------------------------------------------------------------


def test_a_responses_request_maps_every_message_tool_and_limit() -> None:
    api = responding_api(_body(TEXT))
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
    send_request(_responses(api, capabilities=PARALLEL), request=request)
    assert str(api.requests[0].url) == f"{ENDPOINT}/responses"
    assert sent_body(api) == {
        "model": "gpt-5",
        "input": [
            {"type": "message", "role": "system", "content": "Answer only from tool results."},
            {"type": "message", "role": "user", "content": QUESTION},
            {"type": "message", "role": "assistant", "content": "Searching."},
            {
                "type": "function_call",
                "call_id": "call_0",
                "name": "search_resources",
                "arguments": '{"type":"rds_instance"}',
            },
            # A malformed call was never run; it is replayed with no arguments.
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "inspect_resource",
                "arguments": "{}",
            },
            # The protocol has no error flag: a failed tool's content says so itself.
            {"type": "function_call_output", "call_id": "call_0", "output": "[]"},
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "Arguments were not JSON.",
            },
            {"type": "message", "role": "user", "content": "Submit your answer now."},
        ],
        "store": False,
        "max_output_tokens": 512,
        "tools": [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
                "strict": False,
            }
            for tool in (SEARCH_TOOL, INSPECT_TOOL)
        ],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }


@pytest.mark.parametrize(
    ("declared", "limit", "expected"),
    [(PARALLEL, 4, True), (PARALLEL, 1, False), (["tool_calling"], 1, None)],
    ids=["declared-many", "declared-one", "undeclared"],
)
def test_parallel_tool_calls_is_sent_only_when_declared(declared, limit, expected) -> None:
    api = responding_api(_body(TEXT))
    send_request(
        _responses(api, capabilities=declared),
        request=openai_request(max_tool_calls_per_turn=limit),
    )
    assert sent_body(api).get("parallel_tool_calls") is expected


def test_encrypted_reasoning_is_requested_only_when_declared() -> None:
    api = responding_api(_body(TEXT), _body(TEXT))
    send_request(_responses(api, encrypted_reasoning=True))
    send_request(_responses(api))
    assert sent_body(api, 0)["include"] == ["reasoning.encrypted_content"]
    assert "include" not in sent_body(api, 1)


def test_a_request_without_tools_sends_no_tool_fields_and_no_secret() -> None:
    api = responding_api(_body(TEXT))
    send_request(_responses(api, capabilities=PARALLEL), request=openai_request(tools=()))
    body = api.requests[0].content.decode()
    assert SECRET not in body
    assert set(json.loads(body)) == {"model", "input", "store", "max_output_tokens"}


@pytest.mark.parametrize("provider_kind", ["fake", "anthropic"])
def test_another_providers_continuation_is_refused_before_any_request(
    provider_kind: str,
) -> None:
    api = responding_api(_body(TEXT))
    message = {
        "role": "assistant",
        "text": "Thinking.",
        "continuation": {"provider_kind": provider_kind, "blocks": [THINKING]},
    }
    with pytest.raises(ProviderInvalidRequestError):
        send_request(
            _responses(api), request=openai_request(message, {"role": "user", "text": "Go on."})
        )
    assert api.requests == []


# ---- responses ---------------------------------------------------------------------


def _call(item: dict, arguments=None) -> ToolCall:
    value = arguments or ParsedToolArguments(value=json.loads(item["arguments"]))
    return ToolCall(id=item["call_id"], name=item["name"], arguments=value)


@pytest.mark.parametrize(
    ("body", "message", "stop"),
    [
        (
            _body(_message(_text("Let me ")), _message(_text("look.")), LOOKUP),
            AssistantMessage(text="Let me look.", tool_calls=(_call(LOOKUP),)),
            StopReason.TOOL_USE,
        ),
        (
            # Reasoning that cannot go back statelessly is left out of the message.
            _body({**REASONING, "encrypted_content": None}, TEXT),
            AssistantMessage(text="None of them."),
            StopReason.END,
        ),
    ],
    ids=[
        "text-and-calls",
        "unencrypted-reasoning",
    ],
)
def test_responses_map_to_the_shared_contracts(body, message, stop) -> None:
    response = send_request(_responses(responding_api(body)), "async")
    assert response == CompletionResponse(
        message=message, stop_reason=stop, usage=TokenUsage(input_tokens=3, output_tokens=1)
    )


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (
            _body(_message({"type": "refusal", "refusal": "No."}), LOOKUP),
            ProviderRefusalError,
        ),
        (_body(TEXT, status="failed"), ProviderMalformedOutputError),
        (_body(TEXT, {"type": "hologram"}), ProviderMalformedOutputError),
        (_body({**LOOKUP, "name": "not a tool"}), ProviderMalformedOutputError),
    ],
    ids=[
        "refusal-with-call",
        "failed",
        "unknown-item",
        "invalid-tool-name",
    ],
)
def test_unusable_responses_are_typed_errors_without_provider_text(body, error) -> None:
    with pytest.raises(error) as raised:
        send_request(_responses(responding_api(body)))
    assert str(raised.value) == error.message
    assert "None of them" not in "".join(secret_renderings(raised.value))


def test_usage_maps_cached_tokens() -> None:
    usage = {
        "input_tokens": 10,
        "output_tokens": 5,
        "input_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 2},
    }
    expected = TokenUsage(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=3,
        cache_write_input_tokens=2,
    )
    assert send_request(_responses(responding_api(_body(TEXT, usage=usage)))).usage == expected


# ---- continuations -----------------------------------------------------------------


def test_a_response_without_reasoning_has_no_continuation() -> None:
    assert send_request(_responses(responding_api(_body(TEXT)))).message.continuation is None


def test_reasoning_is_replayed_verbatim_and_in_order_on_the_next_request() -> None:
    first = _body(
        REASONING,
        _message(_text("Searching.")),
        {**REASONING, "id": "rs_2", "summary": [{"type": "summary_text", "text": "Plan."}]},
        {**LOOKUP, "arguments": "{}"},
    )
    api = responding_api(first, _body(TEXT))
    result = run_assistant(
        QUESTION,
        provider=_responses(api, encrypted_reasoning=True),
        session=ToolSession.from_paths(SNAPSHOT),
        provider_kind="openai_compatible",
        model="gpt-5",
    )
    assert result.outcome.kind == "answered"
    replayed = sent_body(api, 1)["input"]
    # The instructions and question, the whole first output unchanged, then the tool result.
    assert [item["role"] for item in replayed[:2]] == ["system", "user"]
    assert replayed[2:-1] == first["output"]
    assert replayed[-1]["type"] == "function_call_output"
    assert result.transcript.turns[0].continuation_bytes > 0
    assert SIGNATURE not in result.transcript.model_dump_json()
