import json
from copy import deepcopy
from ipaddress import IPv4Network

import pytest
from pydantic import ValidationError

from archdiscov.models.architecture import (
    ArchitectureSnapshot,
    Ec2Instance,
    Subnet,
    UnknownResource,
    Vpc,
)
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


def test_hierarchy_round_trip(data: dict) -> None:
    snapshot = ArchitectureSnapshot.model_validate(data)
    reloaded = ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json())
    assert reloaded == snapshot
    for model in (snapshot, reloaded):
        vpc = model.regions["eu-west-1"].resources[0]
        subnet, security_group = vpc.children
        instance = subnet.children[0]
        role = model.global_resources[0]
        assert isinstance(vpc, Vpc)
        assert vpc.cidr_blocks == [IPv4Network("10.0.0.0/16")]
        assert vpc.id == "123456789012/eu-west-1/ec2/physical/vpc-main"
        assert isinstance(subnet, Subnet)
        assert subnet.availability_zone == "eu-west-1a"
        assert subnet.cidr_blocks == [IPv4Network("10.0.1.0/24")]
        assert subnet.id == "123456789012/eu-west-1/ec2/physical/subnet-app"
        assert isinstance(instance, Ec2Instance)
        assert instance.instance_type == "t3.micro"
        assert instance.id == "123456789012/eu-west-1/ec2/physical/i-web"
        assert isinstance(security_group, UnknownResource)
        assert security_group.aws_resource_type == "AWS::EC2::SecurityGroup"
        assert security_group.attributes == {"description": "Application security group"}
        assert security_group.id == "123456789012/eu-west-1/ec2/physical/sg-app"
        assert security_group.children == []
        assert isinstance(role, UnknownResource)
        assert role.aws_resource_type == "AWS::IAM::Role"
        assert role.attributes == {}
        assert role.identity.region == "global"
        assert role.id == "123456789012/global/iam/physical/role-app"
        assert all(
            resource.name is None for resource in (vpc, subnet, instance, security_group, role)
        )
        assert set(model.dependencies_by_region) == {"eu-west-1"}
        (dependency,) = model.dependencies_by_region["eu-west-1"]
        assert dependency.source_id == instance.id
        assert dependency.target_id == role.id
        assert dependency.relationship == "assumes_role"
        assert dependency.evidence_ids == ["instance"]
        assert instance.evidence_ids == ["instance"]
        assert instance.fact_evidence == {"/instance_type": ["instance"]}
        evidence = model.evidence["instance"]
        assert evidence.resource_id == dependency.source_id
        assert evidence.snapshot_id == model.snapshot_id
        assert evidence.configuration_path == "Instance"
        assert evidence.value == {"InstanceType": "t3.micro", "RoleName": "role-app"}
        assert model.evidence["role"].resource_id == role.id
        assert model.evidence["role"].snapshot_id == model.snapshot_id
        assert model.evidence["role"].value == "role-app"


def test_duplicate_storage_is_rejected(data: dict) -> None:
    data["regions"]["eu-west-1"]["resources"].append(deepcopy(tree(data)[2]))
    with pytest.raises(ValidationError, match="duplicate resource") as error:
        ArchitectureSnapshot.model_validate(data)
    first = "regions[eu-west-1].resources[0].children[0].children[0]"
    assert f"at {first} and regions[eu-west-1].resources[1]" in str(error.value)


@pytest.mark.parametrize(
    "field,value,diagnostic",
    [
        ("account_id", "999999999999", "account must match snapshot account"),
        ("region", "us-east-1", "region must match enclosing region"),
    ],
)
def test_nested_resource_scope_is_validated(
    data: dict, field: str, value: str, diagnostic: str
) -> None:
    tree(data)[2]["identity"][field] = value
    with pytest.raises(ValidationError, match=diagnostic):
        ArchitectureSnapshot.model_validate(data)


def test_unsupported_discriminator_requires_adapter_normalization(data: dict) -> None:
    data["global_resources"][0]["resource_type"] = "AWS::IAM::User"
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        ArchitectureSnapshot.model_validate(data)


