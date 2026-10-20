"""Load-balancer snapshots for forwarding tests, through the real ELBv2 pipeline."""

from archdiscov.ingestion.elbv2.relationships import merge_resources
from archdiscov.models.architecture import ArchitectureSnapshot, CollectionFailure
from archdiscov.models.enums import AwsRegion, AwsService, CoverageStatus
from tests.support.elbv2_data import GROUP, LB, LISTENER, RULE, responses
from tests.support.elbv2_snapshot import ec2_roots, normalized, snapshot
from tests.support.ids import stable_id

__all__ = ["GROUP", "LB", "LISTENER", "RULE", "build", "resource_id", "responses"]


def resource_id(arn: str) -> str:
    return stable_id(arn, AwsRegion.US_EAST_1, AwsService.ELASTIC_LOAD_BALANCING)


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
