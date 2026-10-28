"""Recorded snapshot fixtures with chosen resources' fields replaced or dependencies dropped."""

from collections.abc import Iterable

from archdiscov.graph import GraphIndex
from archdiscov.services import load_snapshot
from tests.paths import FIXTURES_ROOT


def _walk(resources: list[dict]):
    for resource in resources:
        yield resource
        yield from _walk(resource.get("children") or [])


def edited_index(
    fixture: str, changes: dict[str, dict] | None = None, *, without: Iterable[str] = ()
) -> GraphIndex:
    """The fixture's graph, edited before it is validated again.

    `changes` maps a physical ID to the fields it replaces, and `without` names evidence whose
    citing dependencies are dropped, so a variant is an edit of the recorded fixture.
    """
    snapshot = load_snapshot(FIXTURES_ROOT / "snapshots" / fixture)
    data = snapshot.model_dump(mode="json")
    dropped = set(without)
    for region, region_data in data["regions"].items():
        for resource in _walk(region_data["resources"]):
            resource.update((changes or {}).get(resource["identity"]["physical_id"], {}))
        data["dependencies_by_region"][region] = [
            dependency
            for dependency in data["dependencies_by_region"].get(region, [])
            if not dropped & set(dependency["evidence_ids"])
        ]
    return GraphIndex(type(snapshot).model_validate(data))
