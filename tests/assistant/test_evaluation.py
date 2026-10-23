"""Evaluation suites, reports, and scoring: strict contracts, deterministic records.

Scoring reads real `ask` runs: the README examples answered with their fake transcripts, and
scripted runs that end in each kind of failure. Nothing reaches the network.
"""

import json
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any

import pytest

from archdiscov.assistant.answer_results import AnswerStatus, FailureReason
from archdiscov.assistant.evaluation import (
    MAX_CASES,
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
from archdiscov.assistant.tools.session import ToolSession
from archdiscov.models.enums import AnalysisStatus
from archdiscov.services.ask import AskResult, ask_question
from tests.assistant.test_answer_service import _rejected, _submit
from tests.assistant.test_orchestrator import SNAPSHOT, _calls, _fail
from tests.services.test_ask import _fake
from tests.support.ask_examples import EXAMPLES, SUMMARY, ExampleRun, write_example
from tests.support.evaluation_suites import example_analyzers, example_expectations

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


def _case(expect: dict | None = None, case_id: str = "case") -> EvaluationCase:
    return EvaluationCase.model_validate(
        _case_data(case_id, expect={"outcomes": ["answered"], **(expect or {})})
    )


def _clock(step: float = 0.25):
    """A clock that advances by `step` each time it is read."""
    ticks = count()
    return lambda: next(ticks) * step


def _example(name: str, directory: Path, step: float = 0.25) -> tuple[ExampleRun, AskResult]:
    directory.mkdir(parents=True, exist_ok=True)
    run = write_example(EXAMPLES[name], directory)
    question, *pairs = run.arguments
    options = dict(zip(pairs[::2], pairs[1::2], strict=True))
    result = ask_question(
        question,
        snapshot_path=Path(options["--snapshot"]),
        config_path=Path(options["--config"]),
        comparison_path=Path(options["--compare"]) if "--compare" in options else None,
        comparison_result_paths=(
            [Path(options["--comparison-result"])] if "--comparison-result" in options else []
        ),
        clock=_clock(step),
    )
    return run, result


def _scripted(directory: Path, turns: list[dict], config: str = "") -> AskResult:
    directory.mkdir(parents=True, exist_ok=True)
    return ask_question(
        QUESTION, snapshot_path=SNAPSHOT, config_path=_fake(directory, turns, config)
    )


def _passing(name: str, run: ExampleRun) -> EvaluationCase:
    expect = example_expectations(name, run)
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
    answered = {status.value for status in AnswerStatus if status != AnswerStatus.FAILED}
    assert {outcome.value for outcome in CaseOutcome} == answered | {
        reason.value for reason in FailureReason
    }


@pytest.mark.parametrize(
    "data",
    [
        _suite(_case_data(), extra=1),
        _suite(_case_data(), schema_version="2"),
        _suite(),
        _suite(*(_case_data(f"c{index}") for index in range(MAX_CASES + 1))),
        _suite(_case_data(), _case_data()),
        _suite(_case_data(f"Bad {SECRET_PATH}")),
        _suite(_case_data(question="")),
        _suite(_case_data(snapshot=f"/{SECRET_PATH}/snapshot.json")),
        _suite(_case_data(snapshot=f"../{SECRET_PATH}.json")),
        _suite(_case_data(snapshot=f"a/../../{SECRET_PATH}.json")),
        _suite(_case_data(snapshot=f"a\\{SECRET_PATH}.json")),
        _suite(_case_data(snapshot=f"./{SECRET_PATH}.json")),
        _suite(_case_data(snapshot=f"a//{SECRET_PATH}.json")),
        _suite(_case_data(fake_transcript=f"../{SECRET_PATH}.json")),
        _suite(_case_data(comparison_results=[f"{SECRET_PATH}.json"])),
        _suite(
            _case_data(comparison="c.json", comparison_results=[f"{SECRET_PATH}", f"{SECRET_PATH}"])
        ),
        _suite(_case_data(expect={"outcomes": []})),
        _suite(_case_data(expect={"outcomes": ["answered", "answered"]})),
        _suite(_case_data(expect={"outcomes": [SECRET_PATH]})),
        _suite(_case_data(expect={"outcomes": ["provider_error"], "statuses": ["supported"]})),
        _suite(_case_data(expect={"outcomes": ["limit_reached"], "finding_ids": ["f1"]})),
        _suite(_case_data(expect={"outcomes": ["cancelled"], "limitations": ["x"]})),
        _suite(
            _case_data(expect={"outcomes": ["answered"], "rejections": ["answer_id_not_returned"]})
        ),
        _suite(_case_data(expect={"outcomes": ["answered"], "finding_ids": ["f1", "f1"]})),
        _suite(_case_data(expect={"outcomes": ["answered"], SECRET_PATH: 1})),
    ],
    ids=[
        "unknown-field",
        "schema-version",
        "no-cases",
        "too-many-cases",
        "duplicate-case",
        "bad-case-id",
        "empty-question",
        "absolute-path",
        "parent-path",
        "escaping-path",
        "backslash",
        "dot-segment",
        "empty-segment",
        "escaping-transcript",
        "results-without-comparison",
        "duplicate-results",
        "no-outcomes",
        "duplicate-outcomes",
        "unknown-outcome",
        "status-without-answer",
        "findings-without-answer",
        "limitations-without-answer",
        "rejections-without-rejection",
        "duplicate-finding",
        "unknown-expectation",
    ],
)
def test_invalid_suites_are_refused_with_safe_diagnostics(data: dict) -> None:
    with pytest.raises(EvaluationValidationError) as raised:
        parse_suite(data)
    error = raised.value
    assert error.code == "invalid_evaluation_suite" and error.details
    rendered = str(error) + repr(error.details)
    assert SECRET_PATH not in rendered and QUESTION not in rendered


# ---- scoring -----------------------------------------------------------------------


@pytest.mark.parametrize("name", list(EXAMPLES))
def test_the_readme_examples_pass_their_expectations(tmp_path: Path, name: str) -> None:
    run, result = _example(name, tmp_path)
    record = score_case(_passing(name, run), result)
    assert record.passed, record.checks
    expected = {CheckName.OUTCOME, CheckName.STATUS, *SAFETY_CHECKS}
    if run.finding_ids:
        expected.add(CheckName.FINDING_IDS)
    if example_analyzers(name):
        expected.add(CheckName.ANALYZERS)
    assert set(_checks(record)) == expected
    assert record.cited_finding_ids == tuple(sorted(run.finding_ids))
    assert record.analysis_status == EXAMPLES[name].status
    assert record.analyzers_run == tuple(sorted(example_analyzers(name)))
    assert record.citations_valid is True
    assert record.usage == result.summary.usage
    assert record.latency_seconds == result.answer.transcript.latency_seconds


def test_record_ids_ignore_latency_and_follow_content(tmp_path: Path) -> None:
    run, slow = _example("ownership", tmp_path / "slow", step=2.0)
    _, fast = _example("ownership", tmp_path / "fast", step=0.5)
    case = _passing("ownership", run)
    first, second = score_case(case, slow), score_case(case, fast)
    assert first.latency_seconds != second.latency_seconds
    assert first.id == second.id == record_id(first)
    assert score_case(_case({"statuses": ["partial"]}), slow).id != first.id


@pytest.mark.parametrize(
    ("expect", "failed"),
    [
        ({"statuses": ["partial"]}, CheckName.STATUS),
        ({"finding_ids": ["not-cited"]}, CheckName.FINDING_IDS),
        ({"analyzers": ["internet_exposure"]}, CheckName.ANALYZERS),
        ({"limitations": ["not_a_limitation"]}, CheckName.LIMITATIONS),
        ({"outcomes": ["answered_after_repair"]}, CheckName.OUTCOME),
    ],
    ids=["status", "finding", "analyzer", "limitation", "outcome"],
)
def test_each_unmet_expectation_fails_only_its_check(tmp_path: Path, expect, failed) -> None:
    _, result = _example("ownership", tmp_path)
    record = score_case(_case(expect), result)
    assert not record.passed
    assert {name for name, passed in _checks(record).items() if not passed} == {failed}


def test_required_limitations_are_read_from_the_cited_findings(tmp_path: Path) -> None:
    run, result = _example("trust_limited_coverage", tmp_path)
    record = score_case(_case(), result)
    assert record.limitation_codes
    case = _case({"limitations": list(record.limitation_codes)})
    assert _checks(score_case(case, result))[CheckName.LIMITATIONS] is True


def test_a_failed_run_checks_only_its_outcome_and_safety(tmp_path: Path) -> None:
    result = _scripted(tmp_path, [_fail("provider_unavailable")])
    case = _case(
        {
            "outcomes": ["provider_error", "answered"],
            "statuses": ["supported"],
            "finding_ids": ["f1"],
            "limitations": ["x"],
            "analyzers": ["ownership"],
        }
    )
    record = score_case(case, result)
    assert record.outcome.failure == FailureReason.PROVIDER_ERROR
    assert record.citations_valid is None
    # Analyzers describe the run, not the answer, so a run that ran none fails them.
    assert _checks(record) == {
        CheckName.OUTCOME: True,
        CheckName.ANALYZERS: False,
        **dict.fromkeys(SAFETY_CHECKS, True),
    }


def _rejected_run(tmp_path: Path) -> AskResult:
    session = ToolSession.from_paths(SNAPSHOT)
    proposal = _rejected(session)
    turns = [_submit("a1", proposal), _submit("a2", proposal)]
    return _scripted(tmp_path, turns)


def test_rejections_are_checked_and_invalid_citations_reported(tmp_path: Path) -> None:
    result = _rejected_run(tmp_path)
    expect = {"outcomes": ["rejected_after_repair"], "rejections": ["answer_unknown_id"]}
    record = score_case(_case(expect), result)
    assert record.outcome.failure == FailureReason.REJECTED_AFTER_REPAIR
    assert record.citations_valid is False
    assert _checks(record)[CheckName.REJECTIONS] is True
    missing = {**expect, "rejections": [RejectionCode.NARRATIVE_UNCITED_ID.value]}
    assert _checks(score_case(_case(missing), result))[CheckName.REJECTIONS] is False


def test_a_rejection_without_citation_codes_keeps_valid_citations(tmp_path: Path) -> None:
    """The partial database example, overstated as supported in both answers."""
    write_example(EXAMPLES["database_access"], tmp_path)
    transcript = tmp_path / "transcript.json"
    turns = json.loads(transcript.read_text(encoding="utf-8"))["turns"]
    submit = turns.pop()
    call = submit["response"]["message"]["tool_calls"][0]
    call["arguments"]["value"]["status"] = "supported"
    repeated = json.loads(json.dumps(submit).replace('"answer"', '"repaired"'))
    transcript.write_text(json.dumps({"turns": [*turns, submit, repeated]}), encoding="utf-8")
    result = ask_question(
        EXAMPLES["database_access"].question,
        snapshot_path=tmp_path / "snapshot.json",
        config_path=tmp_path / "provider.toml",
    )
    record = score_case(_case({"outcomes": ["rejected_after_repair"]}), result)
    assert {rejection.code for rejection in record.outcome.rejections} == {
        RejectionCode.STATUS_UPGRADE
    }
    assert record.citations_valid is True


def test_a_limit_is_scored_as_its_outcome(tmp_path: Path) -> None:
    result = _scripted(tmp_path, [_calls(("c1", "search_resources", {}))], "max_requests = 1\n")
    record = score_case(_case({"outcomes": ["limit_reached"]}), result)
    assert record.passed and record.outcome.failure == FailureReason.LIMIT_REACHED


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
    tmp_path: Path,
) -> None:
    _, partial = _example("database_access", tmp_path / "partial")
    _, supported = _example("ownership", tmp_path / "supported")
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
    unqualified = _with_rendered(partial, qualifications=())
    assert _failed_checks(unqualified) == {CheckName.QUALIFIED}
    narrated = _with_rendered(
        supported,
        narrative=supported.answer.rendered.narrative.model_copy(update={"summary": summary}),
    )
    assert _failed_checks(narrated) == {CheckName.QUOTED}


