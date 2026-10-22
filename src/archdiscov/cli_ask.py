"""The `ask` command: thin routing to the ask service and its presentation."""

from pathlib import Path
from typing import Annotated

import typer

from .cli_ask_output import ask_data, print_ask
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
    paths = {
        "snapshot_path": snapshot,
        "comparison_path": compare,
        "config_path": config,
        "transcript_path": transcript,
    }
    with safe_assistant_failures(snapshot, json_output):
        try:
            result = ask_question(
                question,
                snapshot_path=snapshot,
                config_path=config,
                comparison_path=compare,
                transcript_path=transcript,
            )
        except AskCancelled as cancelled:
            result = cancelled.result
    _present(result, paths, json_output)
    if code := result.answer.outcome.exit_code:
        raise typer.Exit(code)


def _present(result: AskResult, paths: dict[str, Path | None], as_json: bool) -> None:
    if as_json:
        emit_json(ask_data(result, paths))
    else:
        print_ask(result, paths)
