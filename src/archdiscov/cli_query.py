"""The `query` command group: thin routing from options to query services."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from .cli_diagnostics import fail_snapshot_error, json_output, print_error
from .cli_query_output import print_inspect, print_neighbours, print_paths, print_search
from .graph import GraphIndex
from .models.enums import (
    AttachmentKind,
    DependencyRelationship,
    QueryDirection,
    QueryEdgeSelection,
    QueryErrorCode,
)
from .services import (
    QueryError,
    SnapshotError,
    inspect_resource,
    load_snapshot,
    query_neighbours,
    query_paths,
    search_resources,
)

query_app = typer.Typer(
    help="Query recorded resources and structure in a snapshot (read-only, no analysis).",
    no_args_is_help=True,
)

SnapshotOption = Annotated[Path, typer.Option("--snapshot", help="Snapshot JSON path to query.")]
JsonOption = Annotated[bool, typer.Option("--json", help="Emit JSON results and errors.")]
LimitOption = Annotated[int, typer.Option("--limit", help="Maximum results (1–10000).")]
DirectionOption = Annotated[
    QueryDirection, typer.Option("--direction", help="outgoing, incoming, or both.")
]
SelectionOption = Annotated[
    QueryEdgeSelection,
    typer.Option("--edges", help="dependencies, containment, or both; containment is structure."),
]
RelationshipOption = Annotated[
    list[DependencyRelationship] | None,
    typer.Option("--relationship", help="Dependency relationship filter; repeatable (OR)."),
]
AttachmentOption = Annotated[
    list[AttachmentKind] | None,
    typer.Option("--attachment-kind", help="Attachment kind filter; repeatable (OR)."),
]

_EXIT_CODES = {QueryErrorCode.INVALID_REQUEST: 2, QueryErrorCode.RESOURCE_NOT_FOUND: 4}


def _run(
    snapshot: Path,
    as_json: bool,
    service: Callable[[GraphIndex, dict[str, Any]], Any],
    request: Callable[[GraphIndex], dict[str, Any]],
    present: Callable[[Any, Path], None],
) -> None:
    """Load and index the snapshot once, run one service call, then present it."""
    try:
        index = GraphIndex(load_snapshot(snapshot))
        result = service(index, request(index))
    except SnapshotError as error:
        if error.path is None:
            error.path = snapshot
        fail_snapshot_error(error, as_json)
    except QueryError as error:
        print_error({"code": error.code.value, "message": error.message}, as_json=as_json)
        raise typer.Exit(_EXIT_CODES[error.code]) from None
    if as_json:
        json_output({"snapshot_path": str(snapshot), **result.model_dump(mode="json")})
    else:
        present(result, snapshot)


def _options(
    direction: QueryDirection,
    edges: QueryEdgeSelection,
    relationships: list[DependencyRelationship] | None,
    attachment_kinds: list[AttachmentKind] | None,
) -> dict[str, Any]:
    return {
        "direction": direction,
        "edge_selection": edges,
        "relationships": relationships or [],
        "attachment_kinds": attachment_kinds or [],
    }


@query_app.command()
def search(
    snapshot: SnapshotOption,
    native_type: Annotated[
        str | None, typer.Option("--type", help="Provider-native resource type.")
    ] = None,
    name: Annotated[str | None, typer.Option("--name", help="Exact resource name.")] = None,
    resource_id: Annotated[str | None, typer.Option("--id", help="Exact stable ID.")] = None,
    tag: Annotated[
        str | None, typer.Option("--tag", help="Recorded tag KEY or KEY=VALUE (exact).")
    ] = None,
    physical_id: Annotated[
        str | None,
        typer.Option("--physical-id", help="Physical ID, qualified by --type and snapshot scope."),
    ] = None,
    limit: LimitOption = 100,
    json_output: JsonOption = False,
) -> None:
    """Search recorded resources; filters combine with AND."""

    def request(index: GraphIndex) -> dict[str, Any]:
        data: dict[str, Any] = {"limit_result": limit}
        if native_type is not None:
            data["native_type"] = native_type
        if name is not None:
            data["name"] = name
        if resource_id is not None:
            data["resource_id"] = resource_id
        if tag is not None:
            key, separator, value = tag.partition("=")
            data["tag"] = {"key": key, "value": value if separator else None}
        if physical_id is not None:
            data["physical_identifier"] = {
                "scope": index.scope.model_dump(mode="json"),
                # A missing type fails request validation, reported as an invalid request.
                "native_type": native_type or "",
                "physical_id": physical_id,
            }
        return data

    _run(snapshot, json_output, search_resources, request, print_search)


@query_app.command()
def inspect(
    resource_id: Annotated[str, typer.Argument(help="Stable resource ID.")],
    snapshot: SnapshotOption,
    json_output: JsonOption = False,
) -> None:
    """Inspect one resource with its citations, evidence, and coverage context."""
    _run(
        snapshot,
        json_output,
        inspect_resource,
        lambda _: {"resource_id": resource_id},
        print_inspect,
    )


@query_app.command()
def neighbours(
    resource_id: Annotated[str, typer.Argument(help="Stable resource ID.")],
    snapshot: SnapshotOption,
    direction: DirectionOption = QueryDirection.OUTGOING,
    edges: SelectionOption = QueryEdgeSelection.DEPENDENCIES,
    relationship: RelationshipOption = None,
    attachment_kind: AttachmentOption = None,
    limit: LimitOption = 100,
    json_output: JsonOption = False,
) -> None:
    """List recorded adjacent edges of a resource."""
    _run(
        snapshot,
        json_output,
        query_neighbours,
        lambda _: {
            "resource_id": resource_id,
            "options": _options(direction, edges, relationship, attachment_kind),
            "limit_result": limit,
        },
        print_neighbours,
    )


@query_app.command()
def paths(
    resource_id: Annotated[str, typer.Argument(help="Stable start resource ID.")],
    snapshot: SnapshotOption,
    direction: DirectionOption = QueryDirection.OUTGOING,
    edges: SelectionOption = QueryEdgeSelection.DEPENDENCIES,
    relationship: RelationshipOption = None,
    attachment_kind: AttachmentOption = None,
    depth: Annotated[int, typer.Option("--depth", help="Maximum steps per path (1–10).")] = 3,
    limit: LimitOption = 100,
    work_limit: Annotated[
        int, typer.Option("--work-limit", help="Maximum candidate edges examined (1–1000000).")
    ] = 10_000,
    json_output: JsonOption = False,
) -> None:
    """Explore bounded, cycle-safe structural paths from a resource."""
    _run(
        snapshot,
        json_output,
        query_paths,
        lambda _: {
            "resource_id": resource_id,
            "options": _options(direction, edges, relationship, attachment_kind),
            "limits": {"depth": depth, "limit_result": limit, "work_limit": work_limit},
        },
        print_paths,
    )
