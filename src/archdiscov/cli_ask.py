"""The `ask` command: thin routing to the ask service and its presentation."""

from pathlib import Path
from typing import Annotated

import typer

from .cli_ask_output import AskPaths, ask_data, print_ask
from .cli_diagnostics import json_output as emit_json
from .cli_diagnostics import safe_assistant_failures
from .cli_options import JsonOption
from .services import AskCancelled, AskResult, ask_question


def ask(
    question: Annotated[str, typer.Argument(help="The question about the selected snapshots.")],
    snapshot: Annotated[Path, typer.Option("--snapshot", help="Snapshot JSON to answer from.")],
    config: Annotated[
        Path, typer.Option("--config", help="Provider configuration TOML; one provider per file.")
    ],
    compare: Annotated[
        Path | None,
        typer.Option("--compare", help="Second snapshot JSON, for comparison questions."),
    ] = None,
    comparison_result: Annotated[
        list[Path] | None,
        typer.Option(
            "--comparison-result",
            help="Saved comparison of the selected snapshots for the model to read; repeatable.",
        ),
    ] = None,
    transcript: Annotated[
        Path | None,
        typer.Option("--transcript", help="New run-transcript path; never overwritten."),
    ] = None,
    json_output: JsonOption = False,
) -> None:
    """Answer one question from the selected snapshots with the configured provider.

    The answer cites findings, evidence, and resources the tools returned, and is validated
    before it is shown; a failure is explained instead. Each run is one question: there is no
    chat or conversation memory. Exit codes: 0 answered, 5 no valid answer, 6 provider error
    or limit reached, 130 cancelled; 1, 2, and 3 for file, input, and conflict errors before
    the run starts.
    """
    comparison_results = comparison_result or []
    paths = {
        "snapshot_path": str(snapshot),
        "comparison_path": None if compare is None else str(compare),
        "comparison_result_paths": [str(path) for path in comparison_results],
        "config_path": str(config),
        "transcript_path": None if transcript is None else str(transcript),
    }
    with safe_assistant_failures(snapshot, json_output):
        try:
            result = ask_question(
                question,
                snapshot_path=snapshot,
                config_path=config,
                comparison_path=compare,
                comparison_result_paths=comparison_results,
                transcript_path=transcript,
            )
        except AskCancelled as cancelled:
            result = cancelled.result
    _present(result, paths, json_output)
    if code := result.answer.outcome.exit_code:
        raise typer.Exit(code)


def _present(result: AskResult, paths: AskPaths, as_json: bool) -> None:
    if as_json:
        emit_json(ask_data(result, paths))
    else:
        print_ask(result, paths)