def test_short_imported_text_is_too_common_to_count_as_quoted(tmp_path: Path) -> None:
    _, result = _example("ownership", tmp_path)
    conclusion = result.answer.rendered.conclusions[0]
    name = untrusted("web-server-01")
    assert len(name.untrusted_text) < MIN_QUOTED_CHARS
    quoted = _with_conclusion(
        result,
        data=(*conclusion.data, name),
        statements=(*conclusion.statements, "The web-server-01 resource is cited."),
    )
    assert _failed_checks(quoted) == set()


# ---- reports -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def records(tmp_path_factory) -> tuple[CaseRecord, CaseRecord]:
    directory = tmp_path_factory.mktemp("records")
    owned_run, owned = _example("ownership", directory / "ownership")
    trust_run, trust = _example("cross_account_trust", directory / "trust")
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


def test_a_report_round_trips_and_holds_no_narrative_or_tool_output(records) -> None:
    report = parse_report(_report(records))
    assert parse_report(json.loads(report.model_dump_json())) == report
    assert report.schema_version == "1"
    text = report.model_dump_json()
    for leaked in (SUMMARY, QUESTION, "untrusted_text", "narrative", "statements"):
        assert leaked not in text
    totals = report.providers[0].totals
    assert (totals.cases, totals.passed, totals.failed) == (2, 2, 0)
    assert totals.provider_requests == sum(r.usage.provider_requests for r in records)


