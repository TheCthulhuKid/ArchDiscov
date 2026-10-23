"""Readable and JSON presentation of evaluation budgets, reports, and report comparisons.

Reports and comparisons hold codes, IDs, counts, and scores only, so nothing here can show a
narrative, tool output, or provider text; readable lines are still redacted and sanitised.
"""

import json
from collections.abc import Iterator

import typer

from .assistant.evaluation import (
    Budget,
    CaseComparison,
    CaseRecord,
    CheckChange,
    ComparedReport,
    ComparedRun,
    EvaluationReport,
    ProviderComparison,
    ProviderRun,
    ReportComparison,
    TokenDelta,
)
from .cli_answer_output import display_text, redacted_data
from .cli_ask_output import TOKEN_LABELS, token_text
from .services.evaluation import PreparedEvaluation

EVALUATION_JSON_VERSION = "1"

# The selected paths as given, keyed as in the JSON envelope; lists hold repeatable options.
type EvaluationPaths = dict[str, str | list[str] | None]


def _echo(lines: Iterator[str], *, err: bool = False) -> None:
    typer.echo(display_text(lines), err=err)


# ---- budget ------------------------------------------------------------------------


def budget_data(prepared: PreparedEvaluation) -> list[dict]:
    """Each provider configuration's worst case, in configuration order."""
    return [
        {
            "provider_kind": provider.config.kind.value,
            "model": provider.config.model,
            **budget.model_dump(mode="json"),
        }
        for provider, budget in zip(prepared.providers, prepared.budgets, strict=True)
    ]


def state_budget(prepared: PreparedEvaluation, *, as_json: bool) -> None:
    """State the budget on standard error, before the first request."""
    if as_json:
        typer.echo(_json({"budgets": budget_data(prepared)}), err=True)
        return
    _echo(_budget_lines(prepared), err=True)


def _budget_lines(prepared: PreparedEvaluation) -> Iterator[str]:
    for provider, budget in zip(prepared.providers, prepared.budgets, strict=True):
        yield (
            f"Budget: {provider.config.kind.value}, model {provider.config.model}: "
            f"{_budget(budget)}"
        )


def _budget(budget: Budget) -> str:
    return (
        f"{budget.runs} runs, at most {budget.max_provider_requests} provider requests "
        f"and {budget.max_output_tokens} output tokens"
    )


def _json(data: dict) -> str:
    return json.dumps(redacted_data(data), indent=2, ensure_ascii=False, allow_nan=False)


# ---- reports -----------------------------------------------------------------------


def evaluate_data(
    report: EvaluationReport, prepared: PreparedEvaluation, paths: EvaluationPaths
) -> dict:
    """The `evaluate` JSON contract: the paths, the stated budgets, and the report."""
    return report_data(report, {**paths, "budgets": budget_data(prepared)})


def report_data(report: EvaluationReport, paths: dict) -> dict:
    """The `evaluation` JSON contract for one report: the paths and the report."""
    return redacted_data(
        {
            "schema_version": EVALUATION_JSON_VERSION,
            **paths,
            "report": report.model_dump(mode="json"),
        }
    )


def print_report(report: EvaluationReport) -> None:
    _echo(_report_lines(report))


def _report_lines(report: EvaluationReport) -> Iterator[str]:
    suite = report.suite
    yield f"Suite: {suite.name}, {len(suite.case_ids)} cases (digest {suite.digest[:12]})"
    yield f"Created: {report.created_at.isoformat()}"
    for position, run in enumerate(report.providers, start=1):
        yield from _run_lines(position, run)


def _run_lines(position: int, run: ProviderRun) -> Iterator[str]:
    totals = run.totals
    cancelled = " (cancelled)" if run.cancelled else ""
    yield (
        f"Provider {position}: {run.provider_kind}, model {run.model}: {totals.passed} passed, "
        f"{totals.failed} failed of {totals.cases} cases{cancelled}"
    )
    yield f"  Budget: {_budget(run.budget)}"
    yield (
        f"  Used: {totals.provider_requests} provider requests, {totals.repairs} repairs, "
        f"{totals.tool_calls} tool calls, tokens {token_text(totals.tokens)}, "
        f"{totals.latency_seconds:.2f}s"
    )
    for record in run.records:
        if not record.passed:
            yield f"  Failed: {_failed(record)}"


