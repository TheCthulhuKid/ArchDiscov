### RDS and Aurora configuration mapping

`collect_rds(session, account, context, snapshot_id, account_id, region)` returns a
`ServiceCollection` with flat `resources`, an `evidence` dictionary, and three
operation `coverage` records. Containment and relationship resolution belong to
the shared integration layer, not this collector.

### Permissions and reads

Only these three permissions are needed for this slice:

| Permission | Response collection | Model and identity |
| --- | --- | --- |
| `rds:DescribeDBInstances` | `DBInstances` | `DbInstance`, required source `DBInstanceArn` |
| `rds:DescribeDBClusters` | `DBClusters` | `DbCluster`, required source `DBClusterArn` |
| `rds:DescribeDBSubnetGroups` | `DBSubnetGroups` | `DbSubnetGroup`, required source `DBSubnetGroupArn` |

All three inventories run independently using bounded `DetailRunner` reads and
`pagination="rds"`: response `Marker` becomes the next request's `Marker`.
Describe responses contain the relevant configuration; no per-instance detail
call, database connection/query, Secrets Manager read, or credential retrieval is
performed. Tags are retained only when returned inline as `TagList`; no tag API
permission is required. A subnet-group read failure does not discard instance or
cluster facts, including their explicit subnet-group references.

### Typed facts and references

| Source field | Normalized field |
| --- | --- |
| `DBInstanceIdentifier` / `DBClusterIdentifier` | `identifier` and `name` |
| `DBSubnetGroupName` | subnet group's `group_name` and `name` |
| `DBInstanceStatus` / `Status` / `SubnetGroupStatus` | `status` |
| `Engine`, `EngineVersion` | `engine`, `engine_version` |
| instance `Endpoint.Address`, `Endpoint.Port` | `endpoint_address`, `endpoint_port` |
| cluster `Endpoint`, `Port`, `ReaderEndpoint` | `endpoint_address`, `endpoint_port`, `reader_endpoint_address` |
| instance `DBClusterIdentifier` | `cluster_identifier` (not a synthesized cluster ARN) |
| instance `DBSubnetGroup.DBSubnetGroupName` / cluster `DBSubnetGroup` | `db_subnet_group_name` |
| subnet-group `VpcId` or instance `DBSubnetGroup.VpcId` | `vpc_id` |
| `Subnets[].SubnetIdentifier` (nested in instance `DBSubnetGroup`) | `subnet_ids` |
| `VpcSecurityGroups[].VpcSecurityGroupId` | `security_group_ids` |
| `AssociatedRoles[].RoleArn` | `associated_role_arns` |
| `DBClusterMembers[]` | `members`: `db_instance_identifier`, `is_cluster_writer`, `promotion_tier` |
| instance `DBInstanceClass`, `AvailabilityZone` | `instance_class`, `availability_zone` |
| cluster `AvailabilityZones` | `availability_zones` |
| `StorageEncrypted`, `PubliclyAccessible`, `AllocatedStorage`, `StorageType` | `storage_encrypted`, `publicly_accessible`, `allocated_storage`, `storage_type` |
| `Iops`, `StorageThroughput`, `BackupRetentionPeriod`, `DeletionProtection`, `MultiAZ` | corresponding snake-case typed facts |
| `TagList` | sanitized `attributes.tags` |

Resource types are `AWS::RDS::DBInstance`, `AWS::RDS::DBCluster`, and
`AWS::RDS::DBSubnetGroup`. Identities use the exact source ARN with its observed
account/region and the `rds` service. Missing, malformed, wrong-kind, or
out-of-scope ARNs produce unresolved coverage, never invented resources or ARNs.
The parent resolver can match `cluster_identifier` against `DbCluster.identifier`
and `db_subnet_group_name` against `DbSubnetGroup.name`. Network and IAM joins use
the explicit ID/ARN lists; this collector neither performs nor guesses joins.

Each retained fact has a citation to its exact request, page, resource index,
and field, e.g.
`Request[{}].Page[0].DBInstances[0].DBSubnetGroup.Subnets[1].SubnetIdentifier`.
Normalized lists use their retained indexes in fact pointers; citations preserve
original source indexes even when a malformed preceding entry is omitted.
Nested membership writer flags, endpoint address/port, subnet IDs, security-group
IDs, and role ARNs have separate leaf citations. Whole-item evidence is an
allowlisted projection, not a raw response dump.

### Safety, partial results, and boundaries

Safe fields are selected and validated **before** creating evidence. Master
usernames, passwords, `MasterUserSecret`, authentication/credential material,
unknown response fields, and unknown nested fields are excluded. Endpoint values
must be host names or IPv4 addresses, never URLs, userinfo, query strings, or
connection strings. Ports are strict integers from `1` through `65535`. Generic
tag redaction applies both to normalized tags and AWS tag-pair evidence.

Optional absent facts remain `None`; confirmed empty collections remain `[]`.
Malformed fields add precise unresolved paths to operation coverage and resource
limitations without discarding surrounding resources or valid siblings. A
nonempty collection with no valid entries is unavailable, not confirmed empty.
Malformed inventory collections are incomplete, not successful empty reads.
Valid pages survive later pagination failures, with incomplete coverage; a
failure before any page is failed coverage. Zero-valued storage, retention, and
promotion-tier facts and explicit `False` flags are retained.

Cluster membership, reader endpoints, AZ, encryption, and public-access flags are
configured observations only. They do not establish live membership health,
successful role assumption, database authentication, effective encryption policy,
network reachability, internet exposure, or application-to-database dependencies.
No database contents, SQL, secrets, or executable code are collected.