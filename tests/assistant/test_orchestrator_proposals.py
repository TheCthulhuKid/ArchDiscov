import json
from typing import Any

import pytest

from archdiscov.assistant.answers import AnswerProposal
from archdiscov.assistant.conversation import ToolResultMessage
from archdiscov.assistant.runs import (
    Answered,
    MalformedOutput,
    MalformedOutputReason,
    Proposed,
    RunLimits,
)
from archdiscov.assistant.tools.contracts import IdKind
from archdiscov.assistant.tools.record import ReturnedIds
from tests.support.scripted_turns import calls_turn, run_turns, text_turn


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
    result, provider, session = run_turns(
        [calls_turn(("c1", "search_resources", {})), calls_turn(_submit(_proposal()))]
    )
    assert result.outcome == Proposed(proposal=AnswerProposal.model_validate(_proposal()))
    messages = result.conversation.messages
    assert messages[-1].tool_calls[0].name == "submit_answer"
    assert not isinstance(messages[-1], ToolResultMessage)
    assert [len(turn.tool_calls) for turn in result.transcript.turns] == [1, 0]
    assert result.returned == session.record.returned()
    assert result.returned != ReturnedIds()
    assert provider.requests[-1].request.tools[-1].name == "submit_answer"


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
    result, _, session = run_turns([calls_turn(_submit(proposal))])
    assert result.outcome.proposal.conclusions[0].citations[0].resource_ids == (resource,)
    assert not session.record.returned().contains(
        IdKind.RESOURCE, session.index.snapshot_id, resource
    )


@pytest.mark.parametrize(
    "turn,reason,kept",
    [
        (calls_turn(_submit(None)), MalformedOutputReason.PROPOSAL_MALFORMED, True),
        (
            calls_turn(
                _submit(_proposal()), ("c2", "search_resources", {}), ignore_request_limits=True
            ),
            MalformedOutputReason.PROPOSAL_MIXED,
            False,
        ),
    ],
    ids=["malformed", "mixed"],
)
def test_unusable_proposals_are_malformed_output(
    turn: dict, reason: MalformedOutputReason, kept: bool
) -> None:
    result, _, session = run_turns([turn])
    assert result.outcome == MalformedOutput(reason=reason)
    assert session.record.returned() == ReturnedIds()
    assert result.transcript.turns[0].tool_calls == ()
    # An unreadable submission stays in the conversation for its repair; a mixed turn does not.
    messages = result.conversation.messages
    if kept:
        assert messages[-1].tool_calls[0].name == "submit_answer"
    else:
        assert [message.role for message in messages] == ["system", "user"]


def test_submitting_does_not_count_against_the_tool_call_limit() -> None:
    result, _, _ = run_turns(
        [calls_turn(("c1", "search_resources", {})), calls_turn(_submit(_proposal()))],
        limits=RunLimits(max_tool_calls=1),
    )
    assert isinstance(result.outcome, Proposed)


def test_without_the_answer_tool_submit_answer_is_an_unknown_tool() -> None:
    result, provider, _ = run_turns(
        [calls_turn(_submit(_proposal())), text_turn()], offer_answer_tool=False
    )
    assert "submit_answer" not in [tool.name for tool in provider.requests[0].request.tools]
    assert isinstance(result.outcome, Answered)
    error = provider.requests[1].request.conversation.messages[-1].results[0]
    assert json.loads(error.content)["code"] == "tool_unknown"


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
