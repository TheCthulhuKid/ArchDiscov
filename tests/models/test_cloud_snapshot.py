import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from archdiscov.models.architecture import ArchitectureSnapshot
from archdiscov.models.cloud.snapshot import CloudSnapshot
from archdiscov.services.snapshot_access import SnapshotAccess, upgrade_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"


@pytest.mark.parametrize(
    "path",
    [
        p
        for p in sorted(FIXTURES.glob("*.json"))
        if json.loads(p.read_text())["schema_version"] in ("1", "2")
    ],
    ids=lambda p: p.stem,
)
def test_explicit_upgrade_preserves_legacy_contract(path):
    legacy = ArchitectureSnapshot.model_validate_json(path.read_text())
    before = legacy.model_dump(mode="json")
    modern = upgrade_snapshot(legacy)
    assert modern.snapshot_id == legacy.snapshot_id
    assert modern.schema_version == "3"
    assert modern.regions == legacy.regions or {
        str(k): v.model_dump(mode="json") for k, v in modern.regions.items()
    } == {str(k): v.model_dump(mode="json") for k, v in legacy.regions.items()}
    assert [r.model_dump(mode="json") for r in modern.scope_resources] == before["global_resources"]
    assert modern.source.provenance == legacy.source
    for key, value in modern.evidence.items():
        assert (
            value.model_dump(mode="json", exclude={"scope", "location", "service"})
            == before["evidence"][key]
        )
    for old, new in zip(legacy.coverage.operations, modern.coverage.operations, strict=True):
        assert old.model_dump(mode="json", exclude={"account_id", "region"}) == new.model_dump(
            mode="json", exclude={"scope", "location"}
        )
    assert legacy.model_dump(mode="json") == before
    assert CloudSnapshot.model_validate_json(modern.model_dump_json()) == modern
    modern.metadata["detached"] = True
    assert "detached" not in legacy.metadata


def modern_data():
    return upgrade_snapshot(
        ArchitectureSnapshot.model_validate_json((FIXTURES / "hierarchy.json").read_text())
    ).model_dump(mode="json")


@pytest.mark.parametrize(
    "defect,diagnostic",
    [
        ("scope", "account must match"),
        ("platform", "source platform"),
        ("placement", "location must match"),
        ("duplicate", "duplicate resource"),
        ("pointer", "fact pointer"),
        ("citation", "unknown evidence citation"),
        ("owner", "belongs to another resource"),
        ("coverage", "coverage scope"),
        ("window", "must not precede"),
        ("lifecycle", "lifecycle requires"),
        ("endpoint", "unknown target_id"),
    ],
)
def test_schema3_integrity_rejects_isolated_defects(defect, diagnostic):
    data = modern_data()
    root = data["regions"]["eu-west-1"]["resources"][0]
    if defect == "scope":
        data["scope"]["account_id"] = "999999999999"
        data["coverage"]["scope"]["account_id"] = "999999999999"
    elif defect == "platform":
        data["source"]["platform"] = "azure"
    elif defect == "placement":
        data["regions"]["eu-west-2"] = data["regions"].pop("eu-west-1")
        data["coverage"]["locations"].append("eu-west-2")
    elif defect == "duplicate":
        data["regions"]["eu-west-1"]["resources"].append(deepcopy(root))
    elif defect == "pointer":
        root["fact_evidence"]["/missing"] = [next(iter(data["evidence"]))]
    elif defect == "citation":
        root["evidence_ids"] = ["missing"]
    elif defect == "owner":
        evidence = next(iter(data["evidence"].values()))
        evidence["resource_id"] = data["scope_resources"][0]["id"]
    elif defect == "coverage":
        data["coverage"]["scope"]["account_id"] = "999999999999"
    elif defect == "window":
        data["collection_completed_at"] = "2020-01-01T00:00:00Z"
    elif defect == "lifecycle":
        root["lifecycle"] = {"actions": ["create"]}
    elif defect == "endpoint":
        data["dependencies"][0]["target"]["resource_id"] = "missing"
    with pytest.raises(ValidationError, match=diagnostic):
        CloudSnapshot.model_validate(data)


def test_access_is_detached_and_returns_containment_not_attachments():
    snapshot = CloudSnapshot.model_validate(modern_data())
    access = SnapshotAccess.from_snapshot(snapshot)
    original = [r.id for r in access.resources()]
    snapshot.regions.clear()
    assert [r.id for r in access.resources()] == original
    first = next(access.resources())
    first.children.clear()
    assert list(access.containment())
    coverage = access.coverage
    coverage.locations.clear()
    assert access.coverage.locations


def test_cross_platform_external_target_retains_own_scope():
    data = modern_data()
    data["dependencies"][0]["target"] = {
        "kind": "external",
        "scope": {"platform": "azure", "subscription_id": "11111111-1111-1111-1111-111111111111"},
        "identifier": "external Azure resource",
        "location": "eastus",
        "service": "Microsoft.Compute",
        "native_type": "Microsoft.Compute/virtualMachines",
    }
    snapshot = CloudSnapshot.model_validate(data)
    assert snapshot.dependencies[0].target.scope.platform == "azure"
    assert (
        json.loads(snapshot.model_dump_json())["dependencies"][0]["target"]["scope"]["platform"]
        == "azure"
    )


def test_schema3_unknown_retains_native_type_without_bypassing_scope():
    data = modern_data()
    resource = data["scope_resources"][0]
    resource["resource_type"] = "cloud_unknown"
    resource["platform"] = "aws"
    resource["native_type"] = resource.pop("aws_resource_type")
    snapshot = CloudSnapshot.model_validate(data)
    assert snapshot.scope_resources[0].native_type == "AWS::IAM::Role"
    resource["platform"] = "azure"
    with pytest.raises(ValidationError, match="platform must match identity"):
        CloudSnapshot.model_validate(data)


def test_upgrade_preserves_dependency_variants_and_unknown_external_scope():
    old = ArchitectureSnapshot.model_validate_json(
        (FIXTURES / "dependency-targets.json").read_text()
    )
    modern = upgrade_snapshot(old)
    assert [d.target.kind for d in modern.dependencies] == ["local", "external", "unresolved"]
    assert (
        modern.dependencies[0].target.resource_id
        == old.dependencies_by_region["eu-west-1"][0].target.resource_id
    )
    assert modern.dependencies[1].target.scope.account_id == "210987654321"
    assert modern.dependencies[2].target == old.dependencies_by_region["eu-west-1"][2].target
    old.dependencies_by_region["eu-west-1"][1].target.account_id = None
    old.dependencies_by_region["eu-west-1"][1].target.identifier = "service principal"
    assert upgrade_snapshot(old).dependencies[1].target.scope.account_id is None
