"""Recorded operation coverage for network-path and database-access tests."""

from datetime import UTC, datetime

from archdiscov.models.architecture import CollectionFailure, Coverage
from archdiscov.models.enums import AwsRegion, AwsService, CoverageStatus
from tests.support.iam_data import ACCOUNT, context

REGION = AwsRegion.EU_WEST_1
COVERAGE_OPERATION = "ReadConfiguration"


def coverage_record(
    service: AwsService,
    operation: str,
    status: str,
    result_count: int = 0,
    region: AwsRegion = REGION,
    at=None,
) -> Coverage:
    """One operation record: success, failed, incomplete, or anything else (skipped)."""
    now = at or context(None).clock()
    data = {
        "account_id": ACCOUNT,
        "region": region,
        "service": service,
        "operation": operation,
        "status": status,
        "started_at": now,
        "completed_at": now,
    }
    if status == "success":
        data["result_count"] = result_count
    elif status == "failed":
        data["failures"] = [{"code": "AccessDenied", "message": "Denied"}]
    elif status == "incomplete":
        data.update(result_count=1, reason="Pagination stopped")
    else:
        data["reason"] = "Not collected"
    return Coverage.model_validate(data)


def ec2_coverage(operation: str, status: str, result_count: int) -> Coverage:
    return coverage_record(AwsService.EC2, operation, status, result_count)


def coverage(status=CoverageStatus.SUCCESS):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Coverage(
        account_id="123456789012",
        region=AwsRegion.US_EAST_1,
        service=AwsService.SQS,
        operation=COVERAGE_OPERATION,
        status=status,
        started_at=now,
        completed_at=now,
        result_count=2 if status in (CoverageStatus.SUCCESS, CoverageStatus.INCOMPLETE) else None,
        failures=[CollectionFailure(code="Denied", message="Unavailable")]
        if status == CoverageStatus.FAILED
        else [],
        reason="Existing limitation" if status != CoverageStatus.SUCCESS else None,
    )
