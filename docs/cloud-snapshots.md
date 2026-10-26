# Cloud snapshot boundary

Archdiscov's implemented cloud-neutral boundary supports AWS snapshots and an **offline
Azure fixture**, not full Azure discovery. Public snapshot validation, persistence,
snapshot-format ingestion, and evidence inspection accept schemas `1`, `2`, and `3`.
No Azure SDK, authentication, live Azure collector, graph/query engine, analyzer, GCP
support, or Terraform HCL parser is supplied by this boundary.

## Versions and compatibility

| Contract | Scope and storage | Behavior |
| --- | --- | --- |
| `ArchitectureSnapshot`, schemas `1`/`2` | One AWS account, AWS-enum `regions`, `global_resources`, `dependencies_by_region` | Legacy serialization and stable resource IDs unchanged; direct validation rejects schema `3` and Azure models |
| `CloudSnapshot`, schema `3` | Explicit `scope`, location-keyed `regions`, `scope_resources`, flat `dependencies` | AWS or Azure; platform-aware identity, source, evidence, coverage, and endpoint validation |

Shared models live under `archdiscov.models.cloud`; Azure and unknown-resource models
live under `archdiscov.models.resources`. The public services package exports
`SupportedSnapshot = ArchitectureSnapshot | CloudSnapshot`, `validate_snapshot`,
`load_snapshot`, `save_snapshot`, `ingest_snapshot`, `get_evidence`, `upgrade_snapshot`,
and `SnapshotAccess`; the latter contracts are implemented in
`archdiscov.models.snapshot_access`, which `archdiscov.services` re-exports.

AWS collectors and Terraform state/plan and CloudFormation importers still emit schema
`2`. Loading or ingesting an existing file preserves its schema; it does not silently
upgrade it. `upgrade_snapshot(legacy)` explicitly returns a freshly validated, detached
AWS `CloudSnapshot`, preserving its UUID, resource IDs, facts, citations, source
provenance, coverage outcomes, containment, and dependency target context. It maps
account-wide `global` context to null locations and infers the AWS partition from legacy
context. It does not migrate or overwrite any file.

## Scope, location, and identity

`SupportedScope` is explicitly discriminated by `platform`:

- `AwsScope`: `platform="aws"`, a 12-digit `account_id`, and `partition` (`aws`,
  `aws-cn`, or `aws-us-gov`; default `aws`). AWS region/ARN context must agree with the
  partition. An account is not inherited from another snapshot or dependency source.
- `AzureScope`: `platform="azure"`, required `subscription_id`, optional `tenant_id`.
  UUIDs must be hyphenated strings; uppercase hexadecimal is accepted and normalized to
  canonical lowercase. UUID objects, compact strings, whitespace, and malformed UUIDs
  are not substitutes for this wire contract.

Schema `3` retains the field name `regions`, but its keys are location strings. Azure
locations such as `eastus` must be canonical lowercase, not `AwsRegion` values.
`scope_resources` separately stores location-independent roots; their identities,
evidence, and operations use `location=null`, with `coverage.scope_wide=true`. Do not
represent Azure scope-wide placement with AWS's `global` sentinel. Every descendant
must match its snapshot scope and enclosing location; duplicate IDs are rejected
across all trees with both storage locations in diagnostics.

### AWS identity compatibility

`ResourceIdentity` retains the legacy `account_id`, `region`, `service`, `physical_id`,
and `logical_address` fields. Its AWS `platform` property is **not serialized**. Existing
stable IDs remain `account/region/service/physical/<quoted-id>` or
`account/region/service/logical/<quoted-address>`. AWS IDs are not case-folded.
`parse_identity(data)` centrally validates `SupportedIdentity`; dictionaries without
`platform` are interpreted as legacy AWS identities, not guessed from identifier text.

### Azure identity and matching

`AzureResourceIdentity` requires `platform="azure"` (the model default), subscription,
native provider namespace `service`, `native_type`, and at least one identifier. It also
accepts optional tenant, resource group, location, ARM `physical_id`, and
`logical_address`. Azure namespaces such as `Microsoft.Network` and `Microsoft.Compute`
are strings validated as Azure context, not entries added to `AwsService`.

ARM validation requires a subscription-scoped path with optional resource group,
`providers/<namespace>`, and alternating type/name pairs. It checks subscription and
group consistency and reconstructs the hierarchical native type, for example
`Microsoft.Network/virtualNetworks/subnets`. Namespace/type/group comparisons are
case-insensitive; surrounding whitespace and malformed paths are rejected. This is
syntax and consistency validation, not proof the resource exists.

`physical_id` preserves original spelling. `canonical_arm_id` supplies a separate
lowercase matching form. The physical stable ID is
`azure/<subscription>/physical/<percent-encoded-canonical-ARM-ID>` and is independent
of location, display name, tree position, and snapshot UUID. Logical-only IDs use a
distinct `azure/<subscription>/logical/` namespace with native type, resource group,
location, service, and quoted logical address. Azure context is normalized for matching;
the logical address itself is **not case-folded**. Different subscriptions or logical
contexts do not collapse into one identity; adding a physical ID changes a logical-only
stable ID while retaining the logical address.

