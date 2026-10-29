from uuid import UUID

import pytest

from archdiscov.ingestion.evidence_builder import EvidenceBuilder
from archdiscov.ingestion.iam.relationships import resolve_relationships
from archdiscov.models.architecture import ArchitectureSnapshot, AwsSource, SnapshotCoverage
from archdiscov.models.enums import AwsRegion, AwsService
from archdiscov.models.resources.iam import IamInstanceProfile, IamRole
from tests.support.iam_data import ACCOUNT, PROFILE_ARN, ROLE_ARN, context


def role_with_trust(principal, *, negative=False):
    builder = EvidenceBuilder(UUID(int=14), ACCOUNT, AwsRegion.GLOBAL, AwsService.IAM)
    statement = {
        "Effect": "Deny",
        "Action": ["sts:AssumeRole"],
        "NotPrincipal" if negative else "Principal": principal,
        "Condition": {
            "StringEquals": {"aws:SourceArn": "arn:aws:lambda:us-east-1:123456789012:function:web"}
        },
    }
    document = {"Statement": [statement]}
    role = builder._build(
        IamRole,
        operation="GetRole",
        item_path='Request[{"RoleName":"web"}].Page[0].Role',
        item={"AssumeRolePolicyDocument": document},
        physical_id="AROA1",
        attributes={},
        facts={"/trust_document": ("AssumeRolePolicyDocument", document)},
        arn=ROLE_ARN,
        trust_document=document,
    )
    return role, builder.evidence


@pytest.mark.parametrize(
    "principal,target_kind,principal_kind",
    [
        ({"AWS": ROLE_ARN}, "local", "aws"),
        ({"AWS": "210987654321"}, "external", "aws"),
        ({"AWS": "arn:aws:iam::210987654321:root"}, "external", "aws"),
        ({"Service": "ec2.amazonaws.com"}, "external", "service"),
        ({"AWS": "*"}, "unresolved", "aws"),
        ("*", "unresolved", "unknown"),
    ],
    ids=["stored-role", "account-id", "account-arn", "service", "wildcard", "not-a-mapping"],
)
def test_trust_principals_are_references_not_grants(principal, target_kind, principal_kind):
    role, evidence = role_with_trust(principal)
    links = resolve_relationships(UUID(int=14), ACCOUNT, [role], [], evidence)
    (link,) = links[AwsRegion.GLOBAL]
    assert link.target.kind == target_kind
    assert link.relationship == "attached_to"
    assert link.attributes == {
        "kind": "trust_principal",
        "principal_kind": principal_kind,
        "not_principal": False,
    }
    principal_evidence, statement_evidence = [evidence[key] for key in link.evidence_ids]
    assert principal_evidence.configuration_path.endswith(
        ".Principal" if isinstance(principal, str) else ".Principal." + next(iter(principal))
    )
    assert statement_evidence.value["Effect"] == "Deny"
    assert statement_evidence.value["Action"] == ["sts:AssumeRole"]
    assert statement_evidence.value["Condition"]["StringEquals"]["aws:SourceArn"].endswith(
        ":function:web"
    )
    assert all(e.resource_id == role.id for e in (principal_evidence, statement_evidence))
    snapshot = ArchitectureSnapshot(
        schema_version="2",
        snapshot_id=UUID(int=14),
        account_id=ACCOUNT,
        collection_started_at=context(None).clock(),
        collection_completed_at=context(None).clock(),
        source=AwsSource(profile="test"),
        global_resources=[role],
        coverage=SnapshotCoverage(regions=[], account_wide=True),
        dependencies_by_region=links,
        evidence=evidence,
    )
    assert ArchitectureSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot


def test_not_principal_remains_unresolved_even_for_stored_role():
    role, evidence = role_with_trust({"AWS": ROLE_ARN}, negative=True)
    (link,) = resolve_relationships(UUID(int=14), ACCOUNT, [role], [], evidence)[AwsRegion.GLOBAL]
    assert link.target.kind == "unresolved"
    assert link.target.source_path.endswith(".NotPrincipal.AWS")
    assert link.attributes["not_principal"] is True


def test_profile_keeps_missing_role_as_unresolved_without_fabrication():
    builder = EvidenceBuilder(UUID(int=14), ACCOUNT, AwsRegion.GLOBAL, AwsService.IAM)
    profile = builder._build(
        IamInstanceProfile,
        operation="ListInstanceProfiles",
        item_path="InstanceProfiles[0]",
        item={"Roles": [{"Arn": ROLE_ARN}]},
        physical_id="AIPA1",
        attributes={},
        facts={"/role_arns/0": ("Roles[0].Arn", ROLE_ARN)},
        arn=PROFILE_ARN,
        role_arns=[ROLE_ARN],
    )
    (link,) = resolve_relationships(UUID(int=14), ACCOUNT, [profile], [], builder.evidence)[
        AwsRegion.GLOBAL
    ]
    assert link.target.kind == "unresolved"
    assert link.target.source_path == "InstanceProfiles[0].Roles[0].Arn"
