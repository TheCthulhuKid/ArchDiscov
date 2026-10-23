"""Evaluate a suite of questions across configured providers.

`prepare_evaluation` does everything that can fail before a request, in this order, so an
error costs nothing and reads no further file:

1. read and validate the suite;
2. load each provider configuration, refusing a kind other than `fake` unless external
   providers are allowed, and build each external provider (which makes no request);
3. for a fake configuration, load every case's fake transcript, which replaces the one the
   configuration names, so each case scripts its own run;
4. load and verify every case's snapshots and saved results.

The prepared evaluation states each provider's budget before anything runs. `run_evaluation`
then runs every case once per provider, in suite order, through the same path as `ask`, and
scores it: whatever happens during a run, such as a provider error, a limit, or a rejected
answer, is that case's record, and the suite goes on. A cancellation ends the evaluation:
`EvaluationCancelled` carries the report so far, the interrupted case recorded as cancelled.

Reports and their comparisons are saved as new files, never overwriting one, and a report is
revalidated before it is written and when it is loaded. `compare_reports` matches provider
runs by kind and model and compares the cases both recorded.
"""

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from archdiscov.assistant.config import (
    FakeProviderConfig,
    ProviderConfig,
    ProviderConfigError,
    load_provider_config,
)
from archdiscov.assistant.evaluation import (
    MAX_PROVIDERS,
    Budget,
    CaseComparison,
    CaseRecord,
    CheckChange,
    CheckName,
    ComparedReport,
    ComparedRun,
    EvaluationCase,
    EvaluationErrorCode,
    EvaluationReport,
    EvaluationSuite,
    EvaluationValidationError,
    ProviderComparison,
    ProviderRun,
    ReportComparison,
    SuiteReference,
    Totals,
    UsageDelta,
    case_outcome,
    load_suite,
    parse_report,
    score_case,
    suite_digest,
)
from archdiscov.assistant.fake import FakeProvider, FakeTranscript, load_fake_transcript
from archdiscov.assistant.orchestrator import run_sync
from archdiscov.assistant.provider import Provider
from archdiscov.assistant.providers import build_provider
from archdiscov.assistant.runs import RunLimits
from archdiscov.assistant.tools.session import ToolSession
from archdiscov.models.validation_errors import serialization_issue

from .ask import AskCancelled, answer_selected
from .publication import SERIALIZATION_ERRORS, publish_text, read_json, render_json

_REPORT = "evaluation report"
_COMPARISON = "evaluation report comparison"


class EvaluationSetupCode(StrEnum):
    PROVIDERS_INVALID = "evaluation_providers_invalid"
    EXTERNAL_NOT_ALLOWED = "evaluation_external_not_allowed"
    TRANSCRIPT_MISSING = "evaluation_transcript_missing"


class EvaluationSetupError(Exception):
    """An evaluation that cannot start; the message is fixed by the code."""

    messages: ClassVar[Mapping[EvaluationSetupCode, str]] = {
        EvaluationSetupCode.PROVIDERS_INVALID: (
            f"An evaluation needs 1 to {MAX_PROVIDERS} provider configurations."
        ),
        EvaluationSetupCode.EXTERNAL_NOT_ALLOWED: (
            "A provider other than the fake runs only when external providers are allowed."
        ),
        EvaluationSetupCode.TRANSCRIPT_MISSING: (
            "The fake provider needs a fake transcript for every case."
        ),
    }

    def __init__(
        self, code: EvaluationSetupCode, *, path: Path | None = None, case_id: str | None = None
    ) -> None:
        self.code = EvaluationSetupCode(code)
        super().__init__(self.messages[self.code])
        self.path = path
        self.case_id = case_id


@dataclass(frozen=True)
class _Provider:
    """One configuration: its built provider, or for the fake, each case's transcript."""

    config: ProviderConfig
    built: Provider | None
    transcripts: tuple[FakeTranscript, ...] = ()

    def for_case(self, index: int) -> Provider:
        """The provider for one run; a fake replays the case's transcript from the start."""
        if self.built is not None:
            return self.built
        return FakeProvider(self.transcripts[index], max_retries=self.config.max_retries)

    @property
    def limits(self) -> RunLimits:
        return RunLimits.from_config(self.config)


@dataclass(frozen=True)
class PreparedEvaluation:
    """A suite whose files are verified and whose providers are built, ready to run."""

    suite: EvaluationSuite
    sessions: tuple[ToolSession, ...]
    providers: tuple[_Provider, ...]

    @property
    def budgets(self) -> tuple[Budget, ...]:
        """Each provider's worst case over the suite, in configuration order."""
        return tuple(
            Budget.for_runs(provider.limits, len(self.suite.cases)) for provider in self.providers
        )


class EvaluationCancelled(asyncio.CancelledError):
    """The evaluation was cancelled; `report` holds every record made so far."""

    def __init__(self, report: EvaluationReport) -> None:
        super().__init__("Evaluation was cancelled.")
        self.report = report


