# Snapshot model and format

The snapshot contracts every source adapter writes and every service reads: the resource
hierarchy, schemas `1`–`3`, evidence and fact citations, collection coverage, sanitization,
identities, and the Python services that load, validate, and save snapshots. Schema `3` is
described in full in [cloud snapshots](cloud-snapshots.md).

- Organize the application into ingestion adapters, normalized models, graph queries, deterministic analyzers, provider adapters, and CLI presentation.
- Define Pydantic models for `AccountArchitecture`, `RegionArchitecture`, typed resources, `Dependency`, `Evidence`, `Coverage`, `Finding`, and `Answer`. Version serialized snapshots.
- Root each `ArchitectureSnapshot` in one account's ID, name, and metadata. Its `regions` mapping is keyed by AWS region name and contains typed resource lists. Store account-wide resources such as IAM roles separately in `global_resources`; retain unsupported types through an explicit `UnknownResource` fallback.
- Give every resource a generic recursive `children` list inherited from `Resource`, along with its ID, name, type, attributes, and evidence references. Specific models add validated fields such as VPC CIDRs or an EC2 instance type. Regions hold top-level resources; any resource can contain further resources.
- Use the tree for containment, such as VPC → subnet → EC2 instance. Keep VPC-wide and multi-subnet resources as direct VPC children. Store each resource once and express other attachments through dependencies. The generic model validates resource attributes without enforcing AWS-specific parent/child rules.
- Keep dependencies grouped by their source region after the architecture fields, followed by evidence. Use `global` for dependencies originating from account-wide resources. Multi-account collection produces one snapshot per account.
- Persist snapshots as local JSON documents with sanitized source evidence. Build graph indexes in memory; no database or graph server is required initially.
- Give resources stable identities using account, region, service, and AWS identifier. Keep IaC logical addresses when physical identifiers are unavailable.
- Attach evidence references to facts and relationships. Evidence records identify the snapshot, source operation or file, resource, configuration path, and relevant value.
- Track collection coverage by account, region, service, and operation, including failures, unsupported resources, timestamps, and unresolved values.
- Keep live observations, Terraform state, Terraform planned changes, and CloudFormation declarations distinguishable. Compare explicitly selected snapshots; match physical identifiers first and report unmatched resources without guessing from names.

These account/region examples describe the legacy AWS contract. Schema `3` uses
`CloudSnapshot`, an explicit AWS or Azure scope, location-keyed `regions`, separate
`scope_resources`, and flat dependencies. `SnapshotAccess` supplies a common detached
read-only view for both contracts, without graph inference or file migration. See
[cloud snapshots](cloud-snapshots.md) for identities, provenance, and extension rules.

## Resource hierarchy

Each legacy AWS snapshot is rooted in one account. Schema `3` instead has an explicit
cloud scope. Regions contain top-level resources, and every resource can contain further
resources through its `children` list; the following example is AWS:

```text
Account snapshot
├── account_id, name, metadata
├── global_resources
│   └── IAM roles and policies
└── regions
    └── eu-west-1
        └── resources
            ├── VPC
            │   └── children
            │       ├── Subnet
            │       │   └── children
            │       │       └── EC2 instance
            │       ├── Security group
            │       ├── Load balancer
            │       └── Database
            ├── SQS queue
            └── Lambda function outside a VPC
```

VPC-wide and multi-subnet resources appear once under the VPC; dependencies record their subnet attachments. Dependencies and evidence remain separate fields on the account snapshot. The service examples shown have dedicated models; unsupported types use `UnknownResource`, and missing parents are not synthesized.

`Dependency.relationship` uses `DependencyRelationship`: `attached_to`, `forwards_to`, or
`assumes_role`. These normalized relationship kinds serialize as strings in JSON; unsupported
values are rejected. Extend the enum when introducing another relationship kind.

## Snapshot format

All supported schemas require an explicit UUID `snapshot_id`, a timezone-aware collection start/end,
one typed `source`, and `coverage` describing the requested scope. Collection completion
must not precede its start. The optional source export timestamp does not replace the
collection window: it is `source.exported_at` in legacy schemas and
`source.provenance.exported_at` in schema `3`.

