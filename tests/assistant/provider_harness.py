"""Provider harnesses for the shared contract suite.

A harness builds a provider that behaves as a list of fake-transcript turns describes,
and reports how many attempts reached it. The turns are the provider-neutral scenario
format: the fake replays them directly, and each real adapter (steps 40-42) adds a
harness that turns them into mocked transport responses for its mapping layer. A harness's
`name` is the provider kind a run declares. Scripted delays advance the given `FakeClock`,
so runs that depend on deadlines behave alike under every harness.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar, Protocol

import httpx2
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage
from openai.types import CompletionUsage
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessage,
    ChatCompletionMessageFunctionToolCall,
)
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message_function_tool_call import Function
from openai.types.completion_usage import PromptTokensDetails
from openai.types.responses import (
    Response,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputRefusal,
    ResponseOutputText,
    ResponseUsage,
)
from openai.types.responses.response import IncompleteDetails
from openai.types.responses.response_usage import InputTokensDetails
from pydantic import SecretStr

from archdiscov.assistant.anthropic_provider import AnthropicProvider
from archdiscov.assistant.config import OpenAIProtocol, parse_provider_config
from archdiscov.assistant.conversation import (
    MAX_TOOL_ARGUMENT_BYTES,
    MalformedArgumentsReason,
    ParsedToolArguments,
    TokenUsage,
    ToolArguments,
)
from archdiscov.assistant.fake import FakeClock, FakeProvider, FakeRespond, FakeTranscript
from archdiscov.assistant.openai_provider import OpenAIProvider
from archdiscov.assistant.provider import (
    CompletionRequest,
    CompletionResponse,
    Provider,
    ProviderCapabilities,
    ProviderErrorCode,
    StopReason,
)
from tests.support.mock_api import (
    DelayedResponse,
    MockApi,
    Served,
    no_wait,
    no_wait_async,
    status_failure,
    transport_failure,
    unparseable_body,
)


class ProviderHarness(Protocol):
    name: str
    # Tells harnesses of one provider kind apart in test IDs.
    label: str
    # False until the adapter maps provider errors, retries, and timeouts (step 41.5).
    maps_errors: bool

    def build(
        self,
        turns: Sequence[dict[str, Any]],
        *,
        capabilities: ProviderCapabilities | None = None,
        max_retries: int = 0,
        clock: FakeClock | None = None,
    ) -> Provider: ...

    def attempts(self, provider: Provider) -> int: ...


class FakeHarness:
    name = label = "fake"
    maps_errors = True

    def build(
        self,
        turns: Sequence[dict[str, Any]],
        *,
        capabilities: ProviderCapabilities | None = None,
        max_retries: int = 0,
        clock: FakeClock | None = None,
    ) -> Provider:
        data: dict[str, Any] = {"turns": list(turns)}
        if capabilities is not None:
            data["capabilities"] = capabilities.model_dump(mode="json")
        return FakeProvider(
            FakeTranscript.model_validate(data),
            clock=FakeClock() if clock is None else clock,
            max_retries=max_retries,
        )

    def attempts(self, provider: Provider) -> int:
        assert isinstance(provider, FakeProvider)
        return len(provider.requests)


ANTHROPIC_KEY = SecretStr("sk-ant-api03-HARNESSkey0123456789abcdefXYZ")
_API_STOP_REASONS = {
    StopReason.END: "end_turn",
    StopReason.TOOL_USE: "tool_use",
    StopReason.LENGTH: "max_tokens",
    StopReason.REFUSAL: "refusal",
}
# A Messages API tool input that maps back to each malformed reason.
_MALFORMED_INPUTS: dict[MalformedArgumentsReason, Any] = {
    MalformedArgumentsReason.INVALID_JSON: "{not json",
    MalformedArgumentsReason.NOT_OBJECT: [],
    MalformedArgumentsReason.TOO_LARGE: {"padding": "x" * MAX_TOOL_ARGUMENT_BYTES},
}


def _tool_input(arguments: ToolArguments) -> Any:
    if isinstance(arguments, ParsedToolArguments):
        return arguments.value
    return _MALFORMED_INPUTS[arguments.reason]


def messages_body(response: CompletionResponse) -> dict[str, Any]:
    """The Messages API body that the Anthropic adapter maps back to `response`."""
    message = response.message
    if message.continuation is not None:
        if message.continuation.provider_kind != "anthropic":
            raise ValueError("the Messages API returns only its own continuations")
        content: list[Any] = list(message.continuation.blocks)
    else:
        content = [] if message.text is None else [TextBlock(type="text", text=message.text)]
        content += [
            ToolUseBlock.model_construct(
                type="tool_use", id=call.id, name=call.name, input=_tool_input(call.arguments)
            )
            for call in message.tool_calls
        ]
    usage = response.usage
    counts = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "cache_creation_input_tokens": usage.cache_write_input_tokens,
    }
    return Message.model_construct(
        id="msg_harness",
        type="message",
        role="assistant",
        model="claude-sonnet-5-5",
        content=content,
        stop_reason=_API_STOP_REASONS[response.stop_reason],
        stop_sequence=None,
        usage=Usage.model_construct(**{k: v for k, v in counts.items() if v is not None}),
        # Malformed inputs break the SDK's types on purpose; serialise them as they are.
    ).model_dump(mode="json", exclude_unset=True, warnings=False)


class ScopedProvider:
    """A provider with scripted capabilities, for the capability cases of the suite."""

    def __init__(self, provider: Provider, capabilities: ProviderCapabilities) -> None:
        self._provider = provider
        self._capabilities = capabilities

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        return self._provider.complete(request)

    async def complete_async(self, request: CompletionRequest) -> CompletionResponse:
        return await self._provider.complete_async(request)


class MockedHarness:
    """An adapter over a mocked API that serves fake turns in its protocol's shape.

    A subclass names the provider kind, builds the protocol's response `body`, shows each
    scripted provider error as the API would (`failures`), and builds its `adapter`.
    """

    name: ClassVar[str]
    failures: ClassVar[Mapping[ProviderErrorCode, Served]]
    maps_errors: ClassVar[bool] = True

    def __init__(self) -> None:
        self._apis: dict[Provider, MockApi] = {}

    @property
    def label(self) -> str:
        return self.name

    @staticmethod
    def body(response: CompletionResponse) -> dict[str, Any]:
        raise NotImplementedError

    def adapter(self, api: MockApi, max_retries: int) -> Provider:
        raise NotImplementedError

    def build(
        self,
        turns: Sequence[dict[str, Any]],
        *,
        capabilities: ProviderCapabilities | None = None,
        max_retries: int = 0,
        clock: FakeClock | None = None,
    ) -> Provider:
        api = self.api(turns, clock)
        provider = self.adapter(api, max_retries)
        if capabilities is not None:
            provider = ScopedProvider(provider, capabilities)
        self._apis[provider] = api
        return provider

    @classmethod
    def api(cls, turns: Sequence[dict[str, Any]], clock: FakeClock | None = None) -> MockApi:
        """A mocked API that serves the turns, for callers that build the adapter."""
        transcript = FakeTranscript.model_validate({"turns": list(turns)})
        return MockApi([cls._served(turn) for turn in transcript.turns], clock)

    @classmethod
    def _served(cls, turn: Any) -> Served:
        if isinstance(turn, FakeRespond):
            return DelayedResponse(turn.delay_seconds, json=cls.body(turn.response))
        return cls.failures[turn.error]

    def attempts(self, provider: Provider) -> int:
        return len(self._apis[provider].requests)


def _anthropic_error(status: int, error_type: str) -> Served:
    return status_failure(
        status, lambda message: {"type": "error", "error": {"type": error_type, "message": message}}
    )


def _cut_short(stop: str) -> Callable[[httpx2.Request], httpx2.Response]:
    return lambda request: httpx2.Response(
        200, json={**messages_body(_EMPTY), "content": [], "stop_reason": stop}
    )


_EMPTY = CompletionResponse.model_validate(
    {"message": {"role": "assistant", "text": "-"}, "stop_reason": "end"}
)


class AnthropicHarness(MockedHarness):
    """The Anthropic adapter over a mocked Messages API that serves the scripted turns."""

    name = "anthropic"
    # How the Messages API shows each scripted provider error.
    failures = {
        ProviderErrorCode.AUTHENTICATION: _anthropic_error(401, "authentication_error"),
        ProviderErrorCode.RATE_LIMIT: _anthropic_error(429, "rate_limit_error"),
        ProviderErrorCode.TIMEOUT: transport_failure(httpx2.ReadTimeout),
        ProviderErrorCode.TRANSPORT: transport_failure(httpx2.ConnectError),
        ProviderErrorCode.INVALID_REQUEST: _anthropic_error(400, "invalid_request_error"),
        ProviderErrorCode.MALFORMED_OUTPUT: unparseable_body,
        ProviderErrorCode.TRUNCATED_OUTPUT: _cut_short("max_tokens"),
        ProviderErrorCode.REFUSAL: _cut_short("refusal"),
        ProviderErrorCode.UNAVAILABLE: _anthropic_error(503, "api_error"),
    }
    body = staticmethod(messages_body)

    def adapter(self, api: MockApi, max_retries: int) -> Provider:
        config = parse_provider_config(
            {
                "kind": "anthropic",
                "model": "claude-sonnet-5-5",
                "api_key_env": "ANTHROPIC_API_KEY",
                "max_retries": max_retries,
            }
        )
        return AnthropicProvider(
            config,
            ANTHROPIC_KEY,
            transport=api,
            async_transport=api,
            sleep=no_wait,
            async_sleep=no_wait_async,
        )


OPENAI_KEY = SecretStr("sk-proj-HARNESSkey0123456789abcdefXYZ")
_FINISH_REASONS = {
    StopReason.END: "stop",
    StopReason.TOOL_USE: "tool_calls",
    StopReason.LENGTH: "length",
    # A refusal is the message's `refusal` field, whatever the finish reason.
    StopReason.REFUSAL: "stop",
}
# Chat Completions arguments text that maps back to each malformed reason.
_MALFORMED_ARGUMENTS: dict[MalformedArgumentsReason, str] = {
    MalformedArgumentsReason.INVALID_JSON: "{not json",
    MalformedArgumentsReason.NOT_OBJECT: "[]",
    MalformedArgumentsReason.TOO_LARGE: json.dumps({"padding": "x" * MAX_TOOL_ARGUMENT_BYTES}),
}


def _arguments_text(arguments: ToolArguments) -> str:
    if isinstance(arguments, ParsedToolArguments):
        return json.dumps(arguments.value)
    return _MALFORMED_ARGUMENTS[arguments.reason]


def _usage(
    usage: TokenUsage, usage_type: type, details_type: type, input_name: str, output_name: str
) -> Any:
    """The protocol's usage object for `usage`, holding only the reported counts."""
    if usage == TokenUsage():
        return None
    details = {
        "cached_tokens": usage.cache_read_input_tokens,
        "cache_write_tokens": usage.cache_write_input_tokens,
    }
    return usage_type.model_construct(
        **{
            f"{input_name}_tokens": usage.input_tokens,
            f"{output_name}_tokens": usage.output_tokens,
            f"{input_name}_tokens_details": details_type.model_construct(
                **{k: v for k, v in details.items() if v is not None}
            ),
        }
    )


