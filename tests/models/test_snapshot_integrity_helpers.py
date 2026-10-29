import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from archdiscov.models.architecture import (
    ArchitectureSnapshot,
    Resource,
    ResourceIdentity,
    Vpc,
    _cache_fact_representations,
    _validate_citations,
    _validate_fact_pointer,
)
from archdiscov.models.cloud.identity import AzureResourceIdentity
from archdiscov.models.resources.azure import AzureVirtualNetwork
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


def test_placement_failure_does_not_index_resource() -> None:
    resources, locations = {}, {}

    def placement(value: Resource, location: str) -> None:
        raise ValueError(f"invalid placement at {location}")

    with pytest.raises(ValueError, match=r"invalid placement at resources\[0\]"):
        index_resources([resource("root")], "resources", resources, locations, placement)
    assert resources == locations == {}


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


@pytest.mark.parametrize("offsets", [(0, 2), (-1, 1), (1, 3), (2, 1)])
def test_collection_window_accepts_common_operation_records(offsets: tuple[int, int]) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(seconds=2)
    operation = SimpleNamespace(
        started_at=start + timedelta(seconds=offsets[0]),
        completed_at=start + timedelta(seconds=offsets[1]),
    )
    if offsets == (0, 2):
        validate_collection_window(start, end, [operation])
    else:
        with pytest.raises(ValueError, match="timestamps must fall within snapshot collection"):
            validate_collection_window(start, end, [operation])
    with pytest.raises(ValueError, match="must not precede collection_started_at"):
        validate_collection_window(end, start, [])


def test_private_helper_imports_remain_compatible() -> None:
    from archdiscov.models import snapshot_integrity

    assert _cache_fact_representations is snapshot_integrity._cache_fact_representations
    assert _validate_citations is snapshot_integrity._validate_citations
    assert _validate_fact_pointer is snapshot_integrity._validate_fact_pointer


@pytest.mark.parametrize("kind", ["aws_unknown", "azure_model"])
def test_legacy_rejects_cloud_resource_models(kind: str) -> None:
    path = FIXTURES_ROOT / "snapshots" / "aws.json"
    snapshot = ArchitectureSnapshot.model_validate(json.loads(path.read_text()))
    if kind == "aws_unknown":
        cloud = CloudUnknownResource(
            identity=resource("unknown").identity, platform="aws", native_type="AWS::Other::Thing"
        )
        message = "legacy snapshots require AWS resource models"
    else:
        identity = AzureResourceIdentity(
            subscription_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            service="Microsoft.Network",
            native_type="Microsoft.Network/virtualNetworks",
            logical_address="network",
        )
        cloud = AzureVirtualNetwork(identity=identity)
        message = "legacy snapshots require AWS identities"
    snapshot.regions["eu-west-1"].resources = [cloud]
    with pytest.raises(ValueError, match=message):
        snapshot.validate_snapshot_integrity()
    with pytest.raises(ValueError, match=message):
        ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json())
