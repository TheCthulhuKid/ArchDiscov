"""Evaluation suites, reports, and scoring: strict contracts, deterministic records.

Scoring reads real `ask` runs: the README examples answered with their fake transcripts, and
the adversarial suite's runs that end rejected, limited, or failed. Nothing reaches the
network.
"""

import json
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any

import pytest

from archdiscov.assistant.answer_results import AnswerStatus, FailureReason
from archdiscov.assistant.evaluation import (
    MIN_QUOTED_CHARS,
    SAFETY_CHECKS,
    Budget,
    CaseOutcome,
    CaseRecord,
    CheckName,
    EvaluationCase,
    EvaluationValidationError,
    ProviderRun,
    Totals,
    parse_report,
    parse_suite,
    record_id,
    score_case,
    suite_digest,
)
from archdiscov.assistant.rejections import RejectionCode
from archdiscov.assistant.runs import RunLimits
from archdiscov.assistant.tools.contracts import untrusted
from archdiscov.assistant.tools.record import FINGERPRINT_STRIDE, FINGERPRINT_WINDOW
from archdiscov.models.enums import AnalysisStatus
from archdiscov.services.ask import AskResult, ask_question
from tests.support.ask_examples import EXAMPLES, SUMMARY, ExampleRun, write_example
from tests.support.evaluation_suites import ADVERSARIAL, example_analyzers, example_expectations

QUESTION = "Which resources are in the network?"
SECRET_PATH = "SECRETvalue0123456789abcdef"


def _suite(*cases: dict, **fields: Any) -> dict:
    return {"schema_version": "1", "name": "readme", "cases": list(cases), **fields}


def _case_data(case_id: str = "ownership", **fields: Any) -> dict:
    return {
        "id": case_id,
        "question": QUESTION,
        "snapshot": "ownership/snapshot.json",
        "fake_transcript": "ownership/transcript.json",
        "expect": {"outcomes": ["answered"]},
        **fields,
    }


def _case(expect: dict | None = None) -> EvaluationCase:
    expect = {"outcomes": ["answered"], **(expect or {})}
    return EvaluationCase.model_validate(_case_data("case", expect=expect))


def _clock(step: float = 0.25):
    """A clock that advances by `step` each time it is read."""
    ticks = count()
    return lambda: next(ticks) * step


def _ask(run: ExampleRun, step: float = 0.25) -> AskResult:
    question, *pairs = run.arguments
    paths = {option: Path(value) for option, value in zip(pairs[::2], pairs[1::2], strict=True)}
    return ask_question(
        question,
        snapshot_path=paths["--snapshot"],
        config_path=paths["--config"],
        comparison_path=paths.get("--compare"),
        comparison_result_paths=[paths["--comparison-result"]] if "--compare" in paths else [],
        clock=_clock(step),
    )


def _example(name: str, directory: Path, step: float = 0.25) -> tuple[ExampleRun, AskResult]:
    directory.mkdir(parents=True, exist_ok=True)
    run = write_example(EXAMPLES[name], directory)
    return run, _ask(run, step)


@pytest.fixture(scope="module")
def ownership(tmp_path_factory) -> tuple[ExampleRun, AskResult]:
    return _example("ownership", tmp_path_factory.mktemp("ownership"))


def _adversarial(name: str, directory: Path) -> AskResult:
    """The adversarial suite's run of that name, which ends unsafe or failed."""
    directory.mkdir(parents=True, exist_ok=True)
    case = ADVERSARIAL[name]
    return _ask(write_example(case.example, directory, case.script))


def _passing(name: str, run: ExampleRun) -> EvaluationCase:
    expect = example_expectations(EXAMPLES[name], run)
    return EvaluationCase.model_validate(_case_data(name.replace("_", "-"), expect=expect))


def _checks(record: CaseRecord) -> dict[CheckName, bool]:
    return {check.name: check.passed for check in record.checks}


# ---- suites ------------------------------------------------------------------------


