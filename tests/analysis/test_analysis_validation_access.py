import json

import pytest

from archdiscov.services.analysis_integrity import _has_citation_subset, _IntegrityContext
from archdiscov.services.analysis_validation import validate_analysis_result
from archdiscov.services.snapshot_access import SnapshotAccess
from archdiscov.services.snapshots import load_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT


def test_integrity_uses_owned_snapshot_without_public_copy_accessors(monkeypatch):
    snapshot = load_snapshot(FIXTURES / "snapshots" / "dependency-targets.json")
    data = json.loads((FIXTURES / "analysis" / "partial-path.json").read_text())

    def copied_access(*args):
        pytest.fail("Internal integrity validation must not request detached public copies")

    for name in ("resources", "dependencies"):
        monkeypatch.setattr(SnapshotAccess, name, copied_access)
    for name in ("scope", "evidence", "coverage"):
        monkeypatch.setattr(SnapshotAccess, name, property(copied_access))
    validate_analysis_result(data, snapshot)


def test_integrity_does_not_mutate_input_snapshot():
    snapshot = load_snapshot(FIXTURES / "snapshots" / "dependency-targets.json")
    data = json.loads((FIXTURES / "analysis" / "partial-path.json").read_text())
    before = snapshot.model_dump_json()
    result = validate_analysis_result(data, snapshot)
    result.findings[0].path[0].target.reason = "Changed detached result"
    assert snapshot.model_dump_json() == before


def test_dependency_index_keeps_sources_and_edge_citations_separate():
    snapshot = load_snapshot(FIXTURES / "snapshots" / "dependency-targets.json")
    access = SnapshotAccess.from_snapshot(snapshot)
    owned = access._borrow_snapshot()
    edge = owned.dependencies[0]
    owned.evidence["other"] = owned.evidence[edge.evidence_ids[0]].model_copy(deep=True)
    second = edge.model_copy(deep=True)
    second.evidence_ids = ["other"]
    owned.dependencies.append(second)
    context = _IntegrityContext.from_access(access)
    for source, candidates in context.dependencies_by_source.items():
        assert all(item.edge.source_id == source for item in candidates)
        assert all(item.citations == frozenset(item.edge.evidence_ids) for item in candidates)
    candidates = [
        context.dependencies_by_source[edge.source_id][0],
        context.dependencies_by_source[edge.source_id][-1],
    ]
    assert _has_citation_subset(edge.evidence_ids, candidates)
    assert _has_citation_subset(["other"], candidates)
    assert not _has_citation_subset([*edge.evidence_ids, "other"], candidates)
