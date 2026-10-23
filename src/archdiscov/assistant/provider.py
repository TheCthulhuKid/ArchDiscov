"""Provider errors, capabilities, and the one-operation `Provider` protocol.

Retry boundary: a provider retries only errors marked retryable (transport and rate
limit), up to its configured retry count, inside `complete`. Nothing retries a rejected
answer; the single repair request belongs to the answer renderer (step 38). Run-wide
limits such as total requests and deadlines belong to the orchestrator (step 36).
"""

from collections.abc import Collection, Mapping
from enum import StrEnum
from typing import Annotated, ClassVar, Protocol, Self, runtime_checkable

from pydantic import Field, model_validator

from archdiscov.models.validation_errors import validation_error

from .conversation import (
    AssistantMessage,
    Contract,
    Conversation,
    TokenUsage,
    ToolDeclaration,
    ToolResultMessage,
)


class ProviderErrorCode(StrEnum):
    AUTHENTICATION = "provider_authentication"
    RATE_LIMIT = "provider_rate_limit"
    TIMEOUT = "provider_timeout"
    TRANSPORT = "provider_transport"
    INVALID_REQUEST = "provider_invalid_request"
    MALFORMED_OUTPUT = "provider_malformed_output"
    UNSUPPORTED_CAPABILITY = "provider_unsupported_capability"
    TRUNCATED_OUTPUT = "provider_truncated_output"
    REFUSAL = "provider_refusal"
    UNAVAILABLE = "provider_unavailable"


class ProviderError(Exception):
    """A provider failure exposed only as a stable code and a fixed message.

    Messages never include keys, request bodies, provider responses, or tool-result
    content; adapters chain the underlying exception for local debugging only.
    """

    code: ClassVar[ProviderErrorCode]
    message: ClassVar[str]
    retryable: ClassVar[bool] = False

    def __init__(self) -> None:
        super().__init__(self.message)

    def diagnostic(self) -> dict[str, str | bool]:
        return {"code": self.code.value, "message": self.message, "retryable": self.retryable}


class ProviderAuthenticationError(ProviderError):
    code = ProviderErrorCode.AUTHENTICATION
    message = "Provider rejected the configured credentials."


class ProviderRateLimitError(ProviderError):
    code = ProviderErrorCode.RATE_LIMIT
    message = "Provider rate limit reached."
    retryable = True


class ProviderTimeoutError(ProviderError):
    code = ProviderErrorCode.TIMEOUT
    message = "Provider request timed out."


class ProviderTransportError(ProviderError):
    code = ProviderErrorCode.TRANSPORT
    message = "Provider could not be reached."
    retryable = True


class ProviderInvalidRequestError(ProviderError):
    code = ProviderErrorCode.INVALID_REQUEST
    message = "Provider rejected the request."


class ProviderMalformedOutputError(ProviderError):
    code = ProviderErrorCode.MALFORMED_OUTPUT
    message = "Provider returned output that does not fit the conversation contracts."


class ProviderTruncatedOutputError(ProviderError):
    code = ProviderErrorCode.TRUNCATED_OUTPUT
    message = "Provider output was truncated before a usable message."


class ProviderRefusalError(ProviderError):
    code = ProviderErrorCode.REFUSAL
    message = "Provider refused to answer."


class ProviderUnavailableError(ProviderError):
    code = ProviderErrorCode.UNAVAILABLE
    message = "Provider is unavailable."


class ProviderCapability(StrEnum):
    TOOL_CALLING = "tool_calling"
    PARALLEL_TOOL_CALLS = "parallel_tool_calls"
    STRUCTURED_OUTPUT = "structured_output"


class UnsupportedCapabilityError(ProviderError):
    """Raised before any provider call; the capability name is a fixed vocabulary value."""

    code = ProviderErrorCode.UNSUPPORTED_CAPABILITY
    message = "Provider does not support a capability the request needs."

    def __init__(self, capability: ProviderCapability) -> None:
        super().__init__()
        self.capability = ProviderCapability(capability)

    def diagnostic(self) -> dict[str, str | bool]:
        return {**super().diagnostic(), "capability": self.capability.value}