def test_a_suite_round_trips_and_has_a_stable_digest() -> None:
    data = _suite(
        _case_data(),
        _case_data(
            "compared",
            snapshot="compared/snapshot.json",
            comparison="compared/comparison.json",
            comparison_results=["compared/comparison-result.json"],
            fake_transcript=None,
            expect={
                "outcomes": ["answered", "rejected_after_repair"],
                "statuses": ["partial"],
                "rejections": ["answer_id_not_returned"],
                "analyzers": ["ownership"],
                "finding_ids": ["f1"],
                "limitations": ["coverage_failed"],
            },
        ),
    )
    suite = parse_suite(data)
    assert parse_suite(json.loads(suite.model_dump_json())) == suite
    assert parse_suite(suite) == suite
    assert suite_digest(suite) == suite_digest(parse_suite(data))
    changed = parse_suite({**data, "name": "other"})
    assert suite_digest(changed) != suite_digest(suite)


def test_case_outcomes_are_the_answered_statuses_and_the_failure_reasons() -> None:
    statuses = {status.value for status in AnswerStatus if status != AnswerStatus.FAILED}
    reasons = {reason.value for reason in FailureReason}
    assert {outcome.value for outcome in CaseOutcome} == statuses | reasons


INVALID_SUITES = {
    "duplicate-case": _suite(_case_data(), _case_data()),
    "path-outside-suite": _suite(_case_data(snapshot=f"/{SECRET_PATH}/snapshot.json")),
    "status-without-answer": _suite(
        _case_data(expect={"outcomes": ["provider_error"], "statuses": ["supported"]})
    ),
}


@pytest.mark.parametrize("data", list(INVALID_SUITES.values()), ids=list(INVALID_SUITES))
def test_invalid_suites_are_refused_with_safe_diagnostics(data: dict) -> None:
    with pytest.raises(EvaluationValidationError) as raised:
        parse_suite(data)
    error = raised.value
    assert error.code == "invalid_evaluation_suite" and error.details
    rendered = str(error) + repr(error.details)
    assert SECRET_PATH not in rendered and QUESTION not in rendered


# ---- scoring -----------------------------------------------------------------------


# One example with findings and analyzers, and one with neither; the checked-in README suite,
# every example, passes in `tests/services/test_evaluation_suites.py`.
@pytest.mark.parametrize("name", ["ownership", "ownership_compared"])
def test_the_readme_examples_pass_their_expectations(tmp_path: Path, name: str) -> None:
    run, result = _example(name, tmp_path)
    analyzers = example_analyzers(EXAMPLES[name])
    record = score_case(_passing(name, run), result)
    assert record.passed, record.checks
    expected = {CheckName.OUTCOME, CheckName.STATUS, *SAFETY_CHECKS}
    if run.finding_ids:
        expected.add(CheckName.FINDING_IDS)
    if analyzers:
        expected.add(CheckName.ANALYZERS)
    assert set(_checks(record)) == expected
    assert record.cited_finding_ids == tuple(sorted(run.finding_ids))
    assert record.analysis_status == EXAMPLES[name].status
    assert record.analyzers_run == tuple(sorted(analyzers))
    assert record.citations_valid is True
    assert record.usage == result.summary.usage
    assert record.latency_seconds == result.answer.transcript.latency_seconds


def test_record_ids_ignore_latency_and_follow_content(ownership, tmp_path: Path) -> None:
    run, result = ownership
    _, slow = _example("ownership", tmp_path, step=2.0)
    case = _passing("ownership", run)
    first, second = score_case(case, result), score_case(case, slow)
    assert first.latency_seconds != second.latency_seconds
    assert first.id == second.id == record_id(first)
    assert score_case(_case({"statuses": ["partial"]}), slow).id != first.id


UNMET = {
    CheckName.STATUS: {"statuses": ["partial"]},
    CheckName.FINDING_IDS: {"finding_ids": ["not-cited"]},
    CheckName.ANALYZERS: {"analyzers": ["internet_exposure"]},
    CheckName.LIMITATIONS: {"limitations": ["not_a_limitation"]},
    CheckName.OUTCOME: {"outcomes": ["answered_after_repair"]},
}


