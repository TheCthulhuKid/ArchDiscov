"""Behaviour every provider must share; the acceptance gate for each adapter.

Most cases run over every harness, each adapter serving them from its own mocked API. The
shared retry policy and the HTTP clients' guard are each tested once here, so an adapter's own
tests cover only what differs: its request and response mapping, usage, and continuations.
"""

import asyncio
import logging

import anthropic
import botocore.session
import httpx2
import openai
import pytest
from botocore.exceptions import ClientError

from archdiscov.assistant import anthropic_provider, openai_provider
from archdiscov.assistant.adapters import (
    HTTP_HEADERS,
    MAX_RETRY_WAIT_SECONDS,
    RETRY_BACKOFF_SECONDS,
    Retries,
    retry_wait,
)
from archdiscov.assistant.bedrock_provider import API_ERRORS
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
    ProviderTransportError,
    RequestLimits,
    StopReason,
    UnsupportedCapabilityError,
    complete,
    complete_async,
)
from tests.support.mock_api import Waits
from tests.support.provider_data import INSPECT_TOOL, SEARCH_TOOL
from tests.support.provider_harness import (
    HARNESS_KEY,
    AnthropicHarness,
    BedrockHarness,
    ChatCompletionsHarness,
    MockedHarness,
    ProviderHarness,
    ResponsesHarness,
)
from tests.support.secrets import secret_renderings

SECRET = "sk-ant-api03-SECRETvalue0123456789abcdefXYZ"
TEXT_ONLY = ProviderCapabilities()
RETRYABLE = [ProviderErrorCode.TRANSPORT, ProviderErrorCode.RATE_LIMIT]
# Every error a turn can script; each harness serves each one as its API would.
SCRIPTED = list(AnthropicHarness.failures)
# The adapters that send HTTP through the shared guarded clients, and every mocked adapter.
HTTP: list[MockedHarness] = [AnthropicHarness(), ChatCompletionsHarness(), ResponsesHarness()]
MOCKED: list[MockedHarness] = [*HTTP, BedrockHarness()]
QUESTION = "Which databases"


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


