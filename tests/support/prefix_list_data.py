"""The prefix-list fixture, with chosen resources' fields replaced."""

from archdiscov.graph import GraphIndex
from archdiscov.services import load_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURE = FIXTURES_ROOT / "snapshots" / "endpoints-and-prefix-lists.json"


def _walk(resources: list[dict]):
    for resource in resources:
        yield resource
        yield from _walk(resource.get("children") or [])


def prefix_list_index(changes: dict[str, dict] | None = None) -> GraphIndex:
    """The fixture's graph, with `changes` mapping a physical ID to the fields it replaces."""
    snapshot = load_snapshot(FIXTURE)
    data = snapshot.model_dump(mode="json")
    for resource in _walk(data["regions"]["eu-west-1"]["resources"]):
        resource.update((changes or {}).get(resource["identity"]["physical_id"], {}))
    return GraphIndex(type(snapshot).model_validate(data))
