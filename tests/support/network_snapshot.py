"""EC2 network snapshots built through the real normalizer, with optional coverage."""

from archdiscov.ingestion.operations import Ec2Operation as Op
from archdiscov.models.architecture import ArchitectureSnapshot, AwsSource, SnapshotCoverage
from archdiscov.models.enums import AwsService
from archdiscov.models.resource_base import ResourceIdentity
from tests.support.coverage_data import REGION, ec2_coverage
from tests.support.iam_data import ACCOUNT, context
from tests.support.network_data import SNAPSHOT_ID, SUBNET, VPC, region


def resource_id(physical_id: str) -> str:
    return ResourceIdentity(
        account_id=ACCOUNT, region=REGION, service=AwsService.EC2, physical_id=physical_id
    ).stable_id


def build_network(
    items: dict[Op, list[dict]],
    *,
    subnets: tuple[str, ...] = ("subnet-1",),
    covered: dict[str, str | None] | None = None,
) -> ArchitectureSnapshot:
    """A VPC with the subnets plus `items`; real citations and association edges.

    `covered` maps an operation name to its coverage status (None records nothing).
    """
    result = region(
        {
            Op.DESCRIBE_VPCS: [VPC],
            Op.DESCRIBE_SUBNETS: [{**SUBNET, "SubnetId": s} for s in subnets],
            **items,
        }
    )
    operations = [
        ec2_coverage(operation, status, len(items.get(Op(operation), [])))
        for operation, status in (covered or {}).items()
        if status
    ]
    return ArchitectureSnapshot(
        schema_version="2",
        snapshot_id=SNAPSHOT_ID,
        account_id=ACCOUNT,
        collection_started_at=context(None).clock(),
        collection_completed_at=context(None).clock(),
        source=AwsSource(profile="test"),
        regions={REGION: result.architecture},
        coverage=SnapshotCoverage(regions=[REGION], account_wide=False, operations=operations),
        dependencies_by_region={REGION: result.dependencies} if result.dependencies else {},
        evidence=result.evidence,
    )
