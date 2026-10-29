import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from archdiscov.models.architecture import ArchitectureSnapshot
from tests.paths import FIXTURES_ROOT

FIXTURE = FIXTURES_ROOT / "snapshots" / "hierarchy.json"


@pytest.fixture
def data() -> dict:
    value = json.loads(FIXTURE.read_text())
    ArchitectureSnapshot.model_validate(value)
    return value


def tree(data: dict) -> list[dict]:
    vpc = data["regions"]["eu-west-1"]["resources"][0]
    subnet = vpc["children"][0]
    return [vpc, subnet, subnet["children"][0]]


def _unresolved_unknown_resource(data: dict) -> None:
    data["coverage"]["operations"] = [
        {
            "account_id": data["account_id"],
            "region": "eu-west-1",
            "service": "ec2",
            "operation": "DescribeInstances",
            "status": "incomplete",
            "result_count": 1,
            "started_at": data["collection_started_at"],
            "completed_at": data["collection_completed_at"],
            "unresolved_values": [
                {"resource_id": "missing", "configuration_path": "VpcId", "reason": "Unknown"}
            ],
        }
    ]


def _dependency(data: dict) -> dict:
    return data["dependencies_by_region"]["eu-west-1"][0]


# One representative defect per integrity rule family.
DEFECTS = {
    "duplicate": (
        lambda data: data["regions"]["eu-west-1"]["resources"].append(deepcopy(tree(data)[2])),
        "duplicate resource",
    ),
    "placement": (
        lambda data: tree(data)[2]["identity"].update(region="us-east-1"),
        "region must match enclosing region",
    ),
    "evidence-owner": (
        lambda data: data["evidence"]["role"].update(resource_id="missing"),
        "unknown resource owner",
    ),
    "fact-citation": (
        lambda data: tree(data)[2]["fact_evidence"].update({"/instance_type": ["role"]}),
        "belongs to another resource",
    ),
    "fact-target": (
        lambda data: tree(data)[2]["fact_evidence"].update({"/attributes/missing": ["instance"]}),
        "missing key",
    ),
    "unresolved-resource": (_unresolved_unknown_resource, "unknown unresolved resource"),
    "dependency-endpoint": (
        lambda data: _dependency(data).update(target_id="missing"),
        "unknown target_id",
    ),
    "dependency-group": (
        lambda data: data["dependencies_by_region"].update(
            {"global": data["dependencies_by_region"].pop("eu-west-1")}
        ),
        "grouping must match source resource region",
    ),
    "dependency-citation": (
        lambda data: _dependency(data).update(evidence_ids=["role"]),
        "belongs to another resource",
    ),
}


@pytest.mark.parametrize("defect", DEFECTS)
def test_integrity_rejects_isolated_defects(data: dict, defect: str) -> None:
    corrupt, diagnostic = DEFECTS[defect]
    corrupt(data)
    with pytest.raises(ValidationError, match=diagnostic):
        ArchitectureSnapshot.model_validate(data)


def test_escaped_pointer_targets_and_fact_only_citations(data: dict) -> None:
    instance = tree(data)[2]
    instance["attributes"] = {"a/b~c": [None, {"": False}]}
    instance["evidence_ids"] = []
    instance["fact_evidence"] = {"/attributes/a~1b~0c/0": ["instance"]}
    snapshot = ArchitectureSnapshot.model_validate(data)
    assert ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot


def test_cross_region_global_and_forward_references(data: dict) -> None:
    remote = deepcopy(tree(data)[0])
    remote["identity"]["region"] = "us-east-1"
    remote["children"] = []
    data["coverage"]["regions"].append("us-east-1")
    data["regions"]["us-east-1"] = {"resources": [remote]}
    instance_id = data["evidence"]["instance"]["resource_id"]
    role_id = data["evidence"]["role"]["resource_id"]
    data["dependencies_by_region"]["eu-west-1"].append(
        {
            "source_id": instance_id,
            "target_id": "123456789012/us-east-1/ec2/physical/vpc-main",
            "relationship": "attached_to",
            "evidence_ids": ["instance"],
        }
    )
    data["dependencies_by_region"]["global"] = [
        {
            "source_id": role_id,
            "target_id": instance_id,
            "relationship": "attached_to",
            "evidence_ids": ["role"],
        }
    ]
    # A root can cite a descendant before traversal reaches that descendant.
    vpc_id = "123456789012/eu-west-1/ec2/physical/vpc-main"
    data["evidence"]["vpc"] = {**data["evidence"]["instance"], "resource_id": vpc_id}
    data["dependencies_by_region"]["eu-west-1"].append(
        {
            "source_id": vpc_id,
            "target_id": instance_id,
            "relationship": "attached_to",
            "evidence_ids": ["vpc"],
        }
    )
    snapshot = ArchitectureSnapshot.model_validate(data)
    assert len(snapshot.dependencies_by_region["eu-west-1"]) == 3
    assert len(snapshot.dependencies_by_region["global"]) == 1
    assert ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
