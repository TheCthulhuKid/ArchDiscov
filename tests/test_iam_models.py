import copy
import json
from pathlib import Path
from urllib.parse import quote

import pytest
from pydantic import ValidationError

from archdiscov.models.architecture import ArchitectureSnapshot, UnknownResource
from archdiscov.models.enums import (
    AttachmentKind,
    AwsRegion,
    AwsService,
    ResourceType,
    TrustPrincipalKind,
)
from archdiscov.models.iam import (
    IamInlinePolicy,
    IamInstanceProfile,
    IamManagedPolicy,
    IamRole,
    normalize_document,
    sanitize_document,
)
from archdiscov.services import load_snapshot, save_snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "snapshots"
ROLE_ARN = "arn:aws:iam::123456789012:role/application"
IDENTITY = {
    "account_id": "123456789012",
    "region": "global",
    "service": "iam",
    "physical_id": ROLE_ARN,
}


def sensitive_document():
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": "arn:aws:iam::111111111111:root"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {
                        "sts:ExternalId": "external-secret",
                        "sts:SourceIdentity": ["source-secret"],
                        "aws:RequestTag/private~key": "tag-secret",
                        "aws:SourceAccount": "111111111111",
                    },
                    "ArnEquals": {"aws:SourceArn": ROLE_ARN},
                },
            }
        ],
        "metadata": {"password": {"nested": "password-secret"}},
        "Tags": [
            {"Key": "api-key", "Value": "api-secret"},
            {"Key": "team", "Value": "platform"},
        ],
    }


@pytest.mark.parametrize("statement", [{"Effect": "Allow"}, [{"Effect": "Allow"}], []])
@pytest.mark.parametrize("encoding", ["dict", "json", "percent"])
def test_normalize_document_forms_and_statement_shapes(statement, encoding):
    document = {"Statement": statement, "Resource": "literal%2Fpath+suffix"}
    value = document if encoding == "dict" else json.dumps(document)
    if encoding == "percent":
        value = quote(value, safe="")
    normalized = normalize_document(value)
    assert normalized == document
    assert normalized is not document


@pytest.mark.parametrize("encoded", [False, True])
def test_sanitizer_redacts_context_tags_and_generic_keys_without_mutation(encoded):
    document = sensitive_document()
    original = copy.deepcopy(document)
    value = quote(json.dumps(document), safe="") if encoded else document
    safe, paths = sanitize_document(value)
    assert document == original
    assert set(paths) == {
        "/metadata/password",
        "/Tags/0/Value",
        "/Statement/0/Condition/StringEquals/sts:ExternalId",
        "/Statement/0/Condition/StringEquals/sts:SourceIdentity",
        "/Statement/0/Condition/StringEquals/aws:RequestTag~1private~0key",
    }
    condition = safe["Statement"][0]["Condition"]
    assert condition["StringEquals"]["sts:ExternalId"] == "[REDACTED]"
    assert condition["StringEquals"]["sts:SourceIdentity"] == "[REDACTED]"
    assert condition["StringEquals"]["aws:SourceAccount"] == "111111111111"
    assert condition["ArnEquals"]["aws:SourceArn"] == ROLE_ARN
    assert safe["Statement"][0]["Principal"] == original["Statement"][0]["Principal"]
    assert safe["Statement"][0]["Action"] == "sts:AssumeRole"
    assert safe["Tags"][0]["Value"] == "[REDACTED]"
    assert safe["Tags"][1]["Value"] == "platform"
    assert "secret" not in json.dumps(safe)
    again, again_paths = sanitize_document(safe)
    assert again == safe
    assert again_paths == paths


def test_condition_redaction_preserves_statement_dict_shape_and_context_scope():
    safe, paths = sanitize_document(
        {
            "Statement": {
                "Sid": "ExternalId",
                "Condition": {"StringLike": {"STS:EXTERNALID": ["secret"], "aws:userid": "user"}},
            },
            "ExternalId": "non-condition-metadata",
        }
    )
    assert isinstance(safe["Statement"], dict)
    assert safe["ExternalId"] == "non-condition-metadata"
    assert paths == [
        "/Statement/Condition/StringLike/STS:EXTERNALID",
        "/Statement/Condition/StringLike/aws:userid",
    ]


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        42,
        "raw-secret",
        "%7Braw-secret",
        '{"Statement": "raw-secret"}',
        {"Statement": None},
        {"Statement": ["raw-secret"]},
        {"Statement": [None]},
        {"Statement": {}, "raw-secret": object()},
        {"Statement": {}, "value": float("nan")},
        {"Statement": {}, "value": float("inf")},
        {"Statement": {}, 1: "raw-secret"},
        {},
    ],
)
@pytest.mark.parametrize("function", [normalize_document, sanitize_document])
def test_malformed_documents_raise_safe_errors(value, function):
    with pytest.raises(ValueError) as exc:
        function(value)
    assert str(exc.value) == "IAM document must be a JSON object with Statement object(s)"
    assert "raw-secret" not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__


