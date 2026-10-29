import json

import pytest
from pydantic import ValidationError

from archdiscov.models.architecture import (
    AccountArchitecture,
    ArchitectureSnapshot,
    AwsSource,
    ResourceIdentity,
)
from archdiscov.models.enums import AwsRegion, AwsService
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"


@pytest.mark.parametrize(
    "arn",
    [
        "arn:aws:iam::123456789012:role/Discovery",
        "arn:aws-us-gov:iam::123456789012:role/aws-service-role/ecs.amazonaws.com/Discovery",
    ],
    ids=["commercial", "partition-and-path"],
)
def test_aws_source_role_arn_round_trip(arn: str) -> None:
    source = AwsSource(role_arn=arn)
    assert source.role_arn == arn
    assert AwsSource.model_validate_json(source.model_dump_json()) == source


def test_invalid_aws_source_role_arn_is_rejected() -> None:
    with pytest.raises(ValidationError) as error:
        AwsSource(role_arn="arn:aws:iam::123456789012:user/Discovery")
    assert error.value.errors()[0]["loc"] == ("role_arn",)


def test_aws_source_role_arn_remains_optional() -> None:
    assert AwsSource().role_arn is None
    assert AwsSource(role_arn=None).role_arn is None


def snapshot_data(filename: str = "aws.json") -> dict:
    return json.loads((FIXTURES / filename).read_text())


@pytest.mark.parametrize("filename", ["aws.json", "terraform-plan.json", "cloudformation.json"])
def test_representative_snapshots_round_trip(filename: str) -> None:
    snapshot = ArchitectureSnapshot.model_validate(snapshot_data(filename))
    assert ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
    resource = snapshot.regions["eu-west-1"].resources[0]
    assert resource.id == resource.identity.stable_id


@pytest.mark.parametrize(
    "filename,logical_address,stable_id",
    [
        (
            "terraform-plan.json",
            "module.network.aws_vpc.main",
            "123456789012/eu-west-1/ec2/logical/module.network.aws_vpc.main",
        ),
        (
            "cloudformation.json",
            "network/MainVpc",
            "123456789012/eu-west-1/ec2/logical/network%2FMainVpc",
        ),
    ],
)
def test_declaration_identity_round_trip(
    filename: str, logical_address: str, stable_id: str
) -> None:
    snapshot = ArchitectureSnapshot.model_validate(snapshot_data(filename))
    reloaded = ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json())
    for model in (snapshot, reloaded):
        resource = model.regions["eu-west-1"].resources[0]
        assert resource.identity.physical_id is None
        assert resource.identity.logical_address == logical_address
        assert resource.identity.stable_id == stable_id
        assert resource.id == stable_id


def test_resource_identity_is_independent_of_snapshot_identity() -> None:
    data = snapshot_data()
    first = ArchitectureSnapshot.model_validate(data)
    data["snapshot_id"] = "00000000-0000-4000-8000-000000000004"
    second = ArchitectureSnapshot.model_validate(data)
    assert first.snapshot_id != second.snapshot_id
    assert first.regions["eu-west-1"].resources[0].id == second.regions["eu-west-1"].resources[0].id


def test_physical_and_logical_identifiers_are_preserved() -> None:
    data = snapshot_data("terraform-plan.json")
    data["regions"]["eu-west-1"]["resources"][0]["identity"]["physical_id"] = "vpc-example"
    resource = ArchitectureSnapshot.model_validate(data).regions["eu-west-1"].resources[0]
    assert resource.identity.logical_address == "module.network.aws_vpc.main"
    assert resource.id == "123456789012/eu-west-1/ec2/physical/vpc-example"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("collection_completed_at", "2026-10-13T07:59:00Z"),
        ("coverage", {"regions": ["global"], "account_wide": True}),
        ("coverage", {"regions": ["eu-west-1", "eu-west-1"], "account_wide": False}),
        ("coverage", {"regions": [], "account_wide": False}),
    ],
    ids=["window", "global-region", "duplicate-region", "empty-scope"],
)
def test_invalid_snapshot_fields_are_rejected(field: str, value: object) -> None:
    data = snapshot_data()
    data[field] = value
    with pytest.raises(ValidationError):
        ArchitectureSnapshot.model_validate(data)


def test_contract_fields_are_required() -> None:
    data = snapshot_data()
    del data["source"]
    with pytest.raises(ValidationError):
        ArchitectureSnapshot.model_validate(data)


