import json
from copy import deepcopy
from urllib.parse import quote
from uuid import UUID

import pytest
from botocore.exceptions import EndpointConnectionError

from archdiscov.ingestion.iam.collector import collect_inventory
from archdiscov.ingestion.iam.execution import IamResult
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
    result, inventory = normalize()
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
    assert profile.role_arns == [ROLE_ARN]
    checks = [
        (role, "/arn", Op.GET_ROLE, ".Role.Arn"),
        (role, "/trust_document", Op.GET_ROLE, ".Role.AssumeRolePolicyDocument"),
        (role, "/boundary_arn", Op.GET_ROLE, ".Role.PermissionsBoundary.PermissionsBoundaryArn"),
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


@pytest.mark.parametrize("failed", ["get_role", "get_role_policy", "get_policy_version"])
def test_detail_failures_preserve_discovered_resources_and_names(failed):
    data = responses()
    data[failed] = EndpointConnectionError(endpoint_url="protected-error")
    result, inventory = normalize(data)
    role = by_id(result, "AROAEXAMPLE")
    assert role.attributes["inline_policy_names"] == ["inline"]
    assert role.attributes["attached_policy_arns"] == [POLICY_ARN]
    assert role.trust_document is not None
    assert role.boundary_arn == POLICY_ARN
    policy = by_id(result, "ANPAEXAMPLE")
    if failed == "get_role":
        assert citation(result, role, "/trust_document").configuration_path == (
            inventory.results[Op.LIST_ROLES].responses[0].path + ".AssumeRolePolicyDocument"
        )
        assert role.attributes["limitations"]
    if failed == "get_role_policy":
        assert role.inline_policies == []
        assert role.attributes["limitations"]
    if failed == "get_policy_version":
        assert policy.document is None
        assert policy.default_version_id == "v7"
        assert policy.attributes["limitations"]
    assert "protected-error" not in persisted(result)


def test_missing_get_role_fields_do_not_replace_list_role_fields():
    data = responses()
    data["get_role"] = {"Role": {"MaxSessionDuration": 7200}}
    result, inventory = normalize(data)
    role = by_id(result, "AROAEXAMPLE")
    assert role.name == "web"
    assert role.boundary_arn == POLICY_ARN
    assert role.attributes["max_session_duration"] == 7200
    assert citation(result, role, "/trust_document").configuration_path == (
        inventory.results[Op.LIST_ROLES].responses[0].path + ".AssumeRolePolicyDocument"
    )


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


@pytest.mark.parametrize(
    ("method", "field", "op", "physical_id", "pointer"),
    [
        (
            "list_roles",
            ("Roles", 0, "AssumeRolePolicyDocument"),
            Op.LIST_ROLES,
            "AROAEXAMPLE",
            None,
        ),
        (
            "get_role",
            ("Role", "AssumeRolePolicyDocument"),
            Op.GET_ROLE,
            "AROAEXAMPLE",
            "/trust_document",
        ),
        (
            "get_role_policy",
            ("PolicyDocument",),
            Op.GET_ROLE_POLICY,
            "AROAEXAMPLE",
            "/inline_policies/0/document",
        ),
        (
            "get_policy_version",
            ("PolicyVersion", "Document"),
            Op.GET_POLICY_VERSION,
            "ANPAEXAMPLE",
            "/document",
        ),
    ],
    ids=["whole-record", "role-trust", "inline-policy", "policy-version"],
)
def test_malformed_documents_are_omitted_with_originating_coverage(
    method, field, op, physical_id, pointer
):
    data = responses()
    item = data[method]
    for key in field[:-1]:
        item = item[key]
    item[field[-1]] = "%7Bprotected-malformed-secret"
    result, _ = normalize(data)
    resource = by_id(result, physical_id)
    assert resource.attributes["limitations"]
    if pointer:
        assert pointer not in resource.fact_evidence
    coverage = next(c for c in result.coverage if c.operation == op)
    assert coverage.status == CoverageStatus.INCOMPLETE
    assert coverage.unresolved_values
    assert all(u.resource_id == resource.id for u in coverage.unresolved_values)
    assert all(c.status == CoverageStatus.SUCCESS for c in result.coverage if c.operation != op)
    assert "protected-malformed-secret" not in persisted(result)


@pytest.mark.parametrize("identity", ["missing", "ambiguous"])
def test_invalid_role_identity_skipped_without_inventing_source_id(identity):
    data = responses()
    role = data["list_roles"]["Roles"][0]
    if identity == "missing":
        role.pop("RoleId")
    else:
        other = deepcopy(role)
        other["RoleId"] = "AROAOTHER"
        data["list_roles"]["Roles"].append(other)
    result, _ = normalize(data)
    assert not any(r.identity.physical_id in ("AROAEXAMPLE", "AROAOTHER") for r in result.resources)
    coverage = next(c for c in result.coverage if c.operation == Op.LIST_ROLES)
    assert coverage.status == CoverageStatus.INCOMPLETE
    assert coverage.unresolved_values
    assert all(u.resource_id is None for u in coverage.unresolved_values)


def test_policy_detail_failure_does_not_invent_managed_policy_identity():
    data = responses()
    data["get_policy"] = EndpointConnectionError(endpoint_url="protected-policy-error")
    result, _ = normalize(data)
    assert not any(r.identity.physical_id == "ANPAEXAMPLE" for r in result.resources)
    role = by_id(result, "AROAEXAMPLE")
    assert role.attributes["attached_policy_arns"] == [POLICY_ARN]
    assert role.boundary_arn == POLICY_ARN
    assert "protected-policy-error" not in persisted(result)


def test_attachment_partial_failure_preserves_observed_policy_and_exact_page_path():
    data = responses()

    def attached(params):
        if "Marker" in params:
            raise EndpointConnectionError(endpoint_url="protected-page-error")
        return {
            **responses()["list_attached_role_policies"],
            "IsTruncated": True,
            "Marker": "next",
        }

    data["list_attached_role_policies"] = attached
    result, inventory = normalize(data)
    role = by_id(result, "AROAEXAMPLE")
    assert role.attributes["attached_policy_arns"] == [POLICY_ARN]
    assert role.attributes["limitations"]
    assert by_id(result, "ANPAEXAMPLE").document == DOCUMENT
    assert citation(result, role, "/attributes/attached_policy_arns/0").configuration_path == (
        inventory.results[Op.LIST_ATTACHED_ROLE_POLICIES].responses[0].path + ".PolicyArn"
    )
    assert "protected-page-error" not in persisted(result)


def test_blocked_version_discovery_keeps_policy_with_explicit_limitation():
    _, inventory = normalize()
    coverage = inventory.results[Op.GET_POLICY_VERSION].coverage.model_copy(
        update={
            "status": CoverageStatus.SKIPPED,
            "result_count": None,
            "reason": "Discovery blocked",
        }
    )
    inventory.results[Op.GET_POLICY_VERSION] = IamResult([], coverage)
    result = normalize_inventory(UUID(int=14), ACCOUNT, inventory)
    policy = by_id(result, "ANPAEXAMPLE")
    assert policy.document is None
    assert policy.attributes["limitations"]
    assert "/document" not in policy.fact_evidence
    assert next(c for c in result.coverage if c.operation == Op.GET_POLICY_VERSION) == coverage


@pytest.mark.parametrize(
    ("method", "field", "op", "physical_id"),
    [
        ("get_role_policy", "PolicyDocument", Op.GET_ROLE_POLICY, "AROAEXAMPLE"),
        ("get_policy_version", "Document", Op.GET_POLICY_VERSION, "ANPAEXAMPLE"),
    ],
)
def test_successful_response_missing_document_is_incomplete(method, field, op, physical_id):
    data = responses()
    raw = data[method]
    if method == "get_policy_version":
        raw = raw["PolicyVersion"]
    raw.pop(field)
    result, inventory = normalize(data)
    resource = by_id(result, physical_id)
    assert resource.attributes["limitations"]
    coverage = next(c for c in result.coverage if c.operation == op)
    assert coverage.status == CoverageStatus.INCOMPLETE
    assert coverage.unresolved_values[0].configuration_path.endswith("." + field)
    assert inventory.results[op].coverage.status == CoverageStatus.SUCCESS
    assert inventory.results[op].coverage.unresolved_values == []


def test_missing_trust_in_both_sources_is_incomplete_without_empty_document():
    data = responses()
    data["list_roles"]["Roles"][0].pop("AssumeRolePolicyDocument")
    data["get_role"]["Role"].pop("AssumeRolePolicyDocument")
    result, inventory = normalize(data)
    role = by_id(result, "AROAEXAMPLE")
    assert role.trust_document is None
    assert "/trust_document" not in role.fact_evidence
    coverage = next(c for c in result.coverage if c.operation == Op.GET_ROLE)
    assert coverage.status == CoverageStatus.INCOMPLETE
    assert coverage.unresolved_values[0].configuration_path == (
        inventory.results[Op.GET_ROLE].responses[0].path + ".Role.AssumeRolePolicyDocument"
    )


def test_documents_embedded_inside_documents_are_recursively_safe():
    data = responses()
    data["get_role"]["Role"]["AssumeRolePolicyDocument"]["Statement"][0]["Document"] = quote(
        json.dumps(TRUST)
    )
    result, _ = normalize(data)
    role = by_id(result, "AROAEXAMPLE")
    nested = role.trust_document["Statement"][0]["Document"]
    assert isinstance(nested, dict)
    assert nested["Statement"][0]["Condition"]["StringEquals"]["sts:ExternalId"] == "[REDACTED]"
    evidence = citation(result, role, "/trust_document")
    assert (
        "/Statement/0/Document/Statement/0/Condition/StringEquals/sts:ExternalId"
        in evidence.redacted_paths
    )
    assert "protected-context" not in persisted(result)
    assert "%7B" not in persisted(result)


def test_sorted_attachments_and_profile_roles_cite_observed_not_normalized_indexes():
    data = responses()
    data["list_attached_role_policies"]["AttachedPolicies"].insert(
        0, {"PolicyArn": "arn:aws:iam::aws:policy/ZLast"}
    )
    data["get_policy"] = lambda params: {
        "Policy": {
            **responses()["get_policy"]["Policy"],
            "Arn": params["PolicyArn"],
            "PolicyId": "ANPAZLAST" if params["PolicyArn"].endswith("ZLast") else "ANPAEXAMPLE",
        }
    }
    data["list_instance_profiles"]["InstanceProfiles"][0]["Roles"].insert(0, {})
    result, inventory = normalize(data)
    role = by_id(result, "AROAEXAMPLE")
    source = inventory.results[Op.LIST_ATTACHED_ROLE_POLICIES].responses[1]
    assert citation(result, role, "/attributes/attached_policy_arns/0").configuration_path == (
        source.path + ".PolicyArn"
    )
    profile = by_id(result, "AIPAEXAMPLE")
    assert profile.role_arns == [ROLE_ARN]
    assert citation(result, profile, "/role_arns/0").configuration_path.endswith(".Roles[1].Arn")
    assert len([r for r in result.resources if r.identity.physical_id.startswith("ANPA")]) == 2


@pytest.mark.parametrize(
    ("method", "container", "id_field", "op", "physical_id"),
    [
        (
            "list_instance_profiles",
            ("InstanceProfiles", 0),
            "InstanceProfileId",
            Op.LIST_INSTANCE_PROFILES,
            "AIPAEXAMPLE",
        ),
        ("get_policy", ("Policy",), "PolicyId", Op.GET_POLICY, "ANPAEXAMPLE"),
    ],
)
def test_missing_profile_or_policy_source_id_is_skipped(
    method, container, id_field, op, physical_id
):
    data = responses()
    raw = data[method]
    for key in container:
        raw = raw[key]
    raw.pop(id_field)
    result, _ = normalize(data)
    assert not any(r.identity.physical_id == physical_id for r in result.resources)
    coverage = next(c for c in result.coverage if c.operation == op)
    assert coverage.status == CoverageStatus.INCOMPLETE
    assert coverage.unresolved_values[0].resource_id is None
