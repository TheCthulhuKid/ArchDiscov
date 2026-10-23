"""The OpenAI-compatible adapter: the only assistant module that imports the `openai` SDK.

One adapter serves both protocols; the configured `protocol` chooses the endpoint, and is
never guessed from the server. The client is built only from the configuration: the key
and the required `endpoint` are passed explicitly, and requests go through the shared
guarded clients (`adapters.py`), so `OPENAI_BASE_URL`, the organisation and project
variables, `OPENAI_CUSTOM_HEADERS`, and an admin key cannot change the target, headers, or
credential. The SDK's loggers stay at `WARNING`, whatever `OPENAI_LOG` says.

A compatible server cannot be asked what its model supports, so the capabilities are the
ones the configuration declares, and a request needing any other is refused before a call.

Building the adapter makes no request. Each `complete` maps the conversation to one request
of the configured protocol and the response back to the shared contracts, through that
protocol's pair of functions in `_MAPPINGS`. The response is untrusted: only the fields the
protocol defines are read, and anything that does not fit is malformed output. Chat
Completions has no reasoning to replay, so it never sets a continuation and refuses one.
Until the Responses mapping arrives (step 41.4), a `responses` configuration raises
`ProviderUnavailableError` without sending anything, and until the failure table arrives
(step 41.5), every SDK failure but an unparseable body is `provider_unavailable`.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx2
import openai
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionAssistantMessageParam,
    ChatCompletionFunctionToolParam,
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionUserMessageParam,
)
from pydantic import SecretStr

from .adapters import (
    HTTP_HEADERS,
    HeaderGuard,
    Retries,
    guarded_http_clients,
    quiet_loggers,
    require_known,
    require_text_only,
    untrusted_response,
)
from .config import OpenAICompatibleProviderConfig, OpenAIProtocol
from .conversation import (
    AssistantMessage,
    Message,
    ParsedToolArguments,
    SystemMessage,
    TokenUsage,
    ToolArguments,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    parse_tool_arguments,
)
from .provider import (
    CompletionRequest,
    CompletionResponse,
    ProviderCapabilities,
    ProviderCapability,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderMalformedOutputError,
    ProviderRefusalError,
    ProviderUnavailableError,
    StopReason,
)

_SDK_HEADER_PREFIX = "x-stainless-"
_SDK_LOGGERS = ("openai", "httpx2")

type _Client = openai.OpenAI | openai.AsyncOpenAI
# The SDK method each protocol sends with; the sync and async clients share its path.
_ENDPOINTS: Mapping[OpenAIProtocol, Callable[[_Client], Callable[..., Any]]] = {
    OpenAIProtocol.CHAT_COMPLETIONS: lambda client: client.chat.completions.create,
    OpenAIProtocol.RESPONSES: lambda client: client.responses.create,
}


@dataclass(frozen=True)
class _Mapping:
    """One protocol's request body (given whether parallel calls are declared) and response."""

    body: Callable[[CompletionRequest, bool], dict[str, Any]]
    response: Callable[[Any], CompletionResponse]


# Until step 41.5 maps each SDK failure, all but an unparseable body are `unavailable`.
SDK_ERRORS: Mapping[type[Exception], type[ProviderError]] = {
    json.JSONDecodeError: ProviderMalformedOutputError,
    openai.APIError: ProviderUnavailableError,
}