def _provider(
    path: Path,
    suite: EvaluationSuite,
    suite_dir: Path,
    *,
    allow_external: bool,
    environ: Mapping[str, str] | None,
) -> _Provider:
    config = load_provider_config(path)
    if not isinstance(config, FakeProviderConfig):
        if not allow_external:
            raise EvaluationSetupError(EvaluationSetupCode.EXTERNAL_NOT_ALLOWED, path=path)
        try:
            built = build_provider(config, config_dir=path.parent, environ=environ)
        except ProviderConfigError as error:
            error.path = path
            raise
        return _Provider(config, built)
    missing = next((case for case in suite.cases if case.fake_transcript is None), None)
    if missing is not None:
        raise EvaluationSetupError(EvaluationSetupCode.TRANSCRIPT_MISSING, case_id=missing.id)
    transcripts = tuple(
        load_fake_transcript(suite_dir / case.fake_transcript) for case in suite.cases
    )
    return _Provider(config, None, transcripts)


def _session(case: EvaluationCase, suite_dir: Path) -> ToolSession:
    return ToolSession.from_paths(
        suite_dir / case.snapshot,
        comparison_path=None if case.comparison is None else suite_dir / case.comparison,
        comparison_result_paths=[suite_dir / path for path in case.comparison_results],
    )


def prepare_evaluation(
    suite_path: Path,
    config_paths: Sequence[Path],
    *,
    allow_external: bool = False,
    environ: Mapping[str, str] | None = None,
) -> PreparedEvaluation:
    """Validate the suite, build the providers, and load every case's files; no request.

    Raises the errors `ask` raises before a run (configuration, credential, fake transcript,
    snapshot, and saved-result errors), `EvaluationValidationError` for the suite, and
    `EvaluationSetupError`.
    """
    if not 1 <= len(config_paths) <= MAX_PROVIDERS:
        raise EvaluationSetupError(EvaluationSetupCode.PROVIDERS_INVALID)
    suite = load_suite(suite_path)
    suite_dir = suite_path.parent
    providers = tuple(
        _provider(path, suite, suite_dir, allow_external=allow_external, environ=environ)
        for path in config_paths
    )
    sessions = tuple(_session(case, suite_dir) for case in suite.cases)
    return PreparedEvaluation(suite, sessions, providers)


def _report(
    prepared: PreparedEvaluation,
    records: Sequence[Sequence[CaseRecord]],
    *,
    created_at: datetime,
    interrupted: int | None = None,
) -> EvaluationReport:
    """The report; with `interrupted`, that provider's run and every later one are cancelled."""
    suite = prepared.suite
    runs = [
        ProviderRun(
            provider_kind=provider.config.kind.value,
            model=provider.config.model,
            limits=provider.limits,
            budget=budget,
            records=tuple(done),
            cancelled=interrupted is not None and position >= interrupted,
            totals=Totals.of(done),
        )
        for position, (provider, budget, done) in enumerate(
            zip(prepared.providers, prepared.budgets, records, strict=True)
        )
    ]
    return EvaluationReport(
        created_at=created_at,
        suite=SuiteReference(
            name=suite.name,
            digest=suite_digest(suite),
            case_ids=tuple(case.id for case in suite.cases),
        ),
        providers=tuple(runs),
    )