@pytest.mark.parametrize("kind", ["terraform_state", "terraform_plan", "cloudformation", "mock"])
def test_source_specific_metadata(kind: str) -> None:
    data = snapshot_data()
    sources = {
        "terraform_state": {
            "kind": kind,
            "path": "state.json",
            "format_version": "1.0",
            "terraform_version": "1.13.0",
            "lineage": "state-lineage",
            "serial": 3,
            "workspace": "production",
        },
        "terraform_plan": snapshot_data("terraform-plan.json")["source"],
        "cloudformation": snapshot_data("cloudformation.json")["source"],
        "mock": {"kind": kind, "label": "test-fixture"},
    }
    data["source"] = sources[kind]
    snapshot = ArchitectureSnapshot.model_validate(data)
    assert snapshot.source.model_dump(mode="json", exclude_none=True) == sources[kind]


def test_source_export_time_requires_timezone() -> None:
    data = snapshot_data("terraform-plan.json")
    data["source"]["exported_at"] = "2026-10-13T07:59:00"
    with pytest.raises(ValidationError):
        ArchitectureSnapshot.model_validate(data)


@pytest.mark.parametrize("identifier", [None, "   "], ids=["no-identifier", "blank"])
def test_resource_requires_a_usable_identifier(identifier: str | None) -> None:
    data = snapshot_data("terraform-plan.json")
    data["regions"]["eu-west-1"]["resources"][0]["identity"]["logical_address"] = identifier
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


def test_account_wide_only_scope() -> None:
    data = snapshot_data()
    data["coverage"]["regions"] = []
    data["regions"] = {}
    snapshot = ArchitectureSnapshot.model_validate(data)
    assert snapshot.coverage.account_wide
    assert snapshot.global_resources[0].identity.region == "global"


@pytest.mark.parametrize(
    "coverage",
    [
        {"regions": ["us-east-1"], "account_wide": True},
        {"regions": ["eu-west-1"], "account_wide": False},
    ],
    ids=["region-outside-scope", "globals-without-account-wide"],
)
def test_scope_cannot_contradict_contents(coverage: dict) -> None:
    data = snapshot_data()
    data["coverage"] = coverage
    with pytest.raises(ValidationError):
        ArchitectureSnapshot.model_validate(data)


def test_empty_requested_region_does_not_claim_success() -> None:
    data = snapshot_data()
    data["regions"] = {}
    data["global_resources"] = []
    snapshot = ArchitectureSnapshot.model_validate(data)
    assert snapshot.coverage.regions == ["eu-west-1"]
    assert not snapshot.regions


def test_collection_window_compares_instants_across_timezones() -> None:
    data = snapshot_data()
    data["collection_started_at"] = "2026-10-13T10:00:00+02:00"
    data["collection_completed_at"] = "2026-10-13T08:00:00Z"
    snapshot = ArchitectureSnapshot.model_validate(data)
    assert snapshot.collection_started_at == snapshot.collection_completed_at


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


def test_identity_context_is_validated() -> None:
    data = snapshot_data()
    data["regions"]["eu-west-1"]["resources"][0]["identity"]["account_id"] = "unknown"
    with pytest.raises(ValidationError):
        ArchitectureSnapshot.model_validate(data)


def test_nested_resources_retain_identity_and_subtype_fields() -> None:
    data = snapshot_data()
    vpc = data["regions"]["eu-west-1"]["resources"][0]
    vpc["children"] = [
        {
            "identity": {**vpc["identity"], "physical_id": "subnet-example"},
            "name": "application",
            "resource_type": "AWS::EC2::Subnet",
            "availability_zone": "eu-west-1a",
            "cidr_blocks": ["10.0.1.0/24"],
            "children": [
                {
                    "identity": {**vpc["identity"], "physical_id": "i-example"},
                    "name": "web",
                    "resource_type": "AWS::EC2::Instance",
                    "instance_type": "t3.micro",
                }
            ],
        }
    ]
    snapshot = ArchitectureSnapshot.model_validate(data)
    loaded = ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json())
    assert loaded == snapshot
    for version in (snapshot, loaded):
        vpc = version.regions["eu-west-1"].resources[0]
        subnet = vpc.children[0]
        assert [str(cidr) for cidr in vpc.cidr_blocks] == ["10.0.0.0/16"]
        assert subnet.availability_zone == "eu-west-1a"
        assert [str(cidr) for cidr in subnet.cidr_blocks] == ["10.0.1.0/24"]
        assert subnet.id.endswith("/physical/subnet-example")
        assert subnet.children[0].instance_type == "t3.micro"
        assert subnet.children[0].id.endswith("/physical/i-example")


