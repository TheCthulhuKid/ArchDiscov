"""Saving, loading, and comparing evaluation reports.

Reports come from the checked-in suites run with the fake provider; nothing reaches the
network. Saved files are never overwritten, and a report is revalidated on both sides.
"""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from archdiscov.assistant.conversation import TokenUsage
from archdiscov.assistant.evaluation import (
    CheckChange,
    CheckName,
    EvaluationErrorCode,
    EvaluationReport,
    EvaluationValidationError,
    ProviderRun,
    ReportComparison,
    TokenDelta,
)
from archdiscov.services.evaluation import (
    compare_reports,
    evaluate_suite,
    load_report,
    save_report,
    save_report_comparison,
)
from archdiscov.services.snapshot_errors import SnapshotConflictError
from tests.support.evaluation_suites import (
    ADVERSARIAL_SUITE,
    FABRICATED_ID,
    README_SUITE,
    SUITE,
    write_fake_config,
    write_readme_suite,
)

NOW = datetime(2026, 10, 23, tzinfo=UTC)
TAMPERED = "TAMPERED-value-0123456789"


def _evaluate(suite: Path, directory: Path, **fields) -> EvaluationReport:
    config = write_fake_config(directory / f"fake-{len(fields)}.toml", **fields)
    return evaluate_suite(suite, [config], now=lambda: NOW)


@pytest.fixture(scope="module")
def report(tmp_path_factory) -> EvaluationReport:
    return _evaluate(README_SUITE, tmp_path_factory.mktemp("config"))


def _saved(report: EvaluationReport, tmp_path: Path) -> dict:
    return json.loads(save_report(report, tmp_path / "report.json").read_text(encoding="utf-8"))


# ---- saving and loading ------------------------------------------------------------


def test_a_saved_report_loads_unchanged_and_is_never_overwritten(
    report: EvaluationReport, tmp_path: Path
) -> None:
    path = save_report(report, tmp_path / "report.json")
    assert load_report(path) == report
    content = path.read_bytes()
    with pytest.raises(SnapshotConflictError) as raised:
        save_report(report, path)
    assert raised.value.code == "snapshot_conflict" and raised.value.path == path
    assert path.read_bytes() == content


def test_an_invalid_report_is_never_written(report: EvaluationReport, tmp_path: Path) -> None:
    [run] = report.providers
    record = run.records[0].model_copy(update={"case_id": run.records[1].case_id})
    tampered = report.model_copy(
        update={"providers": (run.model_copy(update={"records": (record, *run.records[1:])}),)}
    )
    path = tmp_path / "report.json"
    with pytest.raises(EvaluationValidationError) as raised:
        save_report(tampered, path)
    assert raised.value.code == EvaluationErrorCode.INVALID_REPORT
    assert raised.value.path == path and raised.value.details
    assert not path.exists() and list(tmp_path.iterdir()) == []


def _edited_record(data: dict) -> None:
    record = data["providers"][0]["records"][0]
    record["cited_finding_ids"] = sorted([*record["cited_finding_ids"], TAMPERED])


def test_loading_refuses_an_edited_report_safely(report: EvaluationReport, tmp_path: Path) -> None:
    data = _saved(report, tmp_path)
    _edited_record(data)
    path = tmp_path / "edited.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(EvaluationValidationError) as raised:
        load_report(path)
    assert raised.value.code == EvaluationErrorCode.INVALID_REPORT
    assert raised.value.path == path and raised.value.details
    assert TAMPERED not in repr(raised.value.details) and TAMPERED not in str(raised.value)


# ---- comparing ---------------------------------------------------------------------


def _unchanged(comparison: ReportComparison) -> bool:
    return not any(case.changed for run in comparison.providers for case in run.cases)


def test_two_fake_runs_differ_only_in_timing(report: EvaluationReport, tmp_path: Path) -> None:
    before = load_report(save_report(report, tmp_path / "before.json"))
    after = _evaluate(README_SUITE, tmp_path)
    comparison = compare_reports(before, after)
    assert comparison.same_suite and _unchanged(comparison)
    assert (comparison.providers_added, comparison.providers_removed) == ((), ())
    [run] = comparison.providers
    assert (run.cases_added, run.cases_removed) == ((), ())
    assert [case.case_id for case in run.cases] == list(report.suite.case_ids)
    for case in run.cases:
        usage = case.usage
        assert (usage.provider_requests, usage.repairs, usage.tool_calls) == (0, 0, 0)
        assert set(usage.tokens.model_dump().values()) <= {0, None}


