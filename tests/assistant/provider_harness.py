"""Provider harnesses for the shared contract suite.

A harness builds a provider that behaves as a list of fake-transcript turns describes,
and reports how many attempts reached it. The turns are the provider-neutral scenario
format: the fake replays them directly, and each real adapter (steps 40-42) adds a
harness that turns them into mocked transport responses for its mapping layer.
"""

from collections.abc import Sequence
from typing import Any, Protocol

import httpx2
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage
from pydantic import SecretStr

from archdiscov.assistant.anthropic_provider import AnthropicProvider
from archdiscov.assistant.config import parse_provider_config
from archdiscov.assistant.conversation import (
    MAX_TOOL_ARGUMENT_BYTES,
    MalformedArgumentsReason,
    ParsedToolArguments,
    ToolArguments,
)
from archdiscov.assistant.fake import FakeClock, FakeProvider, FakeRespond, FakeTranscript
from archdiscov.assistant.provider import (
    CompletionResponse,
    Provider,
    ProviderCapabilities,
    StopReason,
)


class ProviderHarness(Protocol):
    name: str
    # False until the adapter maps provider errors, retries, and timeouts (step 40.4).
    maps_errors: bool

    def build(
        self,
        turns: Sequence[dict[str, Any]],
        *,
        capabilities: ProviderCapabilities | None = None,
        max_retries: int = 0,
    ) -> Provider: ...

    def attempts(self, provider: Provider) -> int: ...


class FakeHarness:
    name = "fake"
    maps_errors = True

    def build(
        self,
        turns: Sequence[dict[str, Any]],
        *,
        capabilities: ProviderCapabilities | None = None,
        max_retries: int = 0,
    ) -> Provider:
        data: dict[str, Any] = {"turns": list(turns)}
        if capabilities is not None:
            data["capabilities"] = capabilities.model_dump(mode="json")
        return FakeProvider(
            FakeTranscript.model_validate(data), clock=FakeClock(), max_retries=max_retries
        )

    def attempts(self, provider: Provider) -> int:
        assert isinstance(provider, FakeProvider)
        return len(provider.requests)


class MockApi(httpx2.MockTransport):
    """A mocked API: answers each request with the next response, and keeps the requests.

    A response may be an exception, raised as the transport would. A `DelayedResponse` whose
    delay exceeds the request's read timeout is a read timeout.
    """

    def __init__(self, responses: Sequence[httpx2.Response | Exception]) -> None:
        self.requests: list[httpx2.Request] = []
        self._responses = list(responses)
        super().__init__(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if getattr(response, "delay", 0.0) > request.extensions["timeout"]["read"]:
            raise httpx2.ReadTimeout("scripted delay", request=request)
        return response


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


class DelayedResponse(httpx2.Response):
    """A response the API sends after `delay` seconds, without waiting in the test."""

    def __init__(self, delay: float, **kwargs: Any) -> None:
        super().__init__(200, **kwargs)
        self.delay = delay


class _ScopedAnthropicProvider(AnthropicProvider):
    """The adapter with scripted capabilities, for the capability cases of the suite."""

    def __init__(self, *args: Any, capabilities: ProviderCapabilities, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._capabilities = capabilities

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities


class AnthropicHarness:
    """The Anthropic adapter over a mocked Messages API that serves the scripted turns."""

    name = "anthropic"
    maps_errors = False

    def __init__(self) -> None:
        self._apis: dict[Provider, MockApi] = {}

    def build(
        self,
        turns: Sequence[dict[str, Any]],
        *,
        capabilities: ProviderCapabilities | None = None,
        max_retries: int = 0,
    ) -> Provider:
        transcript = FakeTranscript.model_validate({"turns": list(turns)})
        api = MockApi([self._served(turn) for turn in transcript.turns])
        config = parse_provider_config(
            {
                "kind": "anthropic",
                "model": "claude-sonnet-5-5",
                "api_key_env": "ANTHROPIC_API_KEY",
                "max_retries": max_retries,
            }
        )
        options: dict[str, Any] = {"transport": api, "async_transport": api}
        provider = (
            AnthropicProvider(config, ANTHROPIC_KEY, **options)
            if capabilities is None
            else _ScopedAnthropicProvider(
                config, ANTHROPIC_KEY, capabilities=capabilities, **options
            )
        )
        self._apis[provider] = api
        return provider

    @staticmethod
    def _served(turn: Any) -> httpx2.Response:
        if not isinstance(turn, FakeRespond):
            raise NotImplementedError("scripted provider errors arrive with step 40.4")
        return DelayedResponse(turn.delay_seconds, json=messages_body(turn.response))

    def attempts(self, provider: Provider) -> int:
        return len(self._apis[provider].requests)


HARNESSES: list[ProviderHarness] = [FakeHarness(), AnthropicHarness()]
