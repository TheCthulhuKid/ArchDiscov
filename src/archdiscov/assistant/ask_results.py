"""What an `ask` run returns: the validated answer and a summary of what the run used.

They are assistant contracts, kept beside the answer contracts, so that evaluation can score
an `ask` run without depending on the service that runs it.
"""

from collections import Counter

from pydantic import Field

from .answer_results import RenderedSnapshot
from .answer_service import AnswerResult
from .conversation import Contract, TokenUsage
from .runs import RunLimits, RunTranscript


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