| Schema | Model and scope | Compatibility |
| --- | --- | --- |
| `1` | `ArchitectureSnapshot`, AWS account/regions | Strict local dependencies; existing wire format and stable IDs retained |
| `2` | `ArchitectureSnapshot`, AWS account/regions | External/unresolved targets and Terraform-plan lifecycle; AWS collectors and raw importers still emit this schema |
| `3` | `CloudSnapshot`, AWS account/partition or Azure subscription/optional tenant | Explicit platform/provenance, locations, scope-wide resources, context-bearing evidence and coverage |

Public `validate_snapshot`, `load_snapshot`, `save_snapshot`, and snapshot-format
`ingest_snapshot` support all three. Direct `ArchitectureSnapshot` validation still
accepts only `1`/`2` and rejects Azure and `cloud_unknown`. Conversion is explicit through
`upgrade_snapshot`; [the cloud guide](cloud-snapshots.md) documents schema `3`.

Each snapshot represents one scope and one source kind. The following legacy AWS
`source.kind` table also describes schema `3`'s `source.provenance.kind`, except that
schema `3` additionally supports `azure_fixture` with a required offline fixture `label`:

| `source.kind` | Meaning | Source-specific fields |
| --- | --- | --- |
| `aws` | Live observations | Optional `profile`, `role_arn` |
| `terraform_state` | Exported state, not live observations | Required `path`, `format_version`; optional `terraform_version`, `workspace`, `lineage`, nonnegative integer `serial` |
| `terraform_plan` | Planned architecture, not applied state | Required `path`, `format_version`; optional `terraform_version`, `workspace` |
| `cloudformation` | Declarations, not deployed observations | Required `path`; optional `stack_id`, `stack_name`, `template_format_version` |
| `mock` | Synthetic test input | Required `label` |

Source kinds cannot be mixed; compare separate snapshots explicitly. Source-specific fields
are validated rather than hidden in generic `metadata`. File paths identify input provenance,
not permission to open files. Optional profile/role fields identify collection context, not secrets.
`source.kind` uses `SnapshotSourceKind`, with a fixed enum literal for each source model
to preserve discriminated validation. `Evidence.source` uses the separate `SourceType` enum:
`aws`, `terraform`, `cloudformation`, `mock`, or `azure` (schema `3`). Evidence groups Terraform as one source family,
while snapshot kinds distinguish state from plans. Both enums serialize as the existing JSON strings.
When supplied, `role_arn` must be an IAM role ARN such as
`arn:aws:iam::123456789012:role/team/Discovery`. Validation requires an empty region,
a 12-digit account ID, and IAM role-name/path character and length constraints. AWS partition
names such as `aws-cn` and `aws-us-gov` are supported; user and STS assumed-role session ARNs
are not role ARNs. This validates syntax only, not role existence or permission to assume it.

In legacy AWS schemas, `coverage.regions` lists unique requested regions, and `coverage.account_wide` explicitly
requests account-wide collection. At least one region or account-wide scope is required.
`global` is reserved for account-wide resource identity and dependency grouping, not a regional
scope entry. Regional contents must be within the requested regions, and global resources
require account-wide scope. Requested scope is **not proof of successful collection**:
an absent or empty region does not distinguish zero results from failure. Operation-level
outcomes are recorded separately in `coverage.operations`; an absent operation record means
there is no claim about that operation's collection outcome.

## Evidence, fact citations, and collection coverage

Each `Evidence` record requires `snapshot_id`, stable `resource_id`, source family, `locator`
(API operation or input file), and `configuration_path` (the exact source field or rule).
Evidence IDs remain the keys of the snapshot's `evidence` mapping. Resource-level
`evidence_ids` provide broad provenance; `Resource.fact_evidence` maps **resource-relative
JSON Pointers** to nonempty evidence-ID lists for individual normalized fields or rules.
For example, `/attributes/ingress/0` cites a whole rule, while
`/attributes/ingress/0/from_port` cites one field. JSON Pointer escaping uses `~0` for `~`
and `~1` for `/`; dotted model paths are not accepted. Keep source-specific paths in evidence,
not in the normalized fact map. Relationships continue to cite `Dependency.evidence_ids`.
Snapshot validation checks every evidence record, including uncited records: its UUID must
match the snapshot and its owner must be a stored resource. Evidence-map keys and reference
IDs must be nonblank. Resource and fact citations must resolve to evidence owned by the
citing resource; fact citations need not also appear in the broad `evidence_ids` list.
Fact pointers resolve against the resource's JSON-mode representation, including subtype
fields. Dictionary keys must exist; array indices must be canonical nonnegative integers
within bounds (no `-`, negative indices, or leading zeros). Traversal through scalars is
rejected, but a present `null` is a valid target.

