"""The Anthropic Messages API adapter: the only assistant module that imports the SDK.

The client is built only from the configuration. The SDK would otherwise read the
environment for its credential, base URL, extra headers, proxies, tracing, and logging,
so the adapter passes the key and base URL explicitly, and sends every request through a
transport that keeps only the headers the SDK itself needs and sets the credential last;
`httpx2` applies no environment proxy to a client given its own transport. Redirects and
OpenTelemetry spans are off, and the SDK's loggers stay at `WARNING`, because their debug
records hold request bodies.

Building the adapter makes no request. Each `complete` maps the conversation to one
Messages API request and the response back to the shared contracts. The response is
untrusted: only text, tool-use, and reasoning blocks are mapped, and anything else is
malformed output. Reasoning blocks must go back unchanged and in their original order, so
a response holding them becomes a continuation of its whole content, replayed verbatim.

Failures map to typed provider errors by the SDK's exception classes, never by message text,
and chain the SDK exception without copying its text. Only transport and rate-limit errors
(including overload) are retried, up to `max_retries`, after a bounded exponential backoff
or the server's `retry-after`, never longer than the request timeout.
"""

import asyncio
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from itertools import count
from typing import Any

import anthropic
import httpx2
from anthropic.types import (
    MessageParam,
    TextBlockParam,
    ToolChoiceAutoParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
)
from pydantic import SecretStr, ValidationError

from .config import AnthropicProviderConfig, ProviderKind
from .conversation import (
    AssistantMessage,
    Message,
    ParsedToolArguments,
    ProviderContinuation,
    SystemMessage,
    TokenUsage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    parse_tool_arguments,
)
from .provider import (
    CompletionRequest,
    CompletionResponse,
    ProviderAuthenticationError,
    ProviderCapabilities,
    ProviderCapability,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderMalformedOutputError,
    ProviderRateLimitError,
    ProviderRefusalError,
    ProviderTimeoutError,
    ProviderTransportError,
    ProviderTruncatedOutputError,
    ProviderUnavailableError,
    StopReason,
)

DEFAULT_BASE_URL = "https://api.anthropic.com"

# What the SDK sends on its own; anything else (such as `ANTHROPIC_CUSTOM_HEADERS`) is dropped.
_SDK_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "anthropic-version",
        "connection",
        "content-length",
        "content-type",
        "host",
        "user-agent",
    }
)
_SDK_HEADER_PREFIX = "x-stainless-"
_SDK_LOGGERS = ("anthropic", "httpx2")

_KIND = ProviderKind.ANTHROPIC.value
_REASONING_BLOCKS = frozenset({"thinking", "redacted_thinking"})
_KNOWN_BLOCKS = _REASONING_BLOCKS | {"text", "tool_use"}
# `model_context_window_exceeded` cuts the output short, as `max_tokens` does.
_STOP_REASONS: Mapping[str, StopReason] = {
    "end_turn": StopReason.END,
    "stop_sequence": StopReason.END,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.LENGTH,
    "model_context_window_exceeded": StopReason.LENGTH,
    "refusal": StopReason.REFUSAL,
}
# The provider error for each SDK failure, found through the failure's class hierarchy, so a
# subclass (such as `APITimeoutError` of `APIConnectionError`) maps before its base.
SDK_ERRORS: Mapping[type[Exception], type[ProviderError]] = {
    anthropic.BadRequestError: ProviderInvalidRequestError,
    anthropic.NotFoundError: ProviderInvalidRequestError,
    anthropic.RequestTooLargeError: ProviderInvalidRequestError,
    anthropic.UnprocessableEntityError: ProviderInvalidRequestError,
    anthropic.AuthenticationError: ProviderAuthenticationError,
    anthropic.PermissionDeniedError: ProviderAuthenticationError,
    anthropic.RateLimitError: ProviderRateLimitError,
    anthropic.OverloadedError: ProviderRateLimitError,
    anthropic.InternalServerError: ProviderUnavailableError,
    anthropic.ServiceUnavailableError: ProviderUnavailableError,
    anthropic.DeadlineExceededError: ProviderUnavailableError,
    anthropic.APIStatusError: ProviderInvalidRequestError,
    anthropic.APITimeoutError: ProviderTimeoutError,
    anthropic.APIConnectionError: ProviderTransportError,
    anthropic.APIResponseValidationError: ProviderMalformedOutputError,
    json.JSONDecodeError: ProviderMalformedOutputError,
    anthropic.APIError: ProviderUnavailableError,
}
_SDK_FAILURES = (anthropic.APIError, json.JSONDecodeError)
RETRY_BACKOFF_SECONDS = 0.5
MAX_RETRY_WAIT_SECONDS = 30.0

# These stops are usable only as text without tool calls; otherwise they are this error.
_TEXT_ONLY_STOPS: Mapping[StopReason, type[ProviderError]] = {
    StopReason.LENGTH: ProviderTruncatedOutputError,
    StopReason.REFUSAL: ProviderRefusalError,
}


