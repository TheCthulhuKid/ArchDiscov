"""Shared operation summaries for imported and collected snapshots."""

from collections.abc import Iterable

import typer

from .models.architecture import Coverage


def operations_data(operations: Iterable[Coverage]) -> list[dict]:
    return [
        {
            "region": op.region.value,
            "service": op.service.value,
            "operation": op.operation,
            "status": op.status.value,
            "result_count": op.result_count,
            "failures": [failure.code for failure in op.failures],
        }
        for op in operations
    ]


def print_operations(operations: list[dict]) -> None:
    for op in operations:
        count = "" if op["result_count"] is None else f" ({op['result_count']} results)"
        failed = f" [{', '.join(op['failures'])}]" if op["failures"] else ""
        typer.echo(
            f"  {op['region']} {op['service']} {op['operation']}: {op['status']}{count}{failed}"
        )