`coverage.operations` contains one aggregate `Coverage` per account, region, service, and
operation. Adapters aggregate pagination and retries into that record, rather than recording
each page as a separate outcome. Account must match the snapshot; region must be requested
(or `global` with `account_wide: true`). Timezone-aware `started_at` and `completed_at` must
fall within the snapshot collection window. `result_count` counts source records retained
for that operation, not the total normalized resource tree.

| `status` | Required outcome data | Interpretation |
| --- | --- | --- |
| `success` | Explicit nonnegative integer `result_count`; no failures or unresolved values | Collection completed, including zero results |
| `failed` | Nonempty `failures` with `code` and sanitized `message`; no count | Collection failed without retained results |
| `incomplete` | Explicit retained count and failures, unresolved values, or a `reason` | Partial data; not sufficient to infer absence |
| `skipped` | `reason`; no count, failures, or unresolved values | Intentionally not attempted |
| `unsupported` | `reason`; no count, failures, or unresolved values | Adapter does not support the requested operation/data |

`unresolved_values` records `configuration_path`, `reason`, and optional `resource_id`.
When supplied, `resource_id` must identify a resource stored in this snapshot; absent
resource context remains allowed.
It preserves the location and explanation of unknown IaC values or missing facts without
retaining potentially sensitive raw expressions. Use `incomplete` when an import contains
unknown values, unsupported resource summaries, or a later page fails after results were retained.

The [partial-coverage fixture](../tests/fixtures/snapshots/partial-coverage.json) demonstrates a
successfully collected security-group rule allowing TCP 443 from `0.0.0.0/0`, failed
`DescribeRouteTables`, and successful `DescribeInstances` with zero results. It supports
the qualified conclusion that the rule permits that port, **not** that a resource is reachable
from the internet: routing evidence is missing. This is a model example, not an analyzer.

### Shared sanitization policy

Model validation recursively sanitizes generic resource/dependency `attributes`, account/region
`metadata`, and evidence values. Credential, password, token, API/private-key, authorization,
user-data, and connection-string keys are replaced with `[REDACTED]`; key matching ignores
case and punctuation and recognizes common prefixed secret names. Ordinary network facts
and secret ARN references are retained. Evidence also checks sensitive source path components,
so a scalar password fact is redacted even when its value has no dictionary key.
`archdiscov.models.sanitization.sanitize_value` is the shared adapter helper; `sensitive=True`
redacts the entire value when the source marks it sensitive (for example Terraform sensitive
values or CloudFormation `NoEcho` parameters).

Evidence records `value_status` as `present`, `redacted`, `omitted`, or `unresolved`.
`present` requires an explicit `value`, including a legitimate JSON `null`. `redacted` retains
the sanitized value and `redacted_paths` (JSON Pointers relative to that value; `""` means
the whole value). `omitted` and `unresolved` require a nonempty `limitation` and no non-null
value or redaction paths. Adapters may exclude an entire sensitive value with `omitted`.
Redaction markers and evidence limitations survive JSON round trips.

This is a field-name policy, **not a general secret detector**. Adapters must use source
sensitivity annotations, allowlist collected configuration, and exclude secrets in arbitrarily
named strings, tags, free-form failure messages, reasons, paths, or names before constructing
models. Do not collect secret values or workload data. Mutable models can be changed after
validation; persistence revalidates detached current data before saving.

IaC importers must receive an explicit account and region when the input cannot supply them;
do not invent an account or put regional resources under `global`. Region fields use the
`AwsRegion` enum, including `AwsRegion.GLOBAL` for account-wide identities and dependencies.
New AWS regions must be added to the enum before use; JSON still stores region codes as strings.

