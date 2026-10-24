"""The Bedrock Converse adapter: the only assistant module that imports boto3 or botocore.

The adapter has its own AWS session, separate from collection's: its configured `profile`
and `region`, and, with `role_arn`, a role assumed with its `session_name`. An explicit
profile already makes botocore skip environment credentials, and the client is built so
the rest of the environment cannot change its target or retries either:
`ignore_configured_endpoint_urls` ignores `AWS_ENDPOINT_URL*` and a profile's
`endpoint_url`, an empty proxy setting ignores proxy variables, FIPS and dual-stack
endpoints are off, and an explicit retry setting overrides `AWS_MAX_ATTEMPTS`,
`AWS_RETRY_MODE`, and the profile's own. The `boto3` and `botocore` loggers stay at
`WARNING`, because their debug records hold request bodies, and `botocore.credentials` at
`ERROR`, because its warning of a failed role assumption holds STS's message.

Building the adapter makes no request: a role is assumed on the first request, through
refreshable credentials, so an STS failure surfaces as that request's failure. A profile
that cannot be loaded fails before the run. Bedrock models differ in tool support, so the
capabilities are the ones the configuration declares.

Each `complete` maps the conversation to one Converse request and the response back to the
shared contracts. Only fields every tool-capable model accepts are sent: `toolChoice` and a
successful result's `status` are left at their defaults, since some models reject them.
Botocore parses the response into plain dictionaries without checking it, so every field is
untrusted: only text, tool-use, and reasoning blocks are mapped, and anything else is
malformed output. Reasoning blocks must go back unchanged and in their original order, so a
response holding them becomes a continuation of its whole content in its JSON form,
replayed verbatim. Botocore is synchronous, so `complete_async` runs it in a daemon thread
that an interrupt does not wait for.
Botocore fixes the read timeout per client, so a request whose timeout differs from the
configured one is sent by a client made for that timeout.

Failures map to typed provider errors through `SDK_ERRORS`: botocore raises one
`ClientError` for every API error, which maps by its error code through `API_ERRORS`, and
its own exceptions map by class. An `AssumeRole` that STS rejects is a credential failure.
The shared `Retries` retries only transport and rate-limit errors, throttling included.
"""

import asyncio
import base64
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any

import boto3
import botocore.session
from botocore import exceptions as boto
from botocore.config import Config
from botocore.credentials import (
    CredentialProvider,
    CredentialResolver,
    DeferredRefreshableCredentials,
)
from botocore.parsers import ResponseParserError

from archdiscov.core.async_bridge import in_daemon_thread
from archdiscov.ingestion.aws_sessions import (
    assume_role_parameters,
    profile_session,
    role_credentials,
)

from .adapters import (
    ByCode,
    Retries,
    RoleBlocks,
    quiet_loggers,
    require_known,
    require_text_only,
    role_turns,
    untrusted_response,
)
from .config import (
    BedrockProviderConfig,
    ProviderConfigError,
    ProviderConfigErrorCode,
    ProviderKind,
)
from .conversation import (
    AssistantMessage,
    ParsedToolArguments,
    ProviderContinuation,
    SystemMessage,
    TokenUsage,
    ToolCall,
    ToolDeclaration,
    ToolResult,
    ToolResultMessage,
    UserMessage,
    parse_tool_arguments,
)
from .provider import (
    CompletionRequest,
    CompletionResponse,
    ProviderAuthenticationError,
    ProviderCapabilities,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderMalformedOutputError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderTransportError,
    ProviderUnavailableError,
    StopReason,
)

_SERVICE = "bedrock-runtime"
_SDK_LOGGERS = ("boto3", "botocore")
# It warns of a failed role assumption with a traceback holding STS's message.
_CREDENTIAL_LOGGER = "botocore.credentials"
_CREDENTIAL_METHOD = "assume-role"

