# AWS permissions and collection boundaries

Live collection uses runtime boto3 named profiles, optionally assumes an explicitly
configured role, and verifies the final session's account before inventory. It does
not discover accounts or follow trust relationships. The executable allowlist is
[`ALLOWED_OPERATIONS`](../src/archdiscov/ingestion/operations.py): **44 operation
pairs**, comprising 42 inventory/discovery reads and two STS operations. Other
operations are rejected before a client call; this application guard is not an IAM
permissions boundary on the credentials themselves.

## Exact operations and baseline inventory policy

For every action below, the suffix after `:` is the exact AWS API operation name;
the prefix is its IAM service namespace. In particular, the boto3 client `elbv2`
uses `elasticloadbalancing` actions, and SQS tags require `sqs:ListQueueTags`, not
`sqs:ListTagsForResource`.

The following machine-readable policy lists **all 42 collector actions**, including
enabled-region discovery. Attach it to the inventory identity (the target role
when using role assumption). It deliberately excludes both STS operations:

- `sts:GetCallerIdentity` is always used to verify identity but **does not require
  an explicit permission grant**. It is an allowlisted operation, not a missing
  action in this policy.
- `sts:AssumeRole` is optional and separate; see the restricted caller policy below.

This is a broad read-inventory baseline, **not a universal least-privilege policy**.
It contains no write or workload-data actions, but metadata and policies can still
be sensitive. Remove unselected service actions for a narrower deployment, and
remove `ec2:DescribeRegions` when using explicit scope only.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadInventory",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeRegions",
        "ec2:DescribeVpcs",
        "ec2:DescribeSubnets",
        "ec2:DescribeInstances",
        "ec2:DescribeRouteTables",
        "ec2:DescribeInternetGateways",
        "ec2:DescribeNetworkAcls",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeSecurityGroupRules",
        "ec2:DescribeNetworkInterfaces",
        "ec2:DescribeNatGateways",
        "ec2:DescribeEgressOnlyInternetGateways",
        "ec2:DescribeVpcPeeringConnections",
        "iam:ListRoles",
        "iam:GetRole",
        "iam:ListInstanceProfiles",
        "iam:ListAttachedRolePolicies",
        "iam:ListRolePolicies",
        "iam:GetRolePolicy",
        "iam:GetPolicy",
        "iam:GetPolicyVersion",
        "elasticloadbalancing:DescribeLoadBalancers",
        "elasticloadbalancing:DescribeListeners",
        "elasticloadbalancing:DescribeRules",
        "elasticloadbalancing:DescribeTargetGroups",
        "elasticloadbalancing:DescribeTargetHealth",
        "elasticloadbalancing:DescribeTags",
        "ecs:ListClusters",
        "ecs:DescribeClusters",
        "ecs:ListServices",
        "ecs:DescribeServices",
        "ecs:ListTasks",
        "ecs:DescribeTasks",
        "ecs:DescribeTaskDefinition",
        "lambda:ListFunctions",
        "lambda:GetFunctionConfiguration",
        "rds:DescribeDBInstances",
        "rds:DescribeDBClusters",
        "rds:DescribeDBSubnetGroups",
        "sqs:ListQueues",
        "sqs:GetQueueAttributes",
        "sqs:ListQueueTags"
      ],
      "Resource": "*"
    }
  ]
}
```

### Resource and condition scoping

Many inventory list/describe actions require `Resource: "*"`; for example, the
EC2 describes above, `iam:ListRoles`, `iam:ListInstanceProfiles`,
`ecs:ListClusters`, `lambda:ListFunctions`, and `sqs:ListQueues`. An ARN in a
request does not by itself prove that the IAM action supports resource-level
authorization. Do not replace the baseline wildcard with a fabricated common
account/region ARN pattern across services.

Where AWS supports it, split detail reads into separate statements scoped to their
actual resource types: for example `iam:GetRole` to role ARNs,
`iam:GetPolicy`/`iam:GetPolicyVersion` to policy ARNs,
`lambda:GetFunctionConfiguration` to function ARNs, and
`sqs:GetQueueAttributes`/`sqs:ListQueueTags` to queue ARNs. Referenced managed IAM
policies can be AWS-managed (`arn:aws:iam::aws:policy/...`) as well as customer-managed;
account-only policy restrictions may deliberately leave these reads incomplete.
Adjust partitions and paths to the deployment, not just the account number.

Check each action's resource types and condition keys in the
[AWS Service Authorization Reference](https://docs.aws.amazon.com/service-authorization/latest/reference/reference_policies_actions-resources-contextkeys.html)
before tightening it. Resource-tag IAM conditions such as `aws:ResourceTag/...`
or service-specific tag keys apply **only where the action/resource supports
them**, not uniformly to enumeration. A blanket tag condition can deny discovery
rather than filter its response. Region conditions likewise need service/action
review, especially for global IAM, STS, and enabled-region discovery. Validate the
resulting coverage; SCPs, permissions boundaries, session policies, resource
policies, and explicit denies can further restrict effective access.

## Named profiles, SSO, and optional role assumption

Authenticate an SSO profile on the host before collection:

```bash
aws sso login --profile development
.venv/bin/python -m archdiscov collect --account development:123456789012 \
  --service ec2 --region us-east-1 --output snapshots/development-ec2.json