AWS resources carry a separate `identity` with account, region (or `global`), service, and at least
one of `physical_id` or `logical_address`. Services use the `AwsService` enum of ARN service
namespaces; extend it when adding another service namespace. For example, load balancers use
`elasticloadbalancing`, not the Boto3 client name `elbv2`. Collection adapters must map service
namespaces to client names explicitly. `ResourceType` values use CloudFormation resource type
identifiers; `unknown` is an internal fallback discriminator, not an AWS type.
The base `Resource.resource_type` also uses `ResourceType`; preserve unsupported AWS type
strings in `UnknownResource.aws_resource_type` rather than passing them to the base model.
Preserve both identifiers when available. Physical identifiers
may be AWS IDs or ARNs; logical addresses should include their module/stack namespace to avoid
collisions (for example `module.network.aws_vpc.main` or `network/MainVpc`).
The normalized resource `id` is derived as
`account/region/service/physical/<identifier>` when a physical identifier exists, otherwise
`account/region/service/logical/<address>`. The final segment is percent-encoded, including
slashes and percent signs. An input may omit `id`; a supplied `id` must match the derived value.
Names, tree positions, and snapshot UUIDs do not affect resource IDs. Acquiring a physical ID
changes a logical-only ID, but the logical address remains available for later comparison.

Resource names are optional (`null` or absent); no name is synthesized. Snapshot validation
indexes every regional and global tree: a stable ID may be stored only once, and every
descendant must match the snapshot account and its enclosing region (or `global`). Duplicate
errors identify both storage locations. AWS-specific parent/child placement and unsupported
type normalization remain adapter responsibilities; containment does not infer attachments.

Local dependency endpoints must exist in the same snapshot, including cross-region
and regional/global links. The grouping key must match the source resource's region;
each dependency's nonempty evidence list must resolve to source-owned evidence. Endpoint
and evidence IDs must be nonblank. Schema `2` additionally supports explicit external
and unresolved targets, never inferred or silently repaired by validation.

Public snapshot services support schema versions `"1"`, `"2"`, and `"3"`. Missing or
other versions are rejected with safe diagnostics, without echoing arbitrary raw values.
There is no automatic file migration or fallback; historical files are never rewritten
implicitly. Direct legacy model validation remains limited to `"1"` and `"2"`.

Schema `1` keeps strict local dependency targets. Schema `2` dependencies may use a
discriminated target: `local` (must resolve to a stored resource), `external` (a known
identifier outside the snapshot, with optional account, region, service, and AWS type), or
`unresolved` (a sanitized reason and source path). Targets never create resources. Schema `2`
also allows `lifecycle` on resources, for Terraform plan snapshots only. Its ordered
`actions` preserve the plan sequence; optional `value_scope` identifies `before` for
pure deletion or `after` otherwise. Historical action-only snapshots remain valid.
The `dependency-targets.json` fixture demonstrates all target kinds.

An account snapshot has this shape:

```json
{
  "schema_version": "1",
  "snapshot_id": "00000000-0000-4000-8000-000000000001",
  "account_id": "123456789012",
  "name": "production",
  "collection_started_at": "2026-10-13T08:00:00Z",
  "collection_completed_at": "2026-10-13T08:01:00Z",
  "source": {"kind": "mock", "label": "hierarchy-example"},
  "coverage": {"regions": ["eu-west-1"], "account_wide": false},
  "regions": {
    "eu-west-1": {
      "resources": [
        {
          "identity": {
            "account_id": "123456789012",
            "region": "eu-west-1",
            "service": "ec2",
            "physical_id": "vpc-example"
          },
          "name": "main",
          "resource_type": "AWS::EC2::VPC",
          "cidr_blocks": ["10.0.0.0/16"],
          "children": [
            {
              "identity": {
                "account_id": "123456789012",
                "region": "eu-west-1",
                "service": "ec2",
                "physical_id": "subnet-example"
              },
              "name": "application",
              "resource_type": "AWS::EC2::Subnet",
              "availability_zone": "eu-west-1a",
              "cidr_blocks": ["10.0.1.0/24"],
              "children": [
                {
                  "identity": {
                    "account_id": "123456789012",
                    "region": "eu-west-1",
                    "service": "ec2",
                    "physical_id": "i-example"
                  },
                  "name": "web",
                  "resource_type": "AWS::EC2::Instance",
                  "instance_type": "t3.micro"
                }
              ]
            }
          ]
        }
      ]
    }
  },
  "global_resources": [],
  "dependencies_by_region": {},
  "evidence": {}
}
```

