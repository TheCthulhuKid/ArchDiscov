# AWS collection

How `archdiscov collect` reads AWS configuration: discovery, the request model and operation
allowlist, the command and its scope options, and the per-service facts and limits. Exact
permissions are in [AWS permissions](aws-permissions.md).

## AWS discovery

Implemented collection uses boto3 named profiles or configured assume-role access, verifies
accounts through STS, and reads selected regional configuration and IAM once per account.
Pagination, bounded concurrency, retries, an explicit operation allowlist, and partial-result
retention apply. It retrieves configuration metadata, not workload data or secret values.

ALB/NLB listeners/targets, ECS services/tasks and referenced task definitions, Lambda configuration,
RDS/Aurora instances/clusters/subnet groups, and SQS configuration/policies are implemented.
The service-local mappings describe collected facts and deliberately excluded data.

## AWS request model and allowlist

`AwsCollectionRequest` holds one `AwsAccountConfig` per account (unique expected account IDs; one
snapshot per account). Each config names a profile, an expected 12-digit account ID, concrete
unique regions or enabled-region discovery, selected services or `all_services=True`, optional
`bootstrap_region` / `role_arn` / `external_id` / `session_name`, concurrency/call limits, connect and
read timeouts, and `max_attempts`. Explicit IAM-only scope needs no regions. Identity is verified with STS `GetCallerIdentity` on the final
(assumed-role) session before any resource call; a mismatch or failure raises `IngestionError`
(`account_mismatch`, `identity_failed`) and produces no snapshot. Trust principals are never
followed into other accounts.

Only allowlisted operations can be called (`ALLOWED_OPERATIONS` in `archdiscov.ingestion.aws`):
`sts:GetCallerIdentity`, `sts:AssumeRole` (configured roles only), `ec2:DescribeRegions`
(enabled-region discovery), and the twenty-one EC2 inventory read operations
in `Ec2Operation` (steps 12–13 and 50), plus the eight IAM reads in `IamOperation` (step 14) and six
ELB reads in `Elbv2Operation` (step 15), and the configuration reads in `EcsOperation`,
`LambdaOperation`, `RdsOperation`, and `SqsOperation` (step 16). Exact permissions are
published in [the consolidated policy and setup guide](aws-permissions.md), with
resource-scoping constraints, profile/SSO/assume-role examples, and runtime credential
mounts. The policy is checked against the executable allowlist in normal tests.
Other operations raise `operation_not_allowed`
before any client is created. `OperationRunner` paginates, keeps pages gathered before a failure
(`incomplete`), reports first-call failures as `failed`, supports `skipped`/`unsupported`, bounds
concurrency, and emits sanitized diagnostics with no raw exception text. The shared
`DetailRunner` additionally aggregates bounded parameterized reads and handles each
service's pagination convention, retaining successful sibling results after failures.

## AWS configuration collection and the `collect` command

`archdiscov collect` is the first live workflow. It is read-only and collects regional EC2
VPCs, subnets, instances, and network configuration (route tables, internet gateways, NACLs,
security groups and rules, network interfaces, NAT and egress-only internet gateways, peering
connections, VPC endpoints, managed prefix lists, and transit gateways with their attachments and
route tables), plus account-wide IAM roles, instance profiles,
inline policies, and referenced managed policies' default versions. Regional ELBv2 collection
adds ALB/NLB load balancers, listeners, ALB rules, target groups, registrations/health, and tags.
Snapshots are schema `"2"`; attachments and trust references use evidence-backed `attached_to`
dependencies, while observed forwarding uses `forwards_to`. Neither proves granted access or
end-to-end reachability. Mappings, permissions, coverage, redaction, and limitations are in
[docs/network-mapping.md](network-mapping.md) and
[docs/iam-mapping.md](iam-mapping.md), plus
[docs/load-balancing-mapping.md](load-balancing-mapping.md). The CLI accepts `ec2`, `iam`,
`elasticloadbalancing`, `ecs`, `lambda`, `rds`, and `sqs`.
Gateway Load Balancers are explicit unsupported summaries; classic ELB is not collected.

```bash
# Default service is EC2 with explicit region selection.
archdiscov collect --account dev:123456789012 --region us-east-1 --region eu-west-1 \
  --output dev-ec2.json --json
# All seven supported families; discover enabled regions by default.
archdiscov collect --account dev:123456789012 --all-services --output dev-all.json
# Explicit regions override default discovery in all-services mode.
archdiscov collect --account dev:123456789012 --all-services --region us-east-1 \
  --output dev-all-us-east-1.json
# Selective collection can opt into enabled-region discovery too.
archdiscov collect --account dev:123456789012 --service lambda --region-scope enabled \
  --bootstrap-region us-east-1 --output dev-lambda.json
# IAM-only needs no --region.
archdiscov collect --account dev:123456789012 --service iam --output dev-iam.json
# ELB-only requires a concrete region; it does not implicitly collect EC2 or IAM.
archdiscov collect --account dev:123456789012 --region us-east-1 \
  --service elasticloadbalancing --output dev-elbv2.json
# --service is repeatable; combined regional and account-wide collection.
archdiscov collect --account dev:123456789012 --region us-east-1 \
  --service ec2 --service iam --service elasticloadbalancing --output dev-combined.json
# Configuration-only service slices can be selected independently or combined.
archdiscov collect --account dev:123456789012 --region us-east-1 \
  --service ecs --service lambda --service rds --service sqs --output dev-config.json
# Several accounts: repeat --account PROFILE:ACCOUNT_ID[:ROLE_ARN] and give one --output per account, in order.
```

