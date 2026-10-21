"""The answer service: one run, validation, at most one repair, and the rendered answer.

`answer_question_async` runs the tool-calling loop with a repair callback. When the run ends
with a rejected proposal, a plain-text answer, or an unreadable `submit_answer` call, the
callback returns the fixed repair prompt and the loop makes its one reserved request. The
final proposal is validated again, and the outcome is rendered from records
(`render_answer`) or explained as a failure (`render_failure`); nothing the model wrote
becomes a fact.
"""

import asyncio
import time
from collections.abc import Callable

from .answer_rendering import render_answer, render_failure
from .answer_results import AnswerOutcome, RenderedAnswer, final_outcome, needs_repair
from .answer_validation import validate_proposal
from .conversation import AssistantMessage, Contract, Message
from .orchestrator import RunCancelled, RunResult, run_assistant_async, run_sync
from .provider import Provider
from .rejections import Rejection
from .repair import repair_prompt
from .runs import Proposed, RunLimits, RunOutcome, RunTranscript
from .tools.record import ReturnedIds
from .tools.registry import ToolRegistry
from .tools.session import ToolSession


class AnswerResult(Contract):
    """The answer outcome, what to show, the run's transcript, and the IDs tools returned."""

    outcome: AnswerOutcome
    rendered: RenderedAnswer
    transcript: RunTranscript
    returned: ReturnedIds


class AnswerCancelled(asyncio.CancelledError):
    """Cancellation of an answer, carrying its explained `cancelled` result."""

    def __init__(self, result: AnswerResult) -> None:
        super().__init__("Answer was cancelled.")
        self.result = result


class _Answering:
    """Validates each proposal once and remembers whether the repair was offered."""

    def __init__(self, session: ToolSession) -> None:
        self.session = session
        self.repaired = False
        self._validated: tuple[Proposed, tuple[Rejection, ...]] | None = None

    def rejections(self, outcome: RunOutcome) -> tuple[Rejection, ...]:
        if not isinstance(outcome, Proposed):
            return ()
        if self._validated is None or self._validated[0] != outcome:
            self._validated = (outcome, validate_proposal(outcome.proposal, self.session))
        return self._validated[1]

    def repair(self, outcome: RunOutcome, message: AssistantMessage) -> tuple[Message, ...] | None:
        rejections = self.rejections(outcome)
        if not needs_repair(outcome, rejections):
            return None
        self.repaired = True
        return repair_prompt(outcome, message, rejections)

    def finish(self, run: RunResult) -> AnswerResult:
        outcome = final_outcome(run.outcome, self.rejections(run.outcome), repaired=self.repaired)
        rendered = (
            render_failure(self.session, outcome)
            if outcome.failure is not None
            else render_answer(run.outcome.proposal, self.session, outcome)
        )
        return AnswerResult(
            outcome=outcome,
            rendered=rendered,
            transcript=run.transcript,
            returned=run.returned,
        )


async def answer_question_async(
    question: str,
    *,
    provider: Provider,
    session: ToolSession,
    provider_kind: str,
    model: str,
    limits: RunLimits | None = None,
    registry: ToolRegistry | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> AnswerResult:
    """Answer one question with at most one repair; failures are explained, not raised.

    Use one session per answer. Cancelling the task raises `AnswerCancelled`, whose `result`
    explains the cancelled run.
    """
    answering = _Answering(session)
    try:
        run = await run_assistant_async(
            question,
            provider=provider,
            session=session,
            provider_kind=provider_kind,
            model=model,
            limits=limits,
            registry=registry,
            clock=clock,
            repair=answering.repair,
        )
    except RunCancelled as cancelled:
        raise AnswerCancelled(answering.finish(cancelled.result)) from cancelled
    return answering.finish(run)


def answer_question(
    question: str,
    *,
    provider: Provider,
    session: ToolSession,
    provider_kind: str,
    model: str,
    limits: RunLimits | None = None,
    registry: ToolRegistry | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> AnswerResult:
    """Run `answer_question_async` from synchronous code; refused inside a running loop."""
    return run_sync(
        "answer_question",
        lambda: answer_question_async(
            question,
            provider=provider,
            session=session,
            provider_kind=provider_kind,
            model=model,
            limits=limits,
            registry=registry,
            clock=clock,
        ),
    )
