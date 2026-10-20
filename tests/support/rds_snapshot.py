"""RDS snapshots through the real collector and relationship resolution."""

from archdiscov.ingestion.configuration_relationships import merge_resources, resolve_relationships
from archdiscov.models.architecture import (
    ArchitectureSnapshot,
    RegionArchitecture,
    ResourceIdentity,
    SecurityGroup,
    SnapshotCoverage,
    Subnet,
    Vpc,
)
from archdiscov.models.enums import AwsService
from tests.support.ids import stable_id
from tests.support.rds_data import ACCOUNT, REGION, SNAPSHOT, collect, data


def ec2_identity(physical_id):
    return ResourceIdentity(
        account_id=ACCOUNT, region=REGION, service=AwsService.EC2, physical_id=physical_id
    )


def ec2_roots(subnets=("subnet-a",), groups=("sg-a",), vpc="vpc-a"):
    return [
        Vpc(
            identity=ec2_identity(vpc),
            children=[
                *(Subnet(identity=ec2_identity(s)) for s in subnets),
                *(SecurityGroup(identity=ec2_identity(g)) for g in groups),
            ],
        )
    ]


def resource_id(physical_id, service=AwsService.EC2):
    return stable_id(physical_id, REGION, service)


def build(payload=None, *, roots=None, status=None) -> ArchitectureSnapshot:
    """Collect RDS `payload` (default: one Aurora instance, its cluster, and subnet group)."""
    result = collect(payload or data())
    tree = ec2_roots() if roots is None else roots
    merge_resources(tree, result.resources)
    links = resolve_relationships(tree, result.evidence)
    return ArchitectureSnapshot(
        schema_version="2",
        snapshot_id=SNAPSHOT,
        account_id=ACCOUNT,
        collection_started_at=result.coverage[0].started_at,
        collection_completed_at=result.coverage[0].completed_at,
        source={"kind": "aws"},
        regions={REGION: RegionArchitecture(resources=tree)},
        coverage=SnapshotCoverage(regions=[REGION], account_wide=False, operations=result.coverage),
        dependencies_by_region=links,
        evidence=result.evidence,
    )
