"""Conversation and tool-call contracts shared by every provider.

These model the lowest common shape of the Anthropic, OpenAI, and Bedrock formats;
vendor-specific fields belong to adapters. Blocks a provider needs back unchanged, such as
reasoning, ride on an assistant message as an opaque `ProviderContinuation` that only that
provider's adapter reads. The structured answer proposal is not
defined here: `answers.py` holds it, with the `AnalysisStatus` vocabulary findings use.
"""

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import ConfigDict, Field, JsonValue, NonNegativeInt, ValidationError, model_validator
from pydantic_core import PydanticSerializationError

from archdiscov.core.strict_json import reject_constant, unique_object
from archdiscov.models.resource_base import Model, NonEmptyString
from archdiscov.models.validation_errors import (
    ValidationIssue,
    safe_validation_issues,
    serialization_issue,
    validation_error,
)

MAX_TEXT_CHARS = 200_000
MAX_TOOL_DESCRIPTION_CHARS = 4_096
MAX_TOOL_INPUT_SCHEMA_BYTES = 32_768
MAX_TOOL_ARGUMENT_BYTES = 16_384
MAX_TOOL_RESULT_CHARS = 262_144
MAX_TOOL_CALL_ID_CHARS = 256
MAX_CONTINUATION_BYTES = 262_144

# Bedrock is the strictest of the three formats: a leading letter, then letters,
# digits, and underscores, at most 64 characters.
ToolName = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")]
ToolCallId = Annotated[NonEmptyString, Field(max_length=MAX_TOOL_CALL_ID_CHARS)]
MessageText = Annotated[NonEmptyString, Field(max_length=MAX_TEXT_CHARS)]
ProviderKindName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class Contract(Model):
    """Base for assistant contracts: unknown fields rejected, instances immutable."""

    model_config = ConfigDict(frozen=True)


def _json_size(value: Any) -> int:
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return len(text.encode())


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolDeclaration(Contract):
    """A tool offered to the model; the schema describes a JSON object input."""

    name: ToolName
    description: Annotated[NonEmptyString, Field(max_length=MAX_TOOL_DESCRIPTION_CHARS)]
    input_schema: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_input_schema(self) -> Self:
        if self.input_schema.get("type") != "object":
            raise validation_error("tool_input_schema_not_object", "schema type is not object")
        if _json_size(self.input_schema) > MAX_TOOL_INPUT_SCHEMA_BYTES:
            raise validation_error("tool_input_schema_too_large", "schema exceeds the limit")
        return self


class ParsedToolArguments(Contract):
    """Arguments the provider supplied as a JSON object within the size bound."""

    kind: Literal["parsed"] = "parsed"
    value: dict[str, JsonValue]

    @model_validator(mode="after")
    def bound_size(self) -> Self:
        if _json_size(self.value) > MAX_TOOL_ARGUMENT_BYTES:
            raise validation_error("tool_arguments_too_large", "arguments exceed the limit")
        return self


class MalformedArgumentsReason(StrEnum):
    INVALID_JSON = "invalid_json"
    NOT_OBJECT = "not_object"
    TOO_LARGE = "too_large"


class MalformedToolArguments(Contract):
    """Arguments that could not be used; the raw text is not retained.

    Kept as a value so the orchestrator can answer the model with a tool error.
    """

    kind: Literal["malformed"] = "malformed"
    reason: MalformedArgumentsReason


ToolArguments = Annotated[ParsedToolArguments | MalformedToolArguments, Field(discriminator="kind")]


def parse_tool_arguments(
    raw: str | Mapping[str, Any],
) -> ParsedToolArguments | MalformedToolArguments:
    """Classify provider-supplied arguments, given as JSON text or decoded; never raises.

    Text is rejected by size before decoding; duplicate keys and non-finite numbers are
    invalid JSON.
    """
    if isinstance(raw, str):
        if len(raw.encode()) > MAX_TOOL_ARGUMENT_BYTES:
            return MalformedToolArguments(reason=MalformedArgumentsReason.TOO_LARGE)
        try:
            value = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
        except (ValueError, RecursionError):
            return MalformedToolArguments(reason=MalformedArgumentsReason.INVALID_JSON)
    else:
        value = raw
    if not isinstance(value, Mapping):
        return MalformedToolArguments(reason=MalformedArgumentsReason.NOT_OBJECT)
    try:
        if _json_size(value) > MAX_TOOL_ARGUMENT_BYTES:
            return MalformedToolArguments(reason=MalformedArgumentsReason.TOO_LARGE)
        return ParsedToolArguments(value=value)
    except (ValidationError, TypeError, ValueError, RecursionError):
        return MalformedToolArguments(reason=MalformedArgumentsReason.INVALID_JSON)


class ToolCall(Contract):
    """A model request to run one tool; the call ID is assigned by the provider."""

    id: ToolCallId
    name: ToolName
    arguments: ToolArguments


