"""Readable and JSON presentation of an `ask` run: the run summary, then the answer.

The answer itself is presented by `cli_answer_output`; this module adds what the run used
and where its files are, redacted and sanitised the same way.
"""

from collections.abc import Iterator

import typer

from .assistant.conversation import TokenUsage
from .assistant.runs import RunLimits
from .cli_answer_output import answer_data, display_text, print_answer, redacted_data
from .services import AskResult

ASK_JSON_VERSION = "1"

# The selected paths as given, keyed as in the JSON envelope; lists hold repeatable options.
type AskPaths = dict[str, str | list[str] | None]

_TOKEN_LABELS = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_read_input_tokens": "cache read",
    "cache_write_input_tokens": "cache write",
}


def ask_data(result: AskResult, paths: AskPaths) -> dict:
    """The JSON contract: a version, the selected paths, the run summary, and the answer."""
    return redacted_data(
        {
            "schema_version": ASK_JSON_VERSION,
            **paths,
            "summary": result.summary.model_dump(mode="json"),
            "result": answer_data(result.answer.rendered),
        }
    )


def print_ask(result: AskResult, paths: AskPaths) -> None:
    typer.echo(display_text(_summary(result, paths)))
    typer.echo("")
    print_answer(result.answer.rendered)


def _summary(result: AskResult, paths: AskPaths) -> Iterator[str]:
    summary, usage = result.summary, result.summary.usage
    yield f"Snapshot: {paths['snapshot_path']}"
    if paths["comparison_path"] is not None:
        yield f"Comparison: {paths['comparison_path']}"
    for path in paths["comparison_result_paths"]:
        yield f"Comparison result: {path}"
    yield f"Provider: {summary.provider_kind}, model {summary.model}"
    yield f"Limits: {_limits(summary.limits)}"
    repair = " (repair requested)" if usage.repair_requested else ""
    yield f"Provider requests: {usage.provider_requests}{repair}"
    calls = ", ".join(f"{name} {count}" for name, count in usage.tool_calls.items())
    yield f"Tool calls: {calls or 'none'}"
    yield f"Tokens: {_tokens(usage.tokens)}"
    if paths["transcript_path"] is not None:
        yield f"Transcript: {paths['transcript_path']}"


def _limits(limits: RunLimits) -> str:
    return (
        f"{limits.max_provider_requests} provider requests, {limits.max_tool_calls} tool calls "
        f"({limits.max_tool_calls_per_turn} per turn), {limits.max_output_tokens} output tokens "
        f"per request, {limits.max_total_output_chars} conversation characters, "
        f"{limits.request_timeout_seconds:g}s per request, "
        f"{limits.overall_timeout_seconds:g}s overall"
    )


def _tokens(tokens: TokenUsage) -> str:
    counts = (
        f"{count} {_TOKEN_LABELS[name]}"
        for name, count in tokens.model_dump().items()
        if count is not None
    )
    return ", ".join(counts) or "not reported"
