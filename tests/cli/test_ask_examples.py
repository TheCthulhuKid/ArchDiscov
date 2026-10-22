"""The README example questions, answered through the `ask` command with fake transcripts.

Each example is written to a temporary directory and run in both output forms, with the
fake provider and with the Anthropic adapter over a mocked Messages API serving the same
turns; the validated status, the cited findings and differences, and the qualifications are
checked against what the example's tools returned.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from archdiscov.assistant import anthropic_provider
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
from tests.assistant.provider_harness import AnthropicHarness, MockApi
from tests.paths import PROJECT_ROOT
from tests.support.ask_examples import EXAMPLES, README_QUESTIONS, ExampleRun, write_example

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


PROVIDERS = ["fake", "anthropic"]
KEY_VARIABLE = "ASK_EXAMPLE_KEY"


def _through_the_adapter(directory: Path, monkeypatch) -> MockApi:
    """Configure the example for the Anthropic adapter, over a mocked API serving its turns."""
    turns = json.loads((directory / "transcript.json").read_text(encoding="utf-8"))["turns"]
    api = AnthropicHarness.api(turns)
    (directory / "provider.toml").write_text(
        f'kind = "anthropic"\nmodel = "claude-sonnet-5-5"\napi_key_env = "{KEY_VARIABLE}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(KEY_VARIABLE, "sk-ant-api03-EXAMPLEkey0123456789abcdefXYZ")
    adapter = anthropic_provider.AnthropicProvider

    def mocked(config, key):
        return adapter(config, key, transport=api, async_transport=api)

    monkeypatch.setattr(anthropic_provider, "AnthropicProvider", mocked)
    return api


def _ask(name: str, tmp_path: Path, monkeypatch, provider: str, *flags: str):
    run = write_example(EXAMPLES[name], tmp_path)
    api = _through_the_adapter(tmp_path, monkeypatch) if provider == "anthropic" else None
    result = runner.invoke(app, ["ask", *run.arguments, *flags])
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    if api is not None:
        turns = json.loads((tmp_path / "transcript.json").read_text(encoding="utf-8"))["turns"]
        assert len(api.requests) == len(turns)
    return run, result.stdout


def _readme_questions() -> list[str]:
    lines = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    start = lines.index("The first release should answer these example questions:") + 2
    questions = []
    for line in lines[start:]:
        if not line.startswith("- "):
            break
        questions.append(line[2:])
    return questions


def test_the_examples_cover_every_readme_question_and_status() -> None:
    assert _readme_questions() == list(README_QUESTIONS)
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


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("name", list(EXAMPLES))
def test_each_example_answers_in_json(tmp_path: Path, monkeypatch, name: str, provider) -> None:
    example = EXAMPLES[name]
    run, output = _ask(name, tmp_path, monkeypatch, provider, "--json")
    result = json.loads(output)["result"]
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


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("name", list(EXAMPLES))
def test_each_example_answers_readably(tmp_path: Path, monkeypatch, name: str, provider) -> None:
    example = EXAMPLES[name]
    run, output = _ask(name, tmp_path, monkeypatch, provider)
    lines = output.splitlines()
    assert "Answer: answered, exit code 0" in lines
    assert f"Status: {example.status}" in lines
    for qualification in QUALIFICATIONS[name]:
        assert f"  - {qualification}" in lines
    for finding_id in run.finding_ids:
        assert any(line.strip().startswith(f"Finding {finding_id} (") for line in lines)
    for side, resource_ids in run.difference_ids.items():
        assert f"     Cited on the {side} snapshot: resources {', '.join(resource_ids)}" in lines
    if name in DECLARED:
        assert (
            "  Source: terraform_plan, declared or planned configuration, not deployed state"
            in (lines)
        )


@pytest.mark.parametrize("name", list(EXAMPLES))
def test_each_example_answers_alike_through_both_providers(
    tmp_path: Path, monkeypatch, name: str
) -> None:
    answers = {}
    for provider in PROVIDERS:
        directory = tmp_path / provider
        directory.mkdir()
        _, output = _ask(name, directory, monkeypatch, provider, "--json")
        data = json.loads(output)
        assert data["summary"]["provider_kind"] == provider
        answers[provider] = data["result"]
    assert answers["anthropic"] == answers["fake"]