class OpenAIProvider:
    """A `Provider` over an OpenAI-compatible API, built from one configuration and its key.

    `transport` and `async_transport` replace the network transports, so tests can mock
    the API (see `guarded_http_clients`).
    """

    def __init__(
        self,
        config: OpenAICompatibleProviderConfig,
        api_key: SecretStr,
        *,
        transport: httpx2.BaseTransport | None = None,
        async_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        quiet_loggers(_SDK_LOGGERS)
        self.model = config.model
        self.protocol = config.protocol
        self.base_url = str(config.endpoint)
        self._capabilities = ProviderCapabilities(capabilities=config.capabilities)
        self._parallel = ProviderCapability.PARALLEL_TOOL_CALLS in config.capabilities
        self._retries = Retries(SDK_ERRORS, config.max_retries)
        self._connect_timeout = config.connect_timeout_seconds
        timeout = httpx2.Timeout(
            config.request_timeout_seconds, connect=config.connect_timeout_seconds
        )
        guard = HeaderGuard(
            headers=HTTP_HEADERS,
            prefixes=(_SDK_HEADER_PREFIX,),
            credentials={"authorization": SecretStr(f"Bearer {api_key.get_secret_value()}")},
        )
        http, async_http = guarded_http_clients(
            guard, timeout, transport=transport, async_transport=async_transport
        )
        sdk_options: dict[str, Any] = {
            "api_key": api_key.get_secret_value(),
            "base_url": self.base_url,
            "timeout": timeout,
            "max_retries": 0,
        }
        self._client = openai.OpenAI(**sdk_options, http_client=http)
        self._async_client = openai.AsyncOpenAI(**sdk_options, http_client=async_http)

    def __repr__(self) -> str:
        return (
            f"OpenAIProvider(model={self.model!r}, base_url={self.base_url!r}, "
            f"protocol={self.protocol.value!r})"
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        mapping = self._mapping()
        params = self._params(request, mapping)
        raw = self._retries.call(lambda: self._create(**params), request.limits.timeout_seconds)
        return untrusted_response(mapping.response, raw)

    async def complete_async(self, request: CompletionRequest) -> CompletionResponse:
        mapping = self._mapping()
        params = self._params(request, mapping)
        raw = await self._retries.call_async(
            lambda: self._create_async(**params), request.limits.timeout_seconds
        )
        return untrusted_response(mapping.response, raw)

    def _mapping(self) -> _Mapping:
        mapping = _MAPPINGS.get(self.protocol)
        if mapping is None:
            raise ProviderUnavailableError()
        return mapping

    def _params(self, request: CompletionRequest, mapping: _Mapping) -> dict[str, Any]:
        """The endpoint's arguments for one request; no sampling, `n`, or streaming."""
        return {
            "model": self.model,
            **mapping.body(request, self._parallel),
            "timeout": httpx2.Timeout(
                request.limits.timeout_seconds, connect=self._connect_timeout
            ),
        }

    def _create(self, **params: Any) -> Any:
        """Send one request to the protocol's endpoint."""
        return _ENDPOINTS[self.protocol](self._client)(**params)

    async def _create_async(self, **params: Any) -> Any:
        return await _ENDPOINTS[self.protocol](self._async_client)(**params)


# ---- Chat Completions -----------------------------------------------------------------

_TOOL_CALL_TYPES = frozenset({"function"})
# `content_filter` withheld content, so nothing returned is usable; the refusal field is
# read separately, whatever the finish reason.
_FINISH_REASONS: Mapping[str, StopReason | type[ProviderError]] = {
    "stop": StopReason.END,
    "tool_calls": StopReason.TOOL_USE,
    "length": StopReason.LENGTH,
    "content_filter": ProviderRefusalError,
}


def _arguments_text(arguments: ToolArguments) -> str:
    """Compact JSON text; malformed arguments were never run, so the call is replayed empty."""
    if isinstance(arguments, ParsedToolArguments):
        return json.dumps(arguments.value, ensure_ascii=False, separators=(",", ":"))
    return "{}"


def _chat_assistant(message: AssistantMessage) -> list[ChatCompletionMessageParam]:
    # Chat Completions returns no continuation, so any one came from another protocol.
    if message.continuation is not None:
        raise ProviderInvalidRequestError()
    param = ChatCompletionAssistantMessageParam(role="assistant")
    if message.text is not None:
        param["content"] = message.text
    if message.tool_calls:
        param["tool_calls"] = [
            ChatCompletionMessageFunctionToolCallParam(
                type="function",
                id=call.id,
                function={"name": call.name, "arguments": _arguments_text(call.arguments)},
            )
            for call in message.tool_calls
        ]
    return [param]


def _chat_results(message: ToolResultMessage) -> list[ChatCompletionMessageParam]:
    """One `tool` message per result; the protocol has no error flag, so a failed tool's
    content says so itself."""
    return [
        ChatCompletionToolMessageParam(
            role="tool", tool_call_id=result.call_id, content=result.content
        )
        for result in message.results
    ]


def _chat_system(message: SystemMessage) -> list[ChatCompletionMessageParam]:
    return [ChatCompletionSystemMessageParam(role="system", content=message.text)]


def _chat_user(message: UserMessage) -> list[ChatCompletionMessageParam]:
    return [ChatCompletionUserMessageParam(role="user", content=message.text)]


_CHAT_MESSAGES: Mapping[type, Callable[[Any], list[ChatCompletionMessageParam]]] = {
    SystemMessage: _chat_system,
    UserMessage: _chat_user,
    AssistantMessage: _chat_assistant,
    ToolResultMessage: _chat_results,
}


def _chat_messages(messages: tuple[Message, ...]) -> list[ChatCompletionMessageParam]:
    return [param for message in messages for param in _CHAT_MESSAGES[type(message)](message)]


def _chat_body(request: CompletionRequest, parallel: bool) -> dict[str, Any]:
    """`parallel_tool_calls` is sent only when declared, since some servers reject it."""
    limits = request.limits
    body: dict[str, Any] = {
        "messages": _chat_messages(request.conversation.messages),
        "max_completion_tokens": limits.max_output_tokens,
    }
    if request.tools:
        body["tools"] = [
            ChatCompletionFunctionToolParam(
                type="function",
                function={
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            )
            for tool in request.tools
        ]
        body["tool_choice"] = "auto"
        if parallel:
            body["parallel_tool_calls"] = limits.max_tool_calls_per_turn > 1
    return body


def _finish(reason: Any) -> StopReason:
    stop = _FINISH_REASONS.get(reason, ProviderMalformedOutputError)
    if isinstance(stop, StopReason):
        return stop
    raise stop()


def _chat_response(completion: ChatCompletion) -> CompletionResponse:
    """The single choice of a response; several choices or none are malformed."""
    choices = completion.choices
    if not isinstance(choices, list) or len(choices) != 1:
        raise ProviderMalformedOutputError()
    [choice] = choices
    stop = _finish(choice.finish_reason)
    message = choice.message
    calls = message.tool_calls or []
    require_known((call.type for call in calls), _TOOL_CALL_TYPES)
    text = message.content or None
    if message.refusal:
        stop, text = StopReason.REFUSAL, message.refusal
    require_text_only(stop, text, calls)
    return CompletionResponse(
        message=AssistantMessage(
            text=text,
            tool_calls=tuple(
                ToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=parse_tool_arguments(call.function.arguments),
                )
                for call in calls
            ),
        ),
        usage=_chat_usage(getattr(completion, "usage", None)),
        stop_reason=stop,
    )


def _chat_usage(usage: Any) -> TokenUsage:
    """Prompt tokens include the cached ones, as the protocol reports them."""
    details = getattr(usage, "prompt_tokens_details", None)
    return TokenUsage(
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
        cache_read_input_tokens=getattr(details, "cached_tokens", None),
        cache_write_input_tokens=getattr(details, "cache_write_tokens", None),
    )


_MAPPINGS: Mapping[OpenAIProtocol, _Mapping] = {
    OpenAIProtocol.CHAT_COMPLETIONS: _Mapping(_chat_body, _chat_response),
}
