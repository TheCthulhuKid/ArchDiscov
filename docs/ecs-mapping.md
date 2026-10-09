### ECS configuration collection

`collect_ecs(session, account, context, snapshot_id, account_id, region)` returns a
`ServiceCollection`: flat typed resources, resource-owned evidence, and exactly one
aggregate coverage entry for each of the seven operations below. The caller owns
snapshot assembly and relationship resolution; this collector creates no dependencies.
The regional client is `ecs`. Calls use the shared bounded `DetailRunner`, account
retry/concurrency configuration, and `nextToken` pagination.

### Permissions and scope

| Required permission | Requests and limits |
| --- | --- |
| `ecs:ListClusters` | All pages in the selected account/region; `nextToken`. |
| `ecs:DescribeClusters` | Observed cluster ARNs, batches of at most 100. |
| `ecs:ListServices` | All pages per observed cluster; `nextToken`. |
| `ecs:DescribeServices` | Observed service ARNs per cluster, batches of at most 10. |
| `ecs:ListTasks` | All pages per observed cluster with `desiredStatus=RUNNING`; `nextToken`. |
| `ecs:DescribeTasks` | Observed task ARNs per cluster, batches of at most 100. |
| `ecs:DescribeTaskDefinition` | Once per distinct revision ARN observed on described services, service deployments, or tasks. |

Only tasks with **desired status RUNNING** are discovered. This is not stopped-task
history or an assertion that every returned task's last status is RUNNING. ECS can
change between list and describe calls. No `ListTaskDefinitions`, container-instance
inventory, tag reads, log reads, workload execution, secret retrieval, or image pulls
are performed. Unreferenced task-definition revisions are outside scope. Session
creation and account identity verification are caller responsibilities.

### Typed mapping

Every model requires `arn`, uses that exact ARN as `identity.physical_id`, and uses
service `ecs`. Unknown optional fields remain `None`, not fabricated empty facts.

| Model / CloudFormation type | Selected configuration |
| --- | --- |
| `EcsCluster` / `AWS::ECS::Cluster` | `clusterArn`, `clusterName`, status, capacity providers, running/pending task counts and active service count. |
| `EcsService` / `AWS::ECS::Service` | `serviceArn`, `serviceName`, `clusterArn` → `cluster_arn`, `taskDefinition` → `task_definition_arn`, status/counts, launch type, scheduling strategy, platform version, `networkConfiguration.awsvpcConfiguration` subnets/security groups/public-IP assignment. |
| `EcsTask` / `AWS::ECS::Task` | `taskArn`, `clusterArn` → `cluster_arn`, `taskDefinitionArn` → `task_definition_arn`, desired/last status, launch type, platform version, availability zone, CPU/memory. ENI attachment details named `subnetId`, `networkInterfaceId`, or `securityGroupId` populate optional `subnet_ids`, `network_interface_ids`, or `security_group_ids`. Security groups are not inferred from ENIs. |
| `EcsTaskDefinition` / `AWS::ECS::TaskDefinition` | Revision ARN, family/revision/status, `executionRoleArn` → `execution_role_arn`, `taskRoleArn` → `task_role_arn`, network mode, CPU/memory, required compatibilities, safe container definitions. |

`EcsContainerDefinition` keeps name, image reference, CPU, memory/reservation,
essential flag, and `EcsPortMapping` entries (container/host port, protocol,
application protocol, container port range). Image references are configuration,
not image content. Task overrides are excluded, so role overrides are not inferred.

### Evidence and limitations

Source selection uses explicit allowlists **before whole-resource evidence is
created**. No environment variables (even innocuously named ones), environment-file
references, commands, entrypoints, health-check commands, overrides, secrets,
repository credentials, log configuration/secret options, Docker labels, arbitrary
new API fields, tags, free-text descriptions, service events, or stopped reasons
are retained. No workload code or data is collected. API failure reason/detail
strings are not retained either. Input responses are not mutated.

Facts cite exact source fields under `Request[<canonical parameters>].Page[n]`,
including original array indexes. Whole-resource evidence contains only the
selected projection, not the original response. Containers have individual image,
CPU/memory, essential and port-field citations in addition to selected container
collection evidence. Service subnet/security-group facts cite their exact source
lists. Task network facts cite original attachment detail values and indexes;
unselected detail entries remain empty projections to preserve source indexes.
For a listed service/task whose detail lacks a cluster ARN, `cluster_arn` cites the
known `ListServices`/`ListTasks` request parameter at `Request[...].cluster`.

Listed cluster/service/task ARNs and observed task-definition references survive
failed or missing details as typed skeletons with identity citations. No ARN is
constructed from names or failure messages. Describe failure arrays, omitted
results, malformed discovery identities, and omitted requested detail identities
qualify coverage while preserving successful entries. Failed or incomplete
discovery qualifies dependent operations; unavailable prerequisites with no known
requests produce skipped coverage, not successful empty inventories. A genuinely
empty successful cluster listing produces seven successful zero-count entries
without making unnecessary detail calls. Counts describe retained response entries;
duplicate discovery observations retain citations but requests/resources are deduplicated.

Coverage is operation-wide: partial discovery may qualify successful sibling detail
requests in that operation. Cluster description failure does not block service/task
listing when the cluster ARN was discovered successfully. Task-definition coverage
depends on both service and task details because those enumerate revision references.
Only the shared relationship resolver may turn cited ARN/network fields into links;
missing targets remain its responsibility.