def chat_completion_body(response: CompletionResponse) -> dict[str, Any]:
    """The Chat Completions body that the OpenAI-compatible adapter maps back to `response`."""
    message = response.message
    if message.continuation is not None:
        raise ValueError("Chat Completions returns no continuations")
    refused = response.stop_reason == StopReason.REFUSAL
    fields: dict[str, Any] = {"role": "assistant", "content": None if refused else message.text}
    if refused:
        fields["refusal"] = message.text
    if message.tool_calls:
        fields["tool_calls"] = [
            ChatCompletionMessageFunctionToolCall(
                type="function",
                id=call.id,
                function=Function(name=call.name, arguments=_arguments_text(call.arguments)),
            )
            for call in message.tool_calls
        ]
    choice = Choice.model_construct(
        index=0,
        finish_reason=_FINISH_REASONS[response.stop_reason],
        message=ChatCompletionMessage.model_construct(**fields),
    )
    body = ChatCompletion.model_construct(
        id="chatcmpl_harness",
        object="chat.completion",
        created=0,
        model="gpt-5",
        choices=[choice],
    )
    body.usage = _usage(
        response.usage, CompletionUsage, PromptTokensDetails, "prompt", "completion"
    )
    return body.model_dump(mode="json", exclude_unset=True)


def _openai_error(status: int, error_type: str) -> Served:
    return status_failure(
        status, lambda message: {"error": {"message": message, "type": error_type, "code": None}}
    )


