"""Readable and JSON presentation of rendered answers, separate from routing and services.

Both forms come only from a `RenderedAnswer`. Imported text is always quoted and labelled
`imported`, the model's narrative appears only under its own label with every line marked,
so neither can pass for a rendered conclusion. On the way out every string is redacted of
secret-looking text, and readable lines lose control characters.
"""

import json
from collections.abc import Iterable, Iterator

import typer

from .assistant.answer_results import (
    AnswerStatus,
    RenderedAnswer,
    RenderedConclusion,
    RenderedNarrative,
    RenderedSnapshot,
)
from .assistant.answers import Citation
from .assistant.redaction import redact_secrets
from .assistant.tools.contracts import UntrustedText, as_data
from .assistant.tools.views import FindingView

ANSWER_JSON_VERSION = "1"

_HEADINGS = {
    AnswerStatus.ANSWERED: "Answer: answered",
    AnswerStatus.ANSWERED_AFTER_REPAIR: "Answer: answered after one repair",
    AnswerStatus.FAILED: "Answer: failed",
}
_NARRATIVE = "Explanation from the model (not verified; the facts are the conclusions above):"
_EVIDENCE_HINT = "Inspect cited evidence with `archdiscov evidence ID --snapshot PATH`."


def answer_data(rendered: RenderedAnswer) -> dict:
    """The JSON contract: a version, the exit code, and the rendered answer, redacted."""
    return redacted_data(
        {
            "schema_version": ANSWER_JSON_VERSION,
            "exit_code": rendered.outcome.exit_code,
            "answer": rendered.model_dump(mode="json"),
        }
    )


def answer_text(rendered: RenderedAnswer) -> str:
    return display_text(_lines(rendered))


def display_text(lines: Iterable[str]) -> str:
    """Join readable lines, redacted, with control characters such as escapes as spaces."""
    return "\n".join(redact_secrets(as_data(line)).rstrip() for line in lines)


def print_answer(rendered: RenderedAnswer) -> None:
    typer.echo(answer_text(rendered))


def _lines(rendered: RenderedAnswer) -> Iterator[str]:
    outcome = rendered.outcome
    reason = "" if outcome.failure is None else f" ({outcome.failure})"
    yield f"{_HEADINGS[outcome.status]}{reason}, exit code {outcome.exit_code}"
    for snapshot in rendered.snapshots:
        yield from _snapshot(snapshot)
    if rendered.status is not None:
        yield f"Status: {rendered.status}"
    yield from _section("Qualifications:", (f"  - {item}" for item in rendered.qualifications))
    if rendered.failure is not None:
        failure = rendered.failure
        yield from _section("Not verified:", (f"  - {item}" for item in failure.unverified))
        yield from _conclusions("Retrieved findings (not an answer):", failure.retrieved)
        return
    yield from _conclusions("Conclusions:", rendered.conclusions)
    yield from _narrative(rendered.narrative)
    if rendered.conclusions:
        yield _EVIDENCE_HINT


def _section(title: str, lines: Iterator[str]) -> Iterator[str]:
    lines = list(lines)
    if lines:
        yield title
        yield from lines


def _snapshot(snapshot: RenderedSnapshot) -> Iterator[str]:
    state = (
        "declared or planned configuration, not deployed state"
        if snapshot.declared
        else "observed state"
    )
    yield f"Snapshot ({snapshot.side}): {snapshot.snapshot_id}"
    yield f"  Source: {snapshot.source_kind}, {state}"
    started, completed = snapshot.collection_started_at, snapshot.collection_completed_at
    yield f"  Collected: {started.isoformat()} to {completed.isoformat()}"


def _conclusions(title: str, conclusions: tuple[RenderedConclusion, ...]) -> Iterator[str]:
    if not conclusions:
        return
    yield title
    for number, conclusion in enumerate(conclusions, 1):
        yield from _conclusion(number, conclusion)


def _conclusion(number: int, conclusion: RenderedConclusion) -> Iterator[str]:
    status = "" if conclusion.status is None else f" [{conclusion.status}]"
    yield f"  {number}. {conclusion.kind}{status}"
    for statement in conclusion.statements:
        yield f"     {statement}"
    for index, item in enumerate(conclusion.data, 1):
        yield f"     [{index}] imported: {_quoted(item)}"
    for view in conclusion.findings:
        yield from _finding(view)
    for citation in conclusion.citations:
        yield f"     Cited on the {citation.side} snapshot: {_cited(citation)}"


def _finding(view: FindingView) -> Iterator[str]:
    yield f"     Finding {view.finding_id} ({view.status})"
    for limitation in view.limitations:
        yield f"       Limitation {limitation.code}: imported: {_quoted(limitation.message)}"


def _cited(citation: Citation) -> str:
    parts = (
        ("findings", citation.finding_ids),
        ("evidence", citation.evidence_ids),
        ("resources", citation.resource_ids),
    )
    return "; ".join(f"{label} {', '.join(ids)}" for label, ids in parts if ids)


def _quoted(text: UntrustedText) -> str:
    # JSON quoting keeps imported text on one line, so it cannot start a line of its own.
    quoted = json.dumps(text.untrusted_text, ensure_ascii=False)
    return f"{quoted} (truncated)" if text.truncated else quoted


def _narrative(narrative: RenderedNarrative | None) -> Iterator[str]:
    if narrative is None:
        return
    yield _NARRATIVE
    paragraphs = [narrative.summary, *filter(None, [narrative.explanation])]
    for number, paragraph in enumerate(paragraphs):
        if number:
            yield "  |"
        # Every line is marked, so model text cannot pass for a heading or conclusion.
        yield from (f"  | {line}" for line in paragraph.splitlines() or [""])


def redacted_data(value):
    """JSON data with every string redacted of secret-looking text."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: redacted_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redacted_data(item) for item in value]
    return value
