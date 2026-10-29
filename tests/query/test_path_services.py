from copy import deepcopy

import pytest

from archdiscov.graph import GraphIndex
from archdiscov.models.architecture import LocalTarget
from archdiscov.models.enums import QueryErrorCode, QueryLimitationKind, QueryTruncationReason
from archdiscov.models.query.results import QueryPathsResult
from archdiscov.services import QueryError, load_snapshot, query_paths
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"
PREFIX = "123456789012/eu-west-1/ec2/physical/"
RTB, IGW, SGW, SGD, SUB = (
    PREFIX + name for name in ("rtb-main", "igw-main", "sg-web", "sg-db", "subnet-app")
)


def graph(edges=(), *, repeat=1):
    """Network fixture plus synthetic local edges; sources must be RTB, SGW, or ENI."""
    source = load_snapshot(FIXTURES / "network.json")
    region = next(iter(source.dependencies_by_region.values()))
    template = next(edge for edge in region if isinstance(edge.target, LocalTarget))
    # A citation must be owned by the edge's source, so reuse one from an existing source edge.
    owned = {edge.source_id: edge.evidence_ids for edge in region}
    for from_id, to_id in edges:
        for _ in range(repeat):
            edge = deepcopy(template)
            edge.source_id = from_id
            edge.evidence_ids = list(owned[from_id])
            edge.target = LocalTarget(resource_id=to_id)
            region.append(edge)
    return GraphIndex(source)


def ids(result):
    return [[(s.edge.edge.source_id, s.edge.edge.target_id) for s in p.steps] for p in result.paths]


def run(index, resource_id=RTB, **kwargs):
    return query_paths(index, {"resource_id": resource_id, **kwargs})


def test_branching_prefixes_are_shortest_first_and_round_trip():
    index = graph([(RTB, SGW), (SGW, SUB)])
    result = run(index)
    local = [path for path in ids(result) if not path[-1][1].startswith("terminal/")]
    assert local == [
        [(RTB, IGW)],
        [(RTB, SGW)],
        [(RTB, SGW), (SGW, SGD)],
        [(RTB, SGW), (SGW, SUB)],
    ]
    assert [len(path) for path in ids(result)] == sorted(len(path) for path in ids(result))
    assert QueryPathsResult.model_validate_json(result.model_dump_json()) == result


def test_cycles_stop_without_losing_distinct_paths_or_parallel_edges():
    index = graph([(RTB, SGW), (SGW, RTB), (SGW, SGW)])
    result = run(index, limits={"depth": 10})
    local = [path for path in ids(result) if not path[-1][1].startswith("terminal/")]
    assert local == [[(RTB, IGW)], [(RTB, SGW)], [(RTB, SGW), (SGW, SGD)]]
    assert result.truncation.reasons == []
    parallel = run(graph([(RTB, SGW), (SGW, SUB)], repeat=2), limits={"depth": 2})
    through = [p for p in parallel.paths if p.steps[-1].edge.edge.target_id == SUB]
    assert len({tuple(s.edge.edge.id for s in p.steps) for p in through}) == len(through) == 4


def test_depth_boundary_reports_only_when_structure_remains():
    index = graph([(RTB, SGW)])
    shallow = run(index, limits={"depth": 1})
    assert [len(p.steps) for p in shallow.paths] == [1, 1, 1]
    assert shallow.truncation.reasons == [QueryTruncationReason.DEPTH_LIMIT]
    exact = run(index, limits={"depth": 2})
    assert sorted(len(p.steps) for p in exact.paths) == [1, 1, 1, 2]
    assert exact.truncation.reasons == []


