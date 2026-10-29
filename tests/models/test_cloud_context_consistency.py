import pytest
from pydantic import ValidationError

from archdiscov.models.cloud.context import (
    canonical_service_key,
    evidence_context_matches,
    validate_context,
)
from archdiscov.models.cloud.contracts import CloudCoverage, CloudOperationCoverage
from archdiscov.models.cloud.identity import AzureResourceIdentity, ResourceIdentity
from archdiscov.models.cloud.scope import AwsScope, AzureScope
from archdiscov.models.enums import CloudPlatform

SUBSCRIPTION = "12345678-1234-5678-abcd-123456789abc"


def test_context_and_identity_reject_the_same_malformed_namespaces():
    service = "Microsoft..Network"
    scope = AzureScope(subscription_id=SUBSCRIPTION)
    with pytest.raises(ValidationError):
        AzureResourceIdentity(
            subscription_id=SUBSCRIPTION,
            service=service,
            native_type=f"{service}/widgets",
            logical_address="widget.example",
        )
    with pytest.raises(ValueError, match="Azure service must be a native provider namespace"):
        validate_context(scope, "eastus", service)


def operation(service="Microsoft.Network", **overrides):
    return CloudOperationCoverage.model_validate(
        {
            "scope": AzureScope(subscription_id=SUBSCRIPTION),
            "location": "eastus",
            "service": service,
            "operation": "List",
            "status": "success",
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:00:00Z",
            "result_count": 0,
            **overrides,
        }
    )


def test_case_variant_azure_coverage_services_are_duplicates_but_operations_are_not():
    with pytest.raises(ValidationError, match="coverage operation outcomes must be unique"):
        CloudCoverage(
            scope=AzureScope(subscription_id=SUBSCRIPTION),
            locations=["eastus"],
            scope_wide=False,
            operations=[operation(), operation("microsoft.NETWORK")],
        )
    coverage = CloudCoverage(
        scope=AzureScope(subscription_id=SUBSCRIPTION),
        locations=["eastus"],
        scope_wide=False,
        operations=[operation(), operation("microsoft.NETWORK", operation="list")],
    )
    assert [item.operation for item in coverage.operations] == ["List", "list"]


def test_azure_context_matches_service_case_insensitively_and_preserves_spelling():
    service = "microsoft.NETWORK"
    identity = AzureResourceIdentity(
        subscription_id=SUBSCRIPTION,
        location="eastus",
        service=service,
        native_type=f"{service}/widgets",
        logical_address="widget.example",
    )
    scope = identity.scope
    validate_context(scope, "eastus", service)
    record = operation(service)
    coverage = CloudCoverage(
        scope=scope, locations=["eastus"], scope_wide=False, operations=[record]
    )
    assert identity.model_dump(mode="json")["service"] == service
    assert coverage.model_dump(mode="json")["operations"][0]["service"] == service
    assert evidence_context_matches(identity, scope, scope, "eastus", service.swapcase())
    other = AzureScope(subscription_id="87654321-4321-8765-dcba-cba987654321")
    assert not evidence_context_matches(identity, scope, other, "eastus", service)
    assert not evidence_context_matches(identity, scope, scope, "westus", service)
    assert not evidence_context_matches(identity, scope, scope, "eastus", "Microsoft.Compute")


def test_canonical_service_keys_are_platform_specific():
    assert canonical_service_key(CloudPlatform.AZURE, "Microsoft.NETWORK") == "microsoft.network"
    assert canonical_service_key(CloudPlatform.AWS, "ec2") == "ec2"
    assert canonical_service_key(CloudPlatform.AWS, "EC2") == "EC2"


def test_aws_evidence_context_keeps_service_case_and_scope_wide_location():
    identity = ResourceIdentity(
        account_id="123456789012", region="global", service="iam", logical_address="role.example"
    )
    scope = AwsScope(account_id=identity.account_id)
    assert evidence_context_matches(identity, scope, scope, None, "iam")
    assert not evidence_context_matches(identity, scope, scope, None, "IAM")
    assert not evidence_context_matches(identity, scope, scope, "global", "iam")
