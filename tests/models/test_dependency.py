import json

import pytest
from pydantic import ValidationError

from archdiscov.models.architecture import ArchitectureSnapshot, Dependency
from archdiscov.models.enums import DependencyRelationship
from tests.paths import FIXTURES_ROOT


@pytest.mark.parametrize("relationship", ["attached_to", "forwards_to", "assumes_role"])
def test_dependency_relationship_round_trip(relationship: str) -> None:
    dependency = Dependency(
        source_id="source", target_id="target", relationship=relationship, evidence_ids=["e1"]
    )
    assert dependency.relationship is DependencyRelationship(relationship)
    assert dependency.model_dump(mode="json")["relationship"] == relationship
    assert Dependency.model_validate_json(dependency.model_dump_json()) == dependency


@pytest.mark.parametrize("relationship", list(DependencyRelationship))
def test_dependency_accepts_relationship_enum(relationship: DependencyRelationship) -> None:
    dependency = Dependency(
        source_id="source", target_id="target", relationship=relationship, evidence_ids=["e1"]
    )
    assert dependency.relationship is relationship


@pytest.mark.parametrize("relationship", ["", "   ", "ATTACHED_TO", "not-a-relationship"])
def test_unknown_dependency_relationship_is_rejected(relationship: str) -> None:
    with pytest.raises(ValidationError) as error:
        Dependency(
            source_id="source", target_id="target", relationship=relationship, evidence_ids=["e1"]
        )
    assert error.value.errors()[0]["loc"] == ("relationship",)


def test_snapshot_validates_dependency_relationship() -> None:
    path = FIXTURES_ROOT / "snapshots" / "hierarchy.json"
    data = json.loads(path.read_text())
    data["dependencies_by_region"]["eu-west-1"][0]["relationship"] = "attached_to"
    snapshot = ArchitectureSnapshot.model_validate(data)
    assert snapshot.dependencies_by_region["eu-west-1"][0].relationship is (
        DependencyRelationship.ATTACHED_TO
    )
    assert ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
    data["dependencies_by_region"]["eu-west-1"][0]["relationship"] = "not-a-relationship"
    with pytest.raises(ValidationError) as error:
        ArchitectureSnapshot.model_validate(data)
    assert error.value.errors()[0]["loc"] == (
        "dependencies_by_region",
        "eu-west-1",
        0,
        "relationship",
    )


