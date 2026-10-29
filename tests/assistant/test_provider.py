import asyncio

import pytest
from pydantic import ValidationError

from archdiscov.assistant.conversation import (
    AssistantMessage,
    Conversation,
    ParsedToolArguments,
    SystemMessage,
    TokenUsage,
    ToolCall,
    ToolDeclaration,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from archdiscov.assistant.provider import (
    CompletionRequest,
    CompletionResponse,
    Provider,
    ProviderAuthenticationError,
    ProviderCapabilities,
    ProviderCapability,
    ProviderError,
    ProviderErrorCode,
    ProviderInvalidRequestError,
    ProviderMalformedOutputError,
    ProviderRateLimitError,
    ProviderRefusalError,
    ProviderTimeoutError,
    ProviderTransportError,
    ProviderTruncatedOutputError,
    ProviderUnavailableError,
    RequestLimits,
    StopReason,
    UnsupportedCapabilityError,
    check_capabilities,
    complete,
    complete_async,
)

SECRET = "sk-ant-SECRET_PRIVATE_TOKEN"
QUESTION = Conversation(
    messages=(SystemMessage(text="Answer from tools."), UserMessage(text="What is exposed?"))
)
SEARCH = ToolDeclaration(
    name="search_resources", description="Search.", input_schema={"type": "object"}
)
ALL_CAPABILITIES = ProviderCapabilities(capabilities=frozenset(ProviderCapability))
TEXT_ONLY = ProviderCapabilities()


def _call(call_id: str = "call_1") -> ToolCall:
    return ToolCall(id=call_id, name="search_resources", arguments=ParsedToolArguments(value={}))


ERRORS = [
    (ProviderAuthenticationError, ProviderErrorCode.AUTHENTICATION, False),
    (ProviderRateLimitError, ProviderErrorCode.RATE_LIMIT, True),
    (ProviderTimeoutError, ProviderErrorCode.TIMEOUT, False),
    (ProviderTransportError, ProviderErrorCode.TRANSPORT, True),
    (ProviderInvalidRequestError, ProviderErrorCode.INVALID_REQUEST, False),
    (ProviderMalformedOutputError, ProviderErrorCode.MALFORMED_OUTPUT, False),
    (ProviderTruncatedOutputError, ProviderErrorCode.TRUNCATED_OUTPUT, False),
    (ProviderRefusalError, ProviderErrorCode.REFUSAL, False),
    (ProviderUnavailableError, ProviderErrorCode.UNAVAILABLE, False),
]


def test_every_error_code_has_one_error_type() -> None:
    codes = [code for _, code, _ in ERRORS] + [ProviderErrorCode.UNSUPPORTED_CAPABILITY]
    assert sorted(codes) == sorted(ProviderErrorCode)
    assert [code.value for code in ProviderErrorCode] == [
        "provider_authentication",
        "provider_rate_limit",
        "provider_timeout",
        "provider_transport",
        "provider_invalid_request",
        "provider_malformed_output",
        "provider_unsupported_capability",
        "provider_truncated_output",
        "provider_refusal",
        "provider_unavailable",
    ]


@pytest.mark.parametrize("error_type,code,retryable", ERRORS)
def test_mapped_errors_carry_stable_codes_and_redacted_messages(
    error_type: type[ProviderError], code: ProviderErrorCode, retryable: bool
) -> None:
    def adapter_call() -> None:
        try:
            raise RuntimeError(f"HTTP 401 body={{'api_key': '{SECRET}'}}")
        except RuntimeError as exc:
            raise error_type() from exc

    with pytest.raises(ProviderError) as raised:
        adapter_call()
    error = raised.value
    assert error.code == code
    assert error.retryable is retryable
    assert error.diagnostic() == {
        "code": code.value,
        "message": error.message,
        "retryable": retryable,
    }
    for rendered in (str(error), repr(error), repr(error.args), str(error.diagnostic())):
        assert SECRET not in rendered
    assert SECRET in str(error.__cause__)


def test_only_transport_and_rate_limit_errors_are_retryable() -> None:
    retryable = {code for _, code, flag in ERRORS if flag}
    assert retryable == {ProviderErrorCode.TRANSPORT, ProviderErrorCode.RATE_LIMIT}
    assert UnsupportedCapabilityError(ProviderCapability.TOOL_CALLING).retryable is False


def test_unsupported_capability_names_only_the_capability() -> None:
    error = UnsupportedCapabilityError(ProviderCapability.PARALLEL_TOOL_CALLS)
    assert error.diagnostic() == {
        "code": "provider_unsupported_capability",
        "message": "Provider does not support a capability the request needs.",
        "retryable": False,
        "capability": "parallel_tool_calls",
    }


def test_parallel_calls_require_tool_calling() -> None:
    with pytest.raises(ValidationError) as raised:
        ProviderCapabilities(capabilities={ProviderCapability.PARALLEL_TOOL_CALLS})
    assert raised.value.errors()[0]["type"] == "provider_parallel_calls_without_tools"


@pytest.mark.parametrize(
    "request_,required",
    [
        (CompletionRequest(conversation=QUESTION), frozenset()),
        (
            CompletionRequest(conversation=QUESTION, tools=(SEARCH,)),
            {ProviderCapability.TOOL_CALLING},
        ),
        (
            CompletionRequest(
                conversation=Conversation(
                    messages=(
                        UserMessage(text="What is exposed?"),
                        AssistantMessage(tool_calls=(_call(),)),
                        ToolResultMessage(results=(ToolResult(call_id="call_1", content="{}"),)),
                    )
                )
            ),
            {ProviderCapability.TOOL_CALLING},
        ),
        (
            CompletionRequest(
                conversation=QUESTION,
                tools=(SEARCH,),
                limits=RequestLimits(max_tool_calls_per_turn=4),
            ),
            {ProviderCapability.TOOL_CALLING, ProviderCapability.PARALLEL_TOOL_CALLS},
        ),
    ],
)
def test_required_capabilities(request_: CompletionRequest, required: set) -> None:
    assert request_.required_capabilities() == required
    check_capabilities(ALL_CAPABILITIES, request_)


def test_unsupported_capabilities_are_rejected_before_any_call() -> None:
    # The first missing capability is reported; each adapter's refusal is in the contract.
    request = CompletionRequest(
        conversation=QUESTION, tools=(SEARCH,), limits=RequestLimits(max_tool_calls_per_turn=2)
    )
    provider = _EchoProvider(TEXT_ONLY)
    with pytest.raises(UnsupportedCapabilityError) as raised:
        complete(provider, request)
    assert raised.value.capability == ProviderCapability.TOOL_CALLING
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(complete_async(provider, request))
    assert provider.calls == 0


def test_request_rejects_duplicate_tool_names_and_bad_limits() -> None:
    with pytest.raises(ValidationError) as raised:
        CompletionRequest(conversation=QUESTION, tools=(SEARCH, SEARCH))
    assert raised.value.errors()[0]["type"] == "provider_tool_name_duplicate"
    for limits in (
        {"max_output_tokens": 0},
        {"max_output_tokens": 128_001},
        {"timeout_seconds": 0},
        {"timeout_seconds": 601},
        {"max_tool_calls_per_turn": 0},
        {"max_tool_calls_per_turn": 33},
        {"unknown": 1},
    ):
        with pytest.raises(ValidationError):
            RequestLimits.model_validate(limits)
    assert RequestLimits() == RequestLimits(
        max_output_tokens=4_096, timeout_seconds=60.0, max_tool_calls_per_turn=1
    )


@pytest.mark.parametrize(
    "message,stop_reason",
    [
        (AssistantMessage(text="Done."), StopReason.END),
        (AssistantMessage(tool_calls=(_call(),)), StopReason.TOOL_USE),
        (AssistantMessage(text="Partial", tool_calls=(_call(),)), StopReason.TOOL_USE),
        (AssistantMessage(text="Partial answer"), StopReason.LENGTH),
        (AssistantMessage(text="I cannot help."), StopReason.REFUSAL),
    ],
)
def test_response_round_trips(message: AssistantMessage, stop_reason: StopReason) -> None:
    response = CompletionResponse(
        message=message, usage=TokenUsage(input_tokens=5), stop_reason=stop_reason
    )
    assert CompletionResponse.model_validate_json(response.model_dump_json()) == response


@pytest.mark.parametrize(
    "message,stop_reason",
    [
        (AssistantMessage(text="Done."), StopReason.TOOL_USE),
        (AssistantMessage(tool_calls=(_call(),)), StopReason.END),
        (AssistantMessage(tool_calls=(_call(),)), StopReason.LENGTH),
        (AssistantMessage(tool_calls=(_call(),)), StopReason.REFUSAL),
    ],
)
def test_response_stop_reason_must_match_tool_calls(
    message: AssistantMessage, stop_reason: StopReason
) -> None:
    with pytest.raises(ValidationError) as raised:
        CompletionResponse(message=message, stop_reason=stop_reason)
    assert raised.value.errors()[0]["type"] == "provider_stop_reason_mismatch"


def test_request_round_trips_through_json() -> None:
    request = CompletionRequest(
        conversation=QUESTION, tools=(SEARCH,), limits=RequestLimits(timeout_seconds=5)
    )
    assert CompletionRequest.model_validate_json(request.model_dump_json()) == request


class _EchoProvider:
    """A trivial in-test implementation: no SDK, no network."""

    def __init__(self, capabilities: ProviderCapabilities = ALL_CAPABILITIES) -> None:
        self._capabilities = capabilities
        self.calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        last = request.conversation.messages[-1]
        return CompletionResponse(
            message=AssistantMessage(text=f"echo: {last.text}"),
            usage=TokenUsage(output_tokens=2),
            stop_reason=StopReason.END,
        )

    async def complete_async(self, request: CompletionRequest) -> CompletionResponse:
        return self.complete(request)


def test_protocol_is_satisfied_by_a_trivial_implementation() -> None:
    provider = _EchoProvider()
    assert isinstance(provider, Provider)
    request = CompletionRequest(conversation=QUESTION)
    expected = AssistantMessage(text="echo: What is exposed?")
    assert complete(provider, request).message == expected
    assert asyncio.run(complete_async(provider, request)).message == expected
    assert provider.calls == 2