def test_resource_names_are_optional_and_do_not_change_identity() -> None:
    data = snapshot_data("hierarchy.json")
    instance = data["regions"]["eu-west-1"]["resources"][0]["children"][0]["children"][0]
    instance["name"] = None
    snapshot = ArchitectureSnapshot.model_validate(data)
    loaded = ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json())
    for version in (snapshot, loaded):
        resource = version.regions["eu-west-1"].resources[0].children[0].children[0]
        assert resource.name is None
        assert resource.id == "123456789012/eu-west-1/ec2/physical/i-web"
        assert resource.instance_type == "t3.micro"


def test_resource_service_is_an_enum() -> None:
    snapshot = ArchitectureSnapshot.model_validate(snapshot_data())
    service = snapshot.regions["eu-west-1"].resources[0].identity.service
    assert service is AwsService.EC2
    assert snapshot.global_resources[0].identity.service is AwsService.IAM


def test_unknown_resource_service_is_rejected() -> None:
    data = snapshot_data()
    data["regions"]["eu-west-1"]["resources"][0]["identity"]["service"] = "EC2"
    with pytest.raises(ValidationError):
        ArchitectureSnapshot.model_validate(data)


def test_load_balancer_identity_uses_arn_service_namespace() -> None:
    arn = "arn:aws:elasticloadbalancing:eu-west-1:123456789012:loadbalancer/app/web/123"
    identity = ResourceIdentity.model_validate(
        {
            "account_id": "123456789012",
            "region": "eu-west-1",
            "service": arn.split(":")[2],
            "physical_id": arn,
        }
    )
    assert identity.service is AwsService.ELASTIC_LOAD_BALANCING
    assert identity.model_dump(mode="json")["service"] == "elasticloadbalancing"
    assert identity.stable_id == (
        "123456789012/eu-west-1/elasticloadbalancing/physical/"
        "arn%3Aaws%3Aelasticloadbalancing%3Aeu-west-1%3A123456789012%3A"
        "loadbalancer%2Fapp%2Fweb%2F123"
    )
    assert ResourceIdentity.model_validate_json(identity.model_dump_json()) == identity


def test_service_enum_round_trip() -> None:
    for service in AwsService:
        identity = ResourceIdentity.model_validate(
            {
                "account_id": "123456789012",
                "region": "eu-west-1",
                "service": service.value,
                "physical_id": "example",
            }
        )
        assert identity.service is service
        assert identity.stable_id == f"123456789012/eu-west-1/{service.value}/physical/example"
        assert ResourceIdentity.model_validate_json(identity.model_dump_json()) == identity


def test_enum_regions_are_used_throughout_snapshot() -> None:
    region = AwsRegion.US_EAST_1
    data = snapshot_data()
    contents = data["regions"].pop("eu-west-1")
    contents["resources"][0]["identity"]["region"] = region.value
    data["regions"][region.value] = contents
    data["coverage"]["regions"] = [region.value]
    data["dependencies_by_region"] = {region.value: []}
    snapshot = ArchitectureSnapshot.model_validate(data)
    assert next(iter(snapshot.regions)) is region
    assert snapshot.coverage.regions[0] is region
    assert snapshot.regions[region].resources[0].identity.region is region
    assert next(iter(snapshot.dependencies_by_region)) is region
    assert snapshot.regions[region].resources[0].id == (
        f"123456789012/{region.value}/ec2/physical/vpc-example"
    )
    assert ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot


def test_global_enum_supports_identity_and_dependencies() -> None:
    data = snapshot_data()
    data["dependencies_by_region"] = {"global": []}
    snapshot = ArchitectureSnapshot.model_validate(data)
    assert snapshot.global_resources[0].identity.region is AwsRegion.GLOBAL
    assert next(iter(snapshot.dependencies_by_region)) is AwsRegion.GLOBAL
    assert ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot


def test_unknown_region_is_rejected() -> None:
    data = snapshot_data()
    data["coverage"]["regions"].append("zz-fictional-1")
    with pytest.raises(ValidationError):
        ArchitectureSnapshot.model_validate(data)


def test_global_cannot_be_used_as_a_regional_container() -> None:
    with pytest.raises(ValidationError):
        AccountArchitecture.model_validate(
            {"account_id": "123456789012", "regions": {"global": {"resources": []}}}
        )


def test_blank_source_metadata_is_rejected() -> None:
    data = snapshot_data()
    data["source"] = {"kind": "mock", "label": "   "}
    with pytest.raises(ValidationError):
        ArchitectureSnapshot.model_validate(data)
