import json

import pytest
from pydantic import ValidationError

from archdiscov.models.architecture import (
    AccountArchitecture,
    ArchitectureSnapshot,
    AwsSource,
    Ec2Instance,
    Subnet,
    UnknownResource,
)
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"


def test_aws_source_role_arn_accepts_partitions_and_paths_only_for_roles() -> None:
    arn = "arn:aws-us-gov:iam::123456789012:role/aws-service-role/ecs.amazonaws.com/Discovery"
    assert AwsSource(role_arn=arn).role_arn == arn
    with pytest.raises(ValidationError) as error:
        AwsSource(role_arn="arn:aws:iam::123456789012:user/Discovery")
    assert error.value.errors()[0]["loc"] == ("role_arn",)


def snapshot_data(filename: str = "aws.json") -> dict:
    return json.loads((FIXTURES / filename).read_text())


@pytest.mark.parametrize("filename", ["hierarchy.json", "terraform-plan.json"])
def test_representative_snapshots_round_trip(filename: str) -> None:
    snapshot = ArchitectureSnapshot.model_validate(snapshot_data(filename))
    assert ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
    resource = snapshot.regions["eu-west-1"].resources[0]
    assert resource.id == resource.identity.stable_id


def test_nested_resources_keep_their_subtypes() -> None:
    snapshot = ArchitectureSnapshot.model_validate(snapshot_data("hierarchy.json"))
    subnet, security_group = snapshot.regions["eu-west-1"].resources[0].children
    assert isinstance(subnet, Subnet)
    assert isinstance(subnet.children[0], Ec2Instance)
    assert isinstance(security_group, UnknownResource)
    assert security_group.aws_resource_type == "AWS::EC2::SecurityGroup"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("collection_completed_at", "2026-10-13T07:59:00Z"),
        ("coverage", {"regions": ["eu-west-1", "eu-west-1"], "account_wide": False}),
        ("coverage", {"regions": ["us-east-1"], "account_wide": True}),
    ],
    ids=["window", "duplicate-region", "region-outside-scope"],
)
def test_invalid_snapshot_fields_are_rejected(field: str, value: object) -> None:
    data = snapshot_data()
    data[field] = value
    with pytest.raises(ValidationError):
        ArchitectureSnapshot.model_validate(data)


def test_resource_id_cannot_disagree_with_identity() -> None:
    data = snapshot_data()
    data["regions"]["eu-west-1"]["resources"][0]["id"] = "snapshot-local-id"
    with pytest.raises(ValidationError):
        ArchitectureSnapshot.model_validate(data)


def test_identity_segments_cannot_collide() -> None:
    data = snapshot_data()
    resource_data = data["regions"]["eu-west-1"]["resources"][0]
    resource_data["identity"]["physical_id"] = "a/b"
    physical = ArchitectureSnapshot.model_validate(data).regions["eu-west-1"].resources[0]
    del resource_data["identity"]["physical_id"]
    resource_data["identity"]["logical_address"] = "a/b"
    logical = ArchitectureSnapshot.model_validate(data).regions["eu-west-1"].resources[0]
    assert physical.id.endswith("/physical/a%2Fb")
    assert logical.id.endswith("/logical/a%2Fb")
    assert physical.id != logical.id


def test_unsupported_version_has_actionable_error() -> None:
    data = snapshot_data()
    data["schema_version"] = "999"
    with pytest.raises(ValidationError) as error:
        ArchitectureSnapshot.model_validate(data)
    version_error = next(
        item for item in error.value.errors() if item["loc"] == ("schema_version",)
    )
    assert version_error["type"] == "unsupported_schema_version"
    assert "999" in version_error["msg"]
    assert "supported versions: 1, 2" in version_error["msg"]


def test_global_cannot_be_used_as_a_regional_container() -> None:
    with pytest.raises(ValidationError):
        AccountArchitecture.model_validate(
            {"account_id": "123456789012", "regions": {"global": {"resources": []}}}
        )
