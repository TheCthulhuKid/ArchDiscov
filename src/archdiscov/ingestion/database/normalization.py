"""Allowlisted source projection and precise citations for RDS configuration facts."""

import re

from ...models.architecture import Coverage, UnresolvedValue
from ...models.database import (
    DbCluster,
    DbClusterMember,
    DbInstance,
    DbSubnetGroup,
    rds_arn_matches,
)
from ...models.enums import AwsService, CoverageStatus
from ...models.sanitization import sanitize_value
from ..configuration_common import ServiceCollection
from ..coverage_updates import with_unresolved_value
from ..evidence_builder import EvidenceBuilder
from ..operations import RdsOperation as Op


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _number(value):
    return type(value) is int and value >= 0


def _boolean(value):
    return type(value) is bool


def _port(value):
    return type(value) is int and 1 <= value <= 65535


def _host(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", value) is not None


def _role(value):
    return (
        isinstance(value, str)
        and re.fullmatch(
            r"arn:aws(?:-[a-z0-9]+)*:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/\-]+",
            value,
        )
        is not None
    )


_SUBNETS = [
    {
        "SubnetIdentifier": _text,
        "SubnetAvailabilityZone": {"Name": _text},
        "SubnetStatus": _text,
    }
]
_SECURITY_GROUPS = [{"VpcSecurityGroupId": _text, "Status": _text}]
_ROLES = [{"RoleArn": _role, "FeatureName": _text, "Status": _text}]
_TAGS = [{"Key": _text, "Value": lambda value: isinstance(value, str)}]
_COMMON = {
    "Engine": _text,
    "EngineVersion": _text,
    "StorageEncrypted": _boolean,
    "PubliclyAccessible": _boolean,
    "AllocatedStorage": _number,
    "StorageType": _text,
    "Iops": _number,
    "StorageThroughput": _number,
    "BackupRetentionPeriod": _number,
    "DeletionProtection": _boolean,
    "MultiAZ": _boolean,
    "VpcSecurityGroups": _SECURITY_GROUPS,
    "AssociatedRoles": _ROLES,
    "TagList": _TAGS,
}
_SPECS = {
    Op.DESCRIBE_DB_INSTANCES: (
        DbInstance,
        "DBInstances",
        "DBInstanceArn",
        "db",
        {
            **_COMMON,
            "DBInstanceIdentifier": _text,
            "DBInstanceClass": _text,
            "DBInstanceStatus": _text,
            "AvailabilityZone": _text,
            "DBClusterIdentifier": _text,
            "Endpoint": {"Address": _host, "Port": _port},
            "DBSubnetGroup": {"DBSubnetGroupName": _text, "VpcId": _text, "Subnets": _SUBNETS},
        },
    ),
    Op.DESCRIBE_DB_CLUSTERS: (
        DbCluster,
        "DBClusters",
        "DBClusterArn",
        "cluster",
        {
            **_COMMON,
            "DBClusterIdentifier": _text,
            "Status": _text,
            "DBSubnetGroup": _text,
            "Endpoint": _host,
            "ReaderEndpoint": _host,
            "Port": _port,
            "AvailabilityZones": [_text],
            "DBClusterMembers": [
                {
                    "DBInstanceIdentifier": _text,
                    "IsClusterWriter": _boolean,
                    "PromotionTier": _number,
                }
            ],
        },
    ),
    Op.DESCRIBE_DB_SUBNET_GROUPS: (
        DbSubnetGroup,
        "DBSubnetGroups",
        "DBSubnetGroupArn",
        "subgrp",
        {
            "DBSubnetGroupName": _text,
            "SubnetGroupStatus": _text,
            "VpcId": _text,
            "Subnets": _SUBNETS,
            "TagList": _TAGS,
        },
    ),
}
_ENGINE_FIELDS = {
    "Engine": "engine",
    "EngineVersion": "engine_version",
    "StorageEncrypted": "storage_encrypted",
    "PubliclyAccessible": "publicly_accessible",
    "AllocatedStorage": "allocated_storage",
    "StorageType": "storage_type",
    "Iops": "iops",
    "StorageThroughput": "storage_throughput",
    "BackupRetentionPeriod": "backup_retention_period",
    "DeletionProtection": "deletion_protection",
    "MultiAZ": "multi_az",
}
_REQUIRED_KEYS = {
    "Subnets": ("SubnetIdentifier",),
    "VpcSecurityGroups": ("VpcSecurityGroupId",),
    "AssociatedRoles": ("RoleArn",),
    "DBClusterMembers": ("DBInstanceIdentifier",),
    "TagList": ("Key", "Value"),
    "Endpoint": ("Address", "Port"),
    "DBSubnetGroup": ("DBSubnetGroupName",),
}


class _Normalizer(EvidenceBuilder):
    def __init__(self, results, snapshot_id, account_id, region):
        super().__init__(snapshot_id, account_id, region, AwsService.RDS)
        self.results = results
        self.coverage = {
            op: result.coverage.model_copy(deep=True) for op, result in results.items()
        }
        self.resources = {}

    def issue(self, op, path, resource=None):
        reason = "Missing or malformed configuration value"
        unresolved = UnresolvedValue(
            resource_id=resource.id if resource is not None else None,
            configuration_path=path,
            reason=reason,
        )
        self.coverage[op] = with_unresolved_value(self.coverage[op], unresolved)
        if resource is not None:
            resource.attributes.setdefault("limitations", []).append(f"{op}: {path}: {reason}")

    def select(self, value, schema, op, path, resource, collection=None):
        # Only valid allowlisted leaves reach evidence, including whole-resource evidence.
        if isinstance(schema, dict):
            if not isinstance(value, dict):
                self.issue(op, path, resource)
                return None
            selected = {}
            for key, child_schema in schema.items():
                if key not in value:
                    if key in _REQUIRED_KEYS.get(collection, ()):
                        self.issue(op, f"{path}.{key}", resource)
                    continue
                child = self.select(value[key], child_schema, op, f"{path}.{key}", resource, key)
                if child is not None:
                    selected[key] = child
            return selected
        if isinstance(schema, list):
            if not isinstance(value, list):
                self.issue(op, path, resource)
                return None
            return [
                self.select(child, schema[0], op, f"{path}[{index}]", resource, collection)
                for index, child in enumerate(value)
            ]
        if not schema(value):
            self.issue(op, path, resource)
            return None
        return value

    def scalar(self, resource, op, item, path, key, field):
        if key in item:
            setattr(resource, field, item[key])
            self.cite(resource, f"/{field}", op, f"{path}.{key}", item[key])

    def entries(self, resource, op, item, path, key, source_field, field):
        if key not in item:
            return
        entries = item[key]
        values = []
        for index, entry in enumerate(entries):
            value = entry.get(source_field) if isinstance(entry, dict) else entry
            if value is None:
                continue
            suffix = f".{source_field}" if isinstance(entry, dict) else ""
            self.cite(
                resource, f"/{field}/{len(values)}", op, f"{path}.{key}[{index}]{suffix}", value
            )
            values.append(value)
        # A nonempty but entirely malformed list is unavailable, not confirmed empty.
        if values or not entries:
            setattr(resource, field, values)
            self.cite(resource, f"/{field}", op, f"{path}.{key}", entries)

    def subnet_group(self, resource, op, item, path):
        self.scalar(resource, op, item, path, "VpcId", "vpc_id")
        self.entries(resource, op, item, path, "Subnets", "SubnetIdentifier", "subnet_ids")

    def members(self, resource, op, item, path):
        if "DBClusterMembers" not in item:
            return
        members = []
        for index, entry in enumerate(item["DBClusterMembers"]):
            if not isinstance(entry, dict) or "DBInstanceIdentifier" not in entry:
                continue
            fields = {}
            for key, field in {
                "DBInstanceIdentifier": "db_instance_identifier",
                "IsClusterWriter": "is_cluster_writer",
                "PromotionTier": "promotion_tier",
            }.items():
                if key in entry:
                    fields[field] = entry[key]
                    self.cite(
                        resource,
                        f"/members/{len(members)}/{field}",
                        op,
                        f"{path}.DBClusterMembers[{index}].{key}",
                        entry[key],
                    )
            members.append(DbClusterMember(**fields))
        if members or not item["DBClusterMembers"]:
            resource.members = members
            self.cite(
                resource, "/members", op, f"{path}.DBClusterMembers", item["DBClusterMembers"]
            )

    def resource(self, op, value, path):
        cls, _, arn_key, kind, schema = _SPECS[op]
        if not isinstance(value, dict):
            self.issue(op, path)
            return
        arn = value.get(arn_key)
        if not rds_arn_matches(arn, kind, self.account_id, self.region):
            self.issue(op, f"{path}.{arn_key}")
            return
        resource = cls(
            identity={
                "account_id": self.account_id,
                "region": self.region,
                "service": AwsService.RDS,
                "physical_id": arn,
            },
            arn=arn,
        )
        if resource.id in self.resources:
            self.issue(op, f"{path}.{arn_key}", self.resources[resource.id])
            return
        self.resources[resource.id] = resource
        item = self.select(value, schema, op, path, resource)
        item[arn_key] = arn
        self.cite(resource, "/arn", op, f"{path}.{arn_key}", arn)
        if cls is DbSubnetGroup:
            for field in ("group_name", "name"):
                self.scalar(resource, op, item, path, "DBSubnetGroupName", field)
            self.scalar(resource, op, item, path, "SubnetGroupStatus", "status")
            self.subnet_group(resource, op, item, path)
        else:
            identifier = "DBInstanceIdentifier" if cls is DbInstance else "DBClusterIdentifier"
            for field in ("identifier", "name"):
                self.scalar(resource, op, item, path, identifier, field)
            self.scalar(
                resource,
                op,
                item,
                path,
                "DBInstanceStatus" if cls is DbInstance else "Status",
                "status",
            )
            for key, field in _ENGINE_FIELDS.items():
                self.scalar(resource, op, item, path, key, field)
            self.entries(
                resource,
                op,
                item,
                path,
                "VpcSecurityGroups",
                "VpcSecurityGroupId",
                "security_group_ids",
            )
            self.entries(
                resource, op, item, path, "AssociatedRoles", "RoleArn", "associated_role_arns"
            )
            if cls is DbInstance:
                for key, field in {
                    "DBInstanceClass": "instance_class",
                    "AvailabilityZone": "availability_zone",
                    "DBClusterIdentifier": "cluster_identifier",
                }.items():
                    self.scalar(resource, op, item, path, key, field)
                if "Endpoint" in item:
                    self.scalar(
                        resource,
                        op,
                        item["Endpoint"],
                        f"{path}.Endpoint",
                        "Address",
                        "endpoint_address",
                    )
                    self.scalar(
                        resource, op, item["Endpoint"], f"{path}.Endpoint", "Port", "endpoint_port"
                    )
                if "DBSubnetGroup" in item:
                    group = item["DBSubnetGroup"]
                    self.scalar(
                        resource,
                        op,
                        group,
                        f"{path}.DBSubnetGroup",
                        "DBSubnetGroupName",
                        "db_subnet_group_name",
                    )
                    self.subnet_group(resource, op, group, f"{path}.DBSubnetGroup")
            else:
                for key, field in {
                    "Endpoint": "endpoint_address",
                    "Port": "endpoint_port",
                    "ReaderEndpoint": "reader_endpoint_address",
                    "DBSubnetGroup": "db_subnet_group_name",
                }.items():
                    self.scalar(resource, op, item, path, key, field)
                self.entries(
                    resource, op, item, path, "AvailabilityZones", None, "availability_zones"
                )
                self.members(resource, op, item, path)
        if "TagList" in item:
            tags = {
                tag["Key"]: tag["Value"]
                for tag in item["TagList"]
                if isinstance(tag, dict) and "Key" in tag and "Value" in tag
            }
            resource.attributes["tags"] = sanitize_value(tags)[0]
            self.cite(resource, "/attributes/tags", op, f"{path}.TagList", item["TagList"])
        resource.evidence_ids.append(self.source_evidence(resource, op, path, item))

    def run(self):
        for op, result in self.results.items():
            key = _SPECS[op][1]
            count = 0
            for response in result.responses:
                path = f"{response.path}.{key}"
                if not isinstance(response.value, dict) or not isinstance(
                    response.value.get(key), list
                ):
                    self.issue(op, path)
                    continue
                for index, item in enumerate(response.value[key]):
                    count += 1
                    self.resource(op, item, f"{path}[{index}]")
            outcome = self.coverage[op]
            if outcome.status in (CoverageStatus.SUCCESS, CoverageStatus.INCOMPLETE):
                self.coverage[op] = Coverage(**{**outcome.model_dump(), "result_count": count})
        return ServiceCollection(
            list(self.resources.values()), self.evidence, list(self.coverage.values())
        )


def normalize(results, snapshot_id, account_id, region):
    return _Normalizer(results, snapshot_id, account_id, region).run()
