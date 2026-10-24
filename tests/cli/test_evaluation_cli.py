"""The `evaluate` and `evaluation` commands: exit codes, the budget, both renderings, and
safe diagnostics.

Suites are the checked-in README and adversarial suites, run with the fake provider or the
Anthropic adapter over a mocked API; nothing reaches the network.
"""

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from archdiscov import cli_evaluation
from archdiscov.assistant.evaluation import (
    EvaluationReport,
    ReportComparison,
    Totals,
    parse_report,
)
from archdiscov.cli import app
from archdiscov.services.evaluation import EvaluationCancelled, load_report
from tests.assistant.provider_harness import AnthropicHarness
from tests.support.evaluation_suites import (
    ADVERSARIAL_SUITE,
    EVALUATION_KEY_VARIABLE,
    FABRICATED_ID,
    README_SUITE,
    SUITE,
    anthropic_config,
    serve_suite,
    write_fake_config,
)

runner = CliRunner()
KEY = "sk-ant-api03-EVALUATIONcli0123456789abcdef"
ENVELOPE = ["schema_version", "suite_path", "config_paths", "output_path", "budgets", "report"]


def _invoke(*arguments):
    result = runner.invoke(app, [str(item) for item in arguments])
    assert KEY not in result.stdout + result.stderr
    return result


def _evaluate(suite: Path, tmp_path: Path, *extra, output: str = "report.json"):
    config = write_fake_config(tmp_path / "fake.toml")
    return _invoke("evaluate", suite, "--config", config, "--output", tmp_path / output, *extra)


def _failing_suite(tmp_path: Path) -> Path:
    """The adversarial suite with its fabricated citation replaced by the delivered finding,
    so that case answers where it expects a rejection."""
    copy = tmp_path / "adversarial"
    shutil.copytree(ADVERSARIAL_SUITE.parent, copy)
    honest = next(
        case["expect"]["finding_ids"][0]
        for case in json.loads((copy / SUITE).read_text(encoding="utf-8"))["cases"]
        if case["id"] == "repaired"
    )
    transcript = copy / "fabricated_finding" / "transcript.json"
    transcript.write_text(
        transcript.read_text(encoding="utf-8").replace(FABRICATED_ID, honest), encoding="utf-8"
    )
    return copy / SUITE


# ---- evaluate ----------------------------------------------------------------------


def test_a_passing_suite_states_its_budget_and_saves_the_report(tmp_path: Path) -> None:
    result = _evaluate(README_SUITE, tmp_path)
    assert result.exit_code == 0, result.output
    assert result.stderr == (
        "Budget: fake, model fake: 9 runs, at most 81 provider requests and 331776 output tokens\n"
    )
    lines = result.stdout.splitlines()
    assert lines[0] == f"Saved report: {tmp_path / 'report.json'}"
    assert lines[1].startswith("Suite: readme, 9 cases (digest ")
    assert lines[3] == "Provider 1: fake, model fake: 9 passed, 0 failed of 9 cases"
    assert not any(line.lstrip().startswith("Failed:") for line in lines)
    report = load_report(tmp_path / "report.json")
    assert report.providers[0].totals.passed == 9


