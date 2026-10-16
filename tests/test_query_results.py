from pathlib import Path

import pytest
from pydantic import ValidationError

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import QueryDirection, QueryTruncationReason
from archdiscov.models.query_edges import QueryTraversalStep
from archdiscov.models.query_limitations import QueryProvenanceLimitation
from archdiscov.models.query_results import (
    QueryInspectResult,
    QueryNeighboursResult,
    QueryPath,
    QueryPathsResult,
    QuerySearchResult,
    QueryTruncation,
)
from archdiscov.services.snapshots import load_snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "snapshots"


def test_truncation_defaults():
    truncation = QueryTruncation()

    assert truncation.reasons == []


def test_truncation_reasons_round_trip():
    reasons = [
        QueryTruncationReason.DEPTH_LIMIT,
        QueryTruncationReason.WORK_LIMIT,
    ]
    truncation = QueryTruncation(reasons=reasons)

    assert truncation.reasons == reasons
    assert QueryTruncation.model_validate_json(truncation.model_dump_json()) == truncation


def test_truncation_rejects_unknown_reason():
    with pytest.raises(ValidationError) as caught:
        QueryTruncation.model_validate({"reasons": ["invalid"]})

    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("reasons", 0)
    assert errors[0]["type"] == "enum"


@pytest.mark.parametrize("fixture", ["hierarchy", "azure"])
def test_search_result_defaults(fixture):
    index = GraphIndex(load_snapshot(FIXTURES / f"{fixture}.json"))

    result = QuerySearchResult(
        snapshot_id=index.snapshot_id,
        scope=index.scope,
    )

    assert result.snapshot_id == index.snapshot_id
    assert result.scope == index.scope
    assert result.resources == []
    assert result.limitations == []
    assert result.truncation.reasons == []


@pytest.mark.parametrize("fixture", ["hierarchy", "azure"])
def test_search_result_composition_round_trip(fixture):
    index = GraphIndex(load_snapshot(FIXTURES / f"{fixture}.json"))
    resource = index.resources()[0]
    limitation = QueryProvenanceLimitation(
        resource_id=resource.id,
        fact_path="/attributes/tags",
        message="Tag provenance is unavailable.",
    )
    result = QuerySearchResult(
        snapshot_id=index.snapshot_id,
        scope=index.scope,
        resources=[resource],
        limitations=[limitation],
        truncation=QueryTruncation(
            reasons=[QueryTruncationReason.RESULT_LIMIT],
        ),
    )

    restored = QuerySearchResult.model_validate_json(result.model_dump_json())

    assert restored == result
    assert type(restored.resources[0]) is type(resource)
    assert restored.resources[0] == resource
    assert isinstance(restored.limitations[0], QueryProvenanceLimitation)
    assert restored.limitations[0] == limitation
    assert restored.truncation.reasons == [QueryTruncationReason.RESULT_LIMIT]


@pytest.mark.parametrize("fixture", ["hierarchy", "azure"])
def test_inspect_result_defaults(fixture):
    index = GraphIndex(load_snapshot(FIXTURES / f"{fixture}.json"))
    resource = index.resources()[0]

    result = QueryInspectResult(
        snapshot_id=index.snapshot_id,
        scope=index.scope,
        resource=resource,
    )

    assert result.resource == resource
    assert result.evidence == {}
    assert result.coverage == []
    assert result.limitations == []


@pytest.mark.parametrize("fixture", ["hierarchy", "azure"])
def test_inspect_result_composition_round_trip(fixture):
    index = GraphIndex(load_snapshot(FIXTURES / f"{fixture}.json"))
    resource = next(
        resource for resource in index.resources() if index.resource_evidence_ids(resource.id)
    )
    evidence = {
        evidence_id: index.get_evidence(evidence_id)
        for evidence_id in index.resource_evidence_ids(resource.id)
    }
    coverage = list(index.coverage_records())
    limitation = QueryProvenanceLimitation(
        resource_id=resource.id,
        fact_path="/attributes/tags",
        message="Tag provenance is unavailable.",
    )
    result = QueryInspectResult(
        snapshot_id=index.snapshot_id,
        scope=index.scope,
        resource=resource,
        evidence=evidence,
        coverage=coverage,
        limitations=[limitation],
    )

    restored = QueryInspectResult.model_validate_json(result.model_dump_json())
    assert restored == result
    assert type(restored.resource) is type(resource)
    assert restored.resource == resource
    assert restored.evidence == evidence
    assert restored.coverage == coverage
    assert isinstance(restored.limitations[0], QueryProvenanceLimitation)
    assert restored.limitations[0] == limitation


@pytest.mark.parametrize("fixture", ["hierarchy", "azure"])
def test_neighbour_result_defaults(fixture):
    index = GraphIndex(load_snapshot(FIXTURES / f"{fixture}.json"))
    resource_id = index.resource_ids[0]

    result = QueryNeighboursResult(
        snapshot_id=index.snapshot_id,
        scope=index.scope,
        resource_id=resource_id,
    )

    assert result.resource_id == resource_id
    assert result.steps == []
    assert result.limitations == []
    assert result.truncation.reasons == []