def _guard(request: httpx2.Request, api_key: SecretStr) -> httpx2.Request:
    """Keep only the SDK's own headers, then send the configured credential and no other."""
    for name in list(request.headers):
        if name not in _SDK_HEADERS and not name.startswith(_SDK_HEADER_PREFIX):
            del request.headers[name]
    request.headers["authorization"] = f"Bearer {api_key.get_secret_value()}"
    return request


class _GuardedTransport(httpx2.BaseTransport):
    def __init__(self, inner: httpx2.BaseTransport, api_key: SecretStr) -> None:
        self._inner = inner
        self._api_key = api_key

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        return self._inner.handle_request(_guard(request, self._api_key))

    def close(self) -> None:
        self._inner.close()


class _AsyncGuardedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, inner: httpx2.AsyncBaseTransport, api_key: SecretStr) -> None:
        self._inner = inner
        self._api_key = api_key

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return await self._inner.handle_async_request(_guard(request, self._api_key))

    async def aclose(self) -> None:
        await self._inner.aclose()


def _quiet_sdk_loggers() -> None:
    """Hold the SDK's loggers at `WARNING`, whatever `ANTHROPIC_LOG` or the root level says."""
    for name in _SDK_LOGGERS:
        logger = logging.getLogger(name)
        if logger.getEffectiveLevel() < logging.WARNING:
            logger.setLevel(logging.WARNING)


class AnthropicProvider:
    """A `Provider` over the Messages API, built from one configuration and its key.

    `transport` and `async_transport` replace the network transports, so tests can mock
    the API; the guard wraps them as it wraps the real ones. `sleep` and `async_sleep` wait
    between retries.
    """

    def __init__(
        self,
        config: AnthropicProviderConfig,
        api_key: SecretStr,
        *,
        transport: httpx2.BaseTransport | None = None,
        async_transport: httpx2.AsyncBaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        _quiet_sdk_loggers()
        self.model = config.model
        self._max_retries = config.max_retries
        self._sleep = sleep
        self._async_sleep = async_sleep
        self._connect_timeout = config.connect_timeout_seconds
        self.base_url = DEFAULT_BASE_URL if config.endpoint is None else str(config.endpoint)
        timeout = httpx2.Timeout(
            config.request_timeout_seconds, connect=config.connect_timeout_seconds
        )
        client_options: dict[str, Any] = {"timeout": timeout, "follow_redirects": False}
        sdk_options: dict[str, Any] = {
            "api_key": api_key.get_secret_value(),
            "base_url": self.base_url,
            "timeout": timeout,
            "max_retries": 0,
            "open_telemetry": False,
        }
        self._client = anthropic.Anthropic(
            **sdk_options,
            http_client=httpx2.Client(
                transport=_GuardedTransport(transport or httpx2.HTTPTransport(), api_key),
                **client_options,
            ),
        )
        self._async_client = anthropic.AsyncAnthropic(
            **sdk_options,
            http_client=httpx2.AsyncClient(
                transport=_AsyncGuardedTransport(
                    async_transport or httpx2.AsyncHTTPTransport(), api_key
                ),
                **client_options,
            ),
        )

    def __repr__(self) -> str:
        return f"AnthropicProvider(model={self.model!r}, base_url={self.base_url!r})"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            capabilities=frozenset(
                {ProviderCapability.TOOL_CALLING, ProviderCapability.PARALLEL_TOOL_CALLS}
            )
        )

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        params = self._params(request)
        # The last attempt's failure is raised by `_retry_wait`, which ends the loop.
        for attempt in count():
            try:
                return _response(self._client.messages.create(**params))
            except _SDK_FAILURES as error:
                self._sleep(self._retry_wait(error, attempt, request))

    async def complete_async(self, request: CompletionRequest) -> CompletionResponse:
        params = self._params(request)
        for attempt in count():
            try:
                return _response(await self._async_client.messages.create(**params))
            except _SDK_FAILURES as error:
                await self._async_sleep(self._retry_wait(error, attempt, request))

    def _retry_wait(self, error: Exception, attempt: int, request: CompletionRequest) -> float:
        """Seconds to wait before the next attempt; raises the provider error when final."""
        failure = provider_error(error)
        if not failure.retryable or attempt >= self._max_retries:
            raise failure from error
        return retry_wait(error, attempt, request.limits.timeout_seconds)

    def _params(self, request: CompletionRequest) -> dict[str, Any]:
        """The `messages.create` arguments for one request; no sampling or forced tool choice."""
        limits = request.limits
        messages = request.conversation.messages
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": limits.max_output_tokens,
            "messages": _message_params(m for m in messages if not isinstance(m, SystemMessage)),
            # An explicit timeout also stops the SDK demanding streaming for a large max_tokens.
            "timeout": httpx2.Timeout(limits.timeout_seconds, connect=self._connect_timeout),
        }
        system = [_text_block(m.text) for m in messages if isinstance(m, SystemMessage)]
        if system:
            params["system"] = system
        if request.tools:
            params["tools"] = [
                ToolParam(
                    name=tool.name, description=tool.description, input_schema=tool.input_schema
                )
                for tool in request.tools
            ]
            params["tool_choice"] = ToolChoiceAutoParam(
                type="auto", disable_parallel_tool_use=limits.max_tool_calls_per_turn == 1
            )
        return params