def test_each_expectation_passes_when_met_and_fails_only_its_check_otherwise(
    ownership, tmp_path: Path
) -> None:
    for failed, expect in UNMET.items():
        record = score_case(_case(expect), ownership[1])
        assert not record.passed
        assert {name for name, passed in _checks(record).items() if not passed} == {failed}
    # Required limitations are read from the cited findings.
    _, limited = _example("trust_limited_coverage", tmp_path)
    codes = list(score_case(_case(), limited).limitation_codes)
    assert codes
    assert _checks(score_case(_case({"limitations": codes}), limited))[CheckName.LIMITATIONS]


def test_a_failed_run_checks_only_its_outcome_and_safety(tmp_path: Path) -> None:
    result = _adversarial("provider_error", tmp_path / "failed")
    every = {key: value for expect in UNMET.values() for key, value in expect.items()}
    record = score_case(_case({**every, "outcomes": ["provider_error", "answered"]}), result)
    assert record.outcome.failure == FailureReason.PROVIDER_ERROR
    assert record.citations_valid is None
    # Analyzers describe the run, not the answer, so a run that ran none fails them.
    assert _checks(record) == {
        CheckName.OUTCOME: True,
        CheckName.ANALYZERS: False,
        **dict.fromkeys(SAFETY_CHECKS, True),
    }


def test_rejections_are_checked_and_only_citation_codes_invalidate_citations(
    tmp_path: Path,
) -> None:
    fabricated = _adversarial("fabricated_finding", tmp_path / "fabricated")
    expect = {"outcomes": ["rejected_after_repair"], "rejections": ["answer_unknown_id"]}
    record = score_case(_case(expect), fabricated)
    assert record.outcome.failure == FailureReason.REJECTED_AFTER_REPAIR
    assert record.citations_valid is False
    assert _checks(record)[CheckName.REJECTIONS] is True
    missing = {**expect, "rejections": [RejectionCode.NARRATIVE_UNCITED_ID.value]}
    assert _checks(score_case(_case(missing), fabricated))[CheckName.REJECTIONS] is False
    # The limited ownership example, overstated as supported in both answers.
    upgraded = _adversarial("status_upgrade", tmp_path / "upgraded")
    record = score_case(_case({"outcomes": ["rejected_after_repair"]}), upgraded)
    assert {rejection.code for rejection in record.outcome.rejections} == {
        RejectionCode.STATUS_UPGRADE
    }
    assert record.citations_valid is True


# ---- safety checks -----------------------------------------------------------------


def _with_rendered(result: AskResult, **changes: Any) -> AskResult:
    """`result` with its rendered answer changed, unchecked, as a faulty renderer might."""
    rendered = result.answer.rendered.model_copy(update=changes)
    answer = result.answer.model_copy(update={"rendered": rendered})
    return result.model_copy(update={"answer": answer})


def _with_conclusion(result: AskResult, **changes: Any) -> AskResult:
    first, *rest = result.answer.rendered.conclusions
    return _with_rendered(result, conclusions=(first.model_copy(update=changes), *rest))


def _failed_checks(result: AskResult) -> set[CheckName]:
    record = score_case(_case(), result)
    return {name for name, passed in _checks(record).items() if not passed}


