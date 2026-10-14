# Terraform plan mapping

The offline adapter reads local UTF-8 **public `terraform show -json` plan exports**
with `format_version` `1.<minor>`, including future numeric minor versions. It does
not accept public state exports, internal `.tfstate` JSON, backends, or HCL. It never
executes Terraform, loads providers, contacts AWS, or makes network calls. Plans
describe intended configuration, not applied state or proof of reachability/access.

## Python API

Import `TerraformPlanRequest` from `archdiscov.ingestion.terraform` and call
`archdiscov.services.collect_snapshots(request, context=...)` or
`collect_snapshots_async`. Context is optional; the result contains one account
snapshot. See the [fixture example](../README.md#terraform-plan-api-implemented),
using `tests/fixtures/terraform/plan.json`, account `123456789012`, and `eu-west-1`.

The request requires `path`; `account_id`, concrete `region`, and descriptive
`workspace` are optional. Workspace is metadata, not a backend selector. Output uses
schema `2` and source kind `terraform_plan`, with path, format version, optional
Terraform version, and supplied workspace. Persist separately with
`save_snapshot(snapshot, new_path)`; the parent directory must exist and the target
must be new. Inspect citations with `get_evidence(snapshot, evidence_id)`.

The [raw-source CLI](raw-source-ingestion.md) exposes this adapter through
`archdiscov ingest --format terraform-plan`. Schema `1` historical fixtures remain
readable, and historical schema `2` action-only lifecycle records remain valid.

## Selected values and lifecycle

Managed AWS resources come from `resource_changes` and recursively nested
`planned_values.root_module` inventories. Changed resources use their selected
change values rather than merging inventory or prior-state fields. Inventory-only
resources are retained as no-op planned resources. Data sources are skipped with
coverage limitations. Invalid structure, duplicate keys/addresses/physical
identities, incompatible masks, and unsupported action sequences fail safely.

`lifecycle.actions` is an exact ordered list of enum strings, not a set:

| Actions | Selected facts | Emitted `lifecycle.value_scope` |
| --- | --- | --- |
| `["create"]`, `["read"]`, `["update"]`, `["no-op"]` | `change.after` | `after` |
| `["delete", "create"]`, `["create", "delete"]` | `change.after`; replacement order preserved | `after` |
| `["delete"]` | `change.before` | `before` |

Inventory-only resources use their planned `values` with `after` scope. The optional
model field `value_scope` is emitted by this adapter; older action-only snapshots
need not supply it. Pure-deletion resources are retained, but their containment and
value-reference resolution are separate from the after view. A before-only resource
cannot satisfy an after reference. A replacement is one after resource, not a merged
before/after record or a simulated execution sequence.

## Unknowns, sensitivity, and allowed facts

`after_unknown` facts are removed **before mapping**. Missing or unknown `after`
fields never fall back to `before`, `prior_state`, configuration constants, or input
variables. Lists with unknown descendants are omitted rather than compacted or filled
with fictitious null facts; exact unknown locations remain recorded.

Terraform `before_sensitive`/`after_sensitive` masks (and inventory
`sensitive_values`) plus generic secret-key scrubbing run before mapping or evidence
creation. Only the [state adapter's allowlisted facts](terraform-state-mapping.md#allowlisted-facts)
and sanitized citations are retained. Raw `before`/`after` objects, outputs, variable
values, constants, secrets, user data, and IAM documents are not persisted.
Unsupported types remain limited `UnknownResource` summaries. Security-group rules,
route entries, IAM policy evaluation, and other complex mappings omitted by the state
adapter are **not implemented** by the plan adapter either.

Full logical addresses retain module paths, count indexes, and `for_each` keys. Safe
selected `id`, then `arn`, supplies physical identity; if neither is known and safe,
identity falls back to the full logical address. An unknown ID is never rebuilt
from an old before value.

## Account and region context

The [state scope rules](terraform-state-mapping.md#input-identity-and-scope) apply:
exactly one account must resolve, with concrete regions for regional resources and
`global` for IAM. Resources may span independently resolved regions. Safe selected
ownership/ARN/region facts and explicit request context determine scope; missing or
unknown facts may require explicit account and region. Mixed accounts and conflicting
source/request context are rejected. Empty plans require explicit account and region.

Sanitized before identity context is checked for contradictions, **not used to
backfill** after identity, account, region, or planned fields. A replacement may
move between regions within the same account; its prior region need not match the
selected after region. Scope is not guessed from references, availability zones,
module names, or prior-state resources.

## References, evidence, and coverage

Allowlisted exported VPC/subnet/security-group value references follow the state
mapping within their selected lifecycle view. Exported configuration expression
`references` can additionally produce local dependencies when a resource address
(including an attribute traversal) uniquely matches a stored after resource.
Indexed logical addresses remain intact; ambiguous unindexed instances are not
guessed. Local targets must actually exist in the snapshot.

Variables, `count`, module outputs, dynamic expressions, and ambiguous or missing
targets remain unresolved, with exact configuration source paths such as
`/configuration/root_module/resources/0/expressions/subnet_id/references/0`.
There is no HCL evaluation, variable substitution, constant evaluation, or invented
resource. Configuration references do not turn unknown planned values into facts.

Retained facts cite exact RFC 6901 source pointers, for example
`/resource_changes/0/change/after/cidr_block`; deletion facts cite `change/before`,
and inventory-only facts cite `/planned_values/root_module/.../values/...`.
Unknown evidence has `value_status="unresolved"` and no asserted value. It records
the exact `change.after_unknown` mask location, not a fictitious fact citation pointing
to an absent normalized target. Redaction evidence never contains removed secrets.
Inventory-only no-op actions are derived, so they do not cite a nonexistent actions field.

Coverage uses `terraform.show.plan`, grouped by region/service with retained counts.
Unknowns, redactions, unresolved references, unsupported summaries, skipped data
sources, and omitted malformed facts qualify coverage as `incomplete`, with exact
source paths for unknown/unresolved values. Successful zero-result coverage requires
a valid empty plan with explicit scope; invalid input fails rather than becoming an
empty success. Counts and containment do not prove deployment or effective access.

Plans marked `complete=false` or containing deferred changes retain available resources
with incomplete coverage; raw deferred payloads are not copied. Errored plans and malformed
status flags are rejected rather than published as usable planned inventories.
