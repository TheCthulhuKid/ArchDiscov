from itertools import product

import pytest
from pydantic import TypeAdapter, ValidationError

from archdiscov.models.architecture import (
    AwsSource,
    CloudFormationSource,
    Evidence,
    MockSource,
    Resource,
    SupportedSource,
    TerraformPlanSource,
    TerraformStateSource,
    UnknownResource,
)
from archdiscov.models.enums import ResourceType, SnapshotSourceKind, SourceType


def test_evidence_source_round_trip() -> None:
    for source_type, use_enum in product(SourceType, [False, True]):
        evidence = Evidence(
            snapshot_id="00000000-0000-4000-8000-000000000001",
            resource_id="123456789012/eu-west-1/ec2/physical/i-example",
            source=source_type if use_enum else source_type.value,
            locator="input.json",
            configuration_path="resources[0].name",
            value="web",
        )
        assert evidence.source is source_type
        assert evidence.model_dump(mode="json")["source"] == source_type.value
        assert Evidence.model_validate_json(evidence.model_dump_json()) == evidence


def test_invalid_evidence_source_is_rejected() -> None:
    # A snapshot source kind is not an evidence source.
    with pytest.raises(ValidationError):
        Evidence(
            snapshot_id="00000000-0000-4000-8000-000000000001",
            resource_id="123456789012/eu-west-1/ec2/physical/i-example",
            source="terraform_state",
            locator="input.json",
            configuration_path="name",
            value="web",
        )


@pytest.mark.parametrize(
    ("model", "kind", "fields"),
    [
        (AwsSource, SnapshotSourceKind.AWS, {}),
        (
            TerraformStateSource,
            SnapshotSourceKind.TERRAFORM_STATE,
            {"path": "state.json", "format_version": "1.0"},
        ),
        (
            TerraformPlanSource,
            SnapshotSourceKind.TERRAFORM_PLAN,
            {"path": "plan.json", "format_version": "1.0"},
        ),
        (CloudFormationSource, SnapshotSourceKind.CLOUDFORMATION, {"path": "template.json"}),
        (MockSource, SnapshotSourceKind.MOCK, {"label": "fixture"}),
    ],
)
def test_source_kind_discriminator_and_defaults(
    model: type, kind: SnapshotSourceKind, fields: dict
) -> None:
    adapter = TypeAdapter(SupportedSource)
    default = model(**fields)
    assert default.kind is kind
    for input_kind in (kind, kind.value):
        source = adapter.validate_python({"kind": input_kind, **fields})
        assert type(source) is model
        assert source.kind is kind
        assert source == default
        assert source.model_dump(mode="json")["kind"] == kind.value
        assert adapter.validate_json(source.model_dump_json()) == source
    with pytest.raises(ValidationError):
        model(kind="unsupported", **fields)


def test_invalid_snapshot_source_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(SupportedSource).validate_python({"kind": "terraform"})


def test_base_resource_type_round_trip() -> None:
    for resource_type in ResourceType:
        resource = Resource(
            identity={
                "account_id": "123456789012",
                "region": "eu-west-1",
                "service": "ec2",
                "physical_id": "resource-example",
            },
            name="example",
            resource_type=resource_type.value,
        )
        assert resource.resource_type is resource_type
        assert resource.model_dump(mode="json")["resource_type"] == resource_type.value
        assert Resource.model_validate_json(resource.model_dump_json()) == resource


def test_unsupported_aws_type_requires_unknown_resource() -> None:
    fields = {
        "identity": {
            "account_id": "123456789012",
            "region": "eu-west-1",
            "service": "ec2",
            "physical_id": "queue-example",
        },
        "name": "example",
    }
    with pytest.raises(ValidationError):
        Resource(resource_type="AWS::SNS::Topic", **fields)
    unknown = UnknownResource(aws_resource_type="AWS::SNS::Topic", **fields)
    assert unknown.resource_type is ResourceType.UNKNOWN
    assert unknown.aws_resource_type == "AWS::SNS::Topic"
    assert UnknownResource.model_validate_json(unknown.model_dump_json()) == unknown