This example demonstrates structure only; evidence-backed answers require populated evidence and coverage records. Dedicated IAM, EC2 network, ELBv2, ECS, Lambda, RDS/Aurora, and SQS types are implemented. Unsupported types use `UnknownResource`.

Small reusable JSON examples are in [tests/fixtures/snapshots](../tests/fixtures/snapshots):

| Fixture | Purpose |
| --- | --- |
| [aws.json](../tests/fixtures/snapshots/aws.json) | AWS-source regional and account-wide resources |
| [hierarchy.json](../tests/fixtures/snapshots/hierarchy.json) | Typed VPC → subnet → EC2 containment, unknown resources, unnamed resources, and an evidenced dependency |
| [terraform-plan.json](../tests/fixtures/snapshots/terraform-plan.json) | Logical-only declaration without a physical ID |
| [cloudformation.json](../tests/fixtures/snapshots/cloudformation.json) | Logical address with an escaped slash in its stable ID |
| [partial-coverage.json](../tests/fixtures/snapshots/partial-coverage.json) | Cited ingress, denied route-table collection, and successful zero-result instance collection |
| [iam.json](../tests/fixtures/snapshots/iam.json) | Typed account-wide role, instance profile, managed policy, trust and inline-policy documents |
| [network.json](../tests/fixtures/snapshots/network.json) | Typed network configuration, rule citations, and attachments |
| [dependency-targets.json](../tests/fixtures/snapshots/dependency-targets.json) | Schema `2` local, external, and unresolved targets |
| [load-balancing.json](../tests/fixtures/snapshots/load-balancing.json) | Typed ELBv2 resources, listener actions/rules, and target-health observations |
| [azure.json](../tests/fixtures/snapshots/azure.json) | Schema `3` offline VNet → subnet → VM, explicit fixture attachment, known-null/unavailable facts, and success-zero/incomplete/failed coverage |
| [egress-gateways.json](../tests/fixtures/snapshots/egress-gateways.json) | Public NAT gateway with its Elastic IP, egress-only internet gateway, a route to the NAT gateway, and an unresolved NAT network interface |
| [vpc-peering.json](../tests/fixtures/snapshots/vpc-peering.json) | Same-account peering connection between two VPCs and a route to it |
| [transit-gateway.json](../tests/fixtures/snapshots/transit-gateway.json) | Transit gateway, two VPC attachments, a route table with an association, a propagation, and static, propagated, and blackhole routes, and a VPC route to the gateway |
| [endpoints-and-prefix-lists.json](../tests/fixtures/snapshots/endpoints-and-prefix-lists.json) | Gateway and interface endpoints, AWS-managed and customer prefix lists referenced by routes and a rule, and an external transit gateway target |
| [dual-stack.json](../tests/fixtures/snapshots/dual-stack.json) | IPv4 and IPv6 CIDRs, an ENI IPv6 address, IPv6 routes to internet and egress-only gateways, and IPv6 rule and NACL entries |

Containment never implies an attachment or dependency. Azure fixture provenance is
synthetic, not live observation metadata.

Contract tests also cover Terraform state and mock sources, plus incomplete coverage with
failures, unresolved values, or reasons. Unresolved values describe unavailable facts;
they do not permit dangling local dependency endpoints. Schema `"2"` explicitly represents
external and unresolved relationship targets without inventing local resources.

Snapshot-wide containment, uniqueness, relationship, and evidence integrity checks are
implemented, as are explicit-path local persistence and snapshot JSON CLI ingestion.

To support another resource type, subclass `Resource`, give it a unique `Literal` value for `resource_type`, and add it to the discriminated `SupportedResource` union. The same union validates regional resources, global resources, and every level of `children`, preserving subtype fields when loading and saving JSON. Unsupported types can use `UnknownResource` with their original AWS type and generic attributes; they can also have children.

## Network path elements

