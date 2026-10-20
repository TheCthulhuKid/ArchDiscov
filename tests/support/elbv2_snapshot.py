"""ELBv2 snapshots through the real collector and normalizer, merged into an EC2 tree."""

from datetime import UTC, datetime
from uuid import UUID

from archdiscov.ingestion.elbv2.collector import collect_inventory
from archdiscov.ingestion.elbv2.normalization import normalize_inventory
from archdiscov.ingestion.elbv2.relationships import resolve_relationships
from archdiscov.models.architecture import (
    ArchitectureSnapshot,
    Ec2Instance,
    RegionArchitecture,
    Subnet,
    Vpc,
)
from archdiscov.models.enums import AwsRegion
from archdiscov.models.resources.network import SecurityGroup
from tests.support.elbv2_data import ACCOUNT, Elbv2Client, Elbv2Session, config, context


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
