import pytest
from pydantic import TypeAdapter, ValidationError

from archdiscov.models.enums import CoverageStatus, QueryLimitationKind
from archdiscov.models.query_limitations import (
    QueryCoverageReference,
    QueryExternalTargetLimitation,
    QueryLimitation,
    QueryLimitedCoverageLimitation,
    QueryMissingCoverageLimitation,
    QueryProvenanceLimitation,
    QueryUnresolvedTargetLimitation,
)


@pytest.mark.parametrize(
    "scope,location,service",
    [
        (
            {"platform": "aws", "account_id": "123456789012"},
            "eu-west-1",
            "ec2",
        ),
        (
            {"platform": "aws", "account_id": "123456789012"},
            None,
            "iam",
        ),
        (
            {
                "platform": "azure",
                "subscription_id": "11111111-1111-1111-1111-111111111111",
            },
            "eastus",
            "Microsoft.Compute",
        ),
    ],
)
def test_coverage_reference_round_trip(scope, location, service):
    reference = QueryCoverageReference.model_validate(
        {
            "scope": scope,
            "location": location,
            "service": service,
            "operation": "example.operation",
        }
    )

    assert reference.scope.model_dump(mode="json")["platform"] == scope["platform"]
    assert reference.location == location
    assert reference.service == service
    assert reference.operation == "example.operation"
    assert QueryCoverageReference.model_validate_json(reference.model_dump_json()) == reference


def test_coverage_reference_rejects_global_location_sentinel():
    with pytest.raises(
        ValidationError,
        match="scope-wide placement uses null location, not global",
    ) as caught:
        QueryCoverageReference.model_validate(
            {
                "scope": {"platform": "aws", "account_id": "123456789012"},
                "location": "global",
                "service": "iam",
                "operation": "ListRoles",
            }
        )
    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ()
    assert errors[0]["type"] == "value_error"


def test_missing_coverage_limitation_round_trip():
    limitation = QueryMissingCoverageLimitation.model_validate(
        {
            "coverage": {
                "scope": {"platform": "aws", "account_id": "123456789012"},
                "location": "eu-west-1",
                "service": "ec2",
                "operation": "DescribeInstances",
            },
            "message": "No coverage record is available for this operation.",
        }
    )

    assert limitation.kind == QueryLimitationKind.COVERAGE_MISSING
    assert limitation.coverage.operation == "DescribeInstances"
    assert limitation.message == "No coverage record is available for this operation."
    assert (
        QueryMissingCoverageLimitation.model_validate_json(limitation.model_dump_json())
        == limitation
    )


def test_missing_coverage_limitation_rejects_status():
    with pytest.raises(ValidationError) as caught:
        QueryMissingCoverageLimitation.model_validate(
            {
                "coverage": {
                    "scope": {"platform": "aws", "account_id": "123456789012"},
                    "service": "iam",
                    "operation": "ListRoles",
                },
                "message": "No coverage record is available.",
                "status": "failed",
            }
        )

    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("status",)
    assert errors[0]["type"] == "extra_forbidden"


def test_missing_coverage_limitation_validates_nested_context():
    with pytest.raises(
        ValidationError,
        match="scope-wide placement uses null location, not global",
    ) as caught:
        QueryMissingCoverageLimitation.model_validate(
            {
                "coverage": {
                    "scope": {"platform": "aws", "account_id": "123456789012"},
                    "location": "global",
                    "service": "iam",
                    "operation": "ListRoles",
                },
                "message": "No coverage record is available.",
            }
        )
    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("coverage",)


