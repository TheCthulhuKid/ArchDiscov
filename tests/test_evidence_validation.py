import json
from pathlib import Path
from unittest.mock import patch

import pytest

from archdiscov.models.architecture import ArchitectureSnapshot
from archdiscov.models.cloud_snapshot import CloudSnapshot
from archdiscov.models.resource_base import Resource
from archdiscov.services import snapshot_access
from archdiscov.services.evidence import get_evidence
from archdiscov.services.snapshots import SnapshotValidationError, validate_snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "snapshots"


@pytest.mark.parametrize(
    "fixture,model", [("hierarchy", ArchitectureSnapshot), ("azure", CloudSnapshot)]
)
def test_evidence_validates_once_without_conversion_or_tree_copies(fixture, model):
    data = json.loads((FIXTURES / f"{fixture}.json").read_text())
    evidence_id = next(iter(data["evidence"]))
    with (
        patch.object(model, "model_validate", wraps=model.model_validate) as validation,
        patch.object(snapshot_access, "upgrade_snapshot", side_effect=AssertionError("upgrade")),
        patch.object(Resource, "model_copy", side_effect=AssertionError("tree copy")),
    ):
        result = get_evidence(data, evidence_id)
    assert validation.call_count == 1
    assert result.resource.id == data["evidence"][evidence_id]["resource_id"]
    result.resource.attributes["changed"] = True
    assert "changed" not in json.dumps(data)


@pytest.mark.parametrize("fixture", ["hierarchy", "azure"])
def test_evidence_revalidates_mutable_snapshot_and_returns_detached_resources(fixture):
    snapshot = validate_snapshot(json.loads((FIXTURES / f"{fixture}.json").read_text()))
    evidence_id = next(iter(snapshot.evidence))
    original = snapshot.model_dump(mode="json")
    result = get_evidence(snapshot, evidence_id)
    result.resource.attributes["changed"] = True
    assert snapshot.model_dump(mode="json") == original
    snapshot.evidence[evidence_id].resource_id = "missing"
    with pytest.raises(SnapshotValidationError) as error:
        get_evidence(snapshot, evidence_id)
    assert any("unknown resource owner" in item.message for item in error.value.details)


def test_containment_reads_only_ids_without_copying_resource_trees():
    snapshot = validate_snapshot(json.loads((FIXTURES / "azure.json").read_text()))
    access = snapshot_access.SnapshotAccess.from_snapshot(snapshot)
    expected = [
        (resource.id, child.id) for resource in access.resources() for child in resource.children
    ]
    assert len(expected) == 2
    with patch.object(Resource, "model_copy", side_effect=AssertionError("tree copy")):
        assert list(access.containment()) == expected


def test_azure_evidence_and_coverage_match_case_insensitive_namespace():
    data = json.loads((FIXTURES / "azure.json").read_text())
    evidence_id = next(iter(data["evidence"]))
    record = data["evidence"][evidence_id]
    record["service"] = record["service"].upper()
    for operation in data["coverage"]["operations"]:
        operation["service"] = operation["service"].lower()
    result = get_evidence(data, evidence_id)
    assert result.evidence.service == record["service"]
    expected = [
        item["operation"]
        for item in data["coverage"]["operations"]
        if item["location"] == record["location"]
        and item["service"].casefold() == record["service"].casefold()
    ]
    assert expected
    assert [item.operation for item in result.coverage] == expected
