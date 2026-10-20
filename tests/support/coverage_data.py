"""Recorded operation coverage for network-path and database-access tests."""

from archdiscov.models.architecture import Coverage
from archdiscov.models.enums import AwsRegion, AwsService
from tests.support.iam_data import ACCOUNT, context

REGION = AwsRegion.EU_WEST_1


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