Schemas `2` and `3` accept typed models for the elements a network path crosses beyond a
subnet's own route table. Every type and field is optional, so a snapshot written before them
validates unchanged, and an unset field added to an existing model is left out of output.
They are contracts only: the AWS collector, the Terraform and CloudFormation importers, and the
[network-path primitives](network-path-primitives.md) do not produce or decide them yet, so a
path through any of them stays `unknown`.

| Resource type | Model | Recorded for decisions |
| --- | --- | --- |
| `AWS::EC2::NatGateway` | `NatGateway` | state, public or private connectivity, VPC, subnet, network interface, public IPv4 addresses |
| `AWS::EC2::EgressOnlyInternetGateway` | `EgressOnlyInternetGateway` | VPC |
| `AWS::EC2::VPCPeeringConnection` | `VpcPeeringConnection` | state; requester and accepter side, each with VPC, owner account, and CIDRs |
| `AWS::EC2::TransitGateway` | `TransitGateway` | state, owner account, default association and propagation route tables |
| `AWS::EC2::TransitGatewayAttachment` | `TransitGatewayAttachment` | state, transit gateway, attached resource type, ID, and owner, association state and route table |
| `AWS::EC2::TransitGatewayRouteTable` | `TransitGatewayRouteTable` | state, transit gateway, associations, propagations, routes (destination, attachments, type, state), and `routes_complete` |
| `AWS::EC2::VPCEndpoint` | `VpcEndpoint` | gateway, interface, or gateway load balancer type, service name, state, VPC, owner, route tables, subnets, ENIs, security groups |
| `AWS::EC2::PrefixList` | `ManagedPrefixList` | address family, owner (an account ID or `AWS`), state, version, CIDR entries |

Fields added to existing models:

- `Route.prefix_list_id` names the prefix list a route's destination is; `destination` keeps
  its reported form. `RouteTargetKind` gains `egress_only_internet_gateway`.
- `NetworkInterface.ipv6_addresses` lists IPv6 addresses (`null` when not recorded). There is
  no public flag: AWS IPv6 addresses are globally routable.
- VPC and subnet IPv6 CIDRs stay in `cidr_blocks`, told apart by address family. Route
  destinations, security-group rule CIDRs, and NACL entry CIDRs already accept IPv6.
- `ManagedPrefixList.entries` is `null` when the entries were not recorded (as for an
  AWS-managed list) and `[]` for a recorded empty list. `TransitGatewayRouteTable.routes_complete`
  is `false` when the route search reported more routes than it returned and `null` when
  unrecorded.

Validation rejects states AWS cannot report: a peering connection whose sides are the same VPC;
a malformed owner account; a private NAT gateway with a public address, or an available public
one without; a transit gateway route that names attachments while `blackhole`, or none
otherwise; an attachment associated with, or propagating to, a route table of another transit
gateway (`snapshot_transit_gateway_link_mismatch`); a gateway endpoint with subnets or an
interface endpoint with route tables; a prefix-list entry of the other address family; a subnet
IPv6 CIDR outside its VPC's recorded IPv6 CIDRs; an IPv4 value as an IPv6 address; and a
security-group rule naming both a CIDR and a prefix list.

Their links are `attached_to` dependencies whose `attributes["kind"]` is one of:

| `kind` | Source → target |
| --- | --- |
| `route_target` | route table → NAT gateway, egress-only internet gateway, peering connection, transit gateway, or endpoint (with `target_kind`, `destination`, and `state`) |
| `prefix_list_reference` | route table → the prefix list a route's destination names; security group → the prefix list a rule names |
| `nat_gateway_subnet`, `nat_gateway_interface` | NAT gateway → its subnet and network interface |
| `egress_only_internet_gateway_attachment` | egress-only internet gateway → VPC |
| `peering_requester_vpc`, `peering_accepter_vpc` | peering connection → each side's VPC |
| `transit_gateway_attachment`, `transit_gateway_vpc_attachment` | attachment → its transit gateway and attached VPC |
| `transit_gateway_route_table_association`, `transit_gateway_route_table_propagation` | transit gateway route table → the attachments it associates and propagates |
| `endpoint_route_table`, `endpoint_subnet`, `endpoint_interface`, `endpoint_security_group` | endpoint → its route tables, subnets, ENIs, and security groups |

