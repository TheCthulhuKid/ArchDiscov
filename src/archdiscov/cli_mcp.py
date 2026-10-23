"""The `mcp` command: thin routing to the MCP serving service.

Standard output belongs to the protocol, so the command has no `--json` and writes its
diagnostics to standard error. The service is imported only when the command runs.
"""

from pathlib import Path
from typing import Annotated

import typer

from .assistant.answer_results import FAILURE_EXIT_CODES, FailureReason
from .assistant.mcp_limits import (
    DEFAULT_MAX_TOOL_CALLS,
    MAX_MAX_TOOL_CALLS,
    MIN_MAX_TOOL_CALLS,
    ServerLimits,
)
from .cli_diagnostics import safe_assistant_failures


def mcp(
    snapshot: Annotated[Path, typer.Option("--snapshot", help="Snapshot JSON to serve.")],
    compare: Annotated[
        Path | None,
        typer.Option("--compare", help="Second snapshot JSON, for comparison questions."),
    ] = None,
    comparison_result: Annotated[
        list[Path] | None,
        typer.Option(
            "--comparison-result",
            help="Saved comparison of the selected snapshots for the client to read; repeatable.",
        ),
    ] = None,
    max_tool_calls: Annotated[
        int,
        typer.Option(
            "--max-tool-calls",
            min=MIN_MAX_TOOL_CALLS,
            max=MAX_MAX_TOOL_CALLS,
            help="Tool calls one client connection may make, submissions included.",
        ),
    ] = DEFAULT_MAX_TOOL_CALLS,
) -> None:
    """Serve the selected snapshots to an MCP client, such as Claude Code, over stdio.

    The client's model reads the snapshots only through the read-only assistant tools and
    submits its answers for validation. The server reads only the files selected here: it
    never writes a file, changes a snapshot, or reaches a network. Exit codes: 0 when the
    client disconnects, 130 when interrupted; 1 and 2 for file and input errors before
    serving.
    """
    from .services.mcp import serve_selection

    with safe_assistant_failures(snapshot, as_json=False):
        try:
            serve_selection(
                snapshot,
                comparison_path=compare,
                comparison_result_paths=comparison_result or [],
                limits=ServerLimits(max_tool_calls=max_tool_calls),
            )
        except KeyboardInterrupt:
            raise typer.Exit(FAILURE_EXIT_CODES[FailureReason.CANCELLED]) from None