class ToolResult(Contract):
    """The outcome of one tool call, sent back to the model as text."""

    call_id: ToolCallId
    content: Annotated[str, Field(max_length=MAX_TOOL_RESULT_CHARS)]
    is_error: bool = False


class TokenUsage(Contract):
    """Reported token counts; providers differ, so every field is optional."""

    input_tokens: NonNegativeInt | None = None
    output_tokens: NonNegativeInt | None = None
    cache_read_input_tokens: NonNegativeInt | None = None
    cache_write_input_tokens: NonNegativeInt | None = None


class SystemMessage(Contract):
    role: Literal[MessageRole.SYSTEM] = MessageRole.SYSTEM
    text: MessageText


class UserMessage(Contract):
    role: Literal[MessageRole.USER] = MessageRole.USER
    text: MessageText


class ProviderContinuation(Contract):
    """Blocks one provider needs back unchanged with its message, such as reasoning.

    Opaque to everything but that provider's adapter: it is not model output, so nothing
    validates, cites, renders, or records its content.
    """

    provider_kind: ProviderKindName
    blocks: tuple[dict[str, JsonValue], ...] = Field(min_length=1)

    @model_validator(mode="after")
    def bound_size(self) -> Self:
        if self.size > MAX_CONTINUATION_BYTES:
            raise validation_error("continuation_too_large", "continuation exceeds the limit")
        return self

    @property
    def size(self) -> int:
        """Bytes of the blocks as compact JSON."""
        return _json_size(self.blocks)


class AssistantMessage(Contract):
    """Model output: text, tool calls, or both, never neither, and any continuation."""

    role: Literal[MessageRole.ASSISTANT] = MessageRole.ASSISTANT
    text: MessageText | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    continuation: ProviderContinuation | None = None

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        if self.text is None and not self.tool_calls:
            raise validation_error("assistant_message_empty", "no text or tool calls")
        if len({call.id for call in self.tool_calls}) != len(self.tool_calls):
            raise validation_error("conversation_tool_call_id_duplicate", "within one message")
        return self


class ToolResultMessage(Contract):
    """Results for calls from earlier assistant messages, such as one parallel turn."""

    role: Literal[MessageRole.TOOL] = MessageRole.TOOL
    results: tuple[ToolResult, ...] = Field(min_length=1)


Message = Annotated[
    SystemMessage | UserMessage | AssistantMessage | ToolResultMessage,
    Field(discriminator="role"),
]


class Conversation(Contract):
    """An ordered exchange with structural rules every provider can represent.

    System messages lead; tool call IDs are unique across the conversation; each tool
    result answers exactly one earlier call. Run limits are enforced by the orchestrator.
    """

    messages: tuple[Message, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_structure(self) -> Self:
        issued: set[str] = set()
        answered: set[str] = set()
        leading = True
        for index, message in enumerate(self.messages):
            if isinstance(message, SystemMessage):
                if not leading:
                    raise validation_error(
                        "conversation_system_message_position", f"message {index}"
                    )
                continue
            leading = False
            if isinstance(message, AssistantMessage):
                for call in message.tool_calls:
                    if call.id in issued:
                        raise validation_error(
                            "conversation_tool_call_id_duplicate", f"message {index}"
                        )
                    issued.add(call.id)
            elif isinstance(message, ToolResultMessage):
                for result in message.results:
                    if result.call_id not in issued:
                        raise validation_error(
                            "conversation_tool_result_orphan", f"message {index}"
                        )
                    if result.call_id in answered:
                        raise validation_error(
                            "conversation_tool_result_duplicate", f"message {index}"
                        )
                    answered.add(result.call_id)
        return self


_DIAGNOSTIC_FIELDS = frozenset(
    field
    for model in (
        Conversation,
        SystemMessage,
        UserMessage,
        AssistantMessage,
        ToolResultMessage,
        ProviderContinuation,
        ToolCall,
        ToolResult,
        ToolDeclaration,
        ParsedToolArguments,
        MalformedToolArguments,
        TokenUsage,
    )
    for field in model.model_fields
)


class ConversationValidationError(Exception):
    """Safe diagnostics; input text, arguments, and exception contexts are never exposed."""

    code = "invalid_conversation"

    def __init__(self, details: tuple[ValidationIssue, ...]) -> None:
        super().__init__("Conversation validation failed.")
        self.details = details


def parse_conversation(data: Conversation | Mapping[str, Any]) -> Conversation:
    """Validate detached conversation data, reporting only codes and known field names."""
    try:
        if isinstance(data, Conversation):
            data = data.model_dump(mode="json", warnings="error")
        return Conversation.model_validate(data)
    except ValidationError as exc:
        raise ConversationValidationError(
            safe_validation_issues(exc, _DIAGNOSTIC_FIELDS, "conversation")
        ) from exc
    except (
        PydanticSerializationError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as exc:
        raise ConversationValidationError((serialization_issue("Conversation"),)) from exc
