import copy
import json
from urllib.parse import quote

import pytest

from archdiscov.models.architecture import UnknownResource
from archdiscov.models.enums import (
    AwsRegion,
    AwsService,
    ResourceType,
)
from archdiscov.models.resources.iam import (
    IamInlinePolicy,
    IamInstanceProfile,
    IamManagedPolicy,
    IamRole,
    normalize_document,
    sanitize_document,
)
from archdiscov.services import load_snapshot, save_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"
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


def test_sanitizer_redacts_context_tags_and_generic_keys_without_mutation():
    document = sensitive_document()
    original = copy.deepcopy(document)
    safe, paths = sanitize_document(document)
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


def test_malformed_document_raises_a_safe_error():
    with pytest.raises(ValueError) as exc:
        normalize_document("raw-secret")
    assert str(exc.value) == "IAM document must be a JSON object with Statement object(s)"
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__


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


def test_schema_one_fixtures_remain_compatible():
    for name in ("hierarchy.json", "aws.json"):
        assert load_snapshot(FIXTURES / name).schema_version == "1"
    snapshot = load_snapshot(FIXTURES / "aws.json")
    assert isinstance(snapshot.global_resources[0], UnknownResource)