def test_unsafe_answers_fail_their_safety_check_whatever_the_expectations(
    ownership, tmp_path: Path
) -> None:
    _, partial = _example("database_access", tmp_path)
    _, supported = ownership
    assert _failed_checks(partial) == _failed_checks(supported) == set()
    conclusion = supported.answer.rendered.conclusions[0]
    summary = conclusion.findings[0].summary.untrusted_text
    citation = conclusion.citations[0].model_copy(update={"finding_ids": ()})
    partial_view = conclusion.findings[0].model_copy(update={"status": AnalysisStatus.PARTIAL})
    cases = {
        CheckName.CITED: _with_conclusion(supported, citations=(citation,)),
        CheckName.QUALIFIED: _with_conclusion(supported, findings=(partial_view,)),
        CheckName.QUOTED: _with_conclusion(
            supported, statements=(*conclusion.statements, f"It says: {summary.upper()}")
        ),
    }
    for check, unsafe in cases.items():
        assert check in _failed_checks(unsafe), check
    assert _failed_checks(_with_rendered(partial, qualifications=())) == {CheckName.QUALIFIED}
    # A middle stretch, as long as the narrative check always finds, but not the whole value.
    part = summary[20 : 20 + FINGERPRINT_WINDOW + FINGERPRINT_STRIDE - 1]
    assert part != summary
    repeated = f"In short, {part} and so on."
    narrative = supported.answer.rendered.narrative.model_copy(update={"summary": repeated})
    assert _failed_checks(_with_rendered(supported, narrative=narrative)) == {CheckName.QUOTED}
    # Short imported text is too common to count as quoted.
    name = untrusted("web-server-01")
    assert len(name.untrusted_text) < MIN_QUOTED_CHARS
    quoted = _with_conclusion(
        supported,
        data=(*conclusion.data, name),
        statements=(*conclusion.statements, "The web-server-01 resource is cited."),
    )
    assert _failed_checks(quoted) == set()


# ---- reports -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def records(ownership, tmp_path_factory) -> tuple[CaseRecord, CaseRecord]:
    owned_run, owned = ownership
    trust_run, trust = _example("cross_account_trust", tmp_path_factory.mktemp("trust"))
    return (
        score_case(_passing("ownership", owned_run), owned),
        score_case(_passing("cross_account_trust", trust_run), trust),
    )


def _report(records: tuple[CaseRecord, ...], **run: Any) -> dict:
    limits = RunLimits()
    provider = {
        "provider_kind": "fake",
        "model": "fake",
        "limits": limits,
        "budget": Budget.for_runs(limits, 2),
        "records": records,
        "totals": Totals.of(records),
        **run,
    }
    return {
        "created_at": datetime(2026, 10, 23, tzinfo=UTC),
        "suite": {
            "name": "readme",
            "digest": "0" * 64,
            "case_ids": ["ownership", "cross-account-trust"],
        },
        # Edited records are deliberately invalid; dump them as they are.
        "providers": [
            ProviderRun.model_construct(**provider).model_dump(mode="json", warnings=False)
        ],
    }


def test_a_report_round_trips_holds_no_narrative_and_may_stop_when_cancelled(records) -> None:
    report = parse_report(_report(records))
    assert parse_report(json.loads(report.model_dump_json())) == report
    assert report.schema_version == "1"
    text = report.model_dump_json()
    for leaked in (SUMMARY, QUESTION, "untrusted_text", "narrative", "statements"):
        assert leaked not in text
    totals = report.providers[0].totals
    assert (totals.cases, totals.passed, totals.failed) == (2, 2, 0)
    assert totals.provider_requests == sum(r.usage.provider_requests for r in records)
    cancelled = parse_report(_report(records[:1], cancelled=True, totals=Totals.of(records[:1])))
    assert cancelled.providers[0].cancelled
    assert [record.case_id for record in cancelled.providers[0].records] == ["ownership"]


def _edited(records, **changes: Any) -> tuple[CaseRecord, ...]:
    first, *rest = records
    return (first.model_copy(update=changes), *rest)


INVALID_REPORTS = {
    "edited-record": lambda records: _report(_edited(records, cited_finding_ids=["edited"])),
    "wrong-totals": lambda records: _report(
        records, totals={**Totals.of(records).model_dump(), "passed": 1}
    ),
    "out-of-order": lambda records: _report(tuple(reversed(records))),
    "secret-model": lambda records: _report(records, model=f"sk-proj-{SECRET_PATH}XYZ"),
}


@pytest.mark.parametrize("build", list(INVALID_REPORTS.values()), ids=list(INVALID_REPORTS))
def test_invalid_reports_are_refused_with_safe_diagnostics(records, build) -> None:
    with pytest.raises(EvaluationValidationError) as raised:
        parse_report(build(records))
    error = raised.value
    assert error.code == "invalid_evaluation_report" and error.details
    assert SECRET_PATH not in str(error) + repr(error.details)
