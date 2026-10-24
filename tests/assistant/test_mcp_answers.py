"""Answers over MCP equal answers in `ask`: the same rejections and the same renderings.

Every rejection scenario of the answer suite, and the checked-in adversarial proposals, are
submitted through the server after their tool calls and rejected with exactly the codes and
locations `validate_proposal` gives over the same calls. Every README example and the
injected-text case are accepted with exactly the readable answer and answer JSON that
`answer_question` renders from the same transcript with the fake provider.
"""

import json
from pathlib import Path

import pytest

from archdiscov.assistant.answer_output import answer_data, answer_text
from archdiscov.assistant.answer_service import answer_question
from archdiscov.assistant.answer_validation import validate_proposal
from archdiscov.assistant.answers import AnswerProposal
from archdiscov.assistant.conversation import ParsedToolArguments, ToolCall
from archdiscov.assistant.fake import FakeProvider, load_fake_transcript
from archdiscov.assistant.mcp_server import PRESENT_ANSWER
from archdiscov.assistant.rejections import RejectionCode
from archdiscov.assistant.tools.catalog import default_registry
from archdiscov.assistant.tools.session import ToolSession
from archdiscov.services.evaluation import prepare_evaluation
from tests.support.answer_claims import INJECTION
from tests.support.answer_scenarios import REJECTION_SCENARIOS
from tests.support.evaluation_suites import ADVERSARIAL_SUITE, README_SUITE, write_fake_config
from tests.support.mcp_clients import Call, mcp_replies, transcript_calls

# The adversarial cases whose proposal is rejected, and the code each must include.
ADVERSARIAL = {
    "fabricated_finding": RejectionCode.UNKNOWN_ID,
    "unreturned_evidence": RejectionCode.NOT_RETURNED,
    "status_upgrade": RejectionCode.STATUS_UPGRADE,
    "injected_narrative": RejectionCode.NARRATIVE_IMPORTED_TEXT,
}


def _expected_rejections(session: ToolSession, calls: list[Call], proposal: dict) -> list:
    """What `validate_proposal` rejects after the same calls over a fresh session copy."""
    fresh, registry = session.fresh(), default_registry()
    for number, (name, arguments) in enumerate(calls):
        arguments = ParsedToolArguments(value=arguments or {})
        registry.dispatch(fresh, ToolCall(id=f"c{number}", name=name, arguments=arguments))
    rejections = validate_proposal(AnswerProposal.model_validate(proposal), fresh)
    return [(item.code.value, item.location) for item in rejections]


def _rejected(session: ToolSession, calls: list[Call], proposal: dict) -> list:
    *_, (is_error, texts) = mcp_replies(session, [*calls, ("submit_answer", proposal)])
    assert is_error, texts
    [text] = texts
    data = json.loads(text)
    assert data["error"] == "answer_rejected" and data["unlisted"] == 0
    return [(item["code"], item["location"]) for item in data["rejections"]]


@pytest.mark.parametrize("code", list(RejectionCode), ids=lambda code: code.value)
def test_each_rejection_scenario_is_rejected_as_in_ask(code: RejectionCode, tmp_path) -> None:
    session, calls, proposal = REJECTION_SCENARIOS[code](tmp_path)
    served = _rejected(session, calls, proposal)
    assert served == _expected_rejections(session, calls, proposal)
    assert {served_code for served_code, _ in served} == {code.value}


def _cases(suite: Path, tmp_path: Path, ids: set[str] | None = None) -> dict[str, tuple]:
    """The named cases (all by default): each one's verified session, tool calls, submitted
    proposals, and transcript path."""
    prepared = prepare_evaluation(suite, [write_fake_config(tmp_path / "fake.toml")])
    cases = {}
    for case, session in zip(prepared.suite.cases, prepared.sessions, strict=True):
        if ids is not None and case.id not in ids:
            continue
        path = suite.parent / case.fake_transcript
        turns = json.loads(path.read_text(encoding="utf-8"))["turns"]
        proposals = [
            call["arguments"]["value"]
            for turn in turns
            for call in turn["response"]["message"]["tool_calls"]
            if call["name"] == "submit_answer"
        ]
        cases[case.id] = (case, session, transcript_calls(path), proposals, path)
    return cases


@pytest.mark.parametrize("case_id", list(ADVERSARIAL))
def test_each_adversarial_proposal_is_rejected_as_in_ask(case_id: str, tmp_path) -> None:
    cases = _cases(ADVERSARIAL_SUITE, tmp_path, {case_id})
    _, session, calls, [proposal, *_], _ = cases[case_id]
    served = _rejected(session, calls, proposal)
    assert served == _expected_rejections(session, calls, proposal)
    assert ADVERSARIAL[case_id].value in {code for code, _ in served}


def _accepted_cases(tmp_path: Path) -> list[tuple]:
    readme = _cases(README_SUITE, tmp_path / "readme")
    adversarial = _cases(ADVERSARIAL_SUITE, tmp_path / "adversarial", {"injected_text"})
    return [*readme.values(), *adversarial.values()]


def test_every_accepted_rendering_equals_asks(tmp_path) -> None:
    (tmp_path / "readme").mkdir()
    (tmp_path / "adversarial").mkdir()
    for case, session, calls, [proposal], path in _accepted_cases(tmp_path):
        *_, (is_error, texts) = mcp_replies(session, [*calls, ("submit_answer", proposal)])
        assert not is_error, (case.id, texts)
        asked = answer_question(
            case.question,
            provider=FakeProvider(load_fake_transcript(path)),
            session=session.fresh(),
            provider_kind="fake",
            model="fake",
        )
        assert asked.outcome.status == "answered", case.id
        present, readable, data = texts
        assert present == PRESENT_ANSWER
        assert readable == answer_text(asked.rendered), case.id
        assert json.loads(data) == answer_data(asked.rendered), case.id


def test_imported_text_is_only_quoted_data_in_an_accepted_answer(tmp_path) -> None:
    cases = _cases(ADVERSARIAL_SUITE, tmp_path, {"injected_text"})
    _, session, calls, [proposal], _ = cases["injected_text"]
    *_, (is_error, [_, readable, data]) = mcp_replies(
        session, [*calls, ("submit_answer", proposal)]
    )
    assert not is_error
    answer = json.loads(data)["answer"]
    quoted = [item["untrusted_text"] for c in answer["conclusions"] for item in c["data"]]
    statements = [item for c in answer["conclusions"] for item in c["statements"]]
    assert INJECTION in quoted
    assert not any(INJECTION in text for text in [*statements, *answer["qualifications"]])
    lines = [line for line in readable.splitlines() if INJECTION in line]
    assert lines and all("imported: " in line for line in lines)


# Claude Code moves a longer tool result out of the conversation into a file by default.
INLINE_RESULT_CHARS = 50_000


def test_every_readme_answer_fits_inline_without_a_declared_limit(tmp_path) -> None:
    for case, session, calls, [proposal], _ in _cases(README_SUITE, tmp_path).values():
        *_, (is_error, texts) = mcp_replies(session, [*calls, ("submit_answer", proposal)])
        assert not is_error, case.id
        assert sum(len(text) for text in texts) < INLINE_RESULT_CHARS, case.id
        # The JSON is compact: the readable answer beside it is the form a person reads.
        assert "\n" not in texts[2], case.id
