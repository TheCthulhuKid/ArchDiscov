"""One populated AWS scenario per analyzer, with coverage and schema variants.

The audit tests drive every analyzer kind through `SCENARIOS`, so a new kind has no entry
until its scenario is added here and the audit fails.
"""

from collections.abc import Callable
from dataclasses import dataclass

from archdiscov.models.enums import AnalyzerKind, AwsService
from archdiscov.services import upgrade_snapshot, validate_snapshot
from tests.support import lb_exposure_data, source_data
from tests.support.rds_data import INSTANCE
from tests.support.security_group_data import IDS, variant
from tests.support.tagged_snapshots import tagged
from tests.support.trust_data import build as build_trust
from tests.support.trust_data import role_arn, schema_one, statement

LIMITED_STATUSES = ("failed", "incomplete", "skipped", "unsupported")
SCHEMAS = ("1", "2", "3")


@dataclass(frozen=True)
class Scenario:
    """A schema-2 snapshot with full coverage and the parameters that analyze it."""

    kind: AnalyzerKind
    build: Callable[[], object]
    parameters: dict


def _ownership():
    """A resource without an owner tag, with the one recorded EC2 operation covering it."""
    snapshot = tagged("analysis/ownership-snapshot.json", tags={"Name": "app"})[0]
    data = snapshot.model_dump(mode="json")
    resource = next(iter(snapshot.regions.values())).resources[0].identity
    data["coverage"]["operations"] = [
        {
            "account_id": resource.account_id,
            "region": resource.region.value,
            "service": "ec2",
            "operation": "DescribeInstances",
            "status": "success",
            "result_count": 1,
            "started_at": "2026-10-13T08:00:00Z",
            "completed_at": "2026-10-13T08:00:10Z",
        }
    ]
    return validate_snapshot(data)


def _trust():
    return build_trust(
        {
            "web": [
                statement({"AWS": "210987654321"}, condition=True),
                statement({"AWS": role_arn("api")}),
                statement({"AWS": "*"}, effect="Deny"),
            ],
            "api": [statement({"Service": "ec2.amazonaws.com"})],
        },
        {"work": [statement({"Service": "sns.amazonaws.com"}, condition=True)]},
    )


SCENARIOS = {
    AnalyzerKind.OWNERSHIP: Scenario(AnalyzerKind.OWNERSHIP, _ownership, {}),
    AnalyzerKind.SECURITY_GROUP_IMPACT: Scenario(
        AnalyzerKind.SECURITY_GROUP_IMPACT,
        lambda: validate_snapshot(variant()),
        {"resource_id": IDS["sg-app"], "max_depth": 3},
    ),
    AnalyzerKind.CROSS_ACCOUNT_TRUST: Scenario(AnalyzerKind.CROSS_ACCOUNT_TRUST, _trust, {}),
    AnalyzerKind.DATABASE_ACCESS: Scenario(
        AnalyzerKind.DATABASE_ACCESS,
        lambda: source_data.build(task={}, function={}, coverage=source_data.FULL_COVERAGE),
        {"resource_id": source_data.rid(INSTANCE, AwsService.RDS), "port": 5432},
    ),
    AnalyzerKind.INTERNET_EXPOSURE: Scenario(
        AnalyzerKind.INTERNET_EXPOSURE,
        lambda: lb_exposure_data.build(
            ec2=lb_exposure_data.ec2_items(public_eni=True), coverage=lb_exposure_data.FULL_COVERAGE
        ),
        {"max_depth": 3},
    ),
}


def as_schema(snapshot, schema: str):
    """The same AWS snapshot as schema 1 (local dependencies only), 2, or 3."""
    if schema == "1":
        return schema_one(snapshot)
    return upgrade_snapshot(snapshot) if schema == "3" else snapshot


def with_coverage(snapshot, status: str | None):
    """Every recorded operation set to `status`, or no operation recorded when None."""
    data = snapshot.model_dump(mode="json")
    if status is None:
        data["coverage"]["operations"] = []
    for operation in data["coverage"]["operations"]:
        for field in ("result_count", "reason"):
            operation.pop(field, None)
        operation.update(failures=[], unresolved_values=[], status=status)
        if status == "failed":
            operation["failures"] = [{"code": "AccessDenied", "message": "Denied"}]
        elif status == "incomplete":
            operation.update(result_count=1, reason="Partial collection")
        elif status != "success":
            operation["reason"] = "Not collected"
        else:
            operation["result_count"] = 1
    return validate_snapshot(data)