def test_recursive_documents_raise_safe_errors():
    document = {"Statement": {}}
    document["self"] = document
    with pytest.raises(ValueError, match="IAM document must be a JSON object"):
        normalize_document(document)


def test_typed_document_validators_sanitize_all_policy_fields():
    document = sensitive_document()
    encoded = quote(json.dumps(document), safe="")
    safe, _ = sanitize_document(document)
    role = IamRole(
        identity=IDENTITY,
        arn=ROLE_ARN,
        trust_document=encoded,
        inline_policies=[{"name": "inline", "document": encoded}],
    )
    policy = IamManagedPolicy(identity=IDENTITY, arn=ROLE_ARN, document=document)
    assert role.trust_document == safe
    assert isinstance(role.inline_policies[0], IamInlinePolicy)
    assert role.inline_policies[0].document == safe
    assert policy.document == safe
    assert IamRole.model_validate_json(role.model_dump_json()) == role
    assert IamManagedPolicy.model_validate_json(policy.model_dump_json()) == policy
    assert IamRole(identity=IDENTITY, arn=ROLE_ARN).trust_document is None
    assert IamManagedPolicy(identity=IDENTITY, arn=ROLE_ARN).document is None


@pytest.mark.parametrize(
    "model, fields",
    [
        (IamRole, {"trust_document": {"Statement": "invalid"}}),
        (IamRole, {"inline_policies": [{"name": "inline", "document": []}]}),
        (IamManagedPolicy, {"document": "invalid"}),
    ],
)
def test_typed_models_reject_malformed_documents(model, fields):
    with pytest.raises(ValidationError):
        model(identity=IDENTITY, arn=ROLE_ARN, **fields)


def test_iam_fixture_round_trips_with_explicit_models_and_global_identity(tmp_path):
    snapshot = load_snapshot(FIXTURES / "iam.json")
    role, profile, policy = snapshot.global_resources
    assert isinstance(role, IamRole)
    assert isinstance(profile, IamInstanceProfile)
    assert isinstance(policy, IamManagedPolicy)
    assert role.resource_type is ResourceType.IAM_ROLE
    assert profile.resource_type is ResourceType.INSTANCE_PROFILE
    assert policy.resource_type is ResourceType.MANAGED_POLICY
    assert profile.role_arns == [role.arn]
    assert role.boundary_arn == policy.arn
    assert policy.default_version_id == "v1"
    assert role.inline_policies[0].name == "queue-reader"
    for resource in snapshot.global_resources:
        assert resource.identity.region is AwsRegion.GLOBAL
        assert resource.identity.service is AwsService.IAM
        assert resource.identity.physical_id == resource.arn
        assert resource.id == resource.identity.stable_id
        assert "/global/iam/physical/arn%3Aaws%3Aiam%3A%3A" in resource.id
    assert load_snapshot(save_snapshot(snapshot, tmp_path / "iam.json")) == snapshot


def test_recursive_union_accepts_iam_children():
    snapshot = load_snapshot(FIXTURES / "iam.json")
    payload = snapshot.model_dump(mode="json")
    role, profile, policy = payload["global_resources"]
    role["children"] = [profile, policy]
    payload["global_resources"] = [role]
    rebuilt = ArchitectureSnapshot.model_validate(payload)
    assert isinstance(rebuilt.global_resources[0].children[0], IamInstanceProfile)
    assert isinstance(rebuilt.global_resources[0].children[1], IamManagedPolicy)


def test_schema_one_fixtures_remain_compatible():
    for name in ("hierarchy.json", "aws.json"):
        assert load_snapshot(FIXTURES / name).schema_version == "1"
    snapshot = load_snapshot(FIXTURES / "aws.json")
    assert isinstance(snapshot.global_resources[0], UnknownResource)


def test_iam_relationship_and_principal_enums():
    for kind in (
        "instance_profile",
        "profile_role",
        "role_policy",
        "permissions_boundary",
        "trust_principal",
    ):
        assert AttachmentKind(kind).name == kind.upper()
    assert {kind.value for kind in TrustPrincipalKind} == {"aws", "service", "federated", "unknown"}
