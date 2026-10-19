"""Option annotations shared by command modules."""

from typing import Annotated

import typer

JsonOption = Annotated[bool, typer.Option("--json", help="Emit JSON results and errors.")]
