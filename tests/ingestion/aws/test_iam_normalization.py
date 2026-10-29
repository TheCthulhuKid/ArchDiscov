import json
from copy import deepcopy
from urllib.parse import quote
from uuid import UUID

from botocore.exceptions import EndpointConnectionError

from archdiscov.ingestion.iam.collector import collect_inventory
from archdiscov.ingestion.iam.normalization import normalize_inventory
from archdiscov.ingestion.operations import IamOperation as Op
from archdiscov.models.enums import AwsRegion, AwsService, CoverageStatus
from tests.support.iam_data import (
    ACCOUNT,
    DOCUMENT,
    POLICY_ARN,
    ROLE_ARN,
    TRUST,
    IamClient,
    IamSession,
    config,
    context,
)
from tests.support.iam_data import (
    responses as iam_responses,
)


def responses():
    return {method: deepcopy(payload) for method, payload in iam_responses().items()}


def normalize(data=None):
    session = IamSession(IamClient(data))
    inventory = collect_inventory(session, config(), context(session))
    return normalize_inventory(UUID(int=14), ACCOUNT, inventory), inventory


def by_id(result, physical_id):
    return next(r for r in result.resources if r.identity.physical_id == physical_id)


def citation(result, resource, pointer):
    ids = resource.fact_evidence[pointer]
    assert len(ids) == 1
    return result.evidence[ids[0]]


def persisted(result):
    return json.dumps(
        {
            "resources": [r.model_dump(mode="json") for r in result.resources],
            "evidence": {k: e.model_dump(mode="json") for k, e in result.evidence.items()},
            "coverage": [c.model_dump(mode="json") for c in result.coverage],
        },
        sort_keys=True,
    )


def test_global_source_ids_and_exact_field_citations():
    data = responses()
    data["get_role"]["Role"]["MaxSessionDuration"] = 7200
    result, inventory = normalize(data)
    assert {r.identity.physical_id for r in result.resources} == {
        "AROAEXAMPLE",
        "AIPAEXAMPLE",
        "ANPAEXAMPLE",
    }
    assert all(r.identity.region == AwsRegion.GLOBAL for r in result.resources)
    assert all(r.identity.service == AwsService.IAM for r in result.resources)
    assert all(c.status == CoverageStatus.SUCCESS for c in result.coverage)
    role = by_id(result, "AROAEXAMPLE")
    profile = by_id(result, "AIPAEXAMPLE")
    policy = by_id(result, "ANPAEXAMPLE")
    assert role.attributes["attached_policy_arns"] == [POLICY_ARN]
    assert role.attributes["inline_policy_names"] == ["inline"]
    assert role.attributes["max_session_duration"] == 7200
    assert profile.role_arns == [ROLE_ARN]
    checks = [
        (role, "/arn", Op.GET_ROLE, ".Role.Arn"),
        (role, "/trust_document", Op.GET_ROLE, ".Role.AssumeRolePolicyDocument"),
        (role, "/boundary_arn", Op.GET_ROLE, ".Role.PermissionsBoundary.PermissionsBoundaryArn"),
        (role, "/attributes/max_session_duration", Op.GET_ROLE, ".Role.MaxSessionDuration"),
        (role, "/attributes/attached_policy_arns/0", Op.LIST_ATTACHED_ROLE_POLICIES, ".PolicyArn"),
        (role, "/inline_policies/0/document", Op.GET_ROLE_POLICY, ".PolicyDocument"),
        (profile, "/role_arns/0", Op.LIST_INSTANCE_PROFILES, ".Roles[0].Arn"),
        (policy, "/default_version_id", Op.GET_POLICY, ".Policy.DefaultVersionId"),
        (policy, "/document", Op.GET_POLICY_VERSION, ".PolicyVersion.Document"),
    ]
    for resource, pointer, op, suffix in checks:
        evidence = citation(result, resource, pointer)
        assert evidence.configuration_path == inventory.results[op].responses[0].path + suffix
        assert evidence.locator == f"iam:{op}"


