from uuid import UUID

from archdiscov.ingestion.evidence_builder import EvidenceBuilder
from archdiscov.models.architecture import UnknownResource
from archdiscov.models.enums import AwsRegion, AwsService


def test_service_identity_and_parameterized_evidence_paths():
    builder = EvidenceBuilder(UUID(int=14), "123456789012", AwsRegion.GLOBAL, AwsService.IAM)
    resource = builder._build(
        UnknownResource,
        operation="ListRoles",
        item_path="Roles[0]",
        item={"RoleId": "AROA1"},
        physical_id="AROA1",
        attributes={},
        facts={},
        aws_resource_type="AWS::IAM::Role",
    )
    first = builder.source_evidence(
        resource, "GetRolePolicy", "Request[RoleName=web,PolicyName=one].PolicyDocument", {}
    )
    second = builder.source_evidence(
        resource, "GetRolePolicy", "Request[RoleName=web,PolicyName=two].PolicyDocument", {}
    )
    assert first != second
    assert resource.identity.service is AwsService.IAM
    assert {e.locator for e in builder.evidence.values()} == {"iam:ListRoles", "iam:GetRolePolicy"}


def test_ec2_evidence_identifiers_remain_backward_compatible():
    builder = EvidenceBuilder(UUID(int=14), "123456789012", AwsRegion.US_EAST_1)
    resource = builder._build(
        UnknownResource,
        operation="DescribeVpcs",
        item_path="Vpcs[0]",
        item={},
        physical_id="vpc-1",
        attributes={},
        facts={},
        aws_resource_type="AWS::EC2::VPC",
    )
    assert resource.evidence_ids == ["us-east-1/DescribeVpcs/vpc-1"]
    assert builder.evidence[resource.evidence_ids[0]].locator == "ec2:DescribeVpcs"