## Resources, source, and dependencies

Typed Azure models cover only `AzureVirtualNetwork.address_spaces`,
`AzureSubnet.address_prefixes`, and `AzureVirtualMachine.vm_size`, plus inherited
resource fields. Their discriminators are `Microsoft.Network/virtualNetworks`,
`Microsoft.Network/virtualNetworks/subnets`, and `Microsoft.Compute/virtualMachines`.
Typed AWS and Azure models guard their identity platform; Azure native types must agree
with the typed model. Unsupported schema-`3` types can use `CloudUnknownResource` with
`resource_type="cloud_unknown"`, explicit `platform`, `native_type`, and a matching
identity. Azure unknown native types must match the identity too. Legacy AWS
`UnknownResource` with `aws_resource_type` remains supported; it is not replaced.
Legacy snapshots reject `cloud_unknown` even when it has an AWS identity.

`CloudSource` separates the cloud `platform` from source format in `provenance`.
`CloudProvenance` retains AWS, Terraform state/plan, CloudFormation, and mock models and
adds `AzureFixtureSource(kind="azure_fixture", label=...)`. Terraform and mock formats
are not intrinsically AWS cloud platforms, although current raw Terraform adapters map
AWS only. AWS/CloudFormation and Azure-fixture provenance have platform guards. An Azure
fixture label and offline locator are not invented live profile, role, request, or SDK
metadata. Evidence's source family remains separate from snapshot provenance kind.

`CloudDependency` has `source_id`, discriminated `target`, source `location`, relationship,
attributes, and nonempty source-owned `evidence_ids`. Local endpoints must resolve;
unresolved endpoints retain explicit reasons. `CloudExternalTarget` retains its own
`scope`, identifier, location, service, and native type, including cross-platform
context. An external AWS target uses `AwsTargetScope`, which allows unknown account and
partition; neither is filled from the source scope. Targets never synthesize resources.
Terraform-plan lifecycle remains restricted to AWS plan provenance.

Containment is storage organization, not attachment, network reachability, or permission.
The Azure fixture chooses VNet → subnet → VM containment and separately records a VM →
subnet `attached_to` dependency with `attributes.kind="fixture_attachment"`, cited by
VM-owned `vm-attachment`. This synthetic attachment is explicit, not inferred from the
tree or presented as proof of a deployed Azure NIC relationship.

## Evidence and coverage

`CloudEvidence` extends the existing evidence contract with `scope`, nullable `location`,
and native `service`. Each record's snapshot UUID and owner must match the snapshot;
its context must match the owner. Fact citations remain exact resource-relative JSON
Pointers into JSON-mode serialized values, preserving subtype fields and pointer escaping.
Missing fields, bad indices, cross-owner citations, and dangling dependencies are rejected.
A present null field is a valid cited fact; unavailable evidence is a separate status
and limitation, not a citation to an absent field.

`CloudCoverage` contains scope, requested `locations`, `scope_wide`, and
`CloudOperationCoverage` outcomes keyed by location/service/operation. Operations retain
counts, aware timestamps, safe failures, reasons, and unresolved values, and must fit the
collection window and requested scope. Success with zero results is not failure;
incomplete known results and failed operations remain distinct. No operation record means
no claim about that operation, and an empty location is not proof of zero inventory.

`get_evidence(snapshot, evidence_id)` returns an `EvidenceInspection` with the exact record,
owning typed resource, explicit sorted `fact_paths`, source, and **relevant** coverage:
matching platform/scope, location, and service. It does not infer completeness, graph
edges, or conclusions. Schema-`3` JSON presentation adds explicit platform/scope/location
context rather than disguising Azure as an AWS region.

The [Azure fixture](../tests/fixtures/snapshots/azure.json) demonstrates:

| Record | Meaning |
| --- | --- |
| `network-address`, `subnet-prefix` | Exact typed address facts |
| `vm-size` | Known present null `/vm_size` fact |
| `vm-unavailable` | `value_status="unresolved"` with a limitation, no invented fact citation |
| `vm-attachment` | Explicit source-owned fixture attachment evidence |
| `fixture.empty-network-list` | Successful zero-result operation |
| `fixture.virtual-machines` | Incomplete result with one known VM and an unresolved value |
| `fixture.instance-view` | Failed operation with safe `FixtureUnavailable` code |

## Python services and normalized access

Run from the repository root after installation. The destination parent must exist and
the output path must be new:

