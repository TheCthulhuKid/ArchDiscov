from dataclasses import replace

import pytest

from archdiscov.graph import CoverageKey, GraphIndex
from archdiscov.models.cloud.contracts import CloudOperationCoverage
from archdiscov.models.cloud.scope import AwsScope, AzureScope
from archdiscov.models.enums import AwsPartition, CloudPlatform, CoverageStatus
from archdiscov.models.snapshot_access import upgrade_snapshot
from archdiscov.services.snapshots import load_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"


def test_missing_coverage_is_not_successful_zero_or_failure():
    index = GraphIndex(load_snapshot(FIXTURES / "partial-coverage.json"))
    key = CoverageKey.from_values(index.scope, "eu-west-1", "ec2", "DescribeInstances")
    zero = index.get_coverage(key)
    assert zero.status == CoverageStatus.SUCCESS
    assert zero.result_count == 0
    assert zero.failures == []
    failed = index.get_coverage(replace(key, operation="DescribeRouteTables"))
    assert failed.status == CoverageStatus.FAILED
    assert failed.result_count is None
    assert failed.failures[0].code == "UnauthorizedOperation"
    assert index.get_coverage(replace(key, operation="DescribeNetworkInterfaces")) is None
    assert index.get_coverage(replace(key, location=None)) is None
    assert index.get_coverage(replace(key, location="eu-west-2")) is None
    assert index.get_coverage(replace(key, service="iam")) is None
    assert index.get_coverage(replace(key, service="EC2")) is None
    assert index.get_coverage(replace(key, operation="describeinstances")) is None
    for scope in (
        AwsScope(account_id="999999999999"),
        AwsScope(account_id=index.scope.account_id, partition=AwsPartition.AWS_CN),
    ):
        assert (
            index.get_coverage(
                CoverageKey.from_values(scope, key.location, key.service, key.operation)
            )
            is None
        )


def test_azure_casing_and_full_scope_qualification():
    snapshot = load_snapshot(FIXTURES / "azure.json")
    index = GraphIndex(snapshot)
    key = CoverageKey.from_values(
        index.scope, "eastus", "MICROSOFT.COMPUTE", "fixture.virtual-machines"
    )
    record = index.get_coverage(key)
    assert record.service == "Microsoft.Compute"
    assert record.status == CoverageStatus.INCOMPLETE
    assert key.service == "microsoft.compute"
    assert index.get_coverage(replace(key, service="Microsoft.Compute")) == record
    direct = CoverageKey(
        CloudPlatform.AZURE,
        tuple(reversed(key.scope)),
        key.location,
        "MICROSOFT.COMPUTE",
        key.operation,
    )
    assert direct == key
    assert hash(direct) == hash(key)
    for scope in (
        AzureScope(subscription_id="22222222-2222-2222-2222-222222222222"),
        AzureScope(
            subscription_id=index.scope.subscription_id,
            tenant_id="33333333-3333-3333-3333-333333333333",
        ),
        AwsScope(account_id="123456789012"),
    ):
        assert (
            index.get_coverage(
                CoverageKey.from_values(scope, key.location, key.service, key.operation)
            )
            is None
        )
    assert index.get_coverage(replace(key, location=None)) is None


def test_scope_wide_coverage_uses_null_location():
    snapshot = upgrade_snapshot(load_snapshot(FIXTURES / "iam.json"))
    snapshot.coverage.scope_wide = True
    record = CloudOperationCoverage(
        scope=snapshot.scope,
        location=None,
        service="iam",
        operation="scope-wide-inventory",
        status=CoverageStatus.SUCCESS,
        result_count=0,
        started_at=snapshot.collection_started_at,
        completed_at=snapshot.collection_completed_at,
    )
    snapshot.coverage.operations.append(record)
    index = GraphIndex(snapshot)
    key = CoverageKey.from_record(record)
    assert key in index.coverage_keys
    assert key.location is None
    assert index.get_coverage(key) == record
    assert index.get_coverage(replace(key, location="global")) is None
    assert index.get_coverage(replace(key, location="eu-west-1")) is None


def test_coverage_lookups_are_detached():
    snapshot = load_snapshot(FIXTURES / "azure.json")
    index = GraphIndex(snapshot)
    expected = index.coverage_records()
    snapshot.coverage.operations.clear()
    for returned in [
        *index.coverage_records(),
        *(index.get_coverage(key) for key in index.coverage_keys),
    ]:
        returned.scope.subscription_id = "22222222-2222-2222-2222-222222222222"
        for failure in returned.failures:
            failure.message = "changed"
        returned.unresolved_values.clear()
        returned.reason = "changed"
    assert index.coverage_records() == expected
    assert tuple(index.get_coverage(key) for key in index.coverage_keys) == expected


def test_key_rejects_blank_context():
    key = CoverageKey.from_values(AwsScope(account_id="123456789012"), None, "iam", "ListRoles")
    with pytest.raises(ValueError, match="requires a service and operation"):
        replace(key, service=" ")
