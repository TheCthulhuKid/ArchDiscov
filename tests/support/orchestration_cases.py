"""Fake-provider cases that reach every run outcome, keyed by outcome kind and detail."""

from typing import Any

from archdiscov.assistant.provider import ProviderCapabilities, ProviderErrorCode
from tests.support.scripted_turns import calls_turn, continued_turn, fail_turn, text_turn
from tests.support.secrets import SECRET

LOOP_QUESTION = f"Which resources are reachable? (token {SECRET})"
SECRET_SEARCH = ("c1", "search_resources", {"name": SECRET})
PROPOSAL = {
    "status": "insufficient_evidence",
    "contexts": [{"side": "primary", "source_kind": "aws"}],
    "insufficient_reason": "no_applicable_findings",
    "conclusions": [{"kind": "recorded_configuration", "citations": [{"evidence_ids": [SECRET]}]}],
    "summary": f"Nothing applicable; the token {SECRET} is not evidence.",
}


def _case(turns: list[dict], limits: dict | None = None, **options: Any) -> dict:
    return {"turns": turns, "limits": limits or {}, **options}


LOOP_CASES: dict[tuple[str, str], dict] = {
    ("answered", "end"): _case(
        [calls_turn(SECRET_SEARCH, text=f"Looking {SECRET}"), text_turn(f"Done {SECRET}")]
    ),
    ("answered", "length"): _case([text_turn("Partial", stop="length")]),
    ("answered", "refusal"): _case([text_turn("No.", stop="refusal")]),
    ("limit_reached", "provider_requests"): _case(
        [calls_turn(SECRET_SEARCH), calls_turn(("c2", "list_analyzers", {}))],
        {"max_provider_requests": 2},
    ),
    ("limit_reached", "tool_calls_per_turn"): _case(
        [calls_turn(SECRET_SEARCH, ("c2", "list_analyzers", {}), ignore_request_limits=True)]
    ),
    ("limit_reached", "tool_calls"): _case(
        [
            calls_turn(SECRET_SEARCH, ("c2", "list_analyzers", {})),
            calls_turn(("c3", "inspect_coverage", {})),
        ],
        {"max_tool_calls": 2, "max_tool_calls_per_turn": 2},
    ),
    ("limit_reached", "total_output"): _case(
        [calls_turn(("c1", "search_resources", {}))], {"max_total_output_chars": 1_000}
    ),
    ("limit_reached", "request_deadline"): _case(
        [calls_turn(SECRET_SEARCH, delay_seconds=20, ignore_request_limits=True)],
        {"request_timeout_seconds": 10},
        fake_only="an HTTP client cannot receive a response after its own read timeout",
    ),
    ("limit_reached", "overall_deadline"): _case(
        [calls_turn(SECRET_SEARCH, delay_seconds=10), {**text_turn(), "delay_seconds": 8}],
        {"request_timeout_seconds": 10, "overall_timeout_seconds": 15},
    ),
    ("malformed_output", "provider_reported"): _case([fail_turn("provider_malformed_output")]),
    ("malformed_output", "conversation_invalid"): _case(
        [calls_turn(SECRET_SEARCH), calls_turn(("c1", "list_analyzers", {}))]
    ),
    ("malformed_output", "tool_calls_without_tools"): _case(
        [calls_turn(SECRET_SEARCH)], registry=(), offer_answer_tool=False
    ),
    ("malformed_output", "proposal_malformed"): _case([calls_turn(("a", "submit_answer", None))]),
    ("malformed_output", "proposal_invalid"): _case(
        [calls_turn(("a", "submit_answer", {"status": "supported", "summary": SECRET}))]
    ),
    ("malformed_output", "proposal_mixed"): _case(
        [calls_turn(("a", "submit_answer", PROPOSAL), SECRET_SEARCH, ignore_request_limits=True)]
    ),
    ("malformed_output", "tool_not_offered"): _case(
        [text_turn(), calls_turn(SECRET_SEARCH)], repair=True
    ),
    ("malformed_output", "continuation_foreign"): _case(
        [continued_turn(calls_turn(SECRET_SEARCH), "other"), text_turn()],
        fake_only="an adapter marks every continuation with its own kind",
    ),
    ("proposed", ""): _case(
        [calls_turn(SECRET_SEARCH), calls_turn(("a", "submit_answer", PROPOSAL))]
    ),
    ("provider_error", "provider_unsupported_capability"): _case([text_turn()], capabilities=[]),
    **{
        ("provider_error", code.value): _case([calls_turn(SECRET_SEARCH), fail_turn(code.value)])
        for code in ProviderErrorCode
        if code
        not in {
            ProviderErrorCode.MALFORMED_OUTPUT,
            ProviderErrorCode.UNSUPPORTED_CAPABILITY,
            ProviderErrorCode.TIMEOUT,
        }
    },
    ("provider_error", "provider_timeout"): _case(
        [{**text_turn(), "delay_seconds": 20}], {"request_timeout_seconds": 10}
    ),
}


def case_capabilities(case: dict) -> ProviderCapabilities | None:
    if "capabilities" not in case:
        return None
    return ProviderCapabilities.model_validate({"capabilities": case["capabilities"]})
