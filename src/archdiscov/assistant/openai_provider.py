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
protocol's endpoint and pair of functions in `_MAPPINGS`. The response is untrusted: only
the fields the protocol defines are read, and anything that does not fit is malformed
output. Responses requests are stateless, so reasoning a model needs back travels as a
continuation of the whole ordered output, replayed verbatim; Chat Completions has no
reasoning to replay, so it never sets a continuation and refuses one. Until the failure
table arrives (step 41.5), every SDK failure but an unparseable body is
`provider_unavailable`.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

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
from openai.types.responses import (
    EasyInputMessageParam,
    FunctionToolParam,
    Response,
    ResponseFunctionToolCallParam,
    ResponseInputItemParam,
)
from openai.types.responses.response_input_item_param import FunctionCallOutput
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
from .config import OpenAICompatibleProviderConfig, OpenAIProtocol, ProviderKind
from .conversation import (
    AssistantMessage,
    Message,
    ParsedToolArguments,
    ProviderContinuation,
    SystemMessage,
    TokenUsage,
    ToolArguments,
    ToolCall,
    ToolDeclaration,
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

_KIND = ProviderKind.OPENAI_COMPATIBLE.value

type _Client = openai.OpenAI | openai.AsyncOpenAI


@dataclass(frozen=True)
class _Mapping:
    """One protocol: the SDK method it sends with (the sync and async clients share its
    path), its request body for a configuration, and its response mapping."""

    endpoint: Callable[[_Client], Callable[..., Any]]
    body: Callable[[CompletionRequest, OpenAICompatibleProviderConfig], dict[str, Any]]
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
        self._config = config
        self._mapping = _MAPPINGS[config.protocol]
        self._capabilities = ProviderCapabilities(capabilities=config.capabilities)
        self._retries = Retries(SDK_ERRORS, config.max_retries)
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
        params = self._params(request)
        raw = self._retries.call(lambda: self._create(**params), request.limits.timeout_seconds)
        return untrusted_response(self._mapping.response, raw)

    async def complete_async(self, request: CompletionRequest) -> CompletionResponse:
        params = self._params(request)
        raw = await self._retries.call_async(
            lambda: self._create_async(**params), request.limits.timeout_seconds
        )
        return untrusted_response(self._mapping.response, raw)

    def _params(self, request: CompletionRequest) -> dict[str, Any]:
        """The endpoint's arguments for one request; no sampling, `n`, or streaming."""
        return {
            "model": self.model,
            **self._mapping.body(request, self._config),
            "timeout": httpx2.Timeout(
                request.limits.timeout_seconds, connect=self._config.connect_timeout_seconds
            ),
        }

    def _create(self, **params: Any) -> Any:
        """Send one request to the protocol's endpoint."""
        return self._mapping.endpoint(self._client)(**params)

    async def _create_async(self, **params: Any) -> Any:
        return await self._mapping.endpoint(self._async_client)(**params)


# ---- both protocols -------------------------------------------------------------------


def _arguments_text(arguments: ToolArguments) -> str:
    """Compact JSON text; malformed arguments were never run, so the call is replayed empty."""
    if isinstance(arguments, ParsedToolArguments):
        return json.dumps(arguments.value, ensure_ascii=False, separators=(",", ":"))
    return "{}"


def _tool_fields(
    request: CompletionRequest,
    config: OpenAICompatibleProviderConfig,
    declaration: Callable[[ToolDeclaration], Any],
) -> dict[str, Any]:
    """`parallel_tool_calls` is sent only when declared, since some servers reject it."""
    if not request.tools:
        return {}
    fields: dict[str, Any] = {
        "tools": [declaration(tool) for tool in request.tools],
        "tool_choice": "auto",
    }
    if ProviderCapability.PARALLEL_TOOL_CALLS in config.capabilities:
        fields["parallel_tool_calls"] = request.limits.max_tool_calls_per_turn > 1
    return fields


def _outcome[K](outcomes: Mapping[K, StopReason | type[ProviderError]], key: K) -> StopReason:
    """The stop reason for a response's finish, raising the error a table names instead."""
    outcome = outcomes.get(key, ProviderMalformedOutputError)
    if isinstance(outcome, StopReason):
        return outcome
    raise outcome()


def _usage(usage: Any, input_name: str, output_name: str, details_name: str) -> TokenUsage:
    """Each protocol names its counts; reported input tokens include the cached ones."""
    details = getattr(usage, details_name, None)
    return TokenUsage(
        input_tokens=getattr(usage, input_name, None),
        output_tokens=getattr(usage, output_name, None),
        cache_read_input_tokens=getattr(details, "cached_tokens", None),
        cache_write_input_tokens=getattr(details, "cache_write_tokens", None),
    )


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


def _chat_tool(tool: ToolDeclaration) -> ChatCompletionFunctionToolParam:
    return ChatCompletionFunctionToolParam(
        type="function",
        function={
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    )


def _chat_body(
    request: CompletionRequest, config: OpenAICompatibleProviderConfig
) -> dict[str, Any]:
    return {
        "messages": _chat_messages(request.conversation.messages),
        "max_completion_tokens": request.limits.max_output_tokens,
        **_tool_fields(request, config, _chat_tool),
    }


def _chat_response(completion: ChatCompletion) -> CompletionResponse:
    """The single choice of a response; several choices or none are malformed."""
    choices = completion.choices
    if not isinstance(choices, list) or len(choices) != 1:
        raise ProviderMalformedOutputError()
    [choice] = choices
    stop = _outcome(_FINISH_REASONS, choice.finish_reason)
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
        usage=_usage(
            getattr(completion, "usage", None),
            "prompt_tokens",
            "completion_tokens",
            "prompt_tokens_details",
        ),
        stop_reason=stop,
    )


# ---- Responses ------------------------------------------------------------------------

_ITEM_TYPES = frozenset({"message", "function_call", "reasoning"})
_PART_TYPES = frozenset({"output_text", "refusal"})
_ENCRYPTED_REASONING = "reasoning.encrypted_content"
# By `status` and `incomplete_details.reason`; a completed response with calls is tool use,
# and refusal parts are read separately, whatever the status.
_STATUSES: Mapping[tuple[str, str | None], StopReason | type[ProviderError]] = {
    ("completed", None): StopReason.END,
    ("incomplete", "max_output_tokens"): StopReason.LENGTH,
    ("incomplete", "content_filter"): ProviderRefusalError,
}


def _input_message(role: Any, text: str) -> list[ResponseInputItemParam]:
    return [EasyInputMessageParam(type="message", role=role, content=text)]


def _responses_assistant(message: AssistantMessage) -> list[ResponseInputItemParam]:
    """The response's own output when it carried reasoning, else rebuilt in order."""
    if message.continuation is not None:
        if message.continuation.provider_kind != _KIND:
            raise ProviderInvalidRequestError()
        return [cast(ResponseInputItemParam, item) for item in message.continuation.blocks]
    items = [] if message.text is None else _input_message("assistant", message.text)
    items += [
        ResponseFunctionToolCallParam(
            type="function_call",
            call_id=call.id,
            name=call.name,
            arguments=_arguments_text(call.arguments),
        )
        for call in message.tool_calls
    ]
    return items


def _responses_results(message: ToolResultMessage) -> list[ResponseInputItemParam]:
    """The protocol has no error flag either, so a failed tool's content says so itself."""
    return [
        FunctionCallOutput(
            type="function_call_output", call_id=result.call_id, output=result.content
        )
        for result in message.results
    ]


_RESPONSES_INPUT: Mapping[type, Callable[[Any], list[ResponseInputItemParam]]] = {
    SystemMessage: lambda message: _input_message("system", message.text),
    UserMessage: lambda message: _input_message("user", message.text),
    AssistantMessage: _responses_assistant,
    ToolResultMessage: _responses_results,
}


def _responses_tool(tool: ToolDeclaration) -> FunctionToolParam:
    # Responses makes functions strict unless told otherwise, which needs schemas this
    # contract does not require.
    return FunctionToolParam(
        type="function",
        name=tool.name,
        description=tool.description,
        parameters=tool.input_schema,
        strict=False,
    )


def _responses_body(
    request: CompletionRequest, config: OpenAICompatibleProviderConfig
) -> dict[str, Any]:
    """A stateless request: nothing is stored, and no earlier response is referenced."""
    messages = request.conversation.messages
    body: dict[str, Any] = {
        "input": [
            item for message in messages for item in _RESPONSES_INPUT[type(message)](message)
        ],
        "store": False,
        "max_output_tokens": request.limits.max_output_tokens,
        **_tool_fields(request, config, _responses_tool),
    }
    if config.encrypted_reasoning:
        body["include"] = [_ENCRYPTED_REASONING]
    return body


def _joined(parts: list[Any], kind: str, field: str) -> str | None:
    return "".join(getattr(part, field) for part in parts if part.type == kind) or None


def _responses_response(response: Response) -> CompletionResponse:
    output = response.output
    if not isinstance(output, list):
        raise ProviderMalformedOutputError()
    require_known((item.type for item in output), _ITEM_TYPES)
    details = getattr(response, "incomplete_details", None)
    stop = _outcome(_STATUSES, (response.status, getattr(details, "reason", None)))
    parts = [part for item in output if item.type == "message" for part in item.content]
    require_known((part.type for part in parts), _PART_TYPES)
    calls = [item for item in output if item.type == "function_call"]
    if calls and stop is StopReason.END:
        stop = StopReason.TOOL_USE
    text = _joined(parts, "output_text", "text")
    refusal = _joined(parts, "refusal", "refusal")
    if refusal is not None:
        stop, text = StopReason.REFUSAL, refusal
    require_text_only(stop, text, calls)
    return CompletionResponse(
        message=AssistantMessage(
            text=text,
            tool_calls=tuple(
                ToolCall(
                    id=call.call_id,
                    name=call.name,
                    arguments=parse_tool_arguments(call.arguments),
                )
                for call in calls
            ),
            continuation=_responses_continuation(output),
        ),
        usage=_usage(
            getattr(response, "usage", None),
            "input_tokens",
            "output_tokens",
            "input_tokens_details",
        ),
        stop_reason=stop,
    )


def _responses_continuation(output: list[Any]) -> ProviderContinuation | None:
    """The whole ordered output, when it holds reasoning that can go back statelessly.

    Reasoning without encrypted content (when `encrypted_reasoning` is off) cannot be
    replayed to a server that stores nothing, so the response is rebuilt without it.
    """
    if not any(
        item.type == "reasoning" and getattr(item, "encrypted_content", None) for item in output
    ):
        return None
    return ProviderContinuation(
        provider_kind=_KIND,
        blocks=tuple(item.model_dump(mode="json", exclude_unset=True) for item in output),
    )


_MAPPINGS: Mapping[OpenAIProtocol, _Mapping] = {
    OpenAIProtocol.CHAT_COMPLETIONS: _Mapping(
        lambda client: client.chat.completions.create, _chat_body, _chat_response
    ),
    OpenAIProtocol.RESPONSES: _Mapping(
        lambda client: client.responses.create, _responses_body, _responses_response
    ),
}
