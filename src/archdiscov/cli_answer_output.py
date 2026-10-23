"""Readable presentation of rendered answers on the terminal.

The readable and JSON forms are built in `assistant/answer_output.py`, which the MCP server
shares; this module only prints them.
"""

import typer

from .assistant.answer_output import answer_text
from .assistant.answer_results import RenderedAnswer


def print_answer(rendered: RenderedAnswer) -> None:
    typer.echo(answer_text(rendered))
