import pytest
from pydantic import TypeAdapter, ValidationError

from archdiscov.graph import GraphIndex
from archdiscov.models.enums import QueryDirection
from archdiscov.models.query.edges import (
    QueryContainmentEdge,
    QueryDependencyEdge,
    QueryEdge,
    QueryTraversalStep,
)
from archdiscov.services.snapshots import load_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"


@pytest.mark.parametrize("target_kind", ["local", "external", "unresolved"])
def test_dependency_edge_round_trip(target_kind):
    index = GraphIndex(load_snapshot(FIXTURES / "dependency-targets.json"))
    edge = next(edge for edge in index.dependency_edges if edge.target.kind == target_kind)
    result = QueryDependencyEdge(edge=edge)

    restored = QueryDependencyEdge.model_validate_json(result.model_dump_json())

    assert restored.kind == "dependency"
    assert restored.edge == edge
    assert restored.edge.id == edge.id
    assert restored.edge.target_id == edge.target_id
    assert restored.edge.evidence_ids == edge.evidence_ids


@pytest.mark.parametrize("fixture", ["hierarchy", "azure"])
def test_containment_edge_round_trip(fixture):
    index = GraphIndex(load_snapshot(FIXTURES / f"{fixture}.json"))
    edge = index.containment_edges[0]
    result = QueryContainmentEdge(edge=edge)

    restored = QueryContainmentEdge.model_validate_json(result.model_dump_json())

    assert restored.kind == "containment"
    assert restored.edge == edge
    assert restored.edge.source_id == edge.source_id
    assert restored.edge.target_id == edge.target_id
    assert restored.edge.evidence_ids == ()


@pytest.mark.parametrize(
    "kind,expected_class",
    [
        ("dependency", QueryDependencyEdge),
        ("containment", QueryContainmentEdge),
    ],
)
def test_query_edge_union_selects_variant(kind, expected_class):
    index = GraphIndex(load_snapshot(FIXTURES / "hierarchy.json"))
    edge = index.dependency_edges[0] if kind == "dependency" else index.containment_edges[0]
    adapter = TypeAdapter(QueryEdge)

    result = adapter.validate_python({"kind": kind, "edge": edge})
    restored = adapter.validate_json(adapter.dump_json(result))

    assert type(result) is expected_class
    assert result.edge == edge
    assert type(restored) is expected_class
    assert restored == result


@pytest.mark.parametrize(
    "data,error_type",
    [
        ({}, "union_tag_not_found"),
        ({"kind": "invalid"}, "union_tag_invalid"),
    ],
)
def test_query_edge_union_rejects_invalid_discriminator(data, error_type):
    with pytest.raises(ValidationError) as caught:
        TypeAdapter(QueryEdge).validate_python(data)
    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ()
    assert errors[0]["type"] == error_type


@pytest.mark.parametrize("kind", ["dependency", "containment"])
@pytest.mark.parametrize(
    "direction",
    [QueryDirection.OUTGOING, QueryDirection.INCOMING],
)
def test_traversal_step_preserves_edge_and_direction(kind, direction):
    index = GraphIndex(load_snapshot(FIXTURES / "hierarchy.json"))
    edge = index.dependency_edges[0] if kind == "dependency" else index.containment_edges[0]
    step = QueryTraversalStep.model_validate(
        {"edge": {"kind": kind, "edge": edge}, "direction": direction}
    )

    restored = QueryTraversalStep.model_validate_json(step.model_dump_json())

    assert restored == step
    assert restored.direction == direction
    assert restored.edge.edge == edge
    assert restored.edge.edge.source_id == edge.source_id
    assert restored.edge.edge.target_id == edge.target_id
    assert restored.edge.edge.evidence_ids == edge.evidence_ids


def test_traversal_step_rejects_both_direction():
    index = GraphIndex(load_snapshot(FIXTURES / "hierarchy.json"))
    with pytest.raises(ValidationError) as caught:
        QueryTraversalStep.model_validate(
            {
                "edge": {
                    "kind": "dependency",
                    "edge": index.dependency_edges[0],
                },
                "direction": "both",
            }
        )

    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("direction",)
    assert errors[0]["type"] == "literal_error"