def responses_body(response: CompletionResponse) -> dict[str, Any]:
    """The Responses body that the OpenAI-compatible adapter maps back to `response`."""
    message = response.message
    if message.continuation is not None:
        if message.continuation.provider_kind != "openai_compatible":
            raise ValueError("the Responses API returns only its own continuations")
        output: list[Any] = list(message.continuation.blocks)
    else:
        output = [] if message.text is None else [_output_message(response)]
        output += [
            ResponseFunctionToolCall.model_construct(
                type="function_call",
                id=f"fc_{call.id}",
                call_id=call.id,
                name=call.name,
                arguments=_arguments_text(call.arguments),
            )
            for call in message.tool_calls
        ]
    body = Response.model_construct(
        id="resp_harness", object="response", created_at=0, model="gpt-5", output=output
    )
    if response.stop_reason == StopReason.LENGTH:
        body.status = "incomplete"
        body.incomplete_details = IncompleteDetails(reason="max_output_tokens")
    else:
        body.status = "completed"
    body.usage = _usage(response.usage, ResponseUsage, InputTokensDetails, "input", "output")
    # Malformed arguments and verbatim output break the SDK's types on purpose.
    return body.model_dump(mode="json", exclude_unset=True, warnings=False)


def _output_message(response: CompletionResponse) -> ResponseOutputMessage:
    """The message item, its text an `output_text` part or, for a refusal, a `refusal` one."""
    text = response.message.text
    part: Any = (
        ResponseOutputRefusal(type="refusal", refusal=text)
        if response.stop_reason == StopReason.REFUSAL
        else ResponseOutputText(type="output_text", text=text, annotations=[])
    )
    return ResponseOutputMessage(
        type="message", id="msg_harness", role="assistant", status="completed", content=[part]
    )


