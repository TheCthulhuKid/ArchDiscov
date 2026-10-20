"""Fake RDS client data and collection helpers shared by ingestion and analysis tests."""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

from archdiscov.ingestion.adapter import AdapterContext
from archdiscov.ingestion.configuration_common import ServiceCollection
from archdiscov.ingestion.database import collect_rds
from archdiscov.ingestion.requests import AwsAccountConfig
from archdiscov.models.enums import AwsRegion

ACCOUNT = "123456789012"
REGION = AwsRegion.US_EAST_1
PREFIX = f"arn:aws:rds:{REGION}:{ACCOUNT}:"
INSTANCE = PREFIX + "db:writer"
CLUSTER = PREFIX + "cluster:database"
GROUP = PREFIX + "subgrp:private"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/rds-monitoring"
SNAPSHOT = UUID(int=160)


class Session:
    def __init__(self, client):
        self.rds = client
        self.clients = []

    def client(self, name, **kwargs):
        self.clients.append((name, kwargs))
        assert name == "rds"
        return self.rds


class Client:
    def __init__(self, data):
        self.data = data

    def __getattr__(self, name):
        def call(**params):
            value = self.data[name]
            if isinstance(value, Exception):
                raise value
            return deepcopy(value)

        return call


def data():
    return {
        "describe_db_instances": {
            "DBInstances": [
                {
                    "DBInstanceArn": INSTANCE,
                    "DBInstanceIdentifier": "writer",
                    "DBInstanceClass": "db.t4g.medium",
                    "Engine": "aurora-postgresql",
                    "EngineVersion": "16.4",
                    "DBInstanceStatus": "available",
                    "DBClusterIdentifier": "database",
                    "Endpoint": {"Address": "writer.example.rds.amazonaws.com", "Port": 5432},
                    "AvailabilityZone": "us-east-1a",
                    "PubliclyAccessible": False,
                    "StorageEncrypted": True,
                    "AllocatedStorage": 0,
                    "BackupRetentionPeriod": 0,
                    "DBSubnetGroup": {
                        "DBSubnetGroupName": "private",
                        "VpcId": "vpc-a",
                        "Subnets": [{"SubnetIdentifier": "subnet-a"}],
                    },
                    "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-a", "Status": "active"}],
                    "AssociatedRoles": [{"RoleArn": ROLE, "FeatureName": "s3Import"}],
                }
            ]
        },
        "describe_db_clusters": {
            "DBClusters": [
                {
                    "DBClusterArn": CLUSTER,
                    "DBClusterIdentifier": "database",
                    "Engine": "aurora-postgresql",
                    "Status": "available",
                    "Endpoint": "cluster.example.rds.amazonaws.com",
                    "ReaderEndpoint": "reader.example.rds.amazonaws.com",
                    "Port": 5432,
                    "DBSubnetGroup": "private",
                    "DBClusterMembers": [
                        {
                            "DBInstanceIdentifier": "writer",
                            "IsClusterWriter": True,
                            "PromotionTier": 0,
                        }
                    ],
                    "VpcSecurityGroups": [],
                    "AssociatedRoles": [],
                }
            ]
        },
        "describe_db_subnet_groups": {
            "DBSubnetGroups": [
                {
                    "DBSubnetGroupArn": GROUP,
                    "DBSubnetGroupName": "private",
                    "SubnetGroupStatus": "Complete",
                    "VpcId": "vpc-a",
                    "Subnets": [
                        {
                            "SubnetIdentifier": "subnet-a",
                            "SubnetAvailabilityZone": {"Name": "us-east-1a"},
                            "SubnetStatus": "Active",
                        }
                    ],
                }
            ]
        },
    }


def collect(payload=None, client=None):
    session = Session(client or Client(payload or data()))
    account = AwsAccountConfig(
        profile="offline",
        expected_account_id=ACCOUNT,
        regions=(REGION,),
        services=("rds",),
        max_concurrency=1,
    )
    result = collect_rds(
        session,
        account,
        AdapterContext(clock=lambda: datetime(2026, 1, 1, tzinfo=UTC)),
        SNAPSHOT,
        ACCOUNT,
        REGION,
    )
    assert isinstance(result, ServiceCollection)
    assert len(session.clients) == 1
    return result
