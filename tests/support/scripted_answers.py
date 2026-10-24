"""Scripted answer proposals and a run of the answer service over the network snapshot."""

from typing import Any

from archdiscov.assistant.answer_service import AnswerResult, answer_question
from archdiscov.assistant.fake import FakeClock, FakeProvider, FakeTranscript
from archdiscov.assistant.runs import RunLimits, RunTranscript
from archdiscov.assistant.tools.session import ToolSession
from tests.support.scripted_turns import SNAPSHOT, calls_turn
from tests.support.secrets import SECRET

ANSWER_QUESTION = f"Which resources are in the network? (token {SECRET})"
SEARCH = ("c1", "search_resources", {})
PLAIN_TEXT = "The network is fine, trust me."


def answer_proposal(session: ToolSession, **fields: Any) -> dict:
    return {
        "status": "insufficient_evidence",
        "contexts": [{"side": "primary", "source_kind": session.index.source_kind.value}],
        "insufficient_reason": "no_applicable_findings",
        "summary": f"Nothing applicable to the question {SECRET}.",
        **fields,
    }


def rejected_proposal(session: ToolSession) -> dict:
    """Overstated and citing an invented finding, so validation rejects it."""
    return answer_proposal(
        session,
        status="partial",
        insufficient_reason=None,
        summary="Invented reasoning about the network.",
        conclusions=[{"kind": "analyzer_finding", "citations": [{"finding_ids": ["invented"]}]}],
    )


def submit_turn(call_id: str, proposal: dict | None, **turn: Any) -> dict:
    return calls_turn((call_id, "submit_answer", proposal), **turn)


def answer_turns(
    turns, limits: RunLimits | None = None
) -> tuple[AnswerResult, FakeProvider, ToolSession]:
    session = ToolSession.from_paths(SNAPSHOT)
    clock = FakeClock()
    provider = FakeProvider(FakeTranscript.model_validate({"turns": turns(session)}), clock=clock)
    result = answer_question(
        ANSWER_QUESTION,
        provider=provider,
        session=session,
        provider_kind="fake",
        model="fake",
        limits=limits,
        clock=clock.monotonic,
    )
    transcript = result.transcript
    assert RunTranscript.model_validate_json(transcript.model_dump_json()) == transcript
    assert AnswerResult.model_validate_json(result.model_dump_json()) == result
    assert SECRET not in result.model_dump_json()
    return result, provider, session
