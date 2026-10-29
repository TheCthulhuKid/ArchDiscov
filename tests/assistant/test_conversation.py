import json

import pytest
from pydantic import ValidationError

from archdiscov.assistant.conversation import (
    MAX_TOOL_ARGUMENT_BYTES,
    AssistantMessage,
    Conversation,
    ConversationValidationError,
    MalformedArgumentsReason,
    MalformedToolArguments,
    ParsedToolArguments,
    SystemMessage,
    ToolCall,
    ToolDeclaration,
    ToolResult,
    ToolResultMessage,
    UserMessage,
    parse_conversation,
    parse_tool_arguments,
)


def _call(call_id: str, name: str = "search_resources", **arguments) -> dict:
    return {"id": call_id, "name": name, "arguments": {"kind": "parsed", "value": arguments}}


def _conversation(*messages: dict) -> dict:
    return {"messages": list(messages)}


SYSTEM = {"role": "system", "text": "Answer only from tool results."}
USER = {"role": "user", "text": "Which databases are reachable?"}


def _assistant_calls(*calls: dict) -> dict:
    return {"role": "assistant", "tool_calls": list(calls)}


def _results(*call_ids: str) -> dict:
    return {"role": "tool", "results": [{"call_id": cid, "content": "{}"} for cid in call_ids]}


def _valid_conversation() -> Conversation:
    return Conversation(
        messages=(
            SystemMessage(text="Answer only from tool results."),
            UserMessage(text="Which databases are reachable?"),
            AssistantMessage(
                text="Searching first.",
                tool_calls=(
                    ToolCall(
                        id="call_1",
                        name="search_resources",
                        arguments=ParsedToolArguments(value={"type": "rds_instance", "limit": 5}),
                    ),
                    ToolCall(
                        id="call_2",
                        name="inspect_resource",
                        arguments=MalformedToolArguments(
                            reason=MalformedArgumentsReason.INVALID_JSON
                        ),
                    ),
                ),
            ),
            ToolResultMessage(
                results=(
                    ToolResult(call_id="call_1", content='{"resources": []}'),
                    ToolResult(call_id="call_2", content="Invalid arguments.", is_error=True),
                )
            ),
            AssistantMessage(text="No databases were recorded."),
        )
    )


def test_valid_conversation_round_trips_through_json() -> None:
    conversation = _valid_conversation()
    data = json.loads(conversation.model_dump_json())
    assert parse_conversation(data) == conversation
    assert parse_conversation(conversation) == conversation
    assert data["messages"][2]["tool_calls"][1]["arguments"] == {
        "kind": "malformed",
        "reason": "invalid_json",
    }


@pytest.mark.parametrize(
    "data,code",
    [
        (_conversation(USER, _results("call_1")), "conversation_tool_result_orphan"),
        (
            _conversation(
                USER,
                _assistant_calls(_call("call_1")),
                _results("call_1"),
                _assistant_calls(_call("call_1")),
            ),
            "conversation_tool_call_id_duplicate",
        ),
        (
            _conversation(
                USER, _assistant_calls(_call("call_1")), _results("call_1"), _results("call_1")
            ),
            "conversation_tool_result_duplicate",
        ),
        (_conversation(USER, {"role": "assistant"}), "assistant_message_empty"),
        (_conversation(USER, SYSTEM), "conversation_system_message_position"),
        (
            _conversation(
                USER, _assistant_calls(_call("call_1", query="x" * MAX_TOOL_ARGUMENT_BYTES))
            ),
            "tool_arguments_too_large",
        ),
    ],
    ids=[
        "orphan-result",
        "call-id-across-messages",
        "duplicate-result",
        "empty-assistant",
        "system-position",
        "arguments-too-large",
    ],
)
def test_invalid_conversations_fail_with_safe_diagnostics(data: dict, code: str) -> None:
    with pytest.raises(ConversationValidationError) as raised:
        parse_conversation(data)
    error = raised.value
    assert error.code == "invalid_conversation"
    assert code in {issue.type for issue in error.details}
    assert all(issue.message == "Invalid conversation field." for issue in error.details)


@pytest.mark.parametrize(
    "schema,code",
    [
        ({"type": "array"}, "tool_input_schema_not_object"),
        (
            {"type": "object", "description": "x" * 40_000},
            "tool_input_schema_too_large",
        ),
    ],
)
def test_tool_declaration_schema_rules(schema: dict, code: str) -> None:
    with pytest.raises(ValidationError) as raised:
        ToolDeclaration(name="search", description="Search.", input_schema=schema)
    assert raised.value.errors()[0]["type"] == code


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"limit": 5}', ParsedToolArguments(value={"limit": 5})),
        ({"limit": 5}, ParsedToolArguments(value={"limit": 5})),
        ("{not json", MalformedToolArguments(reason=MalformedArgumentsReason.INVALID_JSON)),
        ("[1, 2]", MalformedToolArguments(reason=MalformedArgumentsReason.NOT_OBJECT)),
        (
            json.dumps({"q": "x" * MAX_TOOL_ARGUMENT_BYTES}),
            MalformedToolArguments(reason=MalformedArgumentsReason.TOO_LARGE),
        ),
    ],
    ids=["parsed-text", "parsed-mapping", "invalid-json", "not-an-object", "too-large"],
)
def test_parse_tool_arguments_classifies_without_raising(raw, expected) -> None:
    assert parse_tool_arguments(raw) == expected