_KIND = ProviderKind.BEDROCK.value
_REASONING = "reasoningContent"
# Botocore's stand-in for a union member it does not know; it keeps only the name.
_UNKNOWN_MEMBER = "SDK_UNKNOWN_MEMBER"
# A tool use with a `type` is a server tool the model ran itself, not a call for the client.
_SERVER_TOOL_USE = "serverToolUse"
_UNKNOWN_REASONING = "unknownReasoning"
_KNOWN_BLOCKS = frozenset({"text", "toolUse", _REASONING})
# `model_context_window_exceeded` cuts the output short, as `max_tokens` does; the
# `malformed_*` stops and any unknown one are malformed output.
_STOP_REASONS: Mapping[str, StopReason] = {
    "end_turn": StopReason.END,
    "stop_sequence": StopReason.END,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.LENGTH,
    "model_context_window_exceeded": StopReason.LENGTH,
    "guardrail_intervened": StopReason.REFUSAL,
    "content_filtered": StopReason.REFUSAL,
}
# The provider error for each API error code; any other code is `provider_unavailable`.
API_ERRORS: Mapping[str, type[ProviderError]] = {
    "ThrottlingException": ProviderRateLimitError,
    "ServiceQuotaExceededException": ProviderRateLimitError,
    "AccessDeniedException": ProviderAuthenticationError,
    "UnrecognizedClientException": ProviderAuthenticationError,
    "ExpiredTokenException": ProviderAuthenticationError,
    "InvalidSignatureException": ProviderAuthenticationError,
    "IncompleteSignature": ProviderAuthenticationError,
    "MissingAuthenticationTokenException": ProviderAuthenticationError,
    "InvalidClientTokenId": ProviderAuthenticationError,
    "RequestExpired": ProviderAuthenticationError,
    "ValidationException": ProviderInvalidRequestError,
    "ResourceNotFoundException": ProviderInvalidRequestError,
    "ModelTimeoutException": ProviderTimeoutError,
    "ModelNotReadyException": ProviderUnavailableError,
    "ModelErrorException": ProviderUnavailableError,
    "InternalServerException": ProviderUnavailableError,
    "ServiceUnavailableException": ProviderUnavailableError,
}


def _error_code(error: Exception) -> Any:
    return error.response.get("Error", {}).get("Code")


# The provider error for each botocore failure, found through the failure's class hierarchy,
# so a subclass (such as `ReadTimeoutError` of `HTTPClientError`) maps before its base.
SDK_ERRORS: Mapping[type[Exception], type[ProviderError] | ByCode] = {
    boto.ClientError: ByCode(_error_code, API_ERRORS, ProviderUnavailableError),
    boto.ReadTimeoutError: ProviderTimeoutError,
    boto.ConnectTimeoutError: ProviderTimeoutError,
    boto.HTTPClientError: ProviderTransportError,
    boto.ConnectionError: ProviderTransportError,
    boto.NoCredentialsError: ProviderAuthenticationError,
    boto.PartialCredentialsError: ProviderAuthenticationError,
    boto.CredentialRetrievalError: ProviderAuthenticationError,
    boto.TokenRetrievalError: ProviderAuthenticationError,
    boto.SSOError: ProviderAuthenticationError,
    boto.ParamValidationError: ProviderInvalidRequestError,
    ResponseParserError: ProviderMalformedOutputError,
    boto.BotoCoreError: ProviderUnavailableError,
    # Botocore's parser raises these for a response body whose fields have the wrong types.
    AttributeError: ProviderMalformedOutputError,
    TypeError: ProviderMalformedOutputError,
    ValueError: ProviderMalformedOutputError,
}


class _AssumedRole(CredentialProvider):
    """The only credential source of the role session: the assumed role's credentials."""

    METHOD = _CREDENTIAL_METHOD

    def __init__(self, credentials: DeferredRefreshableCredentials) -> None:
        super().__init__()
        self._credentials = credentials

    def load(self) -> DeferredRefreshableCredentials:
        return self._credentials