# The fixed message of every provider error, for explanations that must not hold provider text.
PROVIDER_ERROR_MESSAGES: Mapping[ProviderErrorCode, str] = {
    error.code: error.message
    for error in (
        ProviderAuthenticationError,
        ProviderRateLimitError,
        ProviderTimeoutError,
        ProviderTransportError,
        ProviderInvalidRequestError,
        ProviderMalformedOutputError,
        ProviderTruncatedOutputError,
        ProviderRefusalError,
        ProviderUnavailableError,
        UnsupportedCapabilityError,
    )
}


def require_tool_calling_for_parallel_calls(capabilities: Collection[ProviderCapability]) -> None:
    if (
        ProviderCapability.PARALLEL_TOOL_CALLS in capabilities
        and ProviderCapability.TOOL_CALLING not in capabilities
    ):
        raise validation_error(
            "provider_parallel_calls_without_tools", "parallel calls need tool calling"
        )


class ProviderCapabilities(Contract):
    """What a provider can do; requests needing more are rejected, not degraded."""

    capabilities: frozenset[ProviderCapability] = frozenset()

    @model_validator(mode="after")
    def require_tool_calling_for_parallel_calls(self) -> Self:
        require_tool_calling_for_parallel_calls(self.capabilities)
        return self


class RequestLimits(Contract):
    """Per-request bounds; run-wide limits belong to the orchestrator."""

    max_output_tokens: Annotated[int, Field(ge=1, le=128_000)] = 4_096
    timeout_seconds: Annotated[float, Field(gt=0, le=600)] = 60.0
    max_tool_calls_per_turn: Annotated[int, Field(ge=1, le=32)] = 1


class CompletionRequest(Contract):
    """One provider turn: the conversation so far, the tools offered, and its limits."""

    conversation: Conversation
    tools: tuple[ToolDeclaration, ...] = ()
    limits: RequestLimits = Field(default_factory=RequestLimits)

    @model_validator(mode="after")
    def require_unique_tool_names(self) -> Self:
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise validation_error("provider_tool_name_duplicate", "duplicate tool name")
        return self

    def required_capabilities(self) -> frozenset[ProviderCapability]:
        """Capabilities needed to send this request; structured output arrives with step 37."""
        required = set()
        uses_tools = bool(self.tools) or any(
            isinstance(message, ToolResultMessage)
            or (isinstance(message, AssistantMessage) and message.tool_calls)
            for message in self.conversation.messages
        )
        if uses_tools:
            required.add(ProviderCapability.TOOL_CALLING)
        if self.limits.max_tool_calls_per_turn > 1:
            required.add(ProviderCapability.PARALLEL_TOOL_CALLS)
        return frozenset(required)


class StopReason(StrEnum):
    END = "end"
    TOOL_USE = "tool_use"
    LENGTH = "length"
    REFUSAL = "refusal"


class CompletionResponse(Contract):
    """The assistant message from one turn, with usage and why the provider stopped.

    `length` and `refusal` responses carry what text the provider returned; a provider
    that returned nothing usable raises the matching error instead.
    """

    message: AssistantMessage
    usage: TokenUsage = Field(default_factory=TokenUsage)
    stop_reason: StopReason

    @model_validator(mode="after")
    def match_stop_reason(self) -> Self:
        if (self.stop_reason == StopReason.TOOL_USE) != bool(self.message.tool_calls):
            raise validation_error(
                "provider_stop_reason_mismatch", "tool_use stop needs tool calls, others none"
            )
        return self


def check_capabilities(capabilities: ProviderCapabilities, request: CompletionRequest) -> None:
    """Reject a request needing an unsupported capability, in a stable order."""
    required = request.required_capabilities()
    for capability in ProviderCapability:
        if capability in required and capability not in capabilities.capabilities:
            raise UnsupportedCapabilityError(capability)


@runtime_checkable
class Provider(Protocol):
    """One operation in sync and async form: complete a single conversation turn.

    Implementations retry only retryable errors and raise only `ProviderError`.
    No streaming, caching, or conversation state.
    """

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    def complete(self, request: CompletionRequest) -> CompletionResponse: ...

    async def complete_async(self, request: CompletionRequest) -> CompletionResponse: ...


def complete(provider: Provider, request: CompletionRequest) -> CompletionResponse:
    """Check capabilities, then make one provider call."""
    check_capabilities(provider.capabilities, request)
    return provider.complete(request)


async def complete_async(provider: Provider, request: CompletionRequest) -> CompletionResponse:
    """Check capabilities, then make one provider call."""
    check_capabilities(provider.capabilities, request)
    return await provider.complete_async(request)