@pytest.mark.parametrize(
    "field,value,diagnostic",
    [
        ("snapshot_id", "00000000-0000-4000-8000-000000000099", "snapshot_id must match snapshot"),
        ("resource_id", "missing", "unknown resource owner"),
    ],
)
def test_evidence_context_including_uncited_records(
    data: dict, field: str, value: str, diagnostic: str
) -> None:
    data["evidence"]["role"][field] = value
    with pytest.raises(ValidationError, match=diagnostic):
        ArchitectureSnapshot.model_validate(data)


@pytest.mark.parametrize(
    "citation,diagnostic",
    [
        ("missing", "unknown evidence citation"),
        ("role", "belongs to another resource"),
    ],
)
def test_nested_citation_integrity(data: dict, citation: str, diagnostic: str) -> None:
    tree(data)[2]["fact_evidence"]["/instance_type"] = [citation]
    with pytest.raises(ValidationError, match=diagnostic):
        ArchitectureSnapshot.model_validate(data)


def test_blank_evidence_references(data: dict) -> None:
    data["evidence"]["   "] = data["evidence"].pop("role")
    with pytest.raises(ValidationError, match="String should"):
        ArchitectureSnapshot.model_validate(data)


@pytest.mark.parametrize(
    "pointer",
    [
        "/attributes/a~1b~0c/0",
        "/attributes/a~1b~0c/1/",
        "/identity/physical_id",
    ],
    ids=["escaped-array-item", "empty-key", "model-field"],
)
def test_pointer_targets_and_fact_only_citations(data: dict, pointer: str) -> None:
    instance = tree(data)[2]
    instance["attributes"] = {"a/b~c": [None, {"": False}], "~1": 42}
    instance["evidence_ids"] = []
    instance["fact_evidence"] = {pointer: ["instance"]}
    snapshot = ArchitectureSnapshot.model_validate(data)
    assert ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot


@pytest.mark.parametrize(
    "pointer,diagnostic",
    [
        ("/attributes/missing", "missing key"),
        ("/instance_type/field", "cannot traverse scalar"),
        ("/attributes/items/01", "invalid array index"),
        ("/attributes/items/1", "array index out of bounds"),
    ],
    ids=["missing-key", "scalar", "non-numeric-index", "index-out-of-bounds"],
)
def test_invalid_fact_targets(data: dict, pointer: str, diagnostic: str) -> None:
    instance = tree(data)[2]
    instance["attributes"] = {"items": [None]}
    instance["fact_evidence"] = {pointer: ["instance"]}
    with pytest.raises(ValidationError, match=diagnostic):
        ArchitectureSnapshot.model_validate(data)


@pytest.mark.parametrize(
    "resource_id", [None, "123456789012/eu-west-1/ec2/physical/i-web", "missing"]
)
def test_coverage_resource_context(data: dict, resource_id: str | None) -> None:
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
                {
                    "resource_id": resource_id,
                    "configuration_path": "VpcId",
                    "reason": "Unknown",
                }
            ],
        }
    ]
    if resource_id == "missing":
        with pytest.raises(ValidationError, match="unknown unresolved resource"):
            ArchitectureSnapshot.model_validate(data)
    else:
        snapshot = ArchitectureSnapshot.model_validate(data)
        assert snapshot.coverage.operations[0].unresolved_values[0].resource_id == resource_id


@pytest.mark.parametrize("endpoint", ["source_id", "target_id"])
def test_dependency_endpoints_must_exist(data: dict, endpoint: str) -> None:
    data["dependencies_by_region"]["eu-west-1"][0][endpoint] = "missing"
    with pytest.raises(ValidationError, match=f"unknown {endpoint}"):
        ArchitectureSnapshot.model_validate(data)


def test_dependency_group_matches_source(data: dict) -> None:
    data["dependencies_by_region"]["global"] = data["dependencies_by_region"].pop("eu-west-1")
    with pytest.raises(ValidationError, match="grouping must match source resource region"):
        ArchitectureSnapshot.model_validate(data)


def test_dependency_citation_ownership(data: dict) -> None:
    data["dependencies_by_region"]["eu-west-1"][0]["evidence_ids"] = ["role"]
    with pytest.raises(ValidationError, match="belongs to another resource"):
        ArchitectureSnapshot.model_validate(data)


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
