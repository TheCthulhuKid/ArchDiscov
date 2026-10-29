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
    PROVIDER_ERROR_MESSAGES,
    CompletionRequest,
    CompletionResponse,
    Provider,
    ProviderCapabilities,
    ProviderCapability,
    ProviderError,
    ProviderErrorCode,
    ProviderRateLimitError,
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


def test_errors_carry_fixed_messages_and_keep_their_cause_out_of_them() -> None:
    # Each adapter's mapping to every code is in `test_provider_contract.py`.
    assert set(PROVIDER_ERROR_MESSAGES) == set(ProviderErrorCode)

    def adapter_call() -> None:
        try:
            raise RuntimeError(f"HTTP 429 body={{'api_key': '{SECRET}'}}")
        except RuntimeError as exc:
            raise ProviderRateLimitError() from exc

    with pytest.raises(ProviderError) as raised:
        adapter_call()
    error = raised.value
    assert error.diagnostic() == {
        "code": "provider_rate_limit",
        "message": PROVIDER_ERROR_MESSAGES[ProviderErrorCode.RATE_LIMIT],
        "retryable": True,
    }
    for rendered in (str(error), repr(error), repr(error.args), str(error.diagnostic())):
        assert SECRET not in rendered
    assert SECRET in str(error.__cause__)


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


def test_request_rejects_duplicate_tool_names() -> None:
    with pytest.raises(ValidationError) as raised:
        CompletionRequest(conversation=QUESTION, tools=(SEARCH, SEARCH))
    assert raised.value.errors()[0]["type"] == "provider_tool_name_duplicate"


def test_response_stop_reason_must_match_tool_calls() -> None:
    for message, stop_reason in (
        (AssistantMessage(text="Done."), StopReason.TOOL_USE),
        (AssistantMessage(tool_calls=(_call(),)), StopReason.END),
    ):
        with pytest.raises(ValidationError) as raised:
            CompletionResponse(message=message, stop_reason=stop_reason)
        assert raised.value.errors()[0]["type"] == "provider_stop_reason_mismatch"


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


def test_a_trivial_implementation_is_called_only_with_supported_requests() -> None:
    provider = _EchoProvider()
    assert isinstance(provider, Provider)
    request = CompletionRequest(conversation=QUESTION)
    expected = AssistantMessage(text="echo: What is exposed?")
    assert complete(provider, request).message == expected
    assert asyncio.run(complete_async(provider, request)).message == expected
    assert provider.calls == 2
    # The first missing capability is reported, before any call.
    text_only = _EchoProvider(TEXT_ONLY)
    request = CompletionRequest(
        conversation=QUESTION, tools=(SEARCH,), limits=RequestLimits(max_tool_calls_per_turn=2)
    )
    with pytest.raises(UnsupportedCapabilityError) as raised:
        complete(text_only, request)
    assert raised.value.capability == ProviderCapability.TOOL_CALLING
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(complete_async(text_only, request))
    assert text_only.calls == 0