# ---- failures -------------------------------------------------------------------------


def provider_error(error: Exception) -> ProviderError:
    """The typed, fixed-message error for an SDK failure."""
    return next(SDK_ERRORS[cls] for cls in type(error).__mro__ if cls in SDK_ERRORS)()


def retry_wait(error: Exception, attempt: int, timeout_seconds: float) -> float:
    """The server's `retry-after`, else exponential backoff, capped by the request timeout."""
    wait = _retry_after(error)
    if wait is None:
        wait = RETRY_BACKOFF_SECONDS * 2**attempt
    return min(wait, MAX_RETRY_WAIT_SECONDS, timeout_seconds)


def _retry_after(error: Exception) -> float | None:
    """`retry-after` in seconds, when the response sent a usable one."""
    response = getattr(error, "response", None)
    value = None if response is None else response.headers.get("retry-after")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


# ---- requests -------------------------------------------------------------------------


def _text_block(text: str) -> TextBlockParam:
    return TextBlockParam(type="text", text=text)


def _user_blocks(message: UserMessage) -> list[Any]:
    return [_text_block(message.text)]


def _result_blocks(message: ToolResultMessage) -> list[Any]:
    return [
        ToolResultBlockParam(
            type="tool_result",
            tool_use_id=result.call_id,
            content=result.content,
            is_error=result.is_error,
        )
        for result in message.results
    ]


def _assistant_blocks(message: AssistantMessage) -> list[Any]:
    """The response's own content when it carried reasoning, else rebuilt in order."""
    if message.continuation is not None:
        if message.continuation.provider_kind != _KIND:
            raise ProviderInvalidRequestError()
        return list(message.continuation.blocks)
    blocks: list[Any] = [] if message.text is None else [_text_block(message.text)]
    blocks += [
        ToolUseBlockParam(
            type="tool_use",
            id=call.id,
            name=call.name,
            # Malformed arguments were never run; the call is replayed with no input.
            input=call.arguments.value if isinstance(call.arguments, ParsedToolArguments) else {},
        )
        for call in message.tool_calls
    ]
    return blocks


_ROLES: Mapping[type, tuple[str, Callable[[Any], list[Any]]]] = {
    UserMessage: ("user", _user_blocks),
    ToolResultMessage: ("user", _result_blocks),
    AssistantMessage: ("assistant", _assistant_blocks),
}


def _message_params(messages: Iterable[Message]) -> list[MessageParam]:
    """One API message per role turn: consecutive user messages, such as tool results and a
    repair prompt, share one."""
    params: list[MessageParam] = []
    for message in messages:
        role, blocks = _ROLES[type(message)]
        if params and params[-1]["role"] == role:
            params[-1]["content"] += blocks(message)
        else:
            params.append({"role": role, "content": blocks(message)})
    return params


# ---- responses ------------------------------------------------------------------------


def _response(message: anthropic.types.Message) -> CompletionResponse:
    """The shared contract for an untrusted response; anything that does not fit is malformed."""
    try:
        return _mapped(message)
    except (ValidationError, AttributeError, TypeError) as error:
        raise ProviderMalformedOutputError() from error


def _mapped(message: anthropic.types.Message) -> CompletionResponse:
    stop = _STOP_REASONS.get(message.stop_reason)
    blocks = message.content
    if stop is None or any(block.type not in _KNOWN_BLOCKS for block in blocks):
        raise ProviderMalformedOutputError()
    text = "".join(block.text for block in blocks if block.type == "text") or None
    tool_uses = [block for block in blocks if block.type == "tool_use"]
    if stop in _TEXT_ONLY_STOPS and (tool_uses or text is None):
        raise _TEXT_ONLY_STOPS[stop]()
    return CompletionResponse(
        message=AssistantMessage(
            text=text,
            tool_calls=tuple(
                ToolCall(id=use.id, name=use.name, arguments=parse_tool_arguments(use.input))
                for use in tool_uses
            ),
            continuation=_continuation(blocks),
        ),
        usage=_usage(getattr(message, "usage", None)),
        stop_reason=stop,
    )


def _continuation(blocks: list[Any]) -> ProviderContinuation | None:
    if not any(block.type in _REASONING_BLOCKS for block in blocks):
        return None
    return ProviderContinuation(
        provider_kind=_KIND,
        blocks=tuple(block.model_dump(mode="json", exclude_unset=True) for block in blocks),
    )


def _usage(usage: Any) -> TokenUsage:
    return TokenUsage(
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", None),
        cache_write_input_tokens=getattr(usage, "cache_creation_input_tokens", None),
    )
