from copy import deepcopy

import pytest

from archdiscov.graph import GraphIndex
from archdiscov.graph.dependencies import DependencyEdge
from archdiscov.models.enums import (
    AttachmentKind,
    DependencyRelationship,
    QueryDirection,
    QueryErrorCode,
    QueryLimitationKind,
    QueryTruncationReason,
)
from archdiscov.models.query.results import QueryNeighboursResult
from archdiscov.services import QueryError, load_snapshot, query_neighbours
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"
WEB = "123456789012/eu-west-1/ec2/physical/i-web"
ROLE = "123456789012/global/iam/physical/role-app"
SG_WEB = "123456789012/eu-west-1/ec2/physical/sg-web"
SG_DB = "123456789012/eu-west-1/ec2/physical/sg-db"
RTB = "123456789012/eu-west-1/ec2/physical/rtb-main"
IGW = "123456789012/eu-west-1/ec2/physical/igw-main"


def targets():
    return GraphIndex(load_snapshot(FIXTURES / "dependency-targets.json"))


def network():
    return GraphIndex(load_snapshot(FIXTURES / "network.json"))


def test_outgoing_terminals_keep_limitations_and_never_become_resources():
    index = targets()
    result = query_neighbours(index, {"resource_id": WEB})
    assert len(result.steps) == 3
    assert {step.direction for step in result.steps} == {QueryDirection.OUTGOING}
    kinds = {item.kind for item in result.limitations}
    assert (
        QueryLimitationKind.EXTERNAL_TARGET in kinds
        or QueryLimitationKind.UNRESOLVED_TARGET in kinds
    )
    terminals = [
        step.edge.edge for step in result.steps if step.edge.edge.target_id.startswith("terminal/")
    ]
    assert len(terminals) == 2
    assert all(index.get_resource(edge.target_id) is None for edge in terminals)
    assert QueryNeighboursResult.model_validate_json(result.model_dump_json()) == result


def test_incoming_reverse_lookup_keeps_stored_edge_and_source_evidence():
    index = targets()
    stored = next(edge for edge in index.dependency_edges if edge.target_id == ROLE)
    result = query_neighbours(index, {"resource_id": ROLE, "options": {"direction": "incoming"}})
    [step] = result.steps
    assert step.direction == QueryDirection.INCOMING
    assert step.edge.edge == stored
    assert step.edge.edge.source_id == WEB
    assert step.edge.edge.evidence_ids == stored.evidence_ids
    assert query_neighbours(index, {"resource_id": ROLE}).steps == []


def test_relationship_and_attachment_filters():
    index = network()
    opts = {"attachment_kinds": ["route_target"]}
    result = query_neighbours(index, {"resource_id": RTB, "options": opts})
    assert len(result.steps) == 2
    assert all(s.edge.edge.attachment_kind == AttachmentKind.ROUTE_TARGET for s in result.steps)
    opts = {"relationships": ["assumes_role"]}
    assert query_neighbours(index, {"resource_id": RTB, "options": opts}).steps == []
    opts = {"relationships": ["attached_to"], "attachment_kinds": ["security_group_reference"]}
    [step] = query_neighbours(index, {"resource_id": SG_WEB, "options": opts}).steps
    assert step.edge.edge.target_id == SG_DB
    assert step.edge.edge.relationship == DependencyRelationship.ATTACHED_TO
    opts = {"direction": "incoming", "attachment_kinds": ["route_target"]}
    [step] = query_neighbours(index, {"resource_id": IGW, "options": opts}).steps
    assert step.edge.edge.source_id == RTB


def test_containment_is_explicit_uncited_and_not_an_attachment():
    index = GraphIndex(load_snapshot(FIXTURES / "hierarchy.json"))
    parent_id = next(iter(index.containment_edges)).source_id
    child_id = index.child_ids(parent_id)[0]
    assert query_neighbours(index, {"resource_id": parent_id}).steps == []
    both = {"edge_selection": "containment", "direction": "both"}
    result = query_neighbours(index, {"resource_id": child_id, "options": both})
    containment = [s for s in result.steps if s.edge.kind == "containment"]
    assert all(s.edge.edge.evidence_ids == () for s in containment)
    assert [(s.direction, s.edge.edge.source_id) for s in containment] == [
        (QueryDirection.INCOMING, index.parent_id(child_id)),
        *[(QueryDirection.OUTGOING, child_id)] * len(index.child_ids(child_id)),
    ]
    down = query_neighbours(
        index, {"resource_id": parent_id, "options": {"edge_selection": "containment"}}
    )
    assert [s.edge.edge.target_id for s in down.steps] == list(index.child_ids(parent_id))
    assert all(s.direction == QueryDirection.OUTGOING for s in down.steps)
    up = query_neighbours(
        index,
        {
            "resource_id": child_id,
            "options": {"edge_selection": "containment", "direction": "incoming"},
        },
    )
    [step] = up.steps
    assert (step.edge.edge.source_id, step.edge.edge.target_id) == (parent_id, child_id)
    assert not any(isinstance(s.edge.edge, DependencyEdge) for s in down.steps + up.steps)


def test_parallel_edges_ordering_limit_and_unchanged_index():
    source = load_snapshot(FIXTURES / "dependency-targets.json")
    for edges in source.dependencies_by_region.values():
        edges.extend(deepcopy(edges))
    index = GraphIndex(source)
    before = index.dependency_edges
    result = query_neighbours(index, {"resource_id": WEB})
    assert len(result.steps) == 6
    assert len({s.edge.edge.id for s in result.steps}) == 6
    keys = [(s.edge.edge.target_id, s.edge.edge.id) for s in result.steps]
    assert keys == sorted(keys)
    limited = query_neighbours(index, {"resource_id": WEB, "limit_result": 4})
    assert limited.steps == result.steps[:4]
    assert limited.truncation.reasons == [QueryTruncationReason.RESULT_LIMIT]
    assert query_neighbours(index, {"resource_id": WEB, "limit_result": 6}).truncation.reasons == []
    result.steps[0].edge.edge.attributes["changed"] = True
    assert index.dependency_edges == before
    assert (
        "changed" not in query_neighbours(index, {"resource_id": WEB}).steps[0].edge.edge.attributes
    )


def test_invalid_and_unknown_requests_are_safe_errors():
    index = targets()
    with pytest.raises(QueryError) as unknown:
        query_neighbours(index, {"resource_id": "absent"})
    assert unknown.value.code == QueryErrorCode.RESOURCE_NOT_FOUND
    for bad in (
        {},
        {"resource_id": WEB, "limit_result": 0},
        {
            "resource_id": WEB,
            "options": {"edge_selection": "containment", "relationships": ["attached_to"]},
        },
    ):
        with pytest.raises(QueryError) as error:
            query_neighbours(index, bad)
        assert error.value.code == QueryErrorCode.INVALID_REQUEST