def _failed(record: CaseRecord) -> str:
    outcome = record.outcome.failure or record.outcome.status
    checks = ", ".join(check.name for check in record.checks if not check.passed)
    return f"{record.case_id} ({outcome}); failed checks: {checks}"


# ---- comparisons -------------------------------------------------------------------


def comparison_data(comparison: ReportComparison, paths: EvaluationPaths) -> dict:
    """The `evaluation --compare` JSON contract: the paths and the comparison."""
    return redacted_data(
        {
            "schema_version": EVALUATION_JSON_VERSION,
            **paths,
            "comparison": comparison.model_dump(mode="json"),
        }
    )


def print_comparison(comparison: ReportComparison) -> None:
    _echo(_comparison_lines(comparison))


def _comparison_lines(comparison: ReportComparison) -> Iterator[str]:
    yield f"Before: {_compared_report(comparison.before)}"
    yield f"After: {_compared_report(comparison.after)}"
    if comparison.same_suite:
        yield "Same suite."
    else:
        yield "Different suites: only the cases both reports recorded are compared."
    for provider in comparison.providers:
        yield from _provider_lines(provider)
    for run in comparison.providers_added:
        yield f"Provider added: {_compared_run(run)}"
    for run in comparison.providers_removed:
        yield f"Provider removed: {_compared_run(run)}"


def _compared_report(report: ComparedReport) -> str:
    suite = report.suite
    return (
        f"suite {suite.name}, {len(suite.case_ids)} cases (digest {suite.digest[:12]}), "
        f"created {report.created_at.isoformat()}"
    )


def _compared_run(run: ComparedRun) -> str:
    return f"{run.provider_kind}, model {run.model} (provider {run.position + 1})"


def _provider_lines(provider: ProviderComparison) -> Iterator[str]:
    changed = [case for case in provider.cases if case.changed]
    yield (
        f"Provider {provider.after.provider_kind}, model {provider.after.model} "
        f"(provider {provider.before.position + 1} before, {provider.after.position + 1} "
        f"after): {len(changed)} of {len(provider.cases)} shared cases changed"
    )
    if provider.cases_added:
        yield f"  Cases added: {', '.join(provider.cases_added)}"
    if provider.cases_removed:
        yield f"  Cases removed: {', '.join(provider.cases_removed)}"
    for case in changed:
        yield f"  {case.case_id}:"
        yield from (f"    {line}" for line in _case_lines(case))
    yield f"  Usage change (information only): {_usage(provider.cases)}"


def _case_lines(case: CaseComparison) -> Iterator[str]:
    for label, before, after in (
        ("outcome", case.outcome_before, case.outcome_after),
        ("analysis status", case.analysis_status_before, case.analysis_status_after),
        ("passed", case.passed_before, case.passed_after),
    ):
        if before != after:
            yield f"{label}: {_value(before)} -> {_value(after)}"
    for check in case.checks:
        yield f"check {check.name}: {_check(check, 'before')} -> {_check(check, 'after')}"
    if case.finding_ids_added:
        yield f"finding IDs added: {', '.join(case.finding_ids_added)}"
    if case.finding_ids_removed:
        yield f"finding IDs removed: {', '.join(case.finding_ids_removed)}"


def _value(value: object) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


_CHECK_RESULTS = {None: "not checked", True: "pass", False: "fail"}


def _check(check: CheckChange, side: str) -> str:
    return _CHECK_RESULTS[getattr(check, side)]


def _usage(cases: tuple[CaseComparison, ...]) -> str:
    usages = [case.usage for case in cases]
    tokens = {
        name: sum(value for usage in usages if (value := getattr(usage.tokens, name)) is not None)
        for name in TokenDelta.model_fields
        if any(getattr(usage.tokens, name) is not None for usage in usages)
    }
    parts = [
        f"provider requests {sum(usage.provider_requests for usage in usages):+d}",
        f"repairs {sum(usage.repairs for usage in usages):+d}",
        f"tool calls {sum(usage.tool_calls for usage in usages):+d}",
        *(f"{TOKEN_LABELS[name]} tokens {count:+d}" for name, count in tokens.items()),
        f"latency {sum(usage.latency_seconds for usage in usages):+.2f}s",
    ]
    return ", ".join(parts)