@pytest.mark.parametrize("code", SCRIPTED, ids=lambda code: code.value)
def test_errors_carry_stable_codes_and_safe_diagnostics(
    harness: ProviderHarness, code: ProviderErrorCode, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
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
    # The mocked APIs' error bodies echo the request, so a leak would show here.
    for rendered in (str(error), repr(error), str(error.diagnostic()), caplog.text):
        assert SECRET not in rendered
        assert QUESTION not in rendered


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


# ---- the shared retry policy -------------------------------------------------------


class _Refused(Exception):
    """An SDK failure that the retry table maps to a retryable transport error."""


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_retries_back_off_exponentially(mode: str) -> None:
    waits = Waits()
    retries = Retries({_Refused: ProviderTransportError}, 2, waits.sleep, waits.sleep_async)
    outcomes = iter([_Refused(), _Refused(), "sent"])

    def attempt() -> str:
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def attempt_async() -> str:
        return attempt()

    if mode == "sync":
        sent = retries.call(attempt, timeout_seconds=60)
    else:
        sent = asyncio.run(retries.call_async(attempt_async, timeout_seconds=60))
    assert sent == "sent"
    assert waits == [RETRY_BACKOFF_SECONDS, RETRY_BACKOFF_SECONDS * 2]


def _throttled(retry_after: str) -> openai.RateLimitError:
    response = httpx2.Response(
        429, headers={"retry-after": retry_after}, request=httpx2.Request("POST", "https://x")
    )
    return openai.RateLimitError("Throttled.", response=response, body=None)


def _throttled_converse(headers: dict | None) -> ClientError:
    """Botocore's parsed response, whose metadata holds the lower-cased HTTP headers."""
    response = {"Error": {"Code": "ThrottlingException"}}
    if headers is not None:
        response["ResponseMetadata"] = {"HTTPHeaders": headers}
    return ClientError(response, "Converse")


@pytest.mark.parametrize(
    ("error", "timeout", "wait"),
    [
        (_throttled("2"), 60, 2.0),
        (_throttled("20"), 10, 10.0),
        (_throttled("120"), 60, MAX_RETRY_WAIT_SECONDS),
        (_throttled("soon"), 60, RETRY_BACKOFF_SECONDS),
        (_throttled_converse({"retry-after": "2"}), 60, 2.0),
        (_throttled_converse(None), 60, RETRY_BACKOFF_SECONDS),
    ],
    ids=["honoured", "request-timeout", "capped", "unusable", "converse", "converse-bare"],
)
def test_retry_after_is_honoured_within_bounds(error: Exception, timeout: int, wait) -> None:
    assert retry_wait(error, 0, timeout) == wait


def _unmapped_sdk_errors(sdk, errors) -> set[type]:
    # Copied first: reading an SDK's lazy attributes can add module entries.
    members = list(vars(sdk).values())
    classes = {cls for cls in members if isinstance(cls, type) and issubclass(cls, sdk.APIError)}
    return {cls for cls in classes if not any(base in errors for base in cls.__mro__)}


def _unmapped_converse_errors() -> set[str]:
    model = botocore.session.get_session().get_service_model("bedrock-runtime")
    return {shape.name for shape in model.operation_model("Converse").error_shapes} - set(
        API_ERRORS
    )


UNMAPPED = {
    "anthropic": lambda: _unmapped_sdk_errors(anthropic, anthropic_provider.SDK_ERRORS),
    "openai_compatible": lambda: _unmapped_sdk_errors(openai, openai_provider.SDK_ERRORS),
    "bedrock": _unmapped_converse_errors,
}


@pytest.mark.parametrize("kind", UNMAPPED)
def test_every_sdk_failure_has_a_provider_error(kind: str) -> None:
    assert UNMAPPED[kind]() == set()


# ---- the adapters' clients ---------------------------------------------------------

HOSTILE_ENVIRONMENT = {
    "ANTHROPIC_API_KEY": "sk-ant-api03-environmentKEY0123456789abcdef",
    "ANTHROPIC_AUTH_TOKEN": "environment-token",
    "ANTHROPIC_BASE_URL": "https://elsewhere.example.com",
    "ANTHROPIC_CUSTOM_HEADERS": "Authorization: Bearer injected\nX-Injected: 1",
    "OPENAI_API_KEY": "sk-proj-environmentKEY0123456789abcdef",
    "OPENAI_BASE_URL": "https://elsewhere.example.com/v1",
    "OPENAI_ORG_ID": "org-someone-else",
    "OPENAI_PROJECT_ID": "proj-someone-else",
    "OPENAI_CUSTOM_HEADERS": "Authorization: Bearer injected\nX-Injected: 1",
    "HTTPS_PROXY": "http://proxy.invalid:3128",
    "ALL_PROXY": "http://proxy.invalid:3128",
}
# The headers an SDK adds to the shared ones; `x-stainless-*` describe the SDK itself.
SDK_HEADERS = {"authorization", "anthropic-version"}


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("harness", HTTP, ids=lambda harness: harness.label)
def test_only_the_configuration_chooses_the_target_and_credential(
    monkeypatch, harness: MockedHarness, mode: str
) -> None:
    for name, value in HOSTILE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    provider = harness.build([_respond(TEXT)])
    if mode == "sync":
        complete(provider, _request())
    else:
        send(provider, _request())
    [sent] = harness.requests(provider)
    assert str(sent.url).startswith(provider.base_url)
    assert sent.headers["authorization"] == f"Bearer {HARNESS_KEY.get_secret_value()}"
    names = {name for name in sent.headers if not name.startswith("x-stainless-")}
    assert names <= HTTP_HEADERS | SDK_HEADERS
    sent_text = sent.content.decode() + str(dict(sent.headers))
    assert not any(value in sent_text for value in HOSTILE_ENVIRONMENT.values())


@pytest.mark.parametrize("harness", HTTP, ids=lambda harness: harness.label)
def test_redirects_are_not_followed(harness: MockedHarness) -> None:
    elsewhere = httpx2.Response(307, headers={"location": "https://elsewhere.example.com/v1"})
    provider = harness.serving([elsewhere])
    with pytest.raises(ProviderError):
        send(provider, _request())
    hosts = [request.url.host for request in harness.requests(provider)]
    assert hosts == [httpx2.URL(provider.base_url).host]


@pytest.mark.parametrize("harness", MOCKED, ids=lambda harness: harness.label)
def test_no_key_or_request_reaches_a_rendering_or_log(harness: MockedHarness, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    for name in harness.loggers:
        logging.getLogger(name).setLevel(logging.DEBUG)
    provider = harness.build([_fail(ProviderErrorCode.INVALID_REQUEST)])
    # Building the adapter quiets its SDK's loggers, which would log requests at debug.
    for name in harness.loggers:
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING
    with pytest.raises(ProviderError) as raised:
        send(provider, _request())
    for rendering in (*secret_renderings(provider), *secret_renderings(raised.value), caplog.text):
        for leaked in (HARNESS_KEY.get_secret_value(), SECRET, QUESTION):
            assert leaked not in rendering
