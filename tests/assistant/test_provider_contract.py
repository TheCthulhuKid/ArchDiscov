"""Behaviour every provider must share; the acceptance gate for each adapter."""

import asyncio

import pytest

from archdiscov.assistant.conversation import (
    AssistantMessage,
    Conversation,
    MalformedArgumentsReason,
    MalformedToolArguments,
    ParsedToolArguments,
    SystemMessage,
    TokenUsage,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from archdiscov.assistant.provider import (
    CompletionRequest,
    CompletionResponse,
    Provider,
    ProviderCapabilities,
    ProviderCapability,
    ProviderError,
    ProviderErrorCode,
    RequestLimits,
    StopReason,
    UnsupportedCapabilityError,
    complete_async,
)
from tests.assistant.provider_harness import ProviderHarness
from tests.support.provider_data import INSPECT_TOOL, SEARCH_TOOL

SECRET = "sk-ant-api03-SECRETvalue0123456789abcdefXYZ"
TEXT_ONLY = ProviderCapabilities()
RETRYABLE = [ProviderErrorCode.TRANSPORT, ProviderErrorCode.RATE_LIMIT]


def send(provider: Provider, completion: CompletionRequest) -> CompletionResponse:
    """Send through the checked async entry point; each adapter's tests cover its sync form."""
    return asyncio.run(complete_async(provider, completion))


_LIMITS = RequestLimits(max_tool_calls_per_turn=4)


def _request(*messages, tools=(SEARCH_TOOL, INSPECT_TOOL), limits=_LIMITS) -> CompletionRequest:
    return CompletionRequest(
        conversation=Conversation(
            messages=(
                SystemMessage(text="Answer only from tool results."),
                UserMessage(text=f"Which databases are reachable? {SECRET}"),
                *messages,
            )
        ),
        tools=tools,
        limits=limits,
    )


def _respond(response: CompletionResponse, **turn) -> dict:
    return {"kind": "respond", "response": response.model_dump(mode="json"), **turn}


def _fail(code: ProviderErrorCode, **turn) -> dict:
    return {"kind": "fail", "error": code.value, **turn}


TEXT = CompletionResponse(
    message=AssistantMessage(text="No databases were recorded."),
    usage=TokenUsage(input_tokens=42, output_tokens=7),
    stop_reason=StopReason.END,
)


def _calls(*names_and_arguments) -> tuple[ToolCall, ...]:
    return tuple(
        ToolCall(id=f"call_{index}", name=name, arguments=arguments)
        for index, (name, arguments) in enumerate(names_and_arguments)
    )


# ---- message mapping ---------------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        TEXT,
        CompletionResponse(
            message=AssistantMessage(
                text="Searching.",
                tool_calls=_calls(
                    ("search_resources", ParsedToolArguments(value={"type": "rds_instance"}))
                ),
            ),
            usage=TokenUsage(input_tokens=10, output_tokens=5, cache_read_input_tokens=3),
            stop_reason=StopReason.TOOL_USE,
        ),
        CompletionResponse(
            message=AssistantMessage(
                tool_calls=_calls(
                    ("inspect_resource", ParsedToolArguments(value={"resource_id": "db-a"})),
                    (
                        "inspect_resource",
                        MalformedToolArguments(reason=MalformedArgumentsReason.INVALID_JSON),
                    ),
                    (
                        "search_resources",
                        MalformedToolArguments(reason=MalformedArgumentsReason.NOT_OBJECT),
                    ),
                )
            ),
            stop_reason=StopReason.TOOL_USE,
        ),
        CompletionResponse(
            message=AssistantMessage(text="The answer was cut"), stop_reason=StopReason.LENGTH
        ),
        CompletionResponse(
            message=AssistantMessage(text="I cannot help."), stop_reason=StopReason.REFUSAL
        ),
    ],
    ids=["text", "single-call", "parallel-malformed", "length", "refusal"],
)
def test_responses_map_to_the_shared_contracts(
    harness: ProviderHarness, response: CompletionResponse
) -> None:
    provider = harness.build([_respond(response)])
    assert send(provider, _request()) == response
    assert harness.attempts(provider) == 1


