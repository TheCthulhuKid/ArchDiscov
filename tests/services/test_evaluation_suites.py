"""The checked-in README and adversarial suites: reproducible, offline, and passing.

Both suites run with the fake provider. Each adversarial case scripts an unsafe or failing
run and passes only on its safe outcome.
"""

import asyncio
import json
from pathlib import Path

import pytest

from archdiscov.assistant.answer_results import RenderedAnswer
from archdiscov.assistant.evaluation import SAFETY_CHECKS, CaseRecord
from archdiscov.services.ask import AskResult, answer_selected
from archdiscov.services.evaluation import evaluate_suite, prepare_evaluation
from tests.support.answer_claims import INJECTION
from tests.support.ask_examples import EXAMPLES, README_QUESTIONS
from tests.support.evaluation_suites import (
    ADVERSARIAL_SUITE,
    FABRICATED_ID,
    FORGED,
    README_SUITE,
    SUITES,
    write_fake_config,
)


def _files(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _suite(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _records(suite: Path, tmp_path: Path) -> dict[str, CaseRecord]:
    [run] = evaluate_suite(suite, [write_fake_config(tmp_path / "fake.toml")]).providers
    return {record.case_id: record for record in run.records}


def _failed(record: CaseRecord) -> list[str]:
    return [check.name for check in record.checks if not check.passed]


@pytest.mark.parametrize("suite", list(SUITES), ids=lambda path: path.parent.name)
def test_the_checked_in_suites_match_the_generator(suite: Path, tmp_path: Path) -> None:
    """Regenerate with `.venv/bin/python -m tests.support.evaluation_suites`."""
    written = SUITES[suite](tmp_path / suite.parent.name)
    assert _files(written.parent) == _files(suite.parent)


def _passing_records(suite: Path, tmp_path: Path) -> dict[str, CaseRecord]:
    """The suite's records, every case in order and none with a failed check."""
    records = _records(suite, tmp_path)
    assert list(records) == [case["id"] for case in _suite(suite)["cases"]]
    assert {case_id: _failed(record) for case_id, record in records.items()} == {
        case_id: [] for case_id in records
    }
    return records


def test_the_readme_suite_covers_the_questions_and_variants() -> None:
    cases = _suite(README_SUITE)["cases"]
    assert [case["id"] for case in cases] == list(EXAMPLES)
    assert set(README_QUESTIONS) <= {case["question"] for case in cases}


def test_both_suites_pass_and_each_adversarial_case_passes_on_its_safe_outcome(
    tmp_path: Path,
) -> None:
    _passing_records(README_SUITE, tmp_path)
    records = _passing_records(ADVERSARIAL_SUITE, tmp_path)
    outcomes = {case_id: record.outcome for case_id, record in records.items()}
    assert {
        case_id: outcome.failure or outcome.status for case_id, outcome in outcomes.items()
    } == {
        "fabricated_finding": "rejected_after_repair",
        "unreturned_evidence": "rejected_after_repair",
        "status_upgrade": "rejected_after_repair",
        "injected_text": "answered",
        "injected_narrative": "rejected_after_repair",
        "provider_error": "provider_error",
        "tool_call_limit": "limit_reached",
        "request_limit": "limit_reached",
        "repaired": "answered_after_repair",
    }
    assert (outcomes["tool_call_limit"].limit, outcomes["request_limit"].limit) == (
        "tool_calls_per_turn",
        "provider_requests",
    )
    assert records["fabricated_finding"].citations_valid is False
    assert FABRICATED_ID not in records["repaired"].cited_finding_ids
    # No case, answered or not, shows an uncited or unqualified conclusion.
    for record in records.values():
        assert {check.name for check in record.checks if check.passed} >= set(SAFETY_CHECKS)


def _answer(suite: Path, case_id: str, tmp_path: Path) -> AskResult:
    """One case's `ask` run with the fake provider, as the evaluation runs it."""
    prepared = prepare_evaluation(suite, [write_fake_config(tmp_path / "fake.toml")])
    [provider] = prepared.providers
    index = [case.id for case in prepared.suite.cases].index(case_id)
    return asyncio.run(
        answer_selected(
            prepared.suite.cases[index].question,
            config=provider.config,
            provider=provider.for_case(index),
            session=prepared.sessions[index].fresh(),
        )
    )


def _prose(rendered: RenderedAnswer) -> str:
    statements = [item for conclusion in rendered.conclusions for item in conclusion.statements]
    return "\n".join([*statements, *rendered.qualifications, rendered.narrative.model_dump_json()])


def test_injected_names_and_tags_are_only_quoted_data(tmp_path: Path) -> None:
    rendered = _answer(ADVERSARIAL_SUITE, "injected_text", tmp_path).answer.rendered
    quoted = "\n".join(
        item.untrusted_text for conclusion in rendered.conclusions for item in conclusion.data
    )
    assert INJECTION in quoted and FORGED in quoted
    assert INJECTION not in _prose(rendered) and FORGED not in _prose(rendered)
