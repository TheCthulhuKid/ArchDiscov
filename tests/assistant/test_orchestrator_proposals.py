import json
from typing import Any

import pytest

from archdiscov.assistant.answers import AnswerProposal
from archdiscov.assistant.conversation import AssistantMessage, ToolResultMessage
from archdiscov.assistant.runs import (
    Answered,
    MalformedOutput,
    MalformedOutputReason,
    Proposed,
    RunLimits,
)
from archdiscov.assistant.tools.contracts import IdKind
from archdiscov.assistant.tools.record import ReturnedIds
from tests.assistant.test_orchestrator import _calls, _run, _text

SECRET = "sk-ant-api03-SECRETvalue0123456789abcdefXYZ"


def _proposal(**fields: Any) -> dict:
    return {
        "status": "insufficient_evidence",
        "contexts": [{"side": "primary", "source_kind": "aws"}],
        "insufficient_reason": "no_applicable_findings",
        "summary": "No applicable findings were returned.",
        **fields,
    }


def _submit(arguments: dict | None, call_id: str = "answer") -> tuple[str, str, Any]:
    return (call_id, "submit_answer", arguments)


def test_a_proposal_after_tool_use_ends_the_run() -> None:
    result, provider, session = _run(
        [_calls(("c1", "search_resources", {})), _calls(_submit(_proposal()))]
    )
    assert result.outcome == Proposed(proposal=AnswerProposal.model_validate(_proposal()))
    messages = result.conversation.messages
    assert messages[-1].tool_calls[0].name == "submit_answer"
    assert not isinstance(messages[-1], ToolResultMessage)
    assert [len(turn.tool_calls) for turn in result.transcript.turns] == [1, 0]
    assert result.returned == session.record.returned()
    assert result.returned != ReturnedIds()
    assert provider.requests[-1].request.tools[-1].name == "submit_answer"


def test_a_proposal_without_tool_use_ends_the_run() -> None:
    result, _, session = _run([_calls(_submit(_proposal()))])
    assert isinstance(result.outcome, Proposed)
    assert session.record.returned() == ReturnedIds()


def test_submit_answer_is_never_dispatched_as_a_tool() -> None:
    resource = "123456789012/eu-west-1/ec2/physical/vpc-0example"
    proposal = _proposal(
        status="supported",
        insufficient_reason=None,
        conclusions=[
            {
                "kind": "recorded_configuration",
                "citations": [{"evidence_ids": ["e-1"], "resource_ids": [resource]}],
            }
        ],
    )
    result, _, session = _run([_calls(_submit(proposal))])
    assert result.outcome.proposal.conclusions[0].citations[0].resource_ids == (resource,)
    assert not session.record.returned().contains(
        IdKind.RESOURCE, session.index.snapshot_id, resource
    )


@pytest.mark.parametrize(
    "turn,reason",
    [
        (_calls(_submit(None)), MalformedOutputReason.PROPOSAL_MALFORMED),
        (_calls(_submit({"status": "supported"})), MalformedOutputReason.PROPOSAL_INVALID),
        (_calls(_submit(_proposal(confidence=1))), MalformedOutputReason.PROPOSAL_INVALID),
        (
            _calls(
                _submit(_proposal()), ("c2", "search_resources", {}), ignore_request_limits=True
            ),
            MalformedOutputReason.PROPOSAL_MIXED,
        ),
    ],
    ids=["malformed", "missing-fields", "unknown-field", "mixed"],
)
def test_unusable_proposals_are_malformed_output(turn: dict, reason: MalformedOutputReason) -> None:
    result, _, session = _run([turn])
    assert result.outcome == MalformedOutput(reason=reason)
    assert session.record.returned() == ReturnedIds()
    assert result.transcript.turns[0].tool_calls == ()