@pytest.mark.parametrize(
    "status",
    [
        CoverageStatus.FAILED,
        CoverageStatus.INCOMPLETE,
        CoverageStatus.SKIPPED,
        CoverageStatus.UNSUPPORTED,
    ],
)
def test_limited_coverage_limitation_round_trip(status):
    limitation = QueryLimitedCoverageLimitation.model_validate(
        {
            "coverage": {
                "scope": {"platform": "aws", "account_id": "123456789012"},
                "service": "iam",
                "operation": "ListRoles",
            },
            "status": status,
            "message": "Recorded collection coverage is limited.",
        }
    )

    assert limitation.kind == QueryLimitationKind.COVERAGE_LIMITED
    assert limitation.status == status
    assert limitation.coverage.operation == "ListRoles"
    assert (
        QueryLimitedCoverageLimitation.model_validate_json(limitation.model_dump_json())
        == limitation
    )


def test_limited_coverage_limitation_rejects_success():
    with pytest.raises(ValidationError) as caught:
        QueryLimitedCoverageLimitation.model_validate(
            {
                "coverage": {
                    "scope": {"platform": "aws", "account_id": "123456789012"},
                    "service": "iam",
                    "operation": "ListRoles",
                },
                "status": "success",
                "message": "Recorded collection coverage is limited.",
            }
        )
    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("status",)
    assert errors[0]["type"] == "literal_error"


def test_provenance_limitation_round_trip():
    limitation = QueryProvenanceLimitation(
        resource_id="resource-1",
        fact_path="/attributes/tags",
        message="Tag provenance is unavailable.",
    )

    assert limitation.kind == QueryLimitationKind.PROVENANCE_UNAVAILABLE
    assert limitation.resource_id == "resource-1"
    assert limitation.fact_path == "/attributes/tags"
    assert limitation.message == "Tag provenance is unavailable."
    assert QueryProvenanceLimitation.model_validate_json(limitation.model_dump_json()) == limitation


@pytest.mark.parametrize("fact_path", ["attributes/tags", "/tags/~2key"])
def test_provenance_limitation_rejects_invalid_pointer(fact_path):
    with pytest.raises(ValidationError) as caught:
        QueryProvenanceLimitation(
            resource_id="resource-1",
            fact_path=fact_path,
            message="Fact provenance is unavailable.",
        )

    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("fact_path",)
    assert errors[0]["type"] == "string_pattern_mismatch"


@pytest.mark.parametrize(
    "scope,identifier",
    [
        (
            {"platform": "aws", "account_id": "999999999999"},
            "external-resource",
        ),
        (
            {
                "platform": "azure",
                "subscription_id": "11111111-1111-1111-1111-111111111111",
            },
            "external-azure-resource",
        ),
        ({"platform": "aws"}, "unknown-account-resource"),
    ],
)
def test_external_target_limitation_round_trip(scope, identifier):
    limitation = QueryExternalTargetLimitation.model_validate(
        {
            "terminal_id": "terminal-1",
            "edge_id": "edge-1",
            "target": {
                "kind": "external",
                "scope": scope,
                "identifier": identifier,
            },
            "message": "Traversal ends at a recorded external target.",
        }
    )

    assert limitation.kind == QueryLimitationKind.EXTERNAL_TARGET
    assert limitation.terminal_id == "terminal-1"
    assert limitation.edge_id == "edge-1"
    target_scope = limitation.target.scope.model_dump(mode="json")
    for key, value in scope.items():
        assert target_scope[key] == value
    if scope == {"platform": "aws"}:
        assert target_scope["account_id"] is None
    assert limitation.target.identifier == identifier
    assert (
        QueryExternalTargetLimitation.model_validate_json(limitation.model_dump_json())
        == limitation
    )


def test_external_target_limitation_requires_originating_edge():
    with pytest.raises(ValidationError) as caught:
        QueryExternalTargetLimitation.model_validate(
            {
                "terminal_id": "terminal-1",
                "target": {
                    "kind": "external",
                    "scope": {"platform": "aws"},
                    "identifier": "external-resource",
                },
                "message": "Traversal ends at an external target.",
            }
        )

    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("edge_id",)
    assert errors[0]["type"] == "missing"