class BedrockProvider:
    """A `Provider` over the Bedrock Converse API, built from one configuration.

    `session_factory` makes the profile session (and, with a role, the role session) in
    place of `boto3.Session`, so tests can register botocore event hooks on them; `sleep`
    and `async_sleep` wait between retries.
    """

    def __init__(
        self,
        config: BedrockProviderConfig,
        *,
        session_factory: Callable[..., Any] = boto3.Session,
        sleep: Callable[[float], None] = time.sleep,
        async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        quiet_loggers(_SDK_LOGGERS)
        quiet_loggers((_CREDENTIAL_LOGGER,), logging.ERROR)
        self.model = config.model
        self.region = config.region.value
        self.profile = config.profile
        self.endpoint = None if config.endpoint is None else str(config.endpoint)
        self._capabilities = ProviderCapabilities(capabilities=config.capabilities)
        self._retries = Retries(SDK_ERRORS, config.max_retries, sleep, async_sleep)
        self._timeout = config.request_timeout_seconds
        self._client_config = Config(
            connect_timeout=config.connect_timeout_seconds,
            read_timeout=config.request_timeout_seconds,
            retries={"total_max_attempts": 1, "mode": "standard"},
            ignore_configured_endpoint_urls=True,
            proxies={},
            use_fips_endpoint=False,
            use_dualstack_endpoint=False,
        )
        try:
            session = profile_session(session_factory, config.profile, self.region)
            if config.role_arn is not None:
                session = _role_session(session, session_factory, config, self._client_config)
            self._session = session
            self._clients = {self._timeout: self._new_client(self._timeout)}
        except boto.BotoCoreError as error:
            raise ProviderConfigError(
                ProviderConfigErrorCode.PROFILE_UNAVAILABLE,
                profile=config.profile,
                kind=config.kind,
            ) from error

    def __repr__(self) -> str:
        return (
            f"BedrockProvider(model={self.model!r}, region={self.region!r}, "
            f"profile={self.profile!r})"
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        params = self._params(request)
        timeout = request.limits.timeout_seconds
        output = self._retries.call(lambda: self._client(timeout).converse(**params), timeout)
        return untrusted_response(_response, output)

    async def complete_async(self, request: CompletionRequest) -> CompletionResponse:
        params = self._params(request)
        timeout = request.limits.timeout_seconds
        output = await self._retries.call_async(
            lambda: in_daemon_thread(lambda: self._client(timeout).converse(**params)), timeout
        )
        return untrusted_response(_response, output)

    def _client(self, timeout: float) -> Any:
        """A client whose read timeout is `timeout`. The configured timeout's client is kept,
        and one other, since a timeout cut short by the run's deadline differs every time."""
        if timeout not in self._clients:
            self._clients = {
                self._timeout: self._clients[self._timeout],
                timeout: self._new_client(timeout),
            }
        return self._clients[timeout]

    def _new_client(self, timeout: float) -> Any:
        return self._session.client(
            _SERVICE,
            region_name=self.region,
            endpoint_url=self.endpoint,
            config=self._client_config.merge(Config(read_timeout=timeout)),
        )

    def _params(self, request: CompletionRequest) -> dict[str, Any]:
        """The `converse` arguments for one request; no sampling or model-specific fields."""
        messages = request.conversation.messages
        params: dict[str, Any] = {
            "modelId": self.model,
            "messages": role_turns(
                (m for m in messages if not isinstance(m, SystemMessage)), _ROLES
            ),
            "inferenceConfig": {"maxTokens": request.limits.max_output_tokens},
        }
        system = [{"text": m.text} for m in messages if isinstance(m, SystemMessage)]
        if system:
            params["system"] = system
        if request.tools:
            params["toolConfig"] = {"tools": [_tool_spec(tool) for tool in request.tools]}
        return params


def _role_session(
    base: Any,
    session_factory: Callable[..., Any],
    config: BedrockProviderConfig,
    client_config: Config,
) -> Any:
    """A session whose only credentials are the role's, assumed on first use from `base`."""
    sts = base.client("sts", region_name=config.region.value, config=client_config)
    params = assume_role_parameters(config.role_arn, config.session_name)

    def assume() -> dict[str, str]:
        try:
            credentials = role_credentials(sts.assume_role(**params))
        except boto.ClientError as error:
            raise _credential_error("assume-role rejected") from error
        expiry = None if credentials is None else credentials.get("Expiration")
        if not isinstance(expiry, datetime):
            raise _credential_error("invalid assume-role credentials")
        return {
            "access_key": credentials["AccessKeyId"],
            "secret_key": credentials["SecretAccessKey"],
            "token": credentials["SessionToken"],
            "expiry_time": expiry.isoformat(),
        }

    role = botocore.session.Session(profile=config.profile)
    deferred = DeferredRefreshableCredentials(refresh_using=assume, method=_CREDENTIAL_METHOD)
    role.register_component("credential_provider", CredentialResolver([_AssumedRole(deferred)]))
    return session_factory(botocore_session=role, region_name=config.region.value)


def _credential_error(reason: str) -> boto.CredentialRetrievalError:
    return boto.CredentialRetrievalError(provider=_CREDENTIAL_METHOD, error_msg=reason)


# ---- requests -------------------------------------------------------------------------


def _tool_spec(tool: ToolDeclaration) -> dict[str, Any]:
    return {
        "toolSpec": {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": {"json": tool.input_schema},
        }
    }


def _user_blocks(message: UserMessage) -> list[Any]:
    return [{"text": message.text}]


def _tool_result(result: ToolResult) -> dict[str, Any]:
    block: dict[str, Any] = {"toolUseId": result.call_id, "content": [{"text": result.content}]}
    if result.is_error:
        block["status"] = "error"
    return {"toolResult": block}


def _result_blocks(message: ToolResultMessage) -> list[Any]:
    return [_tool_result(result) for result in message.results]


def _assistant_blocks(message: AssistantMessage) -> list[Any]:
    """The response's own content when it carried reasoning, else rebuilt in order."""
    if message.continuation is not None:
        if message.continuation.provider_kind != _KIND:
            raise ProviderInvalidRequestError()
        return [_replayed(block) for block in message.continuation.blocks]
    blocks: list[Any] = [] if message.text is None else [{"text": message.text}]
    blocks += [
        {
            "toolUse": {
                "toolUseId": call.id,
                "name": call.name,
                # Malformed arguments were never run; the call is replayed with no input.
                "input": (
                    call.arguments.value if isinstance(call.arguments, ParsedToolArguments) else {}
                ),
            }
        }
        for call in message.tool_calls
    ]
    return blocks


_ROLES: RoleBlocks = {
    UserMessage: ("user", _user_blocks),
    ToolResultMessage: ("user", _result_blocks),
    AssistantMessage: ("assistant", _assistant_blocks),
}


# ---- responses ------------------------------------------------------------------------


def _response(output: Mapping[str, Any]) -> CompletionResponse:
    stop = _STOP_REASONS.get(output["stopReason"])
    if stop is None:
        raise ProviderMalformedOutputError()
    blocks = output["output"]["message"]["content"]
    require_known((_block_kind(block) for block in blocks), _KNOWN_BLOCKS)
    text = "".join(block["text"] for block in blocks if "text" in block) or None
    tool_uses = [block["toolUse"] for block in blocks if "toolUse" in block]
    require_text_only(stop, text, tool_uses)
    return CompletionResponse(
        message=AssistantMessage(
            text=text,
            tool_calls=tuple(
                ToolCall(
                    id=use["toolUseId"],
                    name=use["name"],
                    arguments=parse_tool_arguments(use["input"]),
                )
                for use in tool_uses
            ),
            continuation=_continuation(blocks),
        ),
        usage=_usage(output.get("usage") or {}),
        stop_reason=stop,
    )


def _block_kind(block: Mapping[str, Any]) -> str:
    """The member a content block sets, telling apart a server tool's use and reasoning that
    cannot be replayed; botocore refuses a block that sets more or none."""
    [kind] = block
    if kind == "toolUse" and "type" in block[kind]:
        return _SERVER_TOOL_USE
    if kind == _REASONING and _UNKNOWN_MEMBER in block[kind]:
        return _UNKNOWN_REASONING
    return kind


def _continuation(blocks: list[Any]) -> ProviderContinuation | None:
    if not any(_REASONING in block for block in blocks):
        return None
    return ProviderContinuation(
        provider_kind=_KIND, blocks=tuple(_stored(block) for block in blocks)
    )


# Botocore decodes redacted reasoning from base64 to bytes; the continuation keeps the
# base64 text the API sent, and the request gets the bytes back for botocore to encode.
def _stored(block: dict[str, Any]) -> dict[str, Any]:
    redacted = block.get(_REASONING, {}).get("redactedContent")
    if not isinstance(redacted, bytes):
        return block
    return {_REASONING: {"redactedContent": base64.b64encode(redacted).decode("ascii")}}


def _replayed(block: dict[str, Any]) -> dict[str, Any]:
    redacted = block.get(_REASONING, {}).get("redactedContent")
    if not isinstance(redacted, str):
        return block
    return {_REASONING: {"redactedContent": base64.b64decode(redacted)}}


def _usage(usage: Mapping[str, Any]) -> TokenUsage:
    return TokenUsage(
        input_tokens=usage.get("inputTokens"),
        output_tokens=usage.get("outputTokens"),
        cache_read_input_tokens=usage.get("cacheReadInputTokens"),
        cache_write_input_tokens=usage.get("cacheWriteInputTokens"),
    )