@pytest.mark.parametrize("field", ["source_id", "target_id", "evidence_ids"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_dependency_reference_ids_are_nonblank(field: str, blank: str) -> None:
    data = {
        "source_id": "source",
        "target_id": "target",
        "relationship": "attached_to",
        "evidence_ids": ["e1"],
    }
    data[field] = [blank] if field == "evidence_ids" else blank
    with pytest.raises(ValidationError, match="String should") as error:
        Dependency.model_validate(data)
    assert error.value.errors()[0]["loc"][0] == field


def test_dependency_requires_evidence() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        Dependency(
            source_id="source", target_id="target", relationship="attached_to", evidence_ids=[]
        )


V2_FIXTURE = FIXTURES_ROOT / "snapshots" / "dependency-targets.json"
V1_FIXTURE = FIXTURES_ROOT / "snapshots" / "hierarchy.json"


def _v2() -> dict:
    return json.loads(V2_FIXTURE.read_text())


def test_schema_2_targets_round_trip_with_distinct_kinds() -> None:
    snapshot = ArchitectureSnapshot.model_validate(_v2())
    targets = snapshot.dependencies_by_region["eu-west-1"]
    assert [item.target.kind for item in targets] == ["local", "external", "unresolved"]
    assert targets[1].target.account_id == "210987654321"
    assert targets[2].target.source_path == "Instance.SecurityGroupIds[0]"
    dumped = json.loads(snapshot.model_dump_json())
    assert all("target_id" not in item for item in dumped["dependencies_by_region"]["eu-west-1"])
    assert ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot


def test_schema_1_remains_strict_and_keeps_its_shape() -> None:
    snapshot = ArchitectureSnapshot.model_validate_json(V1_FIXTURE.read_text())
    dumped = json.loads(snapshot.model_dump_json())
    dependency = dumped["dependencies_by_region"]["eu-west-1"][0]
    assert "target_id" in dependency and "target" not in dependency
    assert all("lifecycle" not in r for r in dumped["global_resources"])
    data = json.loads(V1_FIXTURE.read_text())
    data["dependencies_by_region"]["eu-west-1"][0] = {
        **{k: v for k, v in dependency.items() if k != "target_id"},
        "target": {"kind": "unresolved", "reason": "x", "source_path": "y"},
    }
    with pytest.raises(ValidationError, match="schema 1 dependencies require target_id"):
        ArchitectureSnapshot.model_validate(data)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda d: d.update(target_id="x"), "exactly one of target_id or target"),
        (
            lambda d: d.update(target={"kind": "local", "resource_id": "missing"}),
            "unknown target_id",
        ),
        (lambda d: d.update(source_id="missing"), "unknown source_id"),
        (lambda d: d.update(evidence_ids=["role"]), "belongs to another resource"),
        (lambda d: d.update(evidence_ids=["nope"]), "unknown evidence citation"),
        (lambda d: d.update(target={"kind": "bogus"}), "does not match any of the expected tags"),
        (
            lambda d: d.update(
                target={"kind": "unresolved", "reason": "r", "source_path": "A.Password"}
            ),
            "sensitive",
        ),
    ],
)
def test_schema_2_rejects_invalid_targets(mutation, message: str) -> None:
    data = _v2()
    dependency = data["dependencies_by_region"]["eu-west-1"][0]
    mutation(dependency)
    with pytest.raises(ValidationError, match=message):
        ArchitectureSnapshot.model_validate(data)


def test_schema_2_unresolved_dependency_keeps_source_region_grouping() -> None:
    data = _v2()
    deps = data["dependencies_by_region"]
    deps["global"] = [deps["eu-west-1"].pop(2)]
    data["coverage"]["account_wide"] = True
    with pytest.raises(ValidationError, match="grouping must match source resource region"):
        ArchitectureSnapshot.model_validate(data)


def test_unsupported_schema_version_message_lists_supported_versions() -> None:
    data = _v2()
    data["schema_version"] = "3"
    with pytest.raises(ValidationError, match="supported versions: 1, 2"):
        ArchitectureSnapshot.model_validate(data)


def _plan_snapshot(actions, version="2") -> dict:
    data = json.loads((V1_FIXTURE.parent / "terraform-plan.json").read_text())
    data["schema_version"] = version
    resource = _first_resource(data)
    resource["lifecycle"] = {"actions": actions}
    return data


def _first_resource(data: dict) -> dict:
    if data.get("global_resources"):
        return data["global_resources"][0]
    return next(iter(data["regions"].values()))["resources"][0]


@pytest.mark.parametrize("actions", [["create"], ["delete", "create"], ["create", "delete"]])
def test_plan_lifecycle_actions_round_trip_in_order(actions: list[str]) -> None:
    snapshot = ArchitectureSnapshot.model_validate(_plan_snapshot(actions))
    reloaded = ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json())
    assert (
        _first_resource(json.loads(reloaded.model_dump_json()))["lifecycle"]["actions"] == actions
    )


@pytest.mark.parametrize("actions", [[], ["update", "delete"], ["create", "create"], ["bogus"]])
def test_plan_lifecycle_rejects_invalid_sequences(actions: list[str]) -> None:
    with pytest.raises(ValidationError):
        ArchitectureSnapshot.model_validate(_plan_snapshot(actions))


def test_lifecycle_requires_schema_2_and_a_plan_source() -> None:
    with pytest.raises(ValidationError, match="lifecycle requires schema 2"):
        ArchitectureSnapshot.model_validate(_plan_snapshot(["create"], version="1"))
    data = _v2()
    _first_resource(data)["lifecycle"] = {"actions": ["create"]}
    with pytest.raises(ValidationError, match="lifecycle requires schema 2"):
        ArchitectureSnapshot.model_validate(data)
