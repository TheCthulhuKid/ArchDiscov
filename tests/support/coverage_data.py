"""Recorded EC2 operation coverage for network-path tests."""

from archdiscov.models.architecture import Coverage
from archdiscov.models.enums import AwsRegion, AwsService
from tests.support.iam_data import ACCOUNT, context

REGION = AwsRegion.EU_WEST_1


def ec2_coverage(operation: str, status: str, result_count: int) -> Coverage:
    """One EC2 operation record: success, failed, incomplete, or anything else (skipped)."""
    now = context(None).clock()
    data = {
        "account_id": ACCOUNT,
        "region": REGION,
        "service": AwsService.EC2,
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
