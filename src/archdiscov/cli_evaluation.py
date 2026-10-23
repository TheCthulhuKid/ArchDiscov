"""The `evaluate` and `evaluation` commands: thin routing to the evaluation service."""

from pathlib import Path
from typing import Annotated

import typer

from .assistant.answer_results import FAILURE_EXIT_CODES, FailureReason
from .assistant.evaluation import EvaluationReport
from .cli_diagnostics import json_output as emit_json
from .cli_diagnostics import safe_evaluation_failures
from .cli_evaluation_output import (
    comparison_data,
    evaluate_data,
    print_comparison,
    print_report,
    report_data,
    state_budget,
)
from .cli_options import JsonOption
from .services.evaluation import (
    COMPARISON_SUBJECT,
    REPORT_SUBJECT,
    EvaluationCancelled,
    compare_reports,
    load_report,
    prepare_evaluation,
    run_evaluation,
    save_report,
    save_report_comparison,
)
from .services.publication import require_new_path

# A case failed its expectations; 5 and 6 describe one run's outcome, so they are not reused.
CASES_FAILED_EXIT_CODE = 7


def evaluate(
    suite: Annotated[Path, typer.Argument(help="Evaluation suite JSON.")],
    config: Annotated[
        list[Path],
        typer.Option("--config", help="Provider configuration TOML to evaluate; repeatable."),
    ],
    output: Annotated[
        Path, typer.Option("--output", "-o", help="New report path; never overwritten.")
    ],
    allow_external: Annotated[
        bool,
        typer.Option(
            "--allow-external",
            help="Allow providers other than the fake, which send requests and may cost money.",
        ),
    ] = False,
    json_output: JsonOption = False,
) -> None:
    """Run a suite's questions with each configured provider and save the scored report.

    Everything is checked before the first request, and each provider's worst-case budget is
    stated on standard error before it. Exit codes: 0 every case passed, 7 a case failed its
    expectations (the report is still written), 130 cancelled (the report so far is
    written); 1, 2, and 3 for file, input, and conflict errors before the run starts.
    """
    paths = {
        "suite_path": str(suite),
        "config_paths": [str(path) for path in config],
        "output_path": str(output),
    }
    with safe_evaluation_failures(suite, json_output):
        require_new_path(output, subject=REPORT_SUBJECT)
        prepared = prepare_evaluation(suite, config, allow_external=allow_external)
    state_budget(prepared, as_json=json_output)
    with safe_evaluation_failures(output, json_output):
        try:
            report = run_evaluation(prepared)
        except EvaluationCancelled as cancelled:
            report = cancelled.report
        save_report(report, output)
    if json_output:
        emit_json(evaluate_data(report, prepared, paths))
    else:
        typer.echo(f"Saved report: {output}")
        print_report(report)
    if code := _exit_code(report):
        raise typer.Exit(code)


def _exit_code(report: EvaluationReport) -> int:
    if any(run.cancelled for run in report.providers):
        return FAILURE_EXIT_CODES[FailureReason.CANCELLED]
    if any(run.totals.failed for run in report.providers):
        return CASES_FAILED_EXIT_CODE
    return 0


def evaluation(
    report_path: Annotated[Path, typer.Argument(help="Saved evaluation report JSON.")],
    compare: Annotated[
        Path | None,
        typer.Option("--compare", help="A later report to compare with the first."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="New path for the comparison; never overwritten."),
    ] = None,
    json_output: JsonOption = False,
) -> None:
    """Show a saved evaluation report, or what changed between it and a later one.

    Neither report is changed. Exit codes: 0 shown; 1, 2, and 3 for file, input, and
    conflict errors.
    """
    if output is not None and compare is None:
        raise typer.BadParameter("saving a comparison needs --compare.", param_hint="--output")
    with safe_evaluation_failures(report_path, json_output):
        if output is not None:
            require_new_path(output, subject=COMPARISON_SUBJECT)
        before = load_report(report_path)
        after = None if compare is None else load_report(compare)
    if after is None:
        if json_output:
            emit_json(report_data(before, {"report_path": str(report_path)}))
        else:
            typer.echo(f"Report: {report_path}")
            print_report(before)
        return
    comparison = compare_reports(before, after)
    if output is not None:
        with safe_evaluation_failures(output, json_output):
            save_report_comparison(comparison, output)
    paths = {
        "report_path": str(report_path),
        "compare_path": str(compare),
        "output_path": None if output is None else str(output),
    }
    if json_output:
        emit_json(comparison_data(comparison, paths))
    else:
        if output is not None:
            typer.echo(f"Saved comparison: {output}")
        print_comparison(comparison)
