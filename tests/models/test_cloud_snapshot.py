from copy import deepcopy

import pytest
from pydantic import ValidationError

from archdiscov.models.architecture import ArchitectureSnapshot
from archdiscov.models.cloud.snapshot import CloudSnapshot
from archdiscov.models.snapshot_access import upgrade_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"


@pytest.mark.parametrize(
    "path", [FIXTURES / "hierarchy.json", FIXTURES / "partial-coverage.json"], ids=lambda p: p.stem
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


def _root(data: dict) -> dict:
    return data["regions"]["eu-west-1"]["resources"][0]


def _other_account(data: dict) -> None:
    data["scope"]["account_id"] = data["coverage"]["scope"]["account_id"] = "999999999999"


def _other_region(data: dict) -> None:
    data["regions"]["eu-west-2"] = data["regions"].pop("eu-west-1")
    data["coverage"]["locations"].append("eu-west-2")


DEFECTS = {
    "scope": (_other_account, "account must match"),
    "platform": (lambda data: data["source"].update(platform="azure"), "source platform"),
    "placement": (_other_region, "location must match"),
    "duplicate": (
        lambda data: data["regions"]["eu-west-1"]["resources"].append(deepcopy(_root(data))),
        "duplicate resource",
    ),
    "citation": (
        lambda data: _root(data).update(evidence_ids=["missing"]),
        "unknown evidence citation",
    ),
    "coverage": (
        lambda data: data["coverage"]["scope"].update(account_id="999999999999"),
        "coverage scope",
    ),
    "window": (
        lambda data: data.update(collection_completed_at="2020-01-01T00:00:00Z"),
        "must not precede",
    ),
    "lifecycle": (
        lambda data: _root(data).update(lifecycle={"actions": ["create"]}),
        "lifecycle requires",
    ),
    "endpoint": (
        lambda data: data["dependencies"][0]["target"].update(resource_id="missing"),
        "unknown target_id",
    ),
}


@pytest.mark.parametrize("defect", DEFECTS)
def test_schema3_integrity_rejects_isolated_defects(defect):
    data = modern_data()
    corrupt, diagnostic = DEFECTS[defect]
    corrupt(data)
    with pytest.raises(ValidationError, match=diagnostic):
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
