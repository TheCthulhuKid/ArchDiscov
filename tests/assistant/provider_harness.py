"""Provider harnesses for the shared contract suite.

A harness builds a provider that behaves as a list of fake-transcript turns describes,
and reports how many attempts reached it. The turns are the provider-neutral scenario
format: the fake replays them directly, and each real adapter (steps 40-42) adds a
harness that turns them into mocked transport responses for its mapping layer.
"""

from collections.abc import Callable, Sequence
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
    ProviderErrorCode,
    StopReason,
)


class ProviderHarness(Protocol):
    name: str

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


type Served = httpx2.Response | Exception | Callable[[httpx2.Request], httpx2.Response]


class MockApi(httpx2.MockTransport):
    """A mocked API: answers each request with the next response, and keeps the requests.

    A response may be a function of the request, or an exception, raised as the transport
    would. A `DelayedResponse` whose delay exceeds the request's read timeout is a read
    timeout.
    """

    def __init__(self, responses: Sequence[Served]) -> None:
        self.requests: list[httpx2.Request] = []
        self._responses = list(responses)
        super().__init__(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        response = self._responses.pop(0)
        if callable(response):
            response = response(request)
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


def _status(status: int, error_type: str) -> Callable[[httpx2.Request], httpx2.Response]:
    """An API error whose message echoes the request, so a leak of it would show."""

    def respond(request: httpx2.Request) -> httpx2.Response:
        error = {"type": error_type, "message": f"Rejected: {request.content.decode()}"}
        return httpx2.Response(status, json={"type": "error", "error": error})

    return respond


def _transport(error: type[httpx2.TransportError]) -> Callable[[httpx2.Request], Exception]:
    return lambda request: error("scripted transport failure", request=request)


def _cut_short(stop: str) -> Callable[[httpx2.Request], httpx2.Response]:
    return lambda request: httpx2.Response(
        200, json={**messages_body(_EMPTY), "content": [], "stop_reason": stop}
    )


_EMPTY = CompletionResponse.model_validate(
    {"message": {"role": "assistant", "text": "-"}, "stop_reason": "end"}
)
# How the Messages API shows each scripted provider error.
_FAILURES: dict[ProviderErrorCode, Callable[[httpx2.Request], Any]] = {
    ProviderErrorCode.AUTHENTICATION: _status(401, "authentication_error"),
    ProviderErrorCode.RATE_LIMIT: _status(429, "rate_limit_error"),
    ProviderErrorCode.TIMEOUT: _transport(httpx2.ReadTimeout),
    ProviderErrorCode.TRANSPORT: _transport(httpx2.ConnectError),
    ProviderErrorCode.INVALID_REQUEST: _status(400, "invalid_request_error"),
    ProviderErrorCode.MALFORMED_OUTPUT: lambda request: httpx2.Response(
        200, content=b"{not json", headers={"content-type": "application/json"}
    ),
    ProviderErrorCode.TRUNCATED_OUTPUT: _cut_short("max_tokens"),
    ProviderErrorCode.REFUSAL: _cut_short("refusal"),
    ProviderErrorCode.UNAVAILABLE: _status(503, "api_error"),
}


def _no_wait(seconds: float) -> None:
    """Retries in the suite do not wait; the adapter tests check the waits."""


async def _no_wait_async(seconds: float) -> None:
    """Retries in the suite do not wait; the adapter tests check the waits."""


class AnthropicHarness:
    """The Anthropic adapter over a mocked Messages API that serves the scripted turns."""

    name = "anthropic"

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
        options: dict[str, Any] = {
            "transport": api,
            "async_transport": api,
            "sleep": _no_wait,
            "async_sleep": _no_wait_async,
        }
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
    def _served(turn: Any) -> Served:
        if isinstance(turn, FakeRespond):
            return DelayedResponse(turn.delay_seconds, json=messages_body(turn.response))
        return _FAILURES[turn.error]

    def attempts(self, provider: Provider) -> int:
        return len(self._apis[provider].requests)


HARNESSES: list[ProviderHarness] = [FakeHarness(), AnthropicHarness()]
