"""Every run outcome, through fake-provider transcripts, derived from the outcome types.

`EXPECTED` is computed from `RunOutcome`, `RunLimitKind`, `MalformedOutputReason`,
`ProviderErrorCode`, and the answer stop reasons, so a new outcome, limit, reason, or code
fails `test_every_outcome_is_covered` until a case is added below.
"""

import asyncio
import json
from typing import Any, get_args

import pytest

from archdiscov.assistant.fake import FakeClock, FakeProvider, FakeTranscript
from archdiscov.assistant.orchestrator import RunCancelled, RunResult, run_assistant_async
from archdiscov.assistant.provider import ProviderErrorCode
from archdiscov.assistant.runs import (
    Answered,
    MalformedOutputReason,
    RunLimitKind,
    RunLimits,
    RunOutcome,
    RunTranscript,
)
from archdiscov.assistant.tools.registry import ToolRegistry
from archdiscov.assistant.tools.session import ToolSession
from tests.assistant.test_orchestrator import SNAPSHOT, _calls, _fail, _text
from tests.assistant.test_orchestrator_async import _HangingProvider

SECRET = "sk-ant-api03-SECRETvalue0123456789abcdefXYZ"
QUESTION = f"Which resources are reachable? (token {SECRET})"


def _outcome_kinds() -> set[str]:
    return {member.model_fields["kind"].default for member in get_args(get_args(RunOutcome)[0])}


def _expected() -> set[tuple[str, str]]:
    stops = get_args(Answered.model_fields["stop_reason"].annotation)
    detail = {
        "answered": {stop.value for stop in stops},
        "limit_reached": {kind.value for kind in RunLimitKind},
        "malformed_output": {reason.value for reason in MalformedOutputReason},
        "provider_error": {
            code.value for code in ProviderErrorCode if code != ProviderErrorCode.MALFORMED_OUTPUT
        },
        "proposed": {""},
        "cancelled": {""},
    }
    assert set(detail) == _outcome_kinds()
    return {(kind, value) for kind, values in detail.items() for value in values}


def _identity(result: RunResult) -> tuple[str, str]:
    outcome = result.outcome
    value = {
        "answered": lambda: outcome.stop_reason.value,
        "limit_reached": lambda: outcome.limit.value,
        "malformed_output": lambda: outcome.reason.value,
        "provider_error": lambda: outcome.code.value,
        "proposed": lambda: "",
        "cancelled": lambda: "",
    }[outcome.kind]()
    return outcome.kind, value


SEARCH = ("c1", "search_resources", {"name": SECRET})
PROPOSAL = {
    "status": "insufficient_evidence",
    "contexts": [{"side": "primary", "source_kind": "aws"}],
    "insufficient_reason": "no_applicable_findings",
    "conclusions": [{"kind": "recorded_configuration", "citations": [{"evidence_ids": [SECRET]}]}],
    "summary": f"Nothing applicable; the token {SECRET} is not evidence.",
}


def _case(turns: list[dict], limits: dict | None = None, **options: Any) -> dict:
    return {"turns": turns, "limits": limits or {}, **options}


