"""Answer one question about selected snapshots with the configured provider.

The question is checked first, and the provider is built from the configuration before any
snapshot is read, so a request or configuration problem costs nothing. Errors before the run
starts are raised as typed errors (`InvalidQuestionError`, `ProviderConfigError`,
`FakeScriptError`, `SnapshotError`) for the caller's diagnostics; once the run starts,
everything that happens is an answer outcome.

A requested transcript destination is checked before the run starts, so a conflict costs no
provider request, and the transcript is written for every run that started, failed or not.
"""

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path

from pydantic import Field, ValidationError

from archdiscov.assistant.answer_rendering import rendered_snapshots
from archdiscov.assistant.answer_results import RenderedSnapshot
from archdiscov.assistant.answer_service import (
    AnswerCancelled,
    AnswerResult,
    answer_question_async,
)
from archdiscov.assistant.conversation import MAX_TEXT_CHARS, Contract, TokenUsage, UserMessage
from archdiscov.assistant.orchestrator import run_sync
from archdiscov.assistant.providers import load_provider
from archdiscov.assistant.runs import RunLimits, RunTranscript
from archdiscov.assistant.tools.session import ToolSession

from .publication import publish_text, render_json, require_new_path
from .snapshot_errors import SnapshotError

_TRANSCRIPT = "transcript"

_logger = logging.getLogger(__name__)


class ToolUsage(Contract):
    """What a run used, taken from its transcript."""

    provider_requests: int = Field(ge=0)
    tool_calls: dict[str, int]
    repair_requested: bool
    tokens: TokenUsage

    @classmethod
    def from_transcript(cls, transcript: RunTranscript) -> "ToolUsage":
        calls = Counter(call.name for turn in transcript.turns for call in turn.tool_calls)
        return cls(
            provider_requests=len(transcript.turns),
            tool_calls=dict(sorted(calls.items())),
            repair_requested=any(turn.repair for turn in transcript.turns),
            tokens=transcript.usage,
        )


class RunSummary(Contract):
    """The context a presentation shows before the answer."""

    snapshots: tuple[RenderedSnapshot, ...]
    provider_kind: str
    model: str
    limits: RunLimits
    usage: ToolUsage


class AskResult(Contract):
    answer: AnswerResult
    summary: RunSummary


class InvalidQuestionError(ValueError):
    """A question the run cannot send; the message is fixed and never echoes the question."""

    code = "invalid_question"

    def __init__(self) -> None:
        super().__init__(f"The question must contain text and at most {MAX_TEXT_CHARS} characters.")


def _check_question(question: str) -> None:
    try:
        UserMessage(text=question)
    except ValidationError as exc:
        raise InvalidQuestionError() from exc


class AskCancelled(asyncio.CancelledError):
    """Cancellation of a question, carrying its explained `cancelled` result."""

    def __init__(self, result: AskResult) -> None:
        super().__init__("Question was cancelled.")
        self.result = result


def _write_transcript(answer: AnswerResult, path: Path | None) -> None:
    if path is not None:
        publish_text(render_json(answer.transcript), path, subject=_TRANSCRIPT)


async def ask_question_async(
    question: str,
    *,
    snapshot_path: Path,
    config_path: Path,
    comparison_path: Path | None = None,
    transcript_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> AskResult:
    """Answer `question` over the selected snapshots; failures are outcomes, not errors.

    With `transcript_path`, the run's `RunTranscript` JSON is published there without
    overwriting; an existing path or a missing parent raises before the run starts. Cancelling
    the task raises `AskCancelled`, whose `result` explains the cancelled run.
    """
    _check_question(question)
    config, provider = load_provider(config_path, environ=environ)
    session = ToolSession.from_paths(snapshot_path, comparison_path=comparison_path)
    limits = RunLimits.from_config(config)
    if transcript_path is not None:
        require_new_path(transcript_path, subject=_TRANSCRIPT)

    def result(answer: AnswerResult) -> AskResult:
        summary = RunSummary(
            snapshots=rendered_snapshots(session),
            provider_kind=config.kind.value,
            model=config.model,
            limits=limits,
            usage=ToolUsage.from_transcript(answer.transcript),
        )
        return AskResult(answer=answer, summary=summary)

    try:
        answer = await answer_question_async(
            question,
            provider=provider,
            session=session,
            provider_kind=config.kind.value,
            model=config.model,
            limits=limits,
            clock=clock,
        )
    except AnswerCancelled as cancelled:
        try:
            _write_transcript(cancelled.result, transcript_path)
        except SnapshotError:
            # A failed save must not turn the cancellation into another error.
            _logger.warning("Unable to save the transcript of a cancelled question.")
        raise AskCancelled(result(cancelled.result)) from cancelled
    _write_transcript(answer, transcript_path)
    return result(answer)


def ask_question(
    question: str,
    *,
    snapshot_path: Path,
    config_path: Path,
    comparison_path: Path | None = None,
    transcript_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> AskResult:
    """Run `ask_question_async` from synchronous code; refused inside a running loop.

    An interrupt (Ctrl-C) cancels the run and raises `AskCancelled` with its explained result.
    """
    try:
        return run_sync(
            "ask_question",
            lambda: ask_question_async(
                question,
                snapshot_path=snapshot_path,
                config_path=config_path,
                comparison_path=comparison_path,
                transcript_path=transcript_path,
                environ=environ,
                clock=clock,
            ),
        )
    except KeyboardInterrupt as interrupt:
        # asyncio.run cancels the run on an interrupt, then raises KeyboardInterrupt in place
        # of the cancellation, which carries the explained result.
        cancelled = interrupt.__context__
        if not isinstance(cancelled, AskCancelled):
            raise
        raise cancelled from None
