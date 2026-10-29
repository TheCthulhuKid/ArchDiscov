"""ELBv2 snapshots through the real collector and normalizer, merged into an EC2 tree, and the
load-balancer snapshots of the forwarding tests."""

from datetime import UTC, datetime
from uuid import UUID

from archdiscov.ingestion.elbv2.collector import collect_inventory
from archdiscov.ingestion.elbv2.normalization import normalize_inventory
from archdiscov.ingestion.elbv2.relationships import merge_resources, resolve_relationships
from archdiscov.models.architecture import (
    ArchitectureSnapshot,
    CollectionFailure,
    Ec2Instance,
    RegionArchitecture,
    Subnet,
    Vpc,
)
from archdiscov.models.enums import AwsRegion, AwsService, CoverageStatus
from archdiscov.models.resources.network import SecurityGroup
from tests.support.elbv2_data import ACCOUNT, Elbv2Client, Elbv2Session, config, context
from tests.support.ids import stable_id


def identity(identifier):
    return {
        "account_id": ACCOUNT,
        "region": "us-east-1",
        "service": "ec2",
        "physical_id": identifier,
    }


def ec2_roots():
    return [
        Vpc(
            identity=identity("vpc-a"),
            children=[
                Subnet(
                    identity=identity("subnet-a"),
                    children=[Ec2Instance(identity=identity("i-web"))],
                ),
                SecurityGroup(identity=identity("sg-a")),
            ],
        )
    ]


def normalized(data=None):
    session = Elbv2Session(Elbv2Client(data))
    inventory = collect_inventory(session, config(), context(session), AwsRegion.US_EAST_1)
    return normalize_inventory(UUID(int=15), ACCOUNT, AwsRegion.US_EAST_1, inventory)


def snapshot(result, roots):
    links = resolve_relationships(roots, result.evidence)
    return ArchitectureSnapshot(
        schema_version="2",
        snapshot_id=UUID(int=15),
        account_id=ACCOUNT,
        collection_started_at=datetime(2025, 1, 1, tzinfo=UTC),
        collection_completed_at=datetime(2025, 1, 2, tzinfo=UTC),
        source={"kind": "aws"},
        regions={"us-east-1": RegionArchitecture(resources=roots)},
        coverage={"regions": ["us-east-1"], "account_wide": False, "operations": result.coverage},
        dependencies_by_region={"us-east-1": links},
        evidence=result.evidence,
    )


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