CASES: dict[tuple[str, str], dict] = {
    ("answered", "end"): _case([_calls(SEARCH, text=f"Looking {SECRET}"), _text(f"Done {SECRET}")]),
    ("answered", "length"): _case([_text("Partial", stop="length")]),
    ("answered", "refusal"): _case([_text("No.", stop="refusal")]),
    ("limit_reached", "provider_requests"): _case(
        [_calls(SEARCH), _calls(("c2", "list_analyzers", {}))], {"max_provider_requests": 2}
    ),
    ("limit_reached", "tool_calls_per_turn"): _case(
        [_calls(SEARCH, ("c2", "list_analyzers", {}), ignore_request_limits=True)]
    ),
    ("limit_reached", "tool_calls"): _case(
        [_calls(SEARCH, ("c2", "list_analyzers", {})), _calls(("c3", "inspect_coverage", {}))],
        {"max_tool_calls": 2, "max_tool_calls_per_turn": 2},
    ),
    ("limit_reached", "total_output"): _case(
        [_calls(("c1", "search_resources", {}))], {"max_total_output_chars": 1_000}
    ),
    ("limit_reached", "request_deadline"): _case(
        [_calls(SEARCH, delay_seconds=20, ignore_request_limits=True)],
        {"request_timeout_seconds": 10},
    ),
    ("limit_reached", "overall_deadline"): _case(
        [_calls(SEARCH, delay_seconds=10), {**_text(), "delay_seconds": 8}],
        {"request_timeout_seconds": 10, "overall_timeout_seconds": 15},
    ),
    ("malformed_output", "provider_reported"): _case([_fail("provider_malformed_output")]),
    ("malformed_output", "conversation_invalid"): _case(
        [_calls(SEARCH), _calls(("c1", "list_analyzers", {}))]
    ),
    ("malformed_output", "tool_calls_without_tools"): _case(
        [_calls(SEARCH)], registry=(), offer_answer_tool=False
    ),
    ("malformed_output", "proposal_malformed"): _case([_calls(("a", "submit_answer", None))]),
    ("malformed_output", "proposal_invalid"): _case(
        [_calls(("a", "submit_answer", {"status": "supported", "summary": SECRET}))]
    ),
    ("malformed_output", "proposal_mixed"): _case(
        [_calls(("a", "submit_answer", PROPOSAL), SEARCH, ignore_request_limits=True)]
    ),
    ("proposed", ""): _case([_calls(SEARCH), _calls(("a", "submit_answer", PROPOSAL))]),
    ("provider_error", "provider_unsupported_capability"): _case([_text()], capabilities=[]),
    **{
        ("provider_error", code.value): _case([_calls(SEARCH), _fail(code.value)])
        for code in ProviderErrorCode
        if code
        not in {
            ProviderErrorCode.MALFORMED_OUTPUT,
            ProviderErrorCode.UNSUPPORTED_CAPABILITY,
            ProviderErrorCode.TIMEOUT,
        }
    },
    ("provider_error", "provider_timeout"): _case(
        [{**_text(), "delay_seconds": 20}], {"request_timeout_seconds": 10}
    ),
}


def _run_case(case: dict) -> RunResult:
    data: dict[str, Any] = {"turns": case["turns"]}
    if "capabilities" in case:
        data["capabilities"] = {"capabilities": case["capabilities"]}
    clock = FakeClock()
    provider = FakeProvider(FakeTranscript.model_validate(data), clock=clock)
    registry = ToolRegistry(case["registry"]) if "registry" in case else None
    offer_answer_tool = case.get("offer_answer_tool", True)
    return asyncio.run(
        run_assistant_async(
            QUESTION,
            provider=provider,
            session=ToolSession.from_paths(SNAPSHOT),
            provider_kind="fake",
            model="fake",
            limits=RunLimits.model_validate(case["limits"]),
            registry=registry,
            clock=clock.monotonic,
            offer_answer_tool=offer_answer_tool,
        )
    )


def _run_cancelled() -> RunResult:
    provider = _HangingProvider(answer_first=True)

    async def scenario() -> RunResult:
        task = asyncio.create_task(
            run_assistant_async(
                QUESTION,
                provider=provider,
                session=ToolSession.from_paths(SNAPSHOT),
                provider_kind="fake",
                model="fake",
            )
        )
        await provider.entered.wait()
        task.cancel()
        try:
            await task
        except RunCancelled as error:
            return error.result
        raise AssertionError("expected RunCancelled")

    return asyncio.run(scenario())


def _result(identity: tuple[str, str]) -> RunResult:
    return _run_cancelled() if identity == ("cancelled", "") else _run_case(CASES[identity])


def test_every_outcome_is_covered() -> None:
    assert set(CASES) | {("cancelled", "")} == _expected()


@pytest.mark.parametrize(
    "identity", sorted(_expected()), ids=lambda item: "-".join(filter(None, item))
)
def test_each_outcome_is_reached_exactly(identity: tuple[str, str]) -> None:
    result = _result(identity)
    assert _identity(result) == identity
    transcript = result.transcript
    assert RunTranscript.model_validate_json(transcript.model_dump_json()) == transcript
    assert _identity(result.model_copy(update={"outcome": transcript.outcome})) == identity
    assert len(transcript.turns) <= transcript.limits.max_provider_requests
    assert result.returned == result.returned.model_validate_json(result.returned.model_dump_json())


@pytest.mark.parametrize(
    "identity", sorted(_expected()), ids=lambda item: "-".join(filter(None, item))
)
def test_no_transcript_leaks_a_secret(identity: tuple[str, str]) -> None:
    transcript = _result(identity).transcript
    rendered = [
        transcript.model_dump_json(),
        repr(transcript),
        json.dumps(transcript.model_dump(mode="json")),
    ]
    for text in rendered:
        assert SECRET not in text
        assert SECRET[:20] not in text


def test_the_answered_result_keeps_the_raw_answer_for_validation() -> None:
    result = _result(("answered", "end"))
    assert result.outcome.message.text == f"Done {SECRET}"
    assert SECRET not in result.transcript.outcome.message.text