def _finished(reason: str) -> Callable[[httpx2.Request], httpx2.Response]:
    body = chat_completion_body(_EMPTY)
    choice = {**body["choices"][0], "finish_reason": reason, "message": {"role": "assistant"}}
    return lambda request: httpx2.Response(200, json={**body, "choices": [choice]})


def _incomplete(reason: str) -> Callable[[httpx2.Request], httpx2.Response]:
    body = {
        **responses_body(_EMPTY),
        "status": "incomplete",
        "incomplete_details": {"reason": reason},
        "output": [],
    }
    return lambda request: httpx2.Response(200, json=body)


def _openai_failures(truncated: Served, refused: Served) -> dict[ProviderErrorCode, Served]:
    """How an OpenAI-compatible API shows each scripted provider error."""
    return {
        ProviderErrorCode.AUTHENTICATION: _openai_error(401, "invalid_request_error"),
        ProviderErrorCode.RATE_LIMIT: _openai_error(429, "requests"),
        ProviderErrorCode.TIMEOUT: transport_failure(httpx2.ReadTimeout),
        ProviderErrorCode.TRANSPORT: transport_failure(httpx2.ConnectError),
        ProviderErrorCode.INVALID_REQUEST: _openai_error(400, "invalid_request_error"),
        ProviderErrorCode.MALFORMED_OUTPUT: unparseable_body,
        ProviderErrorCode.TRUNCATED_OUTPUT: truncated,
        ProviderErrorCode.REFUSAL: refused,
        ProviderErrorCode.UNAVAILABLE: _openai_error(503, "server_error"),
    }


class OpenAIHarness(MockedHarness):
    """The OpenAI-compatible adapter over a mocked API of one protocol."""

    name = "openai_compatible"
    maps_errors = False
    protocol: ClassVar[OpenAIProtocol]

    @property
    def label(self) -> str:
        return self.protocol.value

    def adapter(self, api: MockApi, max_retries: int) -> Provider:
        config = parse_provider_config(
            {
                "kind": "openai_compatible",
                "model": "gpt-5",
                "api_key_env": "OPENAI_API_KEY",
                "endpoint": "https://api.openai.com/v1",
                "protocol": self.protocol.value,
                "capabilities": ["tool_calling", "parallel_tool_calls"],
                "max_retries": max_retries,
            }
        )
        return OpenAIProvider(config, OPENAI_KEY, transport=api, async_transport=api)


class ChatCompletionsHarness(OpenAIHarness):
    protocol = OpenAIProtocol.CHAT_COMPLETIONS
    failures = _openai_failures(_finished("length"), _finished("content_filter"))
    body = staticmethod(chat_completion_body)


class ResponsesHarness(OpenAIHarness):
    protocol = OpenAIProtocol.RESPONSES
    failures = _openai_failures(_incomplete("max_output_tokens"), _incomplete("content_filter"))
    body = staticmethod(responses_body)


HARNESSES: list[ProviderHarness] = [FakeHarness(), AnthropicHarness()]
# The orchestration and answer-service suites run the OpenAI-compatible adapter from 41.5.
CONTRACT_HARNESSES: list[ProviderHarness] = [
    *HARNESSES,
    ChatCompletionsHarness(),
    ResponsesHarness(),
]
