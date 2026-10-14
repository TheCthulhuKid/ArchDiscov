from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from archdiscov.ingestion.coverage_updates import with_unresolved_value
from archdiscov.ingestion.database.normalization import _Normalizer as RdsNormalizer
from archdiscov.ingestion.elbv2.normalization import _Normalizer as Elbv2Normalizer
from archdiscov.ingestion.functions.collector import _Normalizer as LambdaNormalizer
from archdiscov.ingestion.iam.normalization import _Normalizer as IamNormalizer
from archdiscov.ingestion.queues.collector import _Collector as SqsCollector
from archdiscov.models.architecture import (
    CollectionFailure,
    Coverage,
    ResourceIdentity,
    UnresolvedValue,
)
from archdiscov.models.enums import AwsRegion, AwsService, CoverageStatus, EvidenceValueStatus
from archdiscov.models.queues import SqsQueue

CALLERS = [LambdaNormalizer, RdsNormalizer, SqsCollector, IamNormalizer, Elbv2Normalizer]
DEDUPLICATING = [IamNormalizer, Elbv2Normalizer]
OP = "ReadConfiguration"
PATH = "Result.Value"


def coverage(status=CoverageStatus.SUCCESS):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Coverage(
        account_id="123456789012",
        region=AwsRegion.US_EAST_1,
        service=AwsService.SQS,
        operation=OP,
        status=status,
        started_at=now,
        completed_at=now,
        result_count=2 if status in (CoverageStatus.SUCCESS, CoverageStatus.INCOMPLETE) else None,
        failures=[CollectionFailure(code="Denied", message="Unavailable")]
        if status == CoverageStatus.FAILED
        else [],
        reason="Existing limitation" if status != CoverageStatus.SUCCESS else None,
    )


def state(outcome):
    return SimpleNamespace(
        coverage={OP: outcome},
        evidence={},
        region=AwsRegion.US_EAST_1,
        snapshot_id=UUID(int=1),
    )


def issue(caller, target, resource=None, path=PATH):
    if caller in DEDUPLICATING:
        caller.issue(target, OP, path, "Service-specific limitation", resource)
    else:
        caller.issue(target, OP, path, resource)


@pytest.mark.parametrize("caller", CALLERS)
@pytest.mark.parametrize(
    "status", [CoverageStatus.SUCCESS, CoverageStatus.INCOMPLETE, CoverageStatus.FAILED]
)
def test_caller_status_counts_metadata_and_duplicate_policy(caller, status):
    original = coverage(status)
    before = original.model_dump()
    target = state(original)
    issue(caller, target)
    issue(caller, target)
    updated = target.coverage[OP]
    assert updated.status == (
        CoverageStatus.INCOMPLETE if status == CoverageStatus.SUCCESS else status
    )
    assert updated.result_count == original.result_count
    assert updated.failures == original.failures
    assert updated.reason == original.reason
    assert updated.started_at == original.started_at
    assert updated.completed_at == original.completed_at
    assert len(updated.unresolved_values) == (1 if caller in DEDUPLICATING else 2)
    assert original.model_dump() == before
    assert Coverage.model_validate(updated.model_dump()) == updated


@pytest.mark.parametrize("caller", CALLERS)
@pytest.mark.parametrize("status", [CoverageStatus.SKIPPED, CoverageStatus.UNSUPPORTED])
def test_caller_unavailable_status_policy(caller, status):
    original = coverage(status)
    target = state(original)
    if caller in DEDUPLICATING:
        issue(caller, target)
    else:
        with pytest.raises(ValidationError):
            issue(caller, target)
    assert target.coverage[OP] is original
    assert original.unresolved_values == []


@pytest.mark.parametrize("caller", DEDUPLICATING)
def test_guarded_callers_keep_resource_limitations_without_coverage(caller):
    target = state(coverage())
    target.coverage.clear()
    resource = SimpleNamespace(id="resource", attributes={})
    issue(caller, target, resource)
    issue(caller, target, resource)
    assert len(resource.attributes["limitations"]) == 1
    assert target.coverage == {}


def test_helper_deduplicates_whole_values_only_and_preserves_order():
    original = coverage()
    first = UnresolvedValue(configuration_path=PATH, reason="First limitation")
    updated = with_unresolved_value(original, first, deduplicate=True)
    assert with_unresolved_value(updated, first.model_copy(), deduplicate=True) is updated
    distinct = [
        UnresolvedValue(configuration_path=PATH, reason="Other limitation"),
        UnresolvedValue(configuration_path="Other.Value", reason=first.reason),
        UnresolvedValue(resource_id="resource", configuration_path=PATH, reason=first.reason),
    ]
    for value in distinct:
        updated = with_unresolved_value(updated, value, deduplicate=True)
    assert updated.unresolved_values == [first, *distinct]
    repeated = with_unresolved_value(updated, first)
    assert repeated.unresolved_values == [first, *distinct, first]
    assert updated.unresolved_values == [first, *distinct]
    assert original.unresolved_values == []


def test_sqs_retains_local_omitted_evidence_and_repeated_limitations():
    target = state(coverage())
    resource = SqsQueue(
        identity=ResourceIdentity(
            account_id="123456789012",
            region=AwsRegion.US_EAST_1,
            service=AwsService.SQS,
            physical_id="arn:aws:sqs:us-east-1:123456789012:queue",
        ),
        arn="arn:aws:sqs:us-east-1:123456789012:queue",
        queue_url="https://sqs.us-east-1.amazonaws.com/123456789012/queue",
    )
    issue(SqsCollector, target, resource)
    issue(SqsCollector, target, resource)
    assert len(resource.attributes["limitations"]) == 2
    assert len(resource.evidence_ids) == 2
    assert len(target.evidence) == 1
    evidence = target.evidence[resource.evidence_ids[0]]
    assert evidence.value_status == EvidenceValueStatus.OMITTED
    assert evidence.resource_id == resource.id
    assert evidence.configuration_path == PATH
    assert evidence.limitation == "Configuration value unavailable or malformed"
    assert all(item.resource_id == resource.id for item in target.coverage[OP].unresolved_values)
