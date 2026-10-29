"""The README example questions, answered through the `ask` command with fake transcripts.

Each example is written to a temporary directory and run with the fake provider; the
validated status, the cited findings and differences, and the qualifications in the JSON
answer are checked against what the example's tools returned. The readable rendering is the
goldens' to show (`test_answer_output.py`), and that each adapter gives the fake's answer is
`tests/assistant/test_answer_service_suite.py`'s.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from archdiscov.assistant import questions
from archdiscov.assistant.answer_rendering import (
    INSUFFICIENT_REASONS,
    LIMITED_COVERAGE,
    PARTIAL_ANSWER,
    QUALIFIED_FINDING,
    RECORDED_DIFFERENCES,
)
from archdiscov.assistant.answer_results import RenderedAnswer
from archdiscov.assistant.answers import InsufficientReason
from archdiscov.cli import app
from tests.paths import PROJECT_ROOT
from tests.support.ask_examples import (
    EXAMPLES,
    README_QUESTIONS,
    ExampleRun,
    write_example,
)

runner = CliRunner()
INSUFFICIENT_COVERAGE = INSUFFICIENT_REASONS[InsufficientReason.LIMITED_COVERAGE]
PARTIAL = (PARTIAL_ANSWER, QUALIFIED_FINDING)
QUALIFICATIONS = {
    "internet_exposure": (),
    "database_access": PARTIAL,
    "security_group_impact": PARTIAL,
    "cross_account_trust": (),
    "ownership": (),
    "trust_limited_coverage": (*PARTIAL, LIMITED_COVERAGE),
    "internet_insufficient_evidence": (QUALIFIED_FINDING, LIMITED_COVERAGE, INSUFFICIENT_COVERAGE),
    "ownership_planned": (INSUFFICIENT_COVERAGE,),
    "ownership_compared": (PARTIAL_ANSWER, RECORDED_DIFFERENCES),
}
DECLARED = {"ownership_planned"}


def _readme_questions() -> list[str]:
    lines = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    start = lines.index("The `ask` command answers these example questions:") + 2
    questions = []
    for line in lines[start:]:
        if not line.startswith("- "):
            break
        questions.append(line[2:])
    return questions


def test_the_examples_cover_every_readme_question_and_status() -> None:
    # The README's list is the production list the MCP prompts offer.
    assert _readme_questions() == list(questions.README_QUESTIONS.values())
    assert set(README_QUESTIONS) <= {example.question for example in EXAMPLES.values()}
    assert {example.status for example in EXAMPLES.values()} == {
        "supported",
        "partial",
        "insufficient_evidence",
    }
    assert set(QUALIFICATIONS) == set(EXAMPLES)


def _cited(answer: RenderedAnswer, run: ExampleRun) -> None:
    findings = [
        finding_id
        for conclusion in answer.conclusions
        if conclusion.kind == "analyzer_finding"
        for citation in conclusion.citations
        for finding_id in citation.finding_ids
    ]
    assert tuple(findings) == run.finding_ids
    differences = {
        citation.side.value: citation.resource_ids
        for conclusion in answer.conclusions
        if conclusion.kind == "recorded_difference"
        for citation in conclusion.citations
    }
    assert differences == run.difference_ids
    assert findings or differences


@pytest.mark.parametrize("name", list(EXAMPLES))
def test_each_example_answers_in_json(tmp_path: Path, name: str) -> None:
    example = EXAMPLES[name]
    run = write_example(example, tmp_path)
    asked = runner.invoke(app, ["ask", *run.arguments, "--json"])
    assert asked.exit_code == 0, asked.output
    assert asked.stderr == ""
    result = json.loads(asked.stdout)["result"]
    assert result["exit_code"] == 0
    answer = RenderedAnswer.model_validate(result["answer"])
    assert answer.outcome.status == "answered" and answer.status == example.status
    _cited(answer, run)
    assert set(QUALIFICATIONS[name]) <= set(answer.qualifications)
    sides = ["primary", "comparison"] if example.comparison is not None else ["primary"]
    assert [snapshot.side for snapshot in answer.snapshots] == sides
    assert [snapshot.declared for snapshot in answer.snapshots] == [name in DECLARED] * len(sides)
    for view in (view for conclusion in answer.conclusions for view in conclusion.findings):
        assert view.finding_id in run.finding_ids
