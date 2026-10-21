"""Answer one question about selected snapshots with the configured provider.

The provider is built from the configuration before any snapshot is read, so a
configuration problem costs nothing. Errors before the run starts are raised as the existing
typed errors (`ProviderConfigError`, `FakeScriptError`, `SnapshotError`) for the caller's
diagnostics; once the run starts, everything that happens is an answer outcome.
"""

import asyncio
import time
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path

from pydantic import Field

from archdiscov.assistant.answer_rendering import rendered_snapshots
from archdiscov.assistant.answer_results import RenderedSnapshot
from archdiscov.assistant.answer_service import (
    AnswerCancelled,
    AnswerResult,
    answer_question_async,
)
from archdiscov.assistant.conversation import Contract, TokenUsage
from archdiscov.assistant.orchestrator import run_sync
from archdiscov.assistant.providers import load_provider
from archdiscov.assistant.runs import RunLimits, RunTranscript
from archdiscov.assistant.tools.session import ToolSession


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


class AskCancelled(asyncio.CancelledError):
    """Cancellation of a question, carrying its explained `cancelled` result."""

    def __init__(self, result: AskResult) -> None:
        super().__init__("Question was cancelled.")
        self.result = result


async def ask_question_async(
    question: str,
    *,
    snapshot_path: Path,
    config_path: Path,
    comparison_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> AskResult:
    """Answer `question` over the selected snapshots; failures are outcomes, not errors.

    Cancelling the task raises `AskCancelled`, whose `result` explains the cancelled run.
    """
    config, provider = load_provider(config_path, environ=environ)
    session = ToolSession.from_paths(snapshot_path, comparison_path=comparison_path)
    limits = RunLimits.from_config(config)

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
        raise AskCancelled(result(cancelled.result)) from cancelled
    return result(answer)


def ask_question(
    question: str,
    *,
    snapshot_path: Path,
    config_path: Path,
    comparison_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> AskResult:
    """Run `ask_question_async` from synchronous code; refused inside a running loop."""
    return run_sync(
        "ask_question",
        lambda: ask_question_async(
            question,
            snapshot_path=snapshot_path,
            config_path=config_path,
            comparison_path=comparison_path,
            environ=environ,
            clock=clock,
        ),
    )
