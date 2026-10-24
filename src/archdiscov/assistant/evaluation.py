"""Evaluation suites, reports, and scoring: answers measured against deterministic fixtures.

A suite is a versioned file of cases. Each names a question, the snapshot files an `ask` run
selects (relative to the suite file), an optional fake transcript for the fake provider, and
what the answer must show. A report holds one record per case for each evaluated provider
configuration. A record is scored from what the answer service validated and rendered,
never from wording: the outcome, the analysis status, the cited IDs, the limitation codes of
the cited findings, and the analyzers the run ran.

Three safety checks apply to every case, whatever its expectations: every conclusion cites
what it shows, a status short of `supported` is qualified (and `supported` rests only on
supported findings), and imported text appears only as quoted data. A report holds codes,
IDs, counts, and scores, never a narrative, tool output, provider text, configuration
values, or credentials. A record's ID derives from its content without latency, so a
deterministic run gives the same IDs every time.
"""

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    ValidationError,
    model_validator,
)
from pydantic_core import PydanticSerializationError

from archdiscov.core.strict_json import (
    FileTooLargeError,
    read_bounded,
    reject_constant,
    unique_object,
)
from archdiscov.models.canonical import sha256_json
from archdiscov.models.enums import AnalysisStatus, AnalyzerKind
from archdiscov.models.resource_base import NonEmptyString
from archdiscov.models.validation_errors import (
    ValidationIssue,
    safe_validation_issues,
    serialization_issue,
    validation_error,
)

from .answer_results import AnswerOutcome, AnswerStatus, FailureReason, RenderedAnswer
from .answer_service import AnswerResult
from .answers import ConclusionKind
from .ask_results import AskResult, ToolUsage
from .conversation import Contract, MessageText, ProviderKindName, TokenUsage
from .redaction import looks_secret
from .rejections import RejectionCode
from .runs import RunLimits, RunTranscript
from .tools.record import fingerprints, normalize_text, repeats_recorded_text
from .tools.views import FindingView

SCHEMA_VERSION = "1"
MAX_CASES = 200
MAX_PROVIDERS = 10
MAX_RESULT_PATHS = 20
MAX_EXPECTED_IDS = 100
MAX_PATH_CHARS = 1_024
MAX_SUITE_BYTES = 1024 * 1024
# Imported text shorter than this is too common (a name such as "web") to show a leak.
MIN_QUOTED_CHARS = 16
RUN_ANALYZER = "run_analyzer"

Name = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ExpectedId = Annotated[NonEmptyString, Field(max_length=256)]


def _require_unique(values: Iterable[Any]) -> None:
    values = list(values)
    if len(set(values)) != len(values):
        raise validation_error("evaluation_duplicate", "items must be unique")


def _sorted_unique(values: Iterable[Any]) -> tuple:
    return tuple(sorted(set(values)))


# ---- suites ------------------------------------------------------------------------


