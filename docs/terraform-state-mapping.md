# Terraform state mapping

The offline adapter reads a local UTF-8 export in the public `terraform show -json`
**state** format. It does not accept internal `.tfstate` JSON, backend configuration,
Terraform plans, or HCL. It never executes Terraform, loads providers, contacts AWS,
or makes network calls. State describes exported configuration, not live observations
or proof of deployment, reachability, or effective access.

## Python API

Import `TerraformStateRequest` from `archdiscov.ingestion.terraform` and pass it to
`archdiscov.services.collect_snapshots(request, context=...)`. Context is optional;
the result is a list containing one account snapshot. The async equivalent is
`collect_snapshots_async`. Collection and persistence are separate: use
`save_snapshot(snapshot, new_path)` and inspect citations with
`get_evidence(snapshot, evidence_id)`. See the [runnable fixture example](../README.md#terraform-state-api-implemented).

The request requires `path`; `account_id`, concrete `region`, and descriptive
`workspace` are optional. Workspace is metadata, not a backend selector. Output uses
schema `2` and source kind `terraform_state`, recording the path, format version,
optional Terraform version, and supplied workspace. Saving requires an existing
parent directory and never overwrites a destination.

There is no raw-state CLI command yet: `archdiscov ingest` accepts only Archdiscov
snapshot JSON. Raw-source CLI ingestion is deferred to step 21; plans and
CloudFormation templates are separate planned adapters.

## Input, identity, and scope

- `format_version` must be `1.<minor>`; future numeric minor versions are accepted,
  other majors are rejected. `values.root_module` is required; an empty module is valid.
- Root and recursively nested `child_modules` are traversed. Managed entries must
  identify `registry.terraform.io/hashicorp/aws` and an `aws_...` resource type.
  Duplicate JSON keys, logical addresses, and physical identities are rejected;
  malformed structure or incompatible sensitivity masks fail safely.
- Full logical addresses retain module paths, count indexes, and `for_each` keys.
  Safe `values.id` is preferred as the physical ID; a safe `values.arn` is the
  fallback. If neither survives sanitization, identity falls back to the logical
  address; sensitive IDs are never reconstructed.
- Account ownership comes from resource `owner_id`, `account_id`, or the account
  component of its own `arn`/ARN-valued `id`, together with explicit request context.
  Exactly one account must resolve. Missing ownership needs explicit context;
  supplied conflicts and mixed-account exports are rejected.
- Regional scope comes from a resource's own ARN or `region` fact, otherwise the
  supplied request region. Missing or ambiguous scope needs explicit context;
  conflicting facts or supplied context are rejected. Without a request region,
  independently resolved resources may span multiple regions. IAM is account-wide
  under `global`; the request region itself must be concrete.
- Scope is not inferred from referenced resources, IP addresses, availability-zone
  names, module placement, or invented parents. An empty export requires explicit
  account and region and produces successful zero-result coverage.
- Outputs are not architecture resources and are not retained. Data-source entries
  are skipped and recorded as incomplete coverage, not imported as managed resources.

## Allowlisted facts

The table lists source fields under each resource's `values`. Absent facts stay
unavailable; defaults do not prove an empty configuration. Invalid or redacted fields
are omitted with limitations rather than copied as raw provider data.

| Terraform type | Normalized model | Type-specific facts |
| --- | --- | --- |
| `aws_vpc` | `Vpc` | Valid `cidr_block` and `ipv6_cidr_block` → `cidr_blocks` |
| `aws_subnet` | `Subnet` | CIDR blocks; `availability_zone` |
| `aws_instance` | `Ec2Instance` | `instance_type` |
| `aws_security_group` | `SecurityGroup` | Summary only; inline `ingress`/`egress` rules are not mapped |
| `aws_internet_gateway` | `InternetGateway` | Shared identity, name, tags, and reference facts only |
| `aws_route_table` | `RouteTable` | Summary only; inline `route` entries are not mapped |
| `aws_network_interface` | `NetworkInterface` | Shared identity, name, tags, and reference facts only |
| `aws_iam_role` | `IamRole` | Source `arn`; `permissions_boundary` → `boundary_arn` |
| `aws_iam_instance_profile` | `IamInstanceProfile` | Source `arn`; profile role names are not mapped |
| `aws_iam_policy` | `IamManagedPolicy` | Source `arn`; policy document is not mapped |

Typed mappings share `attributes.terraform_type`, sanitized string-valued `tags`,
and `name` from a safe `tags.Name`, falling back to `values.name`. When present,
`vpc_id`, `subnet_id`, and `iam_instance_profile` are retained as attributes.
`vpc_security_group_ids`, falling back to `security_groups`, supplies the sanitized
`attributes.security_group_ids` list. These exported strings are configuration
facts, not proof of a resolved attachment or effective IAM association.

IAM typed resources require a safe source ARN; without one they become type-only
`UnknownResource` summaries. IAM trust/inline/managed policy documents and profile
role names are deliberately not imported. Likewise, security-group rules and route
entries are limitations, not a complete network model. Other instance/ENI details,
such as IP addressing or user data, are not part of this mapping.

Explicit empty inline rule/route lists receive citations. Absent, null, or nonempty
unmapped lists are named in `attributes.unavailable_fields` and qualify coverage;
the typed model's default list must not be treated as evidence of successful absence.

Unsupported types remain `UnknownResource`, retaining identity and Terraform type
only. `aws_resource_type` uses an explicit AWS type mapping where available, otherwise
the Terraform type itself. Service grouping uses recognized type families, otherwise
the explicit `AwsService.UNKNOWN` namespace. For example, `aws_s3_bucket` is an
unknown model with `aws_resource_type='AWS::S3::Bucket'` and service `S3`, not a typed
S3 import. Typed model validation failure also falls back to a limited summary.

## Evidence, redaction, and coverage

Recursive Terraform `sensitive_values` masks and generic secret-key redaction run
**before** scope resolution and mapping. Only allowlisted normalized facts and their
sanitized citations are persisted: never full raw `values`, unsupported payloads,
outputs, user data, environment secrets, or IAM documents. A resource-level citation
contains only its address and type, not a copy of the resource record.

Each retained fact cites the source file and an exact RFC 6901 JSON pointer. For
example, the fixture VPC CIDR cites
`/values/root_module/resources/0/values/cidr_block`; a nested subnet availability
zone cites `/values/root_module/child_modules/0/resources/0/values/availability_zone`.
Tag keys escape `~` and `/` as `~0` and `~1`. List-item citations preserve source
indexes even when malformed siblings are omitted. Redaction evidence contains
`[REDACTED]` and redacted-path metadata, never the removed value.

Coverage records use operation `terraform.show.state`, grouped by region/service,
with retained resource counts. Redactions, unsupported summaries, malformed omitted
facts, unresolved references, and skipped data sources produce explicit limitations and `incomplete`
coverage. Counts include retained unknown summaries; success is not proof that every
AWS fact was imported. A valid empty export with explicit scope records `success`
with count `0`. Invalid input fails the request rather than masquerading as empty.

## Relationships and containment

Only exported `vpc_id`, `subnet_id`, and security-group references produce
`attached_to` dependencies, with kind `terraform_configuration_reference` and exact
source-field evidence. Targets resolve only to the expected resource type in the
same account/region scope. Missing or wrong-type targets remain unresolved with the
precise source JSON pointer; resources are not invented.

Observed subnet/VPC references place resources under stored parents when available
(subnet preferred for instances and ENIs). Missing parents leave resources at the
regional root with unresolved-parent information. Containment is not inferred from
module paths. There is no HCL expression/reference evaluation, IP ownership inference,
cross-account target guessing, route analysis, or IAM policy evaluation.