@pytest.mark.parametrize("kind", ["dependency", "containment"])
@pytest.mark.parametrize(
    "direction",
    [QueryDirection.OUTGOING, QueryDirection.INCOMING],
)
def test_neighbour_result_composition_round_trip(kind, direction):
    index = GraphIndex(load_snapshot(FIXTURES / "hierarchy.json"))
    edge = index.dependency_edges[0] if kind == "dependency" else index.containment_edges[0]
    resource_id = edge.source_id if direction == QueryDirection.OUTGOING else edge.target_id
    step = QueryTraversalStep.model_validate(
        {"edge": {"kind": kind, "edge": edge}, "direction": direction}
    )
    limitation = QueryProvenanceLimitation(
        resource_id=resource_id,
        fact_path="/attributes/tags",
        message="Tag provenance is unavailable.",
    )
    result = QueryNeighboursResult(
        snapshot_id=index.snapshot_id,
        scope=index.scope,
        resource_id=resource_id,
        steps=[step],
        limitations=[limitation],
        truncation=QueryTruncation(reasons=[QueryTruncationReason.RESULT_LIMIT]),
    )
    restored = QueryNeighboursResult.model_validate_json(result.model_dump_json())

    assert restored == result
    assert restored.steps[0].direction == direction
    assert restored.steps[0].edge.kind == kind
    assert restored.steps[0].edge.edge == edge
    assert restored.limitations[0] == limitation
    assert restored.truncation.reasons == [QueryTruncationReason.RESULT_LIMIT]


def test_path_round_trip_preserves_step_order():
    index = GraphIndex(load_snapshot(FIXTURES / "hierarchy.json"))
    first, second = next(
        (first, second)
        for first in index.containment_edges
        for second in index.containment_edges
        if first.target_id == second.source_id
    )
    steps = [
        QueryTraversalStep.model_validate(
            {
                "edge": {"kind": "containment", "edge": edge},
                "direction": "outgoing",
            }
        )
        for edge in (first, second)
    ]
    limitation = QueryProvenanceLimitation(
        resource_id=second.target_id,
        fact_path="/attributes/tags",
        message="Tag provenance is unavailable.",
    )
    path = QueryPath(steps=steps, limitations=[limitation])

    restored = QueryPath.model_validate_json(path.model_dump_json())

    assert restored == path
    assert [step.edge.edge for step in restored.steps] == [first, second]
    assert restored.limitations == [limitation]


def test_path_limitations_default_to_empty():
    index = GraphIndex(load_snapshot(FIXTURES / "hierarchy.json"))
    step = QueryTraversalStep.model_validate(
        {
            "edge": {
                "kind": "containment",
                "edge": index.containment_edges[0],
            },
            "direction": "outgoing",
        }
    )
    assert QueryPath(steps=[step]).limitations == []


def test_path_rejects_empty_steps():
    with pytest.raises(ValidationError) as caught:
        QueryPath(steps=[])
    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("steps",)
    assert errors[0]["type"] == "too_short"


@pytest.mark.parametrize("fixture", ["hierarchy", "azure"])
def test_paths_result_defaults(fixture):
    index = GraphIndex(load_snapshot(FIXTURES / f"{fixture}.json"))
    resource_id = index.resource_ids[0]

    result = QueryPathsResult(
        snapshot_id=index.snapshot_id,
        scope=index.scope,
        resource_id=resource_id,
    )

    assert result.snapshot_id == index.snapshot_id
    assert result.scope == index.scope
    assert result.resource_id == resource_id
    assert result.paths == []
    assert result.limitations == []
    assert result.truncation.reasons == []


def test_paths_result_composition_round_trip():
    index = GraphIndex(load_snapshot(FIXTURES / "hierarchy.json"))
    edge = index.dependency_edges[0]
    step = QueryTraversalStep.model_validate(
        {
            "edge": {"kind": "dependency", "edge": edge},
            "direction": "outgoing",
        }
    )
    limitation = QueryProvenanceLimitation(
        resource_id=edge.source_id,
        fact_path="/attributes/tags",
        message="Tag provenance is unavailable.",
    )
    path = QueryPath(steps=[step], limitations=[limitation])
    result = QueryPathsResult(
        snapshot_id=index.snapshot_id,
        scope=index.scope,
        resource_id=edge.source_id,
        paths=[path],
        truncation=QueryTruncation(reasons=[QueryTruncationReason.WORK_LIMIT]),
    )
    restored = QueryPathsResult.model_validate_json(result.model_dump_json())

    assert restored == result
    assert restored.paths[0].steps[0].edge.edge == edge
    assert restored.paths[0].limitations == [limitation]
    assert restored.limitations == []
    assert restored.truncation.reasons == [QueryTruncationReason.WORK_LIMIT]