```

The profile may itself use AWS SDK assume-role configuration. Archdiscov's
additional assume-role hop is opt-in via `role_arn`, or the third component of
`--account PROFILE:ACCOUNT_ID:ROLE_ARN`:

```bash
.venv/bin/python -m archdiscov collect \
  --account development:123456789012:arn:aws:iam::123456789012:role/InventoryRead \
  --all-services --bootstrap-region us-east-1 --output snapshots/role-inventory.json
```

The expected account ID is the **target** account, checked after assumption. The
source caller needs authorization to assume only the intended role(s), for example:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AssumeInventoryRole",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "arn:aws:iam::123456789012:role/InventoryRead"
    }
  ]
}
```

The target role also needs a trust policy authorizing the intended caller; avoid
wildcard principals and unrestricted role ARNs. Require `sts:ExternalId` in that
trust policy where appropriate (for example third-party access), and supply the
matching runtime `external_id`. The Python configuration supports this without
putting the value in a shell argument; the CLI has no external-ID option:

```python
import os

from archdiscov.ingestion.requests import AwsAccountConfig, AwsCollectionRequest

request = AwsCollectionRequest(
    accounts=(
        AwsAccountConfig(
            profile="development",
            expected_account_id="123456789012",
            role_arn="arn:aws:iam::123456789012:role/InventoryRead",
            external_id=os.environ["INVENTORY_EXTERNAL_ID"],
            session_name="archdiscov",
            all_services=True,
            bootstrap_region="us-east-1",
        ),
    )
)
```

Pass this request to the collection services described in
[collection-scope.md](collection-scope.md). External IDs are trust conditions,
not substitutes for caller authorization; do not record them in requests committed
to version control, snapshots, or diagnostics.

### Container credentials stay at runtime

The development image includes no credentials. For the repository's Compose
service, explicitly mount host configuration read-only and select a profile:

```bash
docker compose run --rm \
  --volume "$HOME/.aws:/home/dev/.aws:ro" --env AWS_PROFILE=development \
  dev archdiscov collect --account development:123456789012 \
  --service iam --output snapshots/container-iam.json
```

The mount exposes credentials and cached tokens to the container; read-only does
not make them nonsecret. Log in and refresh SSO on the host. Some SDK SSO refresh
flows need a writable cache and cannot persist refreshed tokens through this
read-only mount; if necessary, provide a dedicated protected writable cache at
runtime rather than baking tokens into the image. `credential_process` profiles
also require their executable and runtime dependencies inside the container.
Never copy secrets into images, build arguments, committed files, or snapshots.

## Scope and strict failure boundaries

`--service` and `--region` select work, **not an IAM security boundary**. Credentials
may authorize more than the requested scope; constrain the actual identity's IAM
permissions independently. `--all-services` selects seven supported families, not
every AWS service, and cannot be combined with explicit `--service` selections.
IAM is account-wide once per account; `global` is not a requestable region.

Without explicit regions, all-services uses enabled-region discovery; selective
collection opts in with `--region-scope enabled`. Discovery needs
`ec2:DescribeRegions` on `"*"` even when EC2 inventory is not selected. Explicit
regions bypass discovery. `--bootstrap-region` chooses an authentication/discovery
endpoint, not inventory scope. The supported region enum, partition checks,
SDK endpoint availability, concurrency budgets, and detailed discovery contracts
are documented in [collection-scope.md](collection-scope.md#enabled-region-resolution)
and implemented by [`AwsRegion`](../src/archdiscov/models/enums.py).

- Identity failure/mismatch, bootstrap/discovery failure, or an enabled region
  absent from `AwsRegion` fails that account without a snapshot; scope is never
  silently reduced. Other explicitly configured accounts may still succeed.
- Missing SDK service endpoints retain selected scope with `unsupported` coverage;
  known endpoints can still fail at runtime. Endpoint metadata is not a live
  availability guarantee.
- Inventory denials, outages, or pagination failures retain successful pages and
  siblings as partial results (`failed`/`incomplete`/`skipped` as appropriate).
  They are not successful empty inventory. Inspect operation coverage even after
  a successful save; strict scope validation does not mean fail-fast on every read.
- Configuration, attachments, trust policies, and target-health observations are
  not proof of effective access, reachability, or transactional completeness.

## Detailed mappings and offline inputs

Request shapes, collected facts, exclusions, evidence, and redaction live in the
service guides: [EC2/network](network-mapping.md), [IAM](iam-mapping.md),
[ALB/NLB](load-balancing-mapping.md), [ECS](ecs-mapping.md),
[Lambda](lambda-mapping.md), [RDS/Aurora](rds-mapping.md), and [SQS](sqs-mapping.md).

Offline ingestion requires no AWS grants or login. Use the explicit formats in
[raw-source-ingestion.md](raw-source-ingestion.md): snapshot JSON, Terraform
`show -json` state/plan exports, or local CloudFormation JSON/YAML. The
[Terraform state](terraform-state-mapping.md), [Terraform plan](terraform-plan-mapping.md),
and [CloudFormation](cloudformation-mapping.md) guides define supported facts,
unknown/sensitive values, and bounded local intrinsic evaluation. They do not run
Terraform/providers, resolve arbitrary external imports, or deploy infrastructure;
declarations and planned changes are not live AWS observations.