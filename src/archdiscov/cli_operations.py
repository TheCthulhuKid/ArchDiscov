"""Shared operation summaries for imported and collected snapshots."""

from collections.abc import Iterable

import typer

from .models.architecture import Coverage
from .models.cloud.contracts import CloudOperationCoverage


def operations_data(operations: Iterable[Coverage | CloudOperationCoverage]) -> list[dict]:
    results = []
    for op in operations:
        context = (
            {
                "platform": op.scope.platform.value,
                "scope": op.scope.model_dump(mode="json"),
                "location": op.location,
                "service": op.service,
            }
            if isinstance(op, CloudOperationCoverage)
            else {"region": op.region.value, "service": op.service.value}
        )
        results.append(
            {
                **context,
                "operation": op.operation,
                "status": op.status.value,
                "result_count": op.result_count,
                "failures": [failure.code for failure in op.failures],
            }
        )
    return results


def print_operations(operations: list[dict]) -> None:
    for op in operations:
        count = "" if op["result_count"] is None else f" ({op['result_count']} results)"
        failed = f" [{', '.join(op['failures'])}]" if op["failures"] else ""
        location = op.get("region", op.get("location") or "scope-wide")
        platform = f"{op['platform']} " if "platform" in op else ""
        typer.echo(
            f"  {platform}{location} {op['service']} {op['operation']}: "
            f"{op['status']}{count}{failed}"
        )