def test_the_json_rendering_holds_the_budget_and_report(tmp_path: Path) -> None:
    result = _evaluate(README_SUITE, tmp_path, "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert list(data) == ENVELOPE
    assert json.loads(result.stderr) == {"budgets": data["budgets"]}
    assert data["budgets"] == [
        {
            "provider_kind": "fake",
            "model": "fake",
            "runs": 9,
            "max_provider_requests": 81,
            "max_output_tokens": 331_776,
        }
    ]
    assert parse_report(data["report"]) == load_report(tmp_path / "report.json")


def test_a_failing_case_is_reported_and_exits_7(tmp_path: Path) -> None:
    result = _evaluate(_failing_suite(tmp_path), tmp_path)
    assert result.exit_code == cli_evaluation.CASES_FAILED_EXIT_CODE == 7, result.output
    assert "  Failed: fabricated_finding (answered); failed checks: outcome" in result.stdout
    assert load_report(tmp_path / "report.json").providers[0].totals.failed == 1


def test_a_cancelled_evaluation_saves_the_report_so_far_and_exits_130(
    tmp_path: Path, monkeypatch
) -> None:
    run_evaluation = cli_evaluation.run_evaluation

    def cancelled(prepared, **options) -> EvaluationReport:
        """The full run, cut back to its first record as if cancelled during the second."""
        report = run_evaluation(prepared, **options)
        [run] = report.providers
        records = run.records[:1]
        run = run.model_copy(
            update={"records": records, "cancelled": True, "totals": Totals.of(records)}
        )
        raise EvaluationCancelled(parse_report(report.model_copy(update={"providers": (run,)})))

    monkeypatch.setattr(cli_evaluation, "run_evaluation", cancelled)
    result = _evaluate(README_SUITE, tmp_path)
    assert result.exit_code == 130, result.output
    assert "Provider 1: fake, model fake: 1 passed, 0 failed of 1 cases (cancelled)" in (
        result.stdout
    )
    assert load_report(tmp_path / "report.json").providers[0].cancelled


def test_the_budget_is_stated_before_the_first_adapter_request(tmp_path: Path, monkeypatch) -> None:
    harness = AnthropicHarness()
    api = serve_suite(harness, README_SUITE, monkeypatch)
    monkeypatch.setenv(EVALUATION_KEY_VARIABLE, KEY)
    stated = []
    state_budget = cli_evaluation.state_budget

    def recording(prepared, **options) -> None:
        stated.append(len(api.requests))
        state_budget(prepared, **options)

    monkeypatch.setattr(cli_evaluation, "state_budget", recording)
    config = anthropic_config(tmp_path, harness)
    output = tmp_path / "report.json"
    result = _invoke(
        "evaluate", README_SUITE, "--config", config, "--output", output, "--allow-external"
    )
    assert result.exit_code == 0, result.output
    assert stated == [0] and api.requests
    assert result.stderr.startswith("Budget: anthropic, model ")
    report = load_report(output)
    assert len(api.requests) == report.providers[0].totals.provider_requests


def test_an_external_provider_needs_the_opt_in(tmp_path: Path, monkeypatch) -> None:
    harness = AnthropicHarness()
    api = serve_suite(harness, README_SUITE, monkeypatch)
    monkeypatch.setenv(EVALUATION_KEY_VARIABLE, KEY)
    config = anthropic_config(tmp_path, harness)
    output = tmp_path / "report.json"
    for flags in ([], ["--json"]):
        result = _invoke("evaluate", README_SUITE, "--config", config, "--output", output, *flags)
        assert result.exit_code == 2, result.output
        assert "Budget" not in result.stderr
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "evaluation_external_not_allowed"
    assert error["path"] == str(config)
    assert api.requests == [] and not output.exists()


def test_an_existing_output_path_is_refused_before_any_run(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    output.write_text("kept", encoding="utf-8")
    result = _evaluate(README_SUITE, tmp_path)
    assert result.exit_code == 3, result.output
    assert result.stderr.splitlines()[:2] == [
        "Error [snapshot_conflict]: Destination already exists; choose a new path.",
        f"Path: {output}",
    ]
    assert output.read_text(encoding="utf-8") == "kept"


def _fake(directory: Path) -> Path:
    return write_fake_config(directory / "fake.toml")


def _suite_file(tmp_path: Path, content: str) -> Path:
    path = tmp_path / SUITE
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("setup", "code", "exit_code"),
    [
        (lambda tmp: (tmp / "missing.json", [_fake(tmp)]), "evaluation_suite_unreadable", 1),
        (
            lambda tmp: (_suite_file(tmp, '{"schema_version": "TAMPERED"}'), [_fake(tmp)]),
            "invalid_evaluation_suite",
            2,
        ),
        (lambda tmp: (README_SUITE, [tmp / "missing.toml"]), "provider_config_unreadable", 1),
        (
            lambda tmp: (README_SUITE, [_fake(tmp)] * 11),
            "evaluation_providers_invalid",
            2,
        ),
    ],
    ids=["missing-suite", "invalid-suite", "missing-config", "too-many-configs"],
)
def test_pre_run_failures_are_safe_diagnostics(tmp_path: Path, setup, code, exit_code) -> None:
    suite, configs = setup(tmp_path)
    configs = [option for path in configs for option in ("--config", path)]
    output = tmp_path / "report.json"
    result = _invoke("evaluate", suite, *configs, "--output", output, "--json")
    assert result.exit_code == exit_code, result.output
    error = json.loads(result.stdout)["error"]
    assert error["code"] == code
    assert "TAMPERED" not in result.stdout + result.stderr
    assert result.stderr == "" and not output.exists()


def test_a_missing_output_directory_is_an_io_error(tmp_path: Path) -> None:
    result = _evaluate(README_SUITE, tmp_path, output="missing/report.json")
    assert result.exit_code == 1, result.output
    assert result.stderr.startswith("Error [snapshot_io_error]")


# ---- evaluation --------------------------------------------------------------------


@pytest.fixture(scope="module")
def reports(tmp_path_factory) -> tuple[Path, Path]:
    """A passing adversarial report, and one where the fabricated citation validates."""
    directory = tmp_path_factory.mktemp("reports")
    config = write_fake_config(directory / "fake.toml")
    paths = (directory / "before.json", directory / "after.json")
    for suite, path in zip((ADVERSARIAL_SUITE, _failing_suite(directory)), paths, strict=True):
        runner.invoke(app, ["evaluate", str(suite), "--config", str(config), "-o", str(path)])
    return paths


def test_a_saved_report_is_shown_in_both_renderings(reports: tuple[Path, Path]) -> None:
    before, after = reports
    readable = _invoke("evaluation", after)
    assert readable.exit_code == 0, readable.output
    lines = readable.stdout.splitlines()
    assert lines[0] == f"Report: {after}"
    assert "  Failed: fabricated_finding (answered); failed checks: outcome" in lines
    shown = _invoke("evaluation", before, "--json")
    assert shown.exit_code == 0, shown.output
    data = json.loads(shown.stdout)
    assert list(data) == ["schema_version", "report_path", "report"]
    assert parse_report(data["report"]) == load_report(before)


def test_two_reports_are_compared_and_the_comparison_saved(
    reports: tuple[Path, Path], tmp_path: Path
) -> None:
    before, after = reports
    contents = [path.read_bytes() for path in reports]
    output = tmp_path / "comparison.json"
    result = _invoke("evaluation", before, "--compare", after, "--output", output)
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == f"Saved comparison: {output}"
    assert "Same suite." in lines
    assert (
        "Provider fake, model fake (provider 1 before, 1 after): 1 of 9 shared cases changed"
        in lines
    )
    changes = lines[lines.index("  fabricated_finding:") + 1 :][:5]
    assert changes == [
        "    outcome: rejected_after_repair -> answered",
        "    analysis status: none -> supported",
        "    passed: yes -> no",
        "    check outcome: pass -> fail",
        "    check rejections: pass -> not checked",
    ]
    assert lines[-1].startswith("  Usage change (information only): provider requests -1")
    assert [path.read_bytes() for path in reports] == contents
    saved = ReportComparison.model_validate_json(output.read_text(encoding="utf-8"))
    data = _invoke("evaluation", before, "--compare", after, "--json")
    assert data.exit_code == 0, data.output
    envelope = json.loads(data.stdout)
    assert list(envelope) == [
        "schema_version",
        "report_path",
        "compare_path",
        "output_path",
        "comparison",
    ]
    assert envelope["output_path"] is None
    assert ReportComparison.model_validate(envelope["comparison"]) == saved


def test_an_existing_comparison_path_is_refused(reports: tuple[Path, Path]) -> None:
    before, after = reports
    content = after.read_bytes()
    result = _invoke("evaluation", before, "--compare", after, "--output", after)
    assert result.exit_code == 3, result.output
    assert after.read_bytes() == content


def test_saving_needs_a_comparison(reports: tuple[Path, Path], tmp_path: Path) -> None:
    result = _invoke("evaluation", reports[0], "--output", tmp_path / "comparison.json")
    assert result.exit_code == 2
    assert not (tmp_path / "comparison.json").exists()


def test_an_unusable_report_is_a_safe_diagnostic(
    reports: tuple[Path, Path], tmp_path: Path
) -> None:
    data = json.loads(reports[0].read_text(encoding="utf-8"))
    data["providers"][0]["totals"]["passed"] = 0
    data["suite"]["name"] = "tampered-name"
    edited = tmp_path / "edited.json"
    edited.write_text(json.dumps(data), encoding="utf-8")
    result = _invoke("evaluation", edited, "--json")
    assert result.exit_code == 2, result.output
    error = json.loads(result.stdout)["error"]
    assert (error["code"], error["path"]) == ("invalid_evaluation_report", str(edited))
    assert "tampered-name" not in result.stdout
    missing = _invoke("evaluation", tmp_path / "missing.json")
    assert missing.exit_code == 1
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert _invoke("evaluation", reports[0], "--compare", broken).exit_code == 2
