"""Load-balancer snapshots for forwarding tests, through the real ELBv2 pipeline."""

from archdiscov.ingestion.elbv2.relationships import merge_resources
from archdiscov.models.architecture import ArchitectureSnapshot, CollectionFailure
from archdiscov.models.enums import AwsService, CoverageStatus
from archdiscov.models.resource_base import ResourceIdentity
from tests.support.elbv2_data import ACCOUNT, GROUP, LB, LISTENER, RULE, responses
from tests.support.elbv2_snapshot import ec2_roots, normalized, snapshot

__all__ = ["GROUP", "LB", "LISTENER", "RULE", "build", "resource_id", "responses"]


def resource_id(arn: str) -> str:
    return ResourceIdentity(
        account_id=ACCOUNT,
        region="us-east-1",
        service=AwsService.ELASTIC_LOAD_BALANCING,
        physical_id=arn,
    ).stable_id


def build(data=None, *, limited: str | None = None, roots=None) -> ArchitectureSnapshot:
    """Normalize `data` (default: an internal ALB forwarding port 80 to instance i-web).

    `limited` names an ELBv2 operation whose recorded coverage is marked failed.
    """
    result = normalized(data)
    if limited is not None:
        result.coverage = [
            record.model_copy(
                update={
                    "status": CoverageStatus.FAILED,
                    "result_count": None,
                    "failures": [CollectionFailure(code="AccessDenied", message="Denied")],
                }
            )
            if record.operation == limited
            else record
            for record in result.coverage
        ]
    tree = merge_resources(ec2_roots() if roots is None else roots, result.resources)
    return snapshot(result, tree)
