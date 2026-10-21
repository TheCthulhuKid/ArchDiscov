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
    TokenUsage,
    ToolCall,
    ToolDeclaration,
    ToolResult,
    ToolResultMessage,
    UserMessage,
    parse_conversation,
    parse_tool_arguments,
)
from archdiscov.models.validation_errors import SAFE_VALIDATION_MESSAGES

SECRET = "SECRET_PRIVATE_TOKEN"


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
    assert Conversation.model_validate_json(conversation.model_dump_json()) == conversation
    assert parse_conversation(conversation) == conversation
    assert data["messages"][2]["tool_calls"][1]["arguments"] == {
        "kind": "malformed",
        "reason": "invalid_json",
    }


def test_contracts_are_frozen() -> None:
    conversation = _valid_conversation()
    with pytest.raises(ValidationError):
        conversation.messages = ()
    with pytest.raises(ValidationError):
        conversation.messages[1].text = "changed"


def test_tool_declaration_and_usage_round_trip() -> None:
    declaration = ToolDeclaration(
        name="search_resources",
        description="Search recorded resources.",
        input_schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
    )
    assert ToolDeclaration.model_validate_json(declaration.model_dump_json()) == declaration
    assert TokenUsage().model_dump() == {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_input_tokens": None,
        "cache_write_input_tokens": None,
    }
    assert TokenUsage(input_tokens=10, output_tokens=3).output_tokens == 3


@pytest.mark.parametrize(
    "data,code",
    [
        (_conversation(USER, _results("call_1")), "conversation_tool_result_orphan"),
        (
            _conversation(USER, _results("call_1"), _assistant_calls(_call("call_1"))),
            "conversation_tool_result_orphan",
        ),
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
            _conversation(USER, _assistant_calls(_call("call_1"), _call("call_1"))),
            "conversation_tool_call_id_duplicate",
        ),
        (
            _conversation(
                USER, _assistant_calls(_call("call_1")), _results("call_1"), _results("call_1")
            ),
            "conversation_tool_result_duplicate",
        ),
        (_conversation(USER, {"role": "assistant"}), "assistant_message_empty"),
        (
            _conversation(USER, {"role": "assistant", "tool_calls": []}),
            "assistant_message_empty",
        ),
        (_conversation(USER, SYSTEM), "conversation_system_message_position"),
        (
            _conversation(
                USER, _assistant_calls(_call("call_1", query="x" * MAX_TOOL_ARGUMENT_BYTES))
            ),
            "tool_arguments_too_large",
        ),
        (_conversation({**USER, SECRET: SECRET}), "extra_forbidden"),
        (_conversation({"role": SECRET, "text": SECRET}), "union_tag_invalid"),
        (
            _conversation(USER, _assistant_calls(_call("call_1", name=f"bad-{SECRET}"))),
            "string_pattern_mismatch",
        ),
        (_conversation({"role": "user", "text": " "}), "string_pattern_mismatch"),
        (_conversation(USER, {"role": "tool", "results": []}), "too_short"),
        ({"messages": []}, "too_short"),
    ],
)
def test_invalid_conversations_fail_with_safe_diagnostics(data: dict, code: str) -> None:
    with pytest.raises(ConversationValidationError) as raised:
        parse_conversation(data)
    error = raised.value
    assert error.code == "invalid_conversation"
    assert code in {issue.type for issue in error.details}
    rendered = repr(error.details) + str(error)
    assert SECRET not in rendered
    assert "x" * 100 not in rendered
    assert all(issue.message == "Invalid conversation field." for issue in error.details)


def test_unknown_dynamic_keys_are_not_echoed_in_locations() -> None:
    data = _conversation(USER, _results("call_1"))
    data["messages"][1]["results"][0][SECRET] = 1
    with pytest.raises(ConversationValidationError) as raised:
        parse_conversation(data)
    assert SECRET not in repr(raised.value.details)


def test_non_serializable_conversation_fails_safely() -> None:
    data = _conversation(USER, _assistant_calls(_call("call_1")))
    data["messages"][1]["tool_calls"][0]["arguments"]["value"] = {"bad": {SECRET}}
    with pytest.raises(ConversationValidationError) as raised:
        parse_conversation(data)
    assert SECRET not in repr(raised.value.details)


@pytest.mark.parametrize(
    "schema,code",
    [
        ({"type": "array"}, "tool_input_schema_not_object"),
        ({}, "tool_input_schema_not_object"),
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


@pytest.mark.parametrize("name", ["", "1search", "search-resources", "a" * 65, "search resources"])
def test_tool_names_use_the_common_provider_pattern(name: str) -> None:
    with pytest.raises(ValidationError):
        ToolDeclaration(name=name, description="Search.", input_schema={"type": "object"})


def test_registered_conversation_codes_have_safe_messages() -> None:
    for code in (
        "tool_input_schema_not_object",
        "tool_input_schema_too_large",
        "tool_arguments_too_large",
        "assistant_message_empty",
        "conversation_system_message_position",
        "conversation_tool_call_id_duplicate",
        "conversation_tool_result_orphan",
        "conversation_tool_result_duplicate",
    ):
        assert SAFE_VALIDATION_MESSAGES[code].startswith("Conversation constraint failed:")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"limit": 5}', ParsedToolArguments(value={"limit": 5})),
        ({"limit": 5}, ParsedToolArguments(value={"limit": 5})),
        ("{}", ParsedToolArguments(value={})),
        ("{not json", MalformedToolArguments(reason=MalformedArgumentsReason.INVALID_JSON)),
        ('{"a": 1, "a": 2}', MalformedToolArguments(reason=MalformedArgumentsReason.INVALID_JSON)),
        ('{"a": NaN}', MalformedToolArguments(reason=MalformedArgumentsReason.INVALID_JSON)),
        (
            '{"a":' * 2_500 + "1" + "}" * 2_500,
            MalformedToolArguments(reason=MalformedArgumentsReason.INVALID_JSON),
        ),
        ({"bad": {1, 2}}, MalformedToolArguments(reason=MalformedArgumentsReason.INVALID_JSON)),
        (
            {"bad": float("inf")},
            MalformedToolArguments(reason=MalformedArgumentsReason.INVALID_JSON),
        ),
        ("[1, 2]", MalformedToolArguments(reason=MalformedArgumentsReason.NOT_OBJECT)),
        ('"text"', MalformedToolArguments(reason=MalformedArgumentsReason.NOT_OBJECT)),
        (
            json.dumps({"q": "x" * MAX_TOOL_ARGUMENT_BYTES}),
            MalformedToolArguments(reason=MalformedArgumentsReason.TOO_LARGE),
        ),
        (
            {"q": "x" * MAX_TOOL_ARGUMENT_BYTES},
            MalformedToolArguments(reason=MalformedArgumentsReason.TOO_LARGE),
        ),
    ],
    ids=lambda value: repr(value)[:40] if isinstance(value, (str, dict)) else None,
)
def test_parse_tool_arguments_classifies_without_raising(raw, expected) -> None:
    assert parse_tool_arguments(raw) == expected


def test_malformed_arguments_do_not_retain_raw_text() -> None:
    malformed = parse_tool_arguments(f"{{{SECRET}")
    assert SECRET not in malformed.model_dump_json()
