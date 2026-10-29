import json

import pytest
from pydantic import ValidationError

from archdiscov.models.architecture import ArchitectureSnapshot
from tests.paths import FIXTURES_ROOT

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
        (
            lambda d: d.update(
                target={"kind": "unresolved", "reason": "r", "source_path": "A.Password"}
            ),
            "sensitive",
        ),
    ],
    ids=["both-target-forms", "unknown-target", "sensitive-source-path"],
)
def test_schema_2_rejects_invalid_targets(mutation, message: str) -> None:
    data = _v2()
    dependency = data["dependencies_by_region"]["eu-west-1"][0]
    mutation(dependency)
    with pytest.raises(ValidationError, match=message):
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


def test_plan_lifecycle_keeps_supported_action_order() -> None:
    actions = ["delete", "create"]
    snapshot = ArchitectureSnapshot.model_validate(_plan_snapshot(actions))
    reloaded = ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json())
    assert (
        _first_resource(json.loads(reloaded.model_dump_json()))["lifecycle"]["actions"] == actions
    )
    with pytest.raises(ValidationError, match="unsupported lifecycle action sequence"):
        ArchitectureSnapshot.model_validate(_plan_snapshot(["update", "delete"]))


def test_lifecycle_requires_schema_2_and_a_plan_source() -> None:
    with pytest.raises(ValidationError, match="lifecycle requires schema 2"):
        ArchitectureSnapshot.model_validate(_plan_snapshot(["create"], version="1"))
    data = _v2()
    _first_resource(data)["lifecycle"] = {"actions": ["create"]}
    with pytest.raises(ValidationError, match="lifecycle requires schema 2"):
        ArchitectureSnapshot.model_validate(data)