def test_result_and_work_limits_are_deterministic_and_bounded():
    index = graph([(RTB, SGW), (SGW, SUB)])
    full = run(index)
    assert len(full.paths) == 5
    limited = run(index, limits={"limit_result": 2})
    assert ids(limited) == ids(full)[:2]
    assert limited.truncation.reasons == [QueryTruncationReason.RESULT_LIMIT]
    assert run(index, limits={"limit_result": 5}).truncation.reasons == []
    starved = run(index, limits={"work_limit": 3})
    assert ids(starved) == ids(full)[: len(starved.paths)]
    assert len(starved.paths) < len(full.paths)
    assert starved.truncation.reasons == [QueryTruncationReason.WORK_LIMIT]
    assert run(index, limits={"work_limit": 5}).truncation.reasons == []
    # Candidates rejected by filters are still examined, so they still cost work.
    filtered = {"relationships": ["assumes_role"]}
    exhausted = run(index, options=filtered, limits={"work_limit": 3})
    assert (exhausted.paths, exhausted.truncation.reasons) == ([], [])
    starved = run(index, options=filtered, limits={"work_limit": 1})
    assert starved.truncation.reasons == [QueryTruncationReason.WORK_LIMIT]


def test_terminals_end_paths_with_limitations():
    index = graph()
    result = run(index, limits={"depth": 5})
    [ended] = [p for p in result.paths if p.limitations]
    assert ended.limitations[0].kind in {
        QueryLimitationKind.EXTERNAL_TARGET,
        QueryLimitationKind.UNRESOLVED_TARGET,
    }
    assert len(ended.steps) == 1
    assert index.get_resource(ended.steps[-1].edge.edge.target_id) is None


def test_incoming_direction_preserves_stored_edges():
    index = graph([(RTB, SGW)])
    result = run(index, SGD, options={"direction": "incoming"}, limits={"depth": 2})
    assert ids(result) == [[(SGW, SGD)], [(SGW, SGD), (RTB, SGW)]]
    assert {s.direction for p in result.paths for s in p.steps} == {"incoming"}
    stored = {edge.id: edge for edge in index.dependency_edges}
    for path in result.paths:
        for step in path.steps:
            assert step.edge.edge == stored[step.edge.edge.id]


def test_coverage_gaps_survive_empty_results():
    index = GraphIndex(load_snapshot(FIXTURES / "partial-coverage.json"))
    result = run(index, index.resource_ids[0])
    assert result.paths == []
    assert any(item.kind == QueryLimitationKind.COVERAGE_LIMITED for item in result.limitations)


def test_results_are_detached_from_the_index_and_between_paths():
    index = graph([(RTB, SGW), (SGW, SUB)])
    before = index.dependency_edges
    result = run(index)
    first, second = (p for p in result.paths if len(p.steps) == 2)
    assert first.steps[0] is not second.steps[0]
    first.steps[0].edge.edge.attributes["changed"] = True
    assert "changed" not in second.steps[0].edge.edge.attributes
    assert index.dependency_edges == before


def test_unknown_and_invalid_requests_are_safe_errors():
    index = graph()
    with pytest.raises(QueryError) as unknown:
        run(index, "absent")
    assert unknown.value.code == QueryErrorCode.RESOURCE_NOT_FOUND
    with pytest.raises(QueryError) as error:
        query_paths(index, {"resource_id": RTB, "limits": {"depth": 11}})
    assert error.value.code == QueryErrorCode.INVALID_REQUEST


def test_excluded_resources_are_never_entered_and_never_reported_as_truncation():
    index = graph([(RTB, SGW), (SGW, SUB)])
    full = run(index, limits={"depth": 1})
    assert full.truncation.reasons == [QueryTruncationReason.DEPTH_LIMIT]
    excluded = run(index, exclude_resource_ids=[SGW], limits={"depth": 1})
    assert SGW not in {step.edge.edge.target_id for p in excluded.paths for step in p.steps}
    assert excluded.truncation.reasons == []
    deep = run(index, exclude_resource_ids=[SGW], limits={"depth": 5})
    assert all(SGW not in {s.edge.edge.target_id for s in p.steps} for p in deep.paths)
    # Excluded candidates are still examined, so they still cost work.
    starved = run(index, exclude_resource_ids=[SGW], limits={"work_limit": 2})
    assert starved.truncation.reasons == [QueryTruncationReason.WORK_LIMIT]
