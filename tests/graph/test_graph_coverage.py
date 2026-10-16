from dataclasses import FrozenInstanceError, replace

import pytest

from archdiscov.graph import CoverageKey, GraphIndex
from archdiscov.models.cloud.contracts import CloudOperationCoverage
from archdiscov.models.cloud.scope import AwsScope, AzureScope
from archdiscov.models.enums import AwsPartition, CloudPlatform, CoverageStatus
from archdiscov.services.snapshot_access import SnapshotAccess, upgrade_snapshot
from archdiscov.services.snapshots import load_snapshot
from tests.paths import FIXTURES_ROOT

FIXTURES = FIXTURES_ROOT / "snapshots"


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_exact_coverage_context_and_outcomes_across_fixtures(path):
    snapshot = load_snapshot(path)
    before = snapshot.model_dump(mode="json")
    records = SnapshotAccess.from_snapshot(snapshot).coverage.operations
    expected = {CoverageKey.from_record(record): record for record in records}
    index = GraphIndex(snapshot)
    assert set(index.coverage_keys) == set(expected)
    assert index.coverage_keys == tuple(
        sorted(expected, key=lambda key: (key.location or "", key.service, key.operation))
    )
    assert index.coverage_records() == tuple(expected[key] for key in index.coverage_keys)
    for key, record in expected.items():
        assert index.get_coverage(key) == record
    assert snapshot.model_dump(mode="json") == before


@pytest.mark.parametrize("modern", [False, True])
def test_missing_coverage_is_not_successful_zero_or_failure(modern):
    snapshot = load_snapshot(FIXTURES / "partial-coverage.json")
    if modern:
        snapshot = upgrade_snapshot(snapshot)
    index = GraphIndex(snapshot)
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
    assert index.get_coverage(replace(key, operation="describeinstances")) is None


def test_azure_casing_and_full_scope_qualification():
    snapshot = load_snapshot(FIXTURES / "azure.json")
    index = GraphIndex(snapshot)
    key = CoverageKey.from_values(
        index.scope, "eastus", "MICROSOFT.COMPUTE", "fixture.virtual-machines"
    )
    record = index.get_coverage(key)
    assert record.service == "Microsoft.Compute"
    assert record.status == CoverageStatus.INCOMPLETE
    assert record.unresolved_values[0].reason == "Offline fixture does not provide this fact"
    assert key.service == "microsoft.compute"
    assert index.get_coverage(replace(key, service="Microsoft.Compute")) == record
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


def test_aws_scope_partition_and_service_case_remain_exact():
    index = GraphIndex(load_snapshot(FIXTURES / "partial-coverage.json"))
    key = CoverageKey.from_values(index.scope, "eu-west-1", "ec2", "DescribeInstances")
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
    assert index.get_coverage(replace(key, service="EC2")) is None


@pytest.mark.parametrize("cloud", ["aws", "azure"])
def test_scope_wide_coverage_uses_null_location(cloud):
    snapshot = (
        upgrade_snapshot(load_snapshot(FIXTURES / "iam.json"))
        if cloud == "aws"
        else load_snapshot(FIXTURES / "azure.json")
    )
    snapshot.coverage.scope_wide = True
    record = CloudOperationCoverage(
        scope=snapshot.scope,
        location=None,
        service="iam" if cloud == "aws" else "Microsoft.Resources",
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
    assert (
        index.get_coverage(replace(key, location="eastus" if cloud == "azure" else "eu-west-1"))
        is None
    )


@pytest.mark.parametrize(
    "status", [CoverageStatus.INCOMPLETE, CoverageStatus.SKIPPED, CoverageStatus.UNSUPPORTED]
)
@pytest.mark.parametrize("reverse_operations", [False, True])
def test_non_success_outcomes_are_preserved_without_qualification(status, reverse_operations):
    snapshot = upgrade_snapshot(load_snapshot(FIXTURES / "partial-coverage.json"))
    if reverse_operations:
        snapshot.coverage.operations.reverse()
    record = next(
        item for item in snapshot.coverage.operations if item.operation == "DescribeSecurityGroups"
    )
    record.status = status
    record.reason = "Unavailable selected scope"
    if status != CoverageStatus.INCOMPLETE:
        record.result_count = None
    index = GraphIndex(snapshot)
    found = index.get_coverage(CoverageKey.from_record(record))
    assert found.status == status
    assert found.reason == record.reason
    assert found.result_count == record.result_count


def test_coverage_lookups_are_detached_and_order_independent():
    snapshot = load_snapshot(FIXTURES / "azure.json")
    index = GraphIndex(snapshot)
    expected = index.coverage_records()
    snapshot.coverage.operations.reverse()
    assert GraphIndex(snapshot).coverage_records() == expected
    snapshot.coverage.operations.clear()
    for returned in [
        *index.coverage_records(),
        *(index.get_coverage(key) for key in index.coverage_keys),
    ]:
        returned.scope.subscription_id = "22222222-2222-2222-2222-222222222222"
        for failure in returned.failures:
            failure.message = "changed"
        for unresolved in returned.unresolved_values:
            unresolved.reason = "changed"
        returned.failures.clear()
        returned.unresolved_values.clear()
        returned.reason = "changed"
    assert index.coverage_records() == expected
    assert tuple(index.get_coverage(key) for key in index.coverage_keys) == expected


def test_empty_coverage_and_key_ownership():
    snapshot = load_snapshot(FIXTURES / "azure.json")
    snapshot.coverage.operations.clear()
    index = GraphIndex(snapshot)
    assert index.coverage_keys == ()
    assert index.coverage_records() == ()
    scope = index.scope
    key = CoverageKey.from_values(scope, "eastus", "Microsoft.Compute", "list")
    scope.subscription_id = "22222222-2222-2222-2222-222222222222"
    assert index.get_coverage(key) is None
    assert dict(key.scope)["subscription_id"] == index.scope.subscription_id
    with pytest.raises(FrozenInstanceError):
        key.service = "changed"
    direct = CoverageKey(
        CloudPlatform.AZURE, tuple(reversed(key.scope)), "eastus", "MICROSOFT.COMPUTE", "list"
    )
    assert direct == key
    assert hash(direct) == hash(key)


@pytest.mark.parametrize("field", ["service", "operation"])
def test_key_rejects_blank_context(field):
    key = CoverageKey.from_values(AwsScope(account_id="123456789012"), None, "iam", "ListRoles")
    with pytest.raises(ValueError, match="requires a service and operation"):
        replace(key, **{field: " "})