def test_a_reused_call_id_on_submit_is_an_invalid_conversation() -> None:
    result, _, _ = _run(
        [_calls(("c1", "search_resources", {})), _calls(_submit(_proposal(), "c1"))]
    )
    assert result.outcome == MalformedOutput(reason=MalformedOutputReason.CONVERSATION_INVALID)


def test_text_answers_are_still_answers_not_proposals() -> None:
    result, _, _ = _run([_text("Plain text.")])
    assert result.outcome == Answered(
        message=AssistantMessage(text="Plain text."), stop_reason="end"
    )


def test_submitting_does_not_count_against_the_tool_call_limit() -> None:
    result, _, _ = _run(
        [_calls(("c1", "search_resources", {})), _calls(_submit(_proposal()))],
        limits=RunLimits(max_tool_calls=1),
    )
    assert isinstance(result.outcome, Proposed)


def test_without_the_answer_tool_submit_answer_is_an_unknown_tool() -> None:
    result, provider, _ = _run([_calls(_submit(_proposal())), _text()], offer_answer_tool=False)
    assert "submit_answer" not in [tool.name for tool in provider.requests[0].request.tools]
    assert isinstance(result.outcome, Answered)
    error = provider.requests[1].request.conversation.messages[-1].results[0]
    assert json.loads(error.content)["code"] == "tool_unknown"


def test_proposal_transcripts_never_contain_secrets() -> None:
    proposal = _proposal(
        status="partial",
        insufficient_reason=None,
        conclusions=[{"kind": "analyzer_finding", "citations": [{"finding_ids": [SECRET]}]}],
        limitations=[{"finding_id": SECRET, "limitation": "coverage_failed"}],
        summary=f"Mentions {SECRET}.",
        explanation=f"Authorization: Bearer {SECRET}",
    )
    result, _, _ = _run([_calls(_submit(proposal), text=f"Submitting {SECRET}")])
    assert SECRET not in result.transcript.model_dump_json()
    assert result.outcome.proposal.summary == f"Mentions {SECRET}."


def test_a_registered_tool_named_submit_answer_is_refused_before_any_request() -> None:
    from archdiscov.assistant.tools.catalog import ALL_TOOLS
    from archdiscov.assistant.tools.registry import ToolDefinition, ToolRegistry

    clash = ToolDefinition("submit_answer", "Clashes.", ALL_TOOLS[0].input_model, ALL_TOOLS[0].run)
    with pytest.raises(ValueError, match="submit_answer"):
        _run([_text()], registry=ToolRegistry([clash]))
    result, _, _ = _run([_text()], registry=ToolRegistry([clash]), offer_answer_tool=False)
    assert isinstance(result.outcome, Answered)


def test_answer_contracts_do_not_load_the_tool_session() -> None:
    import subprocess
    import sys

    from tests.paths import PROJECT_ROOT

    script = (
        "import sys\n"
        "import archdiscov.assistant.answers, archdiscov.assistant.runs\n"
        "print(any(m in sys.modules for m in "
        "('archdiscov.graph', 'archdiscov.assistant.tools.session')))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, cwd=PROJECT_ROOT
    )
    assert completed.stdout.strip() == "False"


@pytest.mark.parametrize("arguments", [None, {"status": "supported"}], ids=["malformed", "invalid"])
def test_an_unusable_proposal_stays_in_the_conversation_for_repair(arguments) -> None:
    result, _, _ = _run([_calls(_submit(arguments))])
    assert isinstance(result.outcome, MalformedOutput)
    assert result.conversation.messages[-1].tool_calls[0].name == "submit_answer"


def test_a_mixed_proposal_is_not_added_to_the_conversation() -> None:
    result, _, _ = _run(
        [_calls(_submit(_proposal()), ("c2", "search_resources", {}), ignore_request_limits=True)]
    )
    assert result.outcome == MalformedOutput(reason=MalformedOutputReason.PROPOSAL_MIXED)
    assert [message.role for message in result.conversation.messages] == ["system", "user"]
