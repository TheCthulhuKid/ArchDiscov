"""Answers over MCP equal answers in `ask`: the same rejections and the same renderings.

A checked-in adversarial proposal is submitted through the server after its tool calls and
rejected with exactly the codes and locations `validate_proposal` gives over the same calls.
Every README example and the injected-text case are accepted with exactly the readable
answer and answer JSON that `answer_question` renders from the same transcript with the fake
provider, small enough for a client to keep inline.
"""

import json
from pathlib import Path

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
from tests.support.evaluation_suites import ADVERSARIAL_SUITE, README_SUITE, write_fake_config
from tests.support.mcp_clients import Call, mcp_replies, transcript_calls


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


def test_an_adversarial_proposal_is_rejected_as_in_ask(tmp_path) -> None:
    # One checked-in case stands for all: the server validates with the same function over
    # the same calls, and the answer suite walks every rejection code.
    cases = _cases(ADVERSARIAL_SUITE, tmp_path, {"fabricated_finding"})
    _, session, calls, [proposal, *_], _ = cases["fabricated_finding"]
    served = _rejected(session, calls, proposal)
    assert served == _expected_rejections(session, calls, proposal)
    assert RejectionCode.UNKNOWN_ID.value in {code for code, _ in served}


def _accepted_cases(tmp_path: Path) -> list[tuple]:
    readme = _cases(README_SUITE, tmp_path / "readme")
    adversarial = _cases(ADVERSARIAL_SUITE, tmp_path / "adversarial", {"injected_text"})
    return [*readme.values(), *adversarial.values()]


# Claude Code moves a longer tool result out of the conversation into a file by default.
INLINE_RESULT_CHARS = 50_000


def test_every_accepted_rendering_equals_asks_and_fits_inline(tmp_path) -> None:
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
        assert sum(len(text) for text in texts) < INLINE_RESULT_CHARS, case.id
        # The JSON is compact: the readable answer beside it is the form a person reads.
        assert "\n" not in data, case.id