Each cites the source entry it came from, and the [graph index](graph-index.md) follows it in
both directions. A target in another account, or of a type the snapshot does not record, is
`external`; a reference to a resource the snapshot should hold but does not is `unresolved`.
A snapshot collected before these elements has no coverage record for their operations, which
reads as coverage unrecorded, never as a successful collection that found none.

## Snapshot Python services

Load a selected UTF-8 JSON file or validate supplied snapshot data without CLI commands:

```python
from archdiscov.services import load_snapshot, save_snapshot, validate_snapshot

snapshot = load_snapshot("snapshots/account.json")
validated_copy = validate_snapshot(snapshot)  # Also accepts a mapping of snapshot fields.
saved_path = save_snapshot(validated_copy, "snapshots/new-account.json")
```

Validation returns a fresh, sanitized model and rechecks current mutable model data,
including nested identities and references; it does not invent UUIDs or migrate versions.
Loading applies the same schema, integrity, and sanitization checks.

For common read-only traversal or an explicit legacy-to-schema-`3` conversion, use
`SnapshotAccess` and `upgrade_snapshot`. They return detached data and never rewrite
files; saving an upgrade requires a new path:

```python
from pathlib import Path

from archdiscov.services import SnapshotAccess, load_snapshot, save_snapshot, upgrade_snapshot

legacy = load_snapshot("tests/fixtures/snapshots/hierarchy.json")
upgraded = upgrade_snapshot(legacy)
Path("snapshots").mkdir(exist_ok=True)
save_snapshot(upgraded, "snapshots/aws-schema3-example.json")
access = SnapshotAccess.from_snapshot(legacy)  # Internal normalization; no file migration.
print(access.scope.model_dump(mode="json"))
print([resource.id for resource in access.resources()])
print(list(access.containment()))
print(list(access.dependencies()))
print(list(access.evidence))
print(access.coverage.model_dump(mode="json"))
```

The [cloud guide](cloud-snapshots.md#python-services-and-normalized-access) also
demonstrates location-aware roots and Azure evidence. Choose a fresh output path on reruns.

Saving revalidates detached data and writes normalized, sanitized, indented UTF-8 JSON
with a trailing newline, preserving nulls, defaults, subtype fields, citations, and
coverage. The caller's model is not changed. **Storage immutability means an existing
path cannot be updated**, even with identical content: files, directories, and symlinks
(including dangling ones) are conflicts. Loaded models remain mutable, but changing
them does not change saved bytes. Paths select snapshots, not UUIDs; the same UUID may
be saved at another path. Direct external file edits are outside this guarantee.

The destination parent must already exist. Saving creates a uniquely named temporary
file with restrictive permissions in that directory, flushes and `fsync`s its contents,
then publishes it with an atomic no-overwrite hard link. Successful publication is the
commit point. Temporary-file cleanup is best effort: failures are logged; after commit
the save still returns successfully, and before commit the original error is retained.
A cleanup failure may leave a `.archdiscov-*.tmp` file for manual removal.
Supported local filesystems must allow atomic hard-link publication; unsupported
filesystems fail safely without replacement fallback. This promises complete atomic
visibility and no overwrite, not universal power-loss durability or network-filesystem
guarantees. No parent directories, registry, or migrations are created.

Catch `SnapshotError` for all service errors, or the more specific
`SnapshotValidationError`, `SnapshotJSONError`, `SnapshotConflictError`, and
`SnapshotIOError`. Conflicts use `snapshot_conflict`. Errors expose a
stable `code` and optional `path`. Validation `details` contain only field `location`,
error `type`, and a safe `message`, not raw input values or exception contexts;
unsupported versions use `unsupported_schema_version` rather than `invalid_snapshot`.
JSON failures use `invalid_json` with `line`/`column` for syntax errors, `invalid_json`
for duplicate object keys or non-finite numbers, `invalid_encoding` for non-UTF-8 files, or
`json_too_large` for a file over 256 MiB, which is never read past that point (the same
reader loads saved analyses, comparisons, and evaluation reports, and Terraform exports have
the same limit); filesystem failures use `snapshot_io_error`.
The original exception is retained as `__cause__` for debugging and may contain raw
data: do not expose its traceback or diagnostics to untrusted callers. The CLI renders
only safe public diagnostics and maps error categories to the
[documented exit codes](cli.md).