def test_unresolved_target_limitation_round_trip():
    limitation = QueryUnresolvedTargetLimitation.model_validate(
        {
            "terminal_id": "terminal-1",
            "edge_id": "edge-1",
            "target": {
                "kind": "unresolved",
                "reason": "Referenced resource was not collected.",
                "source_path": "Instances[0].SubnetId",
            },
            "message": "Traversal ends at an unresolved target.",
        }
    )

    assert limitation.kind == QueryLimitationKind.UNRESOLVED_TARGET
    assert limitation.terminal_id == "terminal-1"
    assert limitation.edge_id == "edge-1"
    assert limitation.target.reason == "Referenced resource was not collected."
    assert limitation.target.source_path == "Instances[0].SubnetId"
    assert (
        QueryUnresolvedTargetLimitation.model_validate_json(limitation.model_dump_json())
        == limitation
    )


@pytest.mark.parametrize("field", ["reason", "source_path"])
def test_unresolved_target_limitation_requires_target_context(field):
    target = {
        "kind": "unresolved",
        "reason": "Referenced resource was not collected.",
        "source_path": "Instances[0].SubnetId",
    }
    del target[field]

    with pytest.raises(ValidationError) as caught:
        QueryUnresolvedTargetLimitation.model_validate(
            {
                "terminal_id": "terminal-1",
                "edge_id": "edge-1",
                "target": target,
                "message": "Traversal ends at an unresolved target.",
            }
        )
    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("target", field)
    assert errors[0]["type"] == "missing"


@pytest.mark.parametrize(
    "kind,context,expected_class",
    [
        (
            "coverage_missing",
            {
                "coverage": {
                    "scope": {"platform": "aws", "account_id": "123456789012"},
                    "service": "iam",
                    "operation": "ListRoles",
                }
            },
            QueryMissingCoverageLimitation,
        ),
        (
            "coverage_limited",
            {
                "coverage": {
                    "scope": {"platform": "aws", "account_id": "123456789012"},
                    "service": "iam",
                    "operation": "ListRoles",
                },
                "status": "incomplete",
            },
            QueryLimitedCoverageLimitation,
        ),
        (
            "provenance_unavailable",
            {"resource_id": "resource-1", "fact_path": "/attributes/tags"},
            QueryProvenanceLimitation,
        ),
        (
            "external_target",
            {
                "terminal_id": "terminal-1",
                "edge_id": "edge-1",
                "target": {
                    "kind": "external",
                    "scope": {"platform": "aws"},
                    "identifier": "external-resource",
                },
            },
            QueryExternalTargetLimitation,
        ),
        (
            "unresolved_target",
            {
                "terminal_id": "terminal-1",
                "edge_id": "edge-1",
                "target": {
                    "kind": "unresolved",
                    "reason": "Referenced resource was not collected.",
                    "source_path": "Instances[0].SubnetId",
                },
            },
            QueryUnresolvedTargetLimitation,
        ),
    ],
)
def test_limitation_union_selects_variant(kind, context, expected_class):
    adapter = TypeAdapter(QueryLimitation)
    data = {"kind": kind, "message": "Recorded query limitation.", **context}

    limitation = adapter.validate_python(data)

    assert type(limitation) is expected_class
    assert limitation == expected_class.model_validate(data)
    restored = adapter.validate_json(adapter.dump_json(limitation))
    assert type(restored) is expected_class
    assert restored == limitation


@pytest.mark.parametrize(
    "data,error_type",
    [
        ({"message": "Missing kind."}, "union_tag_not_found"),
        ({"kind": "invalid", "message": "Unknown kind."}, "union_tag_invalid"),
    ],
)
def test_limitation_union_rejects_invalid_discriminator(data, error_type):
    with pytest.raises(ValidationError) as caught:
        TypeAdapter(QueryLimitation).validate_python(data)

    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ()
    assert errors[0]["type"] == error_type
