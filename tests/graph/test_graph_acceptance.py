from collections import Counter

import pytest

from archdiscov.graph import CoverageKey, GraphIndex
from archdiscov.models.cloud.contracts import CloudDependency
from archdiscov.models.cloud.snapshot import CloudSnapshot
from archdiscov.models.resource_tree import walk_resources
from archdiscov.models.snapshot_access import SnapshotAccess, upgrade_snapshot
from archdiscov.services.snapshots import load_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"
# Resources, containment, dependencies, terminals, evidence, coverage.
CONTENTS = {
    "hierarchy": (5, 3, 1, 0, 2, 0),
    "network": (8, 7, 4, 2, 5, 0),
    "iam": (3, 0, 0, 0, 0, 0),
    "load-balancing": (4, 2, 0, 0, 3, 0),
    "dependency-targets": (5, 3, 3, 2, 2, 0),
    "azure": (3, 2, 1, 0, 5, 3),
    "partial-coverage": (1, 0, 0, 0, 1, 3),
}


def graph_state(index):
    """Capture all public index families together, including reverse lookups."""
    resources = index.resources()
    return {
        "resources": resources,
        "lookups": (
            index.snapshot_id,
            index.scope,
            index.resource_ids,
            index.terminal_ids,
            index.evidence_ids,
            index.root_ids,
            index.containment_edges,
            tuple((key, index.parent_id(key), index.child_ids(key)) for key in index.resource_ids),
            tuple((key, index.physical_key(key)) for key in index.resource_ids),
            index.dependency_edges,
            tuple(index.get_terminal(key) for key in index.terminal_ids),
            tuple(index.incoming_edges(key) for key in (*index.resource_ids, *index.terminal_ids)),
            tuple(index.outgoing_edges(key) for key in index.resource_ids),
            tuple(index.get_evidence(key) for key in index.evidence_ids),
            tuple(
                (resource.id, pointer, index.evidence_for_fact(resource.id, pointer))
                for resource in resources
                for pointer in sorted(resource.fact_evidence)
            ),
            index.coverage_keys,
            index.coverage_records(),
        ),
    }


@pytest.mark.parametrize("fixture", CONTENTS)
def test_composed_graph_preserves_exact_snapshot_contents_and_joins(fixture):
    snapshot = load_snapshot(FIXTURES / f"{fixture}.json")
    before = snapshot.model_dump(mode="json")
    access = SnapshotAccess.from_snapshot(snapshot)
    index = GraphIndex(snapshot)
    assert (
        len(index.resource_ids),
        len(index.containment_edges),
        len(index.dependency_edges),
        len(index.terminal_ids),
        len(index.evidence_ids),
        len(index.coverage_keys),
    ) == CONTENTS[fixture]
    resources = {resource.id: resource for resource in access.resources()}
    evidence = access.evidence
    assert index.resource_ids == tuple(sorted(resources))
    assert index.root_ids == tuple(sorted(resource.id for _, resource in access.roots()))
    assert {(edge.source_id, edge.target_id) for edge in index.containment_edges} == set(
        access.containment()
    )
    for resource_id, resource in resources.items():
        assert index.get_resource(resource_id) == resource
        assert index.child_ids(resource_id) == tuple(
            sorted(child.id for child in resource.children)
        )
        for child_id in index.child_ids(resource_id):
            assert index.parent_id(child_id) == resource_id
        physical_key = index.physical_key(resource_id)
        if physical_key is not None:
            assert resource in index.find_physical(physical_key)
        for pointer, ids in resource.fact_evidence.items():
            assert index.fact_evidence_ids(resource_id, pointer) == tuple(ids)
            assert index.evidence_for_fact(resource_id, pointer) == tuple(evidence[i] for i in ids)
        assert index.evidence_for_resource(resource_id) == tuple(
            evidence[key] for key in sorted(evidence) if evidence[key].resource_id == resource_id
        )
    stored_dependencies = Counter(item.model_dump_json() for item in access.dependencies())
    indexed_dependencies = Counter()
    for edge in index.dependency_edges:
        indexed_dependencies[
            CloudDependency(
                source_id=edge.source_id,
                target=edge.target,
                location=edge.location,
                relationship=edge.relationship,
                attributes=edge.attributes,
                evidence_ids=list(edge.evidence_ids),
            ).model_dump_json()
        ] += 1
        assert edge in index.outgoing_edges(edge.source_id)
        assert edge in index.incoming_edges(edge.target_id)
        assert all(
            index.get_evidence(key).resource_id == edge.source_id for key in edge.evidence_ids
        )
        if edge.target.kind == "local":
            assert index.get_resource(edge.target_id) == resources[edge.target_id]
        else:
            terminal = index.get_terminal(edge.target_id)
            assert terminal.edge_id == edge.id
            assert terminal.target == edge.target
            assert index.get_resource(terminal.id) is None
            assert index.outgoing_edges(terminal.id) == ()
    assert indexed_dependencies == stored_dependencies
    assert {key: index.get_evidence(key) for key in index.evidence_ids} == evidence
    assert {key: index.get_coverage(key) for key in index.coverage_keys} == {
        CoverageKey.from_record(record): record for record in access.coverage.operations
    }
    if not isinstance(snapshot, CloudSnapshot):
        assert graph_state(index) == graph_state(GraphIndex(upgrade_snapshot(snapshot)))
    assert snapshot.model_dump(mode="json") == before


@pytest.mark.parametrize("fixture", CONTENTS)
def test_composed_graph_ordering_and_isolation_survive_reordered_storage(fixture):
    snapshot = load_snapshot(FIXTURES / f"{fixture}.json")
    if not isinstance(snapshot, CloudSnapshot):
        snapshot = upgrade_snapshot(snapshot)
    original = GraphIndex(snapshot)
    expected = graph_state(original)
    snapshot.regions = dict(reversed(tuple(snapshot.regions.items())))
    for region in snapshot.regions.values():
        region.resources.reverse()
        for resource in walk_resources(region.resources):
            resource.children.reverse()
    snapshot.scope_resources.reverse()
    for resource in walk_resources(snapshot.scope_resources):
        resource.children.reverse()
    snapshot.dependencies.reverse()
    snapshot.evidence = dict(reversed(tuple(snapshot.evidence.items())))
    snapshot.coverage.operations.reverse()
    index = GraphIndex(snapshot)
    # Returned resource trees deliberately preserve stored child order.
    isolated = graph_state(index)
    assert isolated["lookups"] == expected["lookups"]
    for resource in index.resources():
        resource.attributes.clear()
        resource.fact_evidence.clear()
        resource.children.clear()
    for edge in index.dependency_edges:
        edge.attributes.clear()
        edge.target.kind = "modified"
    for record in index.coverage_records():
        record.failures.clear()
        record.unresolved_values.clear()
    for key in index.evidence_ids:
        index.get_evidence(key).value = "modified"
    snapshot.regions.clear()
    snapshot.scope_resources.clear()
    snapshot.dependencies.clear()
    snapshot.evidence.clear()
    snapshot.coverage.operations.clear()
    assert graph_state(index) == isolated
    assert graph_state(original) == expected