Without `--all-services`, omitting `--service` preserves the EC2 default; explicit services
collect only those selections. `--all-services` conflicts with any explicit `--service`.
It defaults to enabled-region discovery unless repeatable `--region` values select explicit
scope. Selective collection retains explicit scope by default; use `--region-scope enabled`
to discover regions instead. Enabled scope conflicts with any `--region`; explicit regional
collection requires concrete regions, never `global`. IAM-only explicit scope needs none. IAM runs
once per account even when multiple regions are supplied; IAM-only snapshots can have an empty
`regions` mapping with `coverage.account_wide: true`. IAM resources use AWS source IDs in
`global_resources`, including referenced AWS-managed policies scoped to the collection account.

Each account yields its own snapshot file. Outputs must be distinct and are never overwritten
(an existing path is a conflict for that account only). Identity is verified through STS first; a
mismatch produces no snapshot for that account. Failures in one account do not stop the others.

Discovery runs after STS verification and requires `ec2:DescribeRegions` on `"*"`, even
without EC2 inventory. `--bootstrap-region` selects a partition-appropriate endpoint,
not inventory scope. Only supported enabled regions are collected; discovery/bootstrap
failures or unsupported regions fail that account safely rather than silently shrinking
scope. Missing SDK service endpoints produce partial `unsupported` coverage, not empty success.
Supply credentials at runtime through profiles/assume-role configuration, never snapshot files.

The CLI exposes run/account SDK-call budgets, active-account and collector-job limits,
and local runner concurrency; defaults and maxima are listed in
[scope, discovery, and limits](collection-scope.md#limits). Broader scope increases API
traffic, runtime, throttling risk, and possible AWS costs; limits bound concurrency, not
total calls or cost. All-services means seven supported families, not every AWS service,
and collection is not a transactionally consistent point-in-time view.

Readable results include scope lines for services, resolved regions, region mode, and
account-wide IAM. JSON account results expose `collection_scope` from snapshot metadata;
scope is not proof of completeness. Inspect operation coverage and `PARTIAL` outcomes.
Independent collector jobs feed deterministic account assembly and relationship resolution.
Public async collection/persistence APIs and synchronous wrappers are implemented;
cancellation drains running work. Started saves may commit during cancellation, and
committed files remain intact.

- **Containment:** VPC → subnet → instance from observed `VpcId`/`SubnetId`. When a parent is
  not in the collected results, the resource is stored once at the regional root with an
  `attributes.unresolved_parent` explanation; parents are never invented. ELBv2 adds VPC →
  load balancer → listener → rule and VPC → target group, with subnet/security-group attachments
  rather than duplicated resources. ELB-only VPC parents and missing same-scope targets stay
  unresolved; IP ownership is not inferred, and Lambda registrations are external references.
- **Evidence:** every resource cites its whole API item (e.g. `Reservations[0].Instances[0]`);
  facts such as CIDRs, availability zone, instance type, state, and tags cite the exact source
  field. Tag values whose key names a secret are redacted and recorded in `redacted_paths`.
- **Coverage:** EC2 has one record per region and operation; IAM has eight account-wide
  aggregates, and ELBv2 six regional operation aggregates across requests/pages.
  Counts are retained source records, not normalized
  resources. Zero results are `success` with count `0` only with complete prerequisites;
  later-page failures retain earlier pages as `incomplete`. IAM/ELBv2 downstream reads preserve
  known results but mark incomplete discovery, or are `skipped` when no requests can be discovered.
  Missing tag replies are not empty tag maps; empty target-health replies in partial batches
  remain unavailable rather than proving no registrations. Target health is not reachability.
- **Exit codes:** `0` success (the summary says `PARTIAL` when any operation did not succeed),
  `1` filesystem error, `2` invalid request/validation, `3` destination exists, `5` identity or
  other ingestion failure. Required read permissions are listed in
  [docs/network-mapping.md](network-mapping.md), [docs/iam-mapping.md](iam-mapping.md),
  and [docs/load-balancing-mapping.md](load-balancing-mapping.md).

### Compute, database, and queue configuration

Service-local contracts, exact operations/permissions, source paths, and limitations:

| Family | Collected facts | Explicit limits |
| --- | --- | --- |
| [EC2/network](network-mapping.md) | VPC/subnet/instance configuration, routes, rules, ENIs, NAT and egress-only gateways, peering connections, endpoints, prefix lists, transit gateways, attachments | No reachability analysis; nonlocal/missing targets remain explicit |
| [IAM](iam-mapping.md) | Roles, profiles, trust/inline policies, referenced managed policies | Default policy versions only; no users/groups inventory or effective-permission evaluation |
| [ALB/NLB](load-balancing-mapping.md) | Listeners, rules, ordered forwarding, registrations and reported health | No classic ELB; Gateway LB summaries only; health is not reachability and IP ownership is not inferred |
| [ECS](ecs-mapping.md) | Clusters, services, desired-running tasks, referenced task definitions | No stopped-task inventory, workload commands, or environment values |
| [Lambda](lambda-mapping.md) | Function configuration, networking, execution-role references | No function code or environment values |
| [RDS/Aurora](rds-mapping.md) | Instances, clusters, subnet groups, endpoints/ports, networking | No database contents or credentials; configured public flags are not exposure conclusions |
| [SQS](sqs-mapping.md) | Queue configuration, sanitized policies, dead-letter references | No messages or effective-access conclusions |

Each selected service has regional aggregate coverage with retained partial results and
source-owned fact citations. References resolve after all selected inventories are collected;
missing same-scope targets stay unresolved, while known nonlocal ARNs stay external.
Collection does not retrieve queue messages, function code, database contents, workload
commands, or environment-variable values. Role/policy references describe configuration,
not effective access. Schema `1` snapshots remain readable; new output uses schema `2`.