def test_conversations_with_tool_exchanges_are_accepted(harness: ProviderHarness) -> None:
    calls = _calls(("search_resources", ParsedToolArguments(value={})))
    exchange = (
        AssistantMessage(tool_calls=calls),
        ToolResultMessage(
            results=(ToolResult(call_id="call_0", content="Unknown type.", is_error=True),)
        ),
    )
    provider = harness.build([_respond(TEXT)])
    assert send(provider, _request(*exchange)) == TEXT


# ---- errors ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code", [*RETRYABLE, ProviderErrorCode.AUTHENTICATION], ids=lambda code: code.value
)
def test_errors_carry_stable_codes_and_safe_diagnostics(
    harness: ProviderHarness, code: ProviderErrorCode
) -> None:
    provider = harness.build([_fail(code), _respond(TEXT)])
    with pytest.raises(ProviderError) as raised:
        send(provider, _request())
    error = raised.value
    assert error.code == code
    assert error.retryable is (code in RETRYABLE)
    assert error.diagnostic() == {
        "code": code.value,
        "message": error.message,
        "retryable": error.retryable,
    }
    for rendered in (str(error), repr(error), str(error.diagnostic())):
        assert SECRET not in rendered
        assert "Which databases" not in rendered


@pytest.mark.parametrize("code", RETRYABLE, ids=lambda code: code.value)
def test_only_retryable_errors_are_retried_up_to_the_configured_count(
    harness: ProviderHarness, code: ProviderErrorCode
) -> None:
    provider = harness.build([_fail(code), _fail(code), _respond(TEXT)], max_retries=2)
    assert send(provider, _request()) == TEXT
    assert harness.attempts(provider) == 3

    provider = harness.build(
        [_fail(ProviderErrorCode.AUTHENTICATION), _respond(TEXT)], max_retries=3
    )
    with pytest.raises(ProviderError):
        send(provider, _request())
    assert harness.attempts(provider) == 1


# ---- capabilities ------------------------------------------------------------------


@pytest.mark.parametrize(
    "capabilities,request_,missing",
    [
        (TEXT_ONLY, _request(), ProviderCapability.TOOL_CALLING),
        (
            ProviderCapabilities(capabilities={ProviderCapability.TOOL_CALLING}),
            _request(),
            ProviderCapability.PARALLEL_TOOL_CALLS,
        ),
        (
            TEXT_ONLY,
            _request(
                AssistantMessage(
                    tool_calls=_calls(("search_resources", ParsedToolArguments(value={})))
                ),
                ToolResultMessage(results=(ToolResult(call_id="call_0", content="{}"),)),
                tools=(),
                limits=RequestLimits(),
            ),
            ProviderCapability.TOOL_CALLING,
        ),
    ],
    ids=["tools-offered", "parallel-calls", "prior-tool-exchange"],
)
def test_unsupported_capabilities_are_rejected_before_any_attempt(
    harness: ProviderHarness,
    capabilities: ProviderCapabilities,
    request_: CompletionRequest,
    missing: ProviderCapability,
) -> None:
    provider = harness.build([_respond(TEXT)], capabilities=capabilities)
    assert provider.capabilities == capabilities
    with pytest.raises(UnsupportedCapabilityError) as raised:
        send(provider, request_)
    assert raised.value.capability == missing
    assert harness.attempts(provider) == 0


def test_text_only_providers_still_answer_text_requests(harness: ProviderHarness) -> None:
    provider = harness.build([_respond(TEXT)], capabilities=TEXT_ONLY)
    assert send(provider, _request(tools=(), limits=RequestLimits())) == TEXT


# ---- limits ------------------------------------------------------------------------


def test_requests_beyond_the_timeout_fail_as_timeouts_without_retry(
    harness: ProviderHarness,
) -> None:
    provider = harness.build([_respond(TEXT, delay_seconds=20), _respond(TEXT)], max_retries=2)
    with pytest.raises(ProviderError) as raised:
        send(provider, _request(limits=RequestLimits(timeout_seconds=10)))
    assert raised.value.code == ProviderErrorCode.TIMEOUT
    assert harness.attempts(provider) == 1


def test_requests_within_the_timeout_succeed(harness: ProviderHarness) -> None:
    provider = harness.build([_respond(TEXT, delay_seconds=9.5)])
    assert send(provider, _request(limits=RequestLimits(timeout_seconds=10))) == TEXT