```python
from pathlib import Path

from archdiscov.services import (
    SnapshotAccess,
    get_evidence,
    load_snapshot,
    save_snapshot,
    upgrade_snapshot,
)

legacy = load_snapshot("tests/fixtures/snapshots/hierarchy.json")
upgraded = upgrade_snapshot(legacy)
Path("snapshots").mkdir(exist_ok=True)
save_snapshot(upgraded, "snapshots/aws-schema3-example.json")

azure = load_snapshot("tests/fixtures/snapshots/azure.json")
access = SnapshotAccess.from_snapshot(azure)  # Also accepts legacy directly.
print(access.scope.model_dump(mode="json"))
print([(location, root.id) for location, root in access.roots()])
print([resource.id for resource in access.resources()])
print(list(access.containment()))  # Stored parent/child ID pairs, not inferred edges.
print([dependency.relationship for dependency in access.dependencies()])
print(list(access.evidence))
print([(op.location, op.service, op.status) for op in access.coverage.operations])
inspection = get_evidence(azure, "vm-size")
assert inspection.fact_paths == ["/vm_size"]
assert inspection.evidence.value is None
```

`SnapshotAccess.from_snapshot` validates detached data and internally converts legacy
snapshots to the common schema-`3` view. Its `scope`, `evidence`, and `coverage` properties
and `roots()`, `resources()`, `containment()`, and `dependencies()` iterators expose
normalized context without mutating the caller or saved file. Returned models are
detached copies, not frozen graph nodes; editing a returned model cannot change the
adapter's retained snapshot. This is a read-only access contract, not graph indexing.

`validate_snapshot` and `save_snapshot` revalidate even already-created mutable models,
including nested identities and references. Sanitization operates on detached data;
callers remain unchanged. Generic secrets in attributes/metadata and evidence values are
redacted according to the existing policy; adapters must still avoid collecting secrets.
The fixture already contains `attributes.password="[REDACTED]"`.

Storage guarantees are unchanged: no overwrite (including symlink conflicts), restrictive
temporary files, flush/`fsync`, atomic hard-link publication, and cancellation-safe
collection draining. There is no replacement fallback or automatic directory creation.
Safe public diagnostics omit raw input values/secrets; original exception causes remain
debugging-only and must not be rendered to untrusted callers. See
[persistence details](../README.md#snapshot-python-services).

## Offline CLI example

Neither credentials nor network access is needed after installation:

```bash
mkdir -p snapshots
uv run --locked archdiscov ingest tests/fixtures/snapshots/azure.json \
  --output snapshots/azure-example.json
uv run --locked archdiscov evidence vm-size --snapshot snapshots/azure-example.json
uv run --locked archdiscov evidence vm-unavailable \
  --snapshot snapshots/azure-example.json --json
```

`--format snapshot` is the default and preserves schema `3`; formats are never inferred.
The same arguments work with `.venv/bin/python -m archdiscov`. Choose another destination
if it exists. Azure context is rendered as subscription, optional tenant, location, and
native namespace; the incomplete/failed compute outcomes qualify VM evidence.

## Validation and future extensions

Boundary regression tests include `tests/models/test_cloud_identity.py`,
`tests/models/test_cloud_resources.py`, `tests/models/test_cloud_snapshot.py`,
`tests/models/test_snapshot_integrity_helpers.py`, `tests/models/test_azure_snapshot.py`, and
`tests/acceptance/test_cloud_workflows.py`, plus legacy fact-serialization, persistence, evidence,
and CLI regressions. They cover strict scope/ARM inputs, case matching and ID stability,
provider guards, placement/duplicates, citation ownership, exact pointers and nulls,
dependency context, relevant coverage, detached redaction, invalid-model rejection before
publication, no-overwrite bytes, AWS upgrades, and the installed CLI outside the
checkout without credentials/network. No live Azure acceptance is implied.

The pre-phase-3 bridge follows explicit prerequisites:

| Step | Boundary | Prerequisite |
| --- | --- | --- |
| 22.1 | Cloud scope, identity, typed resources, and compatibility contracts | Completed phase 2 |
| 22.2 | Shared schema-`3` integrity and explicit AWS conversion | 22.1 |
| 22.3 | Offline Azure persistence and evidence workflow | 22.2 |
| 22.4 | Common access boundary, documentation, and compatibility acceptance | 22.1–22.3 |

[PLAN.md](../PLAN.md) owns delivery status and subsequent full validation. Verified
22.1–22.4 are prerequisites for phase 3.

Future cloud platforms require explicit code changes: typed scope and identity models,
discriminator/union branches, resource models and provider guards, provenance/source
validation, native location/service rules, endpoint context, public presentation, and
safe tests. There is no runtime registry or catch-all identity that silently enables a
platform. Unknown resources preserve unsupported native types only within a supported
platform; they do not imply analyzer support.

Phase 3 builds [graph queries](graph-queries.md), the [analyzers](analyzers.md), and
[comparisons](comparison-results.md) on this boundary. The security, network, and trust
analyzers target AWS semantics; unsupported Azure
requests must produce actionable limitations rather than apply AWS rules. Phase 4's
[LLM providers](assistant-providers.md) (the Anthropic, OpenAI-compatible, and Bedrock
adapters) are a separate concept from cloud platforms. Schema `3` itself implements neither phase.