def test_encoded_documents_equivalent_and_no_secrets_anywhere():
    decoded, _ = normalize()
    data = responses()
    data["list_roles"]["Roles"][0]["AssumeRolePolicyDocument"] = quote(json.dumps(TRUST))
    data["get_role"]["Role"]["AssumeRolePolicyDocument"] = quote(json.dumps(TRUST))
    data["list_instance_profiles"]["InstanceProfiles"][0]["Roles"][0][
        "AssumeRolePolicyDocument"
    ] = quote(json.dumps(TRUST))
    data["get_role_policy"]["PolicyDocument"] = quote(json.dumps(DOCUMENT))
    data["get_policy_version"]["PolicyVersion"]["Document"] = quote(json.dumps(DOCUMENT))
    encoded, _ = normalize(data)
    assert persisted(encoded) == persisted(decoded)
    serialized = persisted(encoded)
    for forbidden in ("protected-context", "protected-tag", "%7B", "%22"):
        assert forbidden not in serialized
    role = by_id(encoded, "AROAEXAMPLE")
    trust = citation(encoded, role, "/trust_document")
    assert "/Statement/0/Condition/StringEquals/sts:ExternalId" in trust.redacted_paths
    profile = by_id(encoded, "AIPAEXAMPLE")
    whole = encoded.evidence[profile.evidence_ids[0]]
    assert (
        "/Roles/0/AssumeRolePolicyDocument/Statement/0/Condition/StringEquals/sts:ExternalId"
        in whole.redacted_paths
    )
    assert "/Roles/0/Tags/0/Value" in whole.redacted_paths


def test_failed_role_detail_keeps_listed_configuration_and_qualifies_policy_reads():
    data = responses()
    data["get_role"] = EndpointConnectionError(endpoint_url="protected-error")
    result, inventory = normalize(data)
    role = by_id(result, "AROAEXAMPLE")
    assert role.attributes["inline_policy_names"] == ["inline"]
    assert role.attributes["attached_policy_arns"] == [POLICY_ARN]
    assert role.boundary_arn == POLICY_ARN
    assert citation(result, role, "/trust_document").configuration_path == (
        inventory.results[Op.LIST_ROLES].responses[0].path + ".AssumeRolePolicyDocument"
    )
    assert role.attributes["limitations"]
    # Policies referenced only by the unread role detail may be missing.
    assert inventory.results[Op.GET_POLICY].coverage.status == CoverageStatus.INCOMPLETE
    assert inventory.results[Op.GET_POLICY_VERSION].coverage.status == CoverageStatus.INCOMPLETE
    assert "protected-error" not in persisted(result)


def test_multiple_inline_documents_have_distinct_request_evidence():
    data = responses()
    data["list_role_policies"] = {"PolicyNames": ["second", "first"]}
    data["get_role_policy"] = lambda params: {
        **params,
        "PolicyDocument": {"Statement": [{"Sid": params["PolicyName"]}]},
    }
    result, _ = normalize(data)
    role = by_id(result, "AROAEXAMPLE")
    assert [p.name for p in role.inline_policies] == ["first", "second"]
    first = citation(result, role, "/inline_policies/0/document")
    second = citation(result, role, "/inline_policies/1/document")
    assert first.configuration_path != second.configuration_path
    assert first.value["Statement"][0]["Sid"] == "first"
    assert second.value["Statement"][0]["Sid"] == "second"
    assert persisted(result) == persisted(normalize(data)[0])


def test_malformed_document_is_omitted_with_originating_coverage():
    data = responses()
    data["get_role"]["Role"]["AssumeRolePolicyDocument"] = "%7Bprotected-malformed-secret"
    result, _ = normalize(data)
    role = by_id(result, "AROAEXAMPLE")
    assert role.attributes["limitations"]
    assert "/trust_document" not in role.fact_evidence
    coverage = next(c for c in result.coverage if c.operation == Op.GET_ROLE)
    assert coverage.status == CoverageStatus.INCOMPLETE
    assert coverage.unresolved_values
    assert all(u.resource_id == role.id for u in coverage.unresolved_values)
    assert all(
        c.status == CoverageStatus.SUCCESS for c in result.coverage if c.operation != Op.GET_ROLE
    )
    assert "protected-malformed-secret" not in persisted(result)


def test_policy_detail_failure_does_not_invent_managed_policy_identity():
    data = responses()
    data["get_policy"] = EndpointConnectionError(endpoint_url="protected-policy-error")
    result, _ = normalize(data)
    assert not any(r.identity.physical_id == "ANPAEXAMPLE" for r in result.resources)
    role = by_id(result, "AROAEXAMPLE")
    assert role.attributes["attached_policy_arns"] == [POLICY_ARN]
    assert role.boundary_arn == POLICY_ARN
    assert "protected-policy-error" not in persisted(result)


def test_missing_source_id_is_skipped_without_inventing_one():
    data = responses()
    del data["list_roles"]["Roles"][0]["RoleId"]
    result, _ = normalize(data)
    assert not any(r.identity.physical_id == "AROAEXAMPLE" for r in result.resources)
    coverage = next(c for c in result.coverage if c.operation == Op.LIST_ROLES)
    assert coverage.status == CoverageStatus.INCOMPLETE
    assert coverage.unresolved_values
    assert all(u.resource_id is None for u in coverage.unresolved_values)
