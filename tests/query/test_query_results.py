from archdiscov.graph import GraphIndex
from archdiscov.models.enums import QueryTruncationReason
from archdiscov.models.query.results import QueryPath, QueryPathsResult, QueryTruncation
from archdiscov.services.snapshots import load_snapshot
from tests.paths import FIXTURES_ROOT


def test_paths_result_round_trips_both_edge_kinds_and_directions():
    index = GraphIndex(load_snapshot(FIXTURES_ROOT / "snapshots" / "hierarchy.json"))
    dependency = index.dependency_edges[0]
    containment = index.containment_edges[0]
    path = QueryPath(
        steps=[
            {"edge": {"kind": "dependency", "edge": dependency}, "direction": "outgoing"},
            {"edge": {"kind": "containment", "edge": containment}, "direction": "incoming"},
        ],
        limitations=[
            {
                "kind": "provenance_unavailable",
                "resource_id": dependency.source_id,
                "fact_path": "/attributes/tags",
                "message": "Tag provenance is unavailable.",
            }
        ],
    )
    result = QueryPathsResult(
        snapshot_id=index.snapshot_id,
        scope=index.scope,
        resource_id=dependency.source_id,
        paths=[path],
        truncation=QueryTruncation(reasons=[QueryTruncationReason.WORK_LIMIT]),
    )

    restored = QueryPathsResult.model_validate_json(result.model_dump_json())

    assert restored == result
    assert [step.edge.edge for step in restored.paths[0].steps] == [dependency, containment]
