import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from archdiscov.models.architecture import ArchitectureSnapshot, Resource, ResourceIdentity, Vpc
from archdiscov.models.resources.cloud_unknown import CloudUnknownResource
from archdiscov.models.snapshot_integrity import (
    index_resources,
    validate_collection_window,
    validate_evidence,
    validate_resource_facts,
    validate_unresolved_resources,
)
from tests.paths import FIXTURES_ROOT


def resource(name: str) -> Vpc:
    return Vpc(
        identity=ResourceIdentity(
            account_id="123456789012", region="eu-west-1", service="ec2", physical_id=name
        )
    )


def test_indexing_calls_placement_recursively_and_shares_indices() -> None:
    root, child, other = resource("root"), resource("child"), resource("other")
    root.children = [child]
    resources: dict[str, Resource] = {}
    locations: dict[str, str] = {}
    visits = []

    def placement(value: Resource, location: str) -> None:
        visits.append((value.id, location))

    index_resources([root], "scopes[0].resources", resources, locations, placement)
    index_resources([other], "scopes[1].resources", resources, locations, placement)
    assert list(resources.values()) == [root, child, other]
    assert visits == list(locations.items())
    assert locations[child.id] == "scopes[0].resources[0].children[0]"
    with pytest.raises(ValueError) as caught:
        index_resources([child], "scopes[2].resources", resources, locations, placement)
    assert str(caught.value) == (
        f"duplicate resource {child.id!r} at scopes[0].resources[0].children[0] "
        "and scopes[2].resources[0]"
    )


def test_evidence_and_facts_accept_common_records() -> None:
    root = resource("root")
    root.attributes = {"a/b": {"~key": [None]}}
    root.evidence_ids = ["e"]
    root.fact_evidence = {"/attributes/a~1b/~0key/0": ["e"]}
    snapshot_id = uuid4()
    evidence = {"e": SimpleNamespace(snapshot_id=snapshot_id, resource_id=root.id)}
    resources = {root.id: root}
    validate_evidence(snapshot_id, resources, evidence)
    validate_resource_facts(resources, evidence)
    evidence["e"].snapshot_id = uuid4()
    with pytest.raises(ValueError, match="snapshot_id must match snapshot"):
        validate_evidence(snapshot_id, resources, evidence)
    evidence["e"].snapshot_id = snapshot_id
    evidence["e"].resource_id = "absent"
    with pytest.raises(ValueError, match="unknown resource owner 'absent'"):
        validate_evidence(snapshot_id, resources, evidence)
    with pytest.raises(ValueError, match="belongs to another resource"):
        validate_resource_facts(resources, evidence)


def test_unresolved_resources_accept_common_operation_records() -> None:
    root = resource("root")
    operation = SimpleNamespace(
        unresolved_values=[SimpleNamespace(resource_id=None), SimpleNamespace(resource_id=root.id)]
    )
    validate_unresolved_resources([operation], {root.id: root})
    operation.unresolved_values.append(SimpleNamespace(resource_id="absent"))
    with pytest.raises(ValueError) as caught:
        validate_unresolved_resources([SimpleNamespace(unresolved_values=[]), operation], {})
    assert str(caught.value) == (f"coverage.operations[1]: unknown unresolved resource {root.id!r}")


def test_collection_window_contains_operation_records() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(seconds=2)
    validate_collection_window(start, end, [SimpleNamespace(started_at=start, completed_at=end)])
    late = SimpleNamespace(started_at=start, completed_at=end + timedelta(seconds=1))
    with pytest.raises(ValueError, match="timestamps must fall within snapshot collection"):
        validate_collection_window(start, end, [late])
    with pytest.raises(ValueError, match="must not precede collection_started_at"):
        validate_collection_window(end, start, [])


def test_legacy_rejects_cloud_resource_models() -> None:
    path = FIXTURES_ROOT / "snapshots" / "aws.json"
    snapshot = ArchitectureSnapshot.model_validate(json.loads(path.read_text()))
    cloud = CloudUnknownResource(
        identity=resource("unknown").identity, platform="aws", native_type="AWS::Other::Thing"
    )
    message = "legacy snapshots require AWS resource models"
    snapshot.regions["eu-west-1"].resources = [cloud]
    with pytest.raises(ValueError, match=message):
        snapshot.validate_snapshot_integrity()
    with pytest.raises(ValueError, match=message):
        ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json())