async def run_evaluation_async(
    prepared: PreparedEvaluation,
    *,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> EvaluationReport:
    """Run every case once per provider, in order, and report the scored records.

    `clock` times each run and `now` dates the report. Cancelling raises
    `EvaluationCancelled` with the records made so far.
    """
    records: list[list[CaseRecord]] = [[] for _ in prepared.providers]
    for position, (provider, done) in enumerate(zip(prepared.providers, records, strict=True)):
        for index, (case, session) in enumerate(
            zip(prepared.suite.cases, prepared.sessions, strict=True)
        ):
            try:
                result = await answer_selected(
                    case.question,
                    config=provider.config,
                    provider=provider.for_case(index),
                    session=session.fresh(),
                    clock=clock,
                )
            except AskCancelled as cancelled:
                done.append(score_case(case, cancelled.result))
                report = _report(prepared, records, created_at=now(), interrupted=position)
                raise EvaluationCancelled(report) from cancelled
            done.append(score_case(case, result))
    return _report(prepared, records, created_at=now())


def run_evaluation(
    prepared: PreparedEvaluation,
    *,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> EvaluationReport:
    """Run `run_evaluation_async` from synchronous code; refused inside a running loop.

    An interrupt (Ctrl-C) cancels the evaluation and raises `EvaluationCancelled`.
    """
    try:
        return run_sync(
            "run_evaluation", lambda: run_evaluation_async(prepared, clock=clock, now=now)
        )
    except KeyboardInterrupt as interrupt:
        cancelled = interrupt.__context__
        if not isinstance(cancelled, EvaluationCancelled):
            raise
        raise cancelled from None


def evaluate_suite(
    suite_path: Path,
    config_paths: Sequence[Path],
    *,
    allow_external: bool = False,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> EvaluationReport:
    """Prepare and run an evaluation; see `prepare_evaluation` and `run_evaluation`."""
    prepared = prepare_evaluation(
        suite_path, config_paths, allow_external=allow_external, environ=environ
    )
    return run_evaluation(prepared, clock=clock, now=now)


# ---- saving, loading, and comparing ------------------------------------------------


def save_report(report: EvaluationReport, path: Path) -> Path:
    """Revalidate the report and publish it at a new path; nothing invalid is written, and an
    existing path is refused (`SnapshotConflictError`) even for an identical report."""
    try:
        text = render_json(parse_report(report))
    except EvaluationValidationError as error:
        error.path = path
        raise
    except SERIALIZATION_ERRORS as error:
        raise EvaluationValidationError(
            EvaluationErrorCode.INVALID_REPORT, path=path, details=(serialization_issue(_REPORT),)
        ) from error
    return publish_text(text, path, subject=_REPORT)


def load_report(path: Path) -> EvaluationReport:
    """Read a saved report and revalidate it: schema version, record IDs, and totals.

    Raises the snapshot read and JSON errors, and `EvaluationValidationError` with the path.
    """
    data = read_json(path, subject=_REPORT)
    try:
        return parse_report(data)
    except EvaluationValidationError as error:
        error.path = path
        raise


def save_report_comparison(comparison: ReportComparison, path: Path) -> Path:
    """Publish a comparison at a new path; an existing path is refused."""
    return publish_text(render_json(comparison), path, subject=_COMPARISON)


def _compared_run(position: int, run: ProviderRun) -> ComparedRun:
    return ComparedRun(position=position, provider_kind=run.provider_kind, model=run.model)


def _check_changes(before: CaseRecord, after: CaseRecord) -> tuple[CheckChange, ...]:
    old = {check.name: check.passed for check in before.checks}
    new = {check.name: check.passed for check in after.checks}
    return tuple(
        CheckChange(name=name, before=old.get(name), after=new.get(name))
        for name in CheckName
        if old.get(name) != new.get(name)
    )


def _compare_case(before: CaseRecord, after: CaseRecord) -> CaseComparison:
    old, new = set(before.cited_finding_ids), set(after.cited_finding_ids)
    return CaseComparison(
        case_id=after.case_id,
        outcome_before=case_outcome(before.outcome),
        outcome_after=case_outcome(after.outcome),
        analysis_status_before=before.analysis_status,
        analysis_status_after=after.analysis_status,
        passed_before=before.passed,
        passed_after=after.passed,
        checks=_check_changes(before, after),
        finding_ids_added=tuple(sorted(new - old)),
        finding_ids_removed=tuple(sorted(old - new)),
        usage=UsageDelta.between(before, after),
    )


def _compare_runs(
    before: tuple[int, ProviderRun], after: tuple[int, ProviderRun]
) -> ProviderComparison:
    old = {record.case_id: record for record in before[1].records}
    new = {record.case_id: record for record in after[1].records}
    return ProviderComparison(
        before=_compared_run(*before),
        after=_compared_run(*after),
        cases_added=tuple(case_id for case_id in new if case_id not in old),
        cases_removed=tuple(case_id for case_id in old if case_id not in new),
        cases=tuple(
            _compare_case(old[case_id], record) for case_id, record in new.items() if case_id in old
        ),
    )


def _same_provider(before: ProviderRun, after: ProviderRun) -> bool:
    return (before.provider_kind, before.model) == (after.provider_kind, after.model)


def compare_reports(before: EvaluationReport, after: EvaluationReport) -> ReportComparison:
    """Compare two reports, pairing provider runs by kind and model in report order.

    Runs without a partner are listed as added or removed. Within a pair, cases recorded by
    only one run are listed, and each shared case is compared; reports of different suites
    therefore compare only on the cases they share. Neither report is changed.
    """
    unmatched = list(enumerate(before.providers))
    pairs, added = [], []
    for position, run in enumerate(after.providers):
        partner = next((item for item in unmatched if _same_provider(item[1], run)), None)
        if partner is None:
            added.append(_compared_run(position, run))
            continue
        unmatched.remove(partner)
        pairs.append(_compare_runs(partner, (position, run)))
    return ReportComparison(
        before=ComparedReport(created_at=before.created_at, suite=before.suite),
        after=ComparedReport(created_at=after.created_at, suite=after.suite),
        same_suite=before.suite.digest == after.suite.digest,
        providers=tuple(pairs),
        providers_added=tuple(added),
        providers_removed=tuple(_compared_run(*item) for item in unmatched),
    )