def _relative_path(path: str) -> str:
    """A POSIX path below the suite's directory: relative, without `.`, `..`, or `\\`."""
    parts = path.split("/")
    if (
        "\\" in path
        or PurePosixPath(path).is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise validation_error("evaluation_path", "relative, inside the suite's directory")
    return path


SuitePath = Annotated[
    str, Field(min_length=1, max_length=MAX_PATH_CHARS), AfterValidator(_relative_path)
]


class CaseOutcome(StrEnum):
    """How an answer run ended: answered, or the reason it failed."""

    ANSWERED = AnswerStatus.ANSWERED.value
    ANSWERED_AFTER_REPAIR = AnswerStatus.ANSWERED_AFTER_REPAIR.value
    REJECTED_AFTER_REPAIR = FailureReason.REJECTED_AFTER_REPAIR.value
    NO_PROPOSAL_AFTER_REPAIR = FailureReason.NO_PROPOSAL_AFTER_REPAIR.value
    MALFORMED_OUTPUT = FailureReason.MALFORMED_OUTPUT.value
    PROVIDER_ERROR = FailureReason.PROVIDER_ERROR.value
    LIMIT_REACHED = FailureReason.LIMIT_REACHED.value
    CANCELLED = FailureReason.CANCELLED.value


_ANSWERED = frozenset({CaseOutcome.ANSWERED, CaseOutcome.ANSWERED_AFTER_REPAIR})


def case_outcome(outcome: AnswerOutcome) -> CaseOutcome:
    return CaseOutcome(outcome.status if outcome.failure is None else outcome.failure)


class Expectations(Contract):
    """What a case's answer must show; an empty field expects nothing.

    `statuses`, `finding_ids`, and `limitations` describe an answer, so `outcomes` must
    allow one; `rejections` describe a rejected answer, so `outcomes` must allow that.
    """

    outcomes: tuple[CaseOutcome, ...] = Field(min_length=1, max_length=len(CaseOutcome))
    statuses: tuple[AnalysisStatus, ...] = ()
    rejections: tuple[RejectionCode, ...] = Field(default=(), max_length=len(RejectionCode))
    analyzers: tuple[AnalyzerKind, ...] = ()
    finding_ids: tuple[ExpectedId, ...] = Field(default=(), max_length=MAX_EXPECTED_IDS)
    limitations: tuple[ExpectedId, ...] = Field(default=(), max_length=MAX_EXPECTED_IDS)

    @model_validator(mode="after")
    def require_consistent_expectations(self) -> Self:
        for values in (
            self.outcomes,
            self.statuses,
            self.rejections,
            self.analyzers,
            self.finding_ids,
            self.limitations,
        ):
            _require_unique(values)
        answers = self.statuses or self.finding_ids or self.limitations
        if answers and not _ANSWERED & set(self.outcomes):
            raise validation_error("evaluation_expectation_shape", "answer fields need an answer")
        if self.rejections and CaseOutcome.REJECTED_AFTER_REPAIR not in self.outcomes:
            raise validation_error("evaluation_expectation_shape", "rejections need a rejection")
        return self


class EvaluationCase(Contract):
    """One question over files the suite names, and what its answer must show.

    `fake_transcript` scripts the fake provider; other providers answer for themselves.
    """

    id: Name
    question: MessageText
    snapshot: SuitePath
    comparison: SuitePath | None = None
    comparison_results: tuple[SuitePath, ...] = Field(default=(), max_length=MAX_RESULT_PATHS)
    fake_transcript: SuitePath | None = None
    expect: Expectations

    @model_validator(mode="after")
    def require_comparison_for_results(self) -> Self:
        _require_unique(self.comparison_results)
        if self.comparison_results and self.comparison is None:
            raise validation_error("evaluation_case_shape", "saved results need a comparison")
        return self


class EvaluationSuite(Contract):
    schema_version: Literal["1"]
    name: Name
    cases: tuple[EvaluationCase, ...] = Field(min_length=1, max_length=MAX_CASES)

    @model_validator(mode="after")
    def require_unique_cases(self) -> Self:
        _require_unique(case.id for case in self.cases)
        return self


def suite_digest(suite: EvaluationSuite) -> str:
    """SHA-256 of the suite's canonical JSON, naming it in reports."""
    return sha256_json(suite.model_dump(mode="json"))


# ---- reports -----------------------------------------------------------------------


class CheckName(StrEnum):
    OUTCOME = "outcome"
    STATUS = "status"
    REJECTIONS = "rejections"
    ANALYZERS = "analyzers"
    FINDING_IDS = "finding_ids"
    LIMITATIONS = "limitations"
    # The safety checks, applied to every case.
    CITED = "cited"
    QUALIFIED = "qualified"
    QUOTED = "quoted"


SAFETY_CHECKS = (CheckName.CITED, CheckName.QUALIFIED, CheckName.QUOTED)


class Check(Contract):
    name: CheckName
    passed: bool


class CaseRecord(Contract):
    """A case's scored answer for one provider: codes, IDs, counts, and checks only.

    `citations_valid` is None when no proposal was validated (the run ended first).
    """

    id: Digest
    case_id: Name
    outcome: AnswerOutcome
    analysis_status: AnalysisStatus | None
    cited_finding_ids: tuple[NonEmptyString, ...]
    cited_evidence_ids: tuple[NonEmptyString, ...]
    cited_resource_ids: tuple[NonEmptyString, ...]
    limitation_codes: tuple[NonEmptyString, ...]
    citations_valid: bool | None
    analyzers_run: tuple[AnalyzerKind, ...]
    usage: ToolUsage
    latency_seconds: NonNegativeFloat
    checks: tuple[Check, ...] = Field(min_length=len(SAFETY_CHECKS), max_length=len(CheckName))
    passed: bool

    @model_validator(mode="after")
    def require_consistent_record(self) -> Self:
        for values in (
            self.cited_finding_ids,
            self.cited_evidence_ids,
            self.cited_resource_ids,
            self.limitation_codes,
            self.analyzers_run,
        ):
            if values != _sorted_unique(values):
                raise validation_error("evaluation_record_shape", "sorted and unique")
        names = [check.name for check in self.checks]
        in_order = names == [name for name in CheckName if name in names]
        if not in_order or not set(SAFETY_CHECKS) <= set(names):
            raise validation_error("evaluation_record_shape", "checks in order, safety included")
        if self.passed != all(check.passed for check in self.checks):
            raise validation_error("evaluation_record_shape", "passed means every check passed")
        if self.id != record_id(self):
            raise validation_error("evaluation_record_id", "the ID derives from the content")
        return self


def record_id(record: CaseRecord | Mapping[str, Any]) -> str:
    """SHA-256 of a record's content, without its ID or latency."""
    data = record.model_dump(mode="json") if isinstance(record, CaseRecord) else dict(record)
    return sha256_json({k: v for k, v in data.items() if k not in {"id", "latency_seconds"}})


class Budget(Contract):
    """The most a provider's runs over a suite may use: each run's requests, plus its
    repair, of at most `max_output_tokens` each."""

    runs: NonNegativeInt
    max_provider_requests: NonNegativeInt
    max_output_tokens: NonNegativeInt

    @classmethod
    def for_runs(cls, limits: RunLimits, runs: int) -> "Budget":
        requests = runs * (limits.max_provider_requests + 1)
        return cls(
            runs=runs,
            max_provider_requests=requests,
            max_output_tokens=requests * limits.max_output_tokens,
        )


class Totals(Contract):
    cases: NonNegativeInt
    passed: NonNegativeInt
    failed: NonNegativeInt
    provider_requests: NonNegativeInt
    repairs: NonNegativeInt
    tool_calls: NonNegativeInt
    tokens: TokenUsage
    latency_seconds: NonNegativeFloat

    @classmethod
    def of(cls, records: Iterable[CaseRecord]) -> "Totals":
        records = list(records)
        tokens: dict[str, int | None] = dict.fromkeys(TokenUsage.model_fields)
        for record in records:
            for name in tokens:
                value = getattr(record.usage.tokens, name)
                if value is not None:
                    tokens[name] = (tokens[name] or 0) + value
        passed = sum(record.passed for record in records)
        return cls(
            cases=len(records),
            passed=passed,
            failed=len(records) - passed,
            provider_requests=sum(record.usage.provider_requests for record in records),
            repairs=sum(record.usage.repair_requested for record in records),
            tool_calls=sum(sum(record.usage.tool_calls.values()) for record in records),
            tokens=TokenUsage(**tokens),
            latency_seconds=sum(record.latency_seconds for record in records),
        )


class ProviderRun(Contract):
    """One provider configuration's records over the suite, named only by kind, model,
    and limits; `cancelled` runs stop early."""

    provider_kind: ProviderKindName
    model: Annotated[str, Field(min_length=1, max_length=256)]
    limits: RunLimits
    budget: Budget
    records: tuple[CaseRecord, ...] = Field(max_length=MAX_CASES)
    cancelled: bool = False
    totals: Totals

    @model_validator(mode="after")
    def require_consistent_run(self) -> Self:
        if looks_secret(self.model):
            raise validation_error("evaluation_secret", "the model name looks like a secret")
        if self.totals != Totals.of(self.records):
            raise validation_error("evaluation_totals", "totals match the records")
        return self


class SuiteReference(Contract):
    name: Name
    digest: Digest
    case_ids: tuple[Name, ...] = Field(min_length=1, max_length=MAX_CASES)

    @model_validator(mode="after")
    def require_unique_cases(self) -> Self:
        _require_unique(self.case_ids)
        return self


class EvaluationReport(Contract):
    """One evaluation of a suite across provider configurations, created once."""

    schema_version: Literal["1"] = SCHEMA_VERSION
    created_at: AwareDatetime
    suite: SuiteReference
    providers: tuple[ProviderRun, ...] = Field(min_length=1, max_length=MAX_PROVIDERS)

    @model_validator(mode="after")
    def require_suite_cases(self) -> Self:
        cases = self.suite.case_ids
        for run in self.providers:
            recorded = tuple(record.case_id for record in run.records)
            if recorded != cases[: len(recorded)] or (not run.cancelled and recorded != cases):
                raise validation_error("evaluation_report_cases", "records follow the suite")
            if run.budget != Budget.for_runs(run.limits, len(cases)):
                raise validation_error("evaluation_budget", "budget matches limits and suite")
        return self


# ---- comparisons -------------------------------------------------------------------


class CheckChange(Contract):
    """A check whose result differs between two records; None where it was not checked."""

    name: CheckName
    before: bool | None
    after: bool | None


class TokenDelta(Contract):
    """After minus before for each token count both records report, else None."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None

    @classmethod
    def between(cls, before: TokenUsage, after: TokenUsage) -> "TokenDelta":
        values: dict[str, int | None] = {}
        for name in TokenUsage.model_fields:
            old, new = getattr(before, name), getattr(after, name)
            values[name] = None if old is None or new is None else new - old
        return cls(**values)


class UsageDelta(Contract):
    """After minus before; information only, since usage varies across providers and runs."""

    provider_requests: int
    repairs: int
    tool_calls: int
    tokens: TokenDelta
    latency_seconds: float

    @classmethod
    def between(cls, before: CaseRecord, after: CaseRecord) -> "UsageDelta":
        old, new = before.usage, after.usage
        return cls(
            provider_requests=new.provider_requests - old.provider_requests,
            repairs=int(new.repair_requested) - int(old.repair_requested),
            tool_calls=sum(new.tool_calls.values()) - sum(old.tool_calls.values()),
            tokens=TokenDelta.between(old.tokens, new.tokens),
            latency_seconds=after.latency_seconds - before.latency_seconds,
        )


class CaseComparison(Contract):
    """One case recorded in both reports: what its answer showed before and after."""

    case_id: Name
    outcome_before: CaseOutcome
    outcome_after: CaseOutcome
    analysis_status_before: AnalysisStatus | None
    analysis_status_after: AnalysisStatus | None
    passed_before: bool
    passed_after: bool
    checks: tuple[CheckChange, ...] = Field(max_length=len(CheckName))
    finding_ids_added: tuple[NonEmptyString, ...]
    finding_ids_removed: tuple[NonEmptyString, ...]
    usage: UsageDelta

    @property
    def changed(self) -> bool:
        """Whether anything but usage changed."""
        return bool(
            self.outcome_before != self.outcome_after
            or self.analysis_status_before != self.analysis_status_after
            or self.passed_before != self.passed_after
            or self.checks
            or self.finding_ids_added
            or self.finding_ids_removed
        )


class ComparedRun(Contract):
    """A provider run, by its position in its report, kind, and model."""

    position: NonNegativeInt
    provider_kind: ProviderKindName
    model: Annotated[str, Field(min_length=1, max_length=256)]


class ProviderComparison(Contract):
    """One provider's runs in both reports: cases only one recorded, and each shared case
    in the later run's order."""

    before: ComparedRun
    after: ComparedRun
    cases_added: tuple[Name, ...] = Field(max_length=MAX_CASES)
    cases_removed: tuple[Name, ...] = Field(max_length=MAX_CASES)
    cases: tuple[CaseComparison, ...] = Field(max_length=MAX_CASES)


class ComparedReport(Contract):
    created_at: AwareDatetime
    suite: SuiteReference


class ReportComparison(Contract):
    """Two reports compared provider by provider; across suites (`same_suite` false), only
    the cases both recorded are compared."""

    schema_version: Literal["1"] = SCHEMA_VERSION
    before: ComparedReport
    after: ComparedReport
    same_suite: bool
    providers: tuple[ProviderComparison, ...] = Field(max_length=MAX_PROVIDERS)
    providers_added: tuple[ComparedRun, ...] = Field(max_length=MAX_PROVIDERS)
    providers_removed: tuple[ComparedRun, ...] = Field(max_length=MAX_PROVIDERS)

    @model_validator(mode="after")
    def require_consistent_suites(self) -> Self:
        if self.same_suite != (self.before.suite.digest == self.after.suite.digest):
            raise validation_error("evaluation_comparison_suite", "same suite means same digest")
        return self


# ---- parsing -----------------------------------------------------------------------

_DIAGNOSTIC_FIELDS = frozenset(
    field
    for model in (
        EvaluationSuite,
        EvaluationCase,
        Expectations,
        EvaluationReport,
        SuiteReference,
        ProviderRun,
        Budget,
        Totals,
        CaseRecord,
        Check,
        AnswerOutcome,
        ToolUsage,
        TokenUsage,
        RunLimits,
    )
    for field in model.model_fields
)


class EvaluationErrorCode(StrEnum):
    SUITE_UNREADABLE = "evaluation_suite_unreadable"
    SUITE_TOO_LARGE = "evaluation_suite_too_large"
    INVALID_SUITE = "invalid_evaluation_suite"
    INVALID_REPORT = "invalid_evaluation_report"


class EvaluationValidationError(Exception):
    """A suite or report that cannot be used: a fixed message for its code, and details of
    known field names and fixed messages only, never the input."""

    messages: ClassVar[Mapping[EvaluationErrorCode, str]] = {
        EvaluationErrorCode.SUITE_UNREADABLE: "Evaluation suite file could not be read.",
        EvaluationErrorCode.SUITE_TOO_LARGE: f"Evaluation suite exceeds {MAX_SUITE_BYTES} bytes.",
        EvaluationErrorCode.INVALID_SUITE: "Evaluation suite validation failed.",
        EvaluationErrorCode.INVALID_REPORT: "Evaluation report validation failed.",
    }

    def __init__(
        self,
        code: EvaluationErrorCode,
        *,
        path: Path | None = None,
        details: tuple[ValidationIssue, ...] = (),
    ) -> None:
        self.code = EvaluationErrorCode(code)
        super().__init__(self.messages[self.code])
        self.path = path
        self.details = details


def _parse[T: Contract](model: type[T], data: Any, code: EvaluationErrorCode, subject: str) -> T:
    try:
        if isinstance(data, model):
            data = data.model_dump(mode="json", warnings="error")
        return model.model_validate(data)
    except ValidationError as exc:
        issues = safe_validation_issues(exc, _DIAGNOSTIC_FIELDS, subject)
        raise EvaluationValidationError(code, details=issues) from exc
    except (PydanticSerializationError, TypeError, ValueError, RecursionError) as exc:
        details = (serialization_issue(subject),)
        raise EvaluationValidationError(code, details=details) from exc


def parse_suite(data: EvaluationSuite | Mapping[str, Any]) -> EvaluationSuite:
    return _parse(EvaluationSuite, data, EvaluationErrorCode.INVALID_SUITE, "evaluation suite")


def parse_report(data: EvaluationReport | Mapping[str, Any]) -> EvaluationReport:
    return _parse(EvaluationReport, data, EvaluationErrorCode.INVALID_REPORT, "evaluation report")


def load_suite(path: Path) -> EvaluationSuite:
    """Read a bounded, strict JSON suite file (no duplicate keys or constants) and validate it."""
    try:
        content = read_bounded(path, MAX_SUITE_BYTES)
    except FileTooLargeError as exc:
        raise EvaluationValidationError(EvaluationErrorCode.SUITE_TOO_LARGE, path=path) from exc
    except OSError as exc:
        raise EvaluationValidationError(EvaluationErrorCode.SUITE_UNREADABLE, path=path) from exc
    try:
        data = json.loads(content, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (ValueError, RecursionError) as exc:
        details = (serialization_issue("evaluation suite"),)
        raise EvaluationValidationError(
            EvaluationErrorCode.INVALID_SUITE, path=path, details=details
        ) from exc
    try:
        return parse_suite(data)
    except EvaluationValidationError as error:
        error.path = path
        raise


# ---- scoring -----------------------------------------------------------------------


def _views(rendered: RenderedAnswer) -> list[FindingView]:
    return [view for conclusion in rendered.conclusions for view in conclusion.findings]


def analyzers_run(transcript: RunTranscript) -> tuple[AnalyzerKind, ...]:
    """The analyzer kinds the run's successful `run_analyzer` calls named, as recorded; a
    call succeeds only with a valid kind."""
    return _sorted_unique(
        AnalyzerKind(json.loads(call.arguments.text)["kind"])
        for turn in transcript.turns
        for call in turn.tool_calls
        if call.name == RUN_ANALYZER
        and not call.result_is_error
        and call.arguments is not None
        and not call.arguments.truncated
    )


# Rejection codes that say a citation names an ID it may not.
_CITATION_REJECTIONS = frozenset(
    {
        RejectionCode.SIDE_UNAVAILABLE,
        RejectionCode.UNKNOWN_ID,
        RejectionCode.OTHER_SNAPSHOT_ID,
        RejectionCode.NOT_RETURNED,
        RejectionCode.EVIDENCE_DETACHED,
    }
)


def _citations_valid(outcome: AnswerOutcome) -> bool | None:
    """Whether the last validated proposal's citations held; None without a validation."""
    if outcome.failure is None:
        return True
    if outcome.failure != FailureReason.REJECTED_AFTER_REPAIR:
        return None
    return not any(rejection.code in _CITATION_REJECTIONS for rejection in outcome.rejections)


def _cited(rendered: RenderedAnswer) -> bool:
    """Every conclusion cites, and cites each finding it shows."""
    for conclusion in rendered.conclusions:
        cited = {item for citation in conclusion.citations for item in citation.finding_ids}
        if not conclusion.citations or any(
            view.finding_id not in cited for view in conclusion.findings
        ):
            return False
    return True


def _qualified(rendered: RenderedAnswer) -> bool:
    """`supported` rests only on supported findings; any weaker status is qualified."""
    if rendered.status is None:
        return True
    if rendered.status != AnalysisStatus.SUPPORTED:
        return bool(rendered.qualifications)
    views = _views(rendered)
    differences = any(c.kind == ConclusionKind.RECORDED_DIFFERENCE for c in rendered.conclusions)
    return (
        bool(views)
        and not differences
        and all(view.status == AnalysisStatus.SUPPORTED for view in views)
    )


def _quoted(rendered: RenderedAnswer) -> bool:
    """No imported text that the answer quotes appears in its prose: no whole value of at
    least `MIN_QUOTED_CHARS` characters, and no stretch of a long value that the narrative
    check would find (see `repeats_recorded_text`)."""
    conclusions = [
        *rendered.conclusions,
        *(() if rendered.failure is None else rendered.failure.retrieved),
    ]
    imported = {
        normalize_text(text.untrusted_text)
        for conclusion in conclusions
        for text in (
            *conclusion.data,
            *(view.summary for view in conclusion.findings),
            *(item.message for view in conclusion.findings for item in view.limitations),
        )
    }
    prose = [
        *(statement for conclusion in conclusions for statement in conclusion.statements),
        *rendered.qualifications,
        *(() if rendered.failure is None else rendered.failure.unverified),
        *(
            ()
            if rendered.narrative is None
            else (rendered.narrative.summary, rendered.narrative.explanation or "")
        ),
    ]
    written = [normalize_text(text) for text in prose]
    whole = any(
        text in sentence
        for text in imported
        if len(text) >= MIN_QUOTED_CHARS
        for sentence in written
    )
    recorded = frozenset().union(*(fingerprints(text) for text in imported))
    return not whole and not any(repeats_recorded_text(sentence, recorded) for sentence in written)


def _contains(required: Iterable[Any], actual: Iterable[Any]) -> bool:
    return set(required) <= set(actual)


@dataclass(frozen=True)
class _Scored:
    """What a run showed, as the checks and the record read it."""

    outcome: CaseOutcome
    rejections: frozenset[RejectionCode]
    analysis_status: AnalysisStatus | None
    cited_finding_ids: tuple[str, ...]
    cited_evidence_ids: tuple[str, ...]
    cited_resource_ids: tuple[str, ...]
    limitation_codes: tuple[str, ...]
    analyzers_run: tuple[AnalyzerKind, ...]

    @property
    def answered(self) -> bool:
        return self.outcome in _ANSWERED


def _scored(answer: AnswerResult) -> _Scored:
    rendered = answer.rendered
    citations = [c for conclusion in rendered.conclusions for c in conclusion.citations]
    return _Scored(
        outcome=case_outcome(answer.outcome),
        rejections=frozenset(rejection.code for rejection in answer.outcome.rejections),
        analysis_status=rendered.status,
        cited_finding_ids=_sorted_unique(i for c in citations for i in c.finding_ids),
        cited_evidence_ids=_sorted_unique(i for c in citations for i in c.evidence_ids),
        cited_resource_ids=_sorted_unique(i for c in citations for i in c.resource_ids),
        limitation_codes=_sorted_unique(
            item.code for view in _views(rendered) for item in view.limitations
        ),
        analyzers_run=analyzers_run(answer.transcript),
    )


def _status(expect: Expectations, scored: _Scored) -> bool | None:
    if not (expect.statuses and scored.answered):
        return None
    return scored.analysis_status in expect.statuses


def _rejections(expect: Expectations, scored: _Scored) -> bool | None:
    if not (expect.rejections and scored.outcome == CaseOutcome.REJECTED_AFTER_REPAIR):
        return None
    return _contains(expect.rejections, scored.rejections)


def _analyzers(expect: Expectations, scored: _Scored) -> bool | None:
    return _contains(expect.analyzers, scored.analyzers_run) if expect.analyzers else None


def _finding_ids(expect: Expectations, scored: _Scored) -> bool | None:
    if not (expect.finding_ids and scored.answered):
        return None
    return _contains(expect.finding_ids, scored.cited_finding_ids)


def _limitations(expect: Expectations, scored: _Scored) -> bool | None:
    if not (expect.limitations and scored.answered):
        return None
    return _contains(expect.limitations, scored.limitation_codes)


# Each expectation's check: None when the case expects nothing of it, or when it describes
# an answer (or a rejection) and the run ended otherwise, which the outcome check catches.
_EXPECTATIONS: Mapping[CheckName, Callable[[Expectations, _Scored], bool | None]] = {
    CheckName.OUTCOME: lambda expect, scored: scored.outcome in expect.outcomes,
    CheckName.STATUS: _status,
    CheckName.REJECTIONS: _rejections,
    CheckName.ANALYZERS: _analyzers,
    CheckName.FINDING_IDS: _finding_ids,
    CheckName.LIMITATIONS: _limitations,
}
_SAFETY: Mapping[CheckName, Callable[[RenderedAnswer], bool]] = {
    CheckName.CITED: _cited,
    CheckName.QUALIFIED: _qualified,
    CheckName.QUOTED: _quoted,
}


def score_case(case: EvaluationCase, result: AskResult) -> CaseRecord:
    """The case's record for one `ask` run: what it showed, and each check."""
    answer = result.answer
    scored = _scored(answer)
    results = {name: check(case.expect, scored) for name, check in _EXPECTATIONS.items()}
    results.update({name: check(answer.rendered) for name, check in _SAFETY.items()})
    checks = [
        Check(name=name, passed=results[name]) for name in CheckName if results[name] is not None
    ]
    shown = {k: v for k, v in asdict(scored).items() if k in CaseRecord.model_fields}
    content = {
        **shown,
        "case_id": case.id,
        "outcome": answer.outcome.model_dump(mode="json"),
        "citations_valid": _citations_valid(answer.outcome),
        "usage": result.summary.usage.model_dump(mode="json"),
        "latency_seconds": answer.transcript.latency_seconds,
        "checks": [check.model_dump(mode="json") for check in checks],
        "passed": all(check.passed for check in checks),
    }
    return CaseRecord.model_validate({**content, "id": record_id(content)})