def test_a_changed_fixture_shows_exactly_its_changes(tmp_path: Path) -> None:
    """The fabricated citation replaced with the delivered finding: that case now answers."""
    before = _evaluate(ADVERSARIAL_SUITE, tmp_path)
    copy = tmp_path / "adversarial"
    shutil.copytree(ADVERSARIAL_SUITE.parent, copy)
    [repaired] = [record for record in before.providers[0].records if record.case_id == "repaired"]
    [honest] = repaired.cited_finding_ids
    transcript = copy / "fabricated_finding" / "transcript.json"
    transcript.write_text(
        transcript.read_text(encoding="utf-8").replace(FABRICATED_ID, honest), encoding="utf-8"
    )
    after = _evaluate(copy / SUITE, tmp_path)
    comparison = compare_reports(before, after)
    assert comparison.same_suite
    [run] = comparison.providers
    changed = [case for case in run.cases if case.changed]
    [case] = changed
    assert case.case_id == "fabricated_finding"
    assert (case.outcome_before, case.outcome_after) == ("rejected_after_repair", "answered")
    assert (case.analysis_status_before, case.analysis_status_after) == (None, "supported")
    assert (case.passed_before, case.passed_after) == (True, False)
    # The rejections check describes a rejection, so an answer leaves it unchecked.
    assert case.checks == (
        CheckChange(name=CheckName.OUTCOME, before=True, after=False),
        CheckChange(name=CheckName.REJECTIONS, before=True, after=None),
    )
    assert (case.finding_ids_added, case.finding_ids_removed) == ((honest,), ())
    assert case.usage.repairs == -1


def test_reports_of_different_suites_compare_on_shared_cases(
    report: EvaluationReport, tmp_path: Path
) -> None:
    names = ["ownership", "internet_exposure"]
    subset = _evaluate(write_readme_suite(tmp_path / "subset", names), tmp_path)
    comparison = compare_reports(report, subset)
    assert not comparison.same_suite and _unchanged(comparison)
    [run] = comparison.providers
    assert [case.case_id for case in run.cases] == names
    assert run.cases_added == ()
    assert run.cases_removed == tuple(i for i in report.suite.case_ids if i not in names)


def _with_model(run: ProviderRun, model: str) -> ProviderRun:
    return run.model_copy(update={"model": model})


def test_provider_runs_pair_by_kind_and_model(report: EvaluationReport) -> None:
    [run] = report.providers
    after = report.model_copy(update={"providers": (_with_model(run, "other"), run, run)})
    comparison = compare_reports(report, after)
    [paired] = comparison.providers
    assert (paired.before.position, paired.after.position) == (0, 1)
    assert [(item.position, item.model) for item in comparison.providers_added] == [
        (0, "other"),
        (2, "fake"),
    ]
    reverse = compare_reports(after, report)
    assert [item.position for item in reverse.providers_removed] == [0, 2]


def test_comparing_changes_no_file_and_a_saved_comparison_reloads(
    report: EvaluationReport, tmp_path: Path
) -> None:
    paths = [save_report(report, tmp_path / name) for name in ("a.json", "b.json")]
    contents = [path.read_bytes() for path in paths]
    comparison = compare_reports(*(load_report(path) for path in paths))
    assert [path.read_bytes() for path in paths] == contents
    saved = save_report_comparison(comparison, tmp_path / "comparison.json")
    assert ReportComparison.model_validate_json(saved.read_text(encoding="utf-8")) == comparison


def test_a_token_delta_needs_both_counts() -> None:
    before = TokenUsage(input_tokens=5, output_tokens=3)
    after = TokenUsage(input_tokens=7, cache_read_input_tokens=2)
    assert TokenDelta.between(before, after) == TokenDelta(input_tokens=2)