def test_a_budget_bounds_every_request_and_output_token() -> None:
    limits = RunLimits(max_provider_requests=4, max_output_tokens=1_000)
    assert Budget.for_runs(limits, 3) == Budget(
        runs=3, max_provider_requests=15, max_output_tokens=15_000
    )


def _edited(records, **changes: Any) -> tuple[CaseRecord, ...]:
    first, *rest = records
    return (first.model_copy(update=changes), *rest)


def _recount(report: dict, **changes: Any) -> dict:
    run = {**report["providers"][0], **changes}
    return {**report, "providers": [run]}


@pytest.mark.parametrize(
    "build",
    [
        lambda records: _report(_edited(records, cited_finding_ids=["edited"])),
        lambda records: _report(_edited(records, passed=False)),
        lambda records: _report(_edited(records, checks=[{"name": "cited", "passed": True}])),
        lambda records: _report(_edited(records, cited_finding_ids=["b", "a"])),
        lambda records: _recount(
            _report(records), totals={**Totals.of(records).model_dump(), "passed": 1}
        ),
        lambda records: _report(tuple(reversed(records))),
        lambda records: _report(records[:1]),
        lambda records: _report(records, budget=Budget.for_runs(RunLimits(), 3)),
        lambda records: _report(records, model=f"sk-proj-{SECRET_PATH}XYZ"),
        lambda records: {**_report(records), "schema_version": "2"},
        lambda records: {**_report(records), "providers": []},
        lambda records: {**_report(records), "extra": SECRET_PATH},
        lambda records: {
            **_report(records),
            "suite": {"name": "readme", "digest": "0" * 64, "case_ids": ["a", "a"]},
        },
    ],
    ids=[
        "edited-record",
        "passed-without-checks",
        "missing-safety-checks",
        "unsorted-ids",
        "wrong-totals",
        "out-of-order",
        "missing-case",
        "wrong-budget",
        "secret-model",
        "schema-version",
        "no-providers",
        "unknown-field",
        "duplicate-suite-case",
    ],
)
def test_invalid_reports_are_refused_with_safe_diagnostics(records, build) -> None:
    with pytest.raises(EvaluationValidationError) as raised:
        parse_report(build(records))
    error = raised.value
    assert error.code == "invalid_evaluation_report" and error.details
    assert SECRET_PATH not in str(error) + repr(error.details)


def test_a_cancelled_run_may_stop_after_any_case(records) -> None:
    report = parse_report(_report(records[:1], cancelled=True, totals=Totals.of(records[:1])))
    assert report.providers[0].cancelled
    assert [record.case_id for record in report.providers[0].records] == ["ownership"]
