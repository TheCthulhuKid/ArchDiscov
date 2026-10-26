# Cloud-platform foundation before phase 3

Archived from [PLAN.md](../../PLAN.md). Steps 22.1–22.4 are complete; this file records
how they were planned and verified and is no longer updated.

This bridge introduces schema `3` without changing historical AWS schemas `1`–`2`,
AWS stable IDs, or schema `2` collector/importer output. Azure is an offline contract
slice, not live discovery. Cloud platforms are distinct from phase 4 LLM providers.
See [cloud snapshots](../../docs/cloud-snapshots.md).

## Step 22.1: Cloud scope, identity, and compatibility contracts
**Status:** Implemented and verified; final compatibility acceptance is recorded in step 22.4.
**Prerequisites:** Completed phase 2.

Add AWS account/partition and Azure subscription/tenant scope, discriminated
identities, and typed Azure VNet/subnet/VM models. Preserve AWS constructors,
imports, serialized fields, and stable IDs. Validate ARM subscription, resource
group, namespace, and hierarchical type; preserve original spelling separately
from lowercase physical matching. Logical identifiers remain distinct.

**Advice:** Extend platforms through explicit typed contracts, not fabricated AWS fields.

**Acceptance check:** AWS compatibility, ARM case equivalence, malformed identifiers,
cross-subscription noncollision, provider rejection, and recursive subtype preservation pass.

## Step 22.2: Shared schema-3 integrity and explicit AWS conversion
**Status:** Implemented and verified; final compatibility acceptance is recorded in step 22.4.
**Prerequisites:** Step 22.1.

Introduce `CloudSnapshot` with explicit platform/source format, scope, locations,
scope-wide resources, evidence, coverage, and target context. Reuse typed AWS models
and narrow integrity primitives, including the per-validation fact representation
cache. Unknown resources retain platform/native type without bypassing validation.
External targets keep their own context; never infer their account from the source.
Detached `upgrade_snapshot` preserves UUIDs, IDs, child order, subtype fields,
lifecycle, citations, redactions, dependencies, and coverage without file migration.

**Advice:** Keep provider-specific validation separate from shared integrity.

**Acceptance check:** All legacy fixtures convert without mutation; scope/placement,
duplicate, pointer, citation, endpoint, and lifecycle defects are rejected. Unknown
resources and cross-platform external targets retain explicit context.

## Step 22.3: Offline Azure persistence and evidence workflow
**Status:** Implemented and verified; final compatibility acceptance is recorded in step 22.4.
**Prerequisites:** Step 22.2.

Add the cited VNet → subnet → VM fixture with explicit attachments and successful-zero,
incomplete, and failed coverage. Containment is a fixture choice, not attachment proof.
Public validation, immutable persistence, default snapshot ingestion, and evidence
inspection support schemas `1`–`3`. Coverage matches complete platform/scope/location/
service context; known-null and unavailable facts remain distinct.

**Advice:** Preserve no-overwrite publication and cancellation policy; add no Azure SDK,
authentication, or live collection flags.

**Acceptance check:** Both clouds round-trip through saves/imports; conflicts preserve
bytes, invalid mutations publish nothing, and redaction leaves caller data unchanged.
Installed script/module evidence journeys work outside checkout without credentials/network.

## Step 22.4: Common access boundary and compatibility acceptance
**Status:** Implemented and verified: 2,342 offline tests passed using `.venv/bin/python`.
Ruff lint/format, documentation numbering/links/examples, and whitespace checks passed.
**Prerequisites:** Steps 22.1–22.3.

Expose detached read-only `SnapshotAccess` for scope, resources, containment,
dependencies, evidence, and coverage across supported versions. This is a contract
adapter, not a graph index or assembly layer. Document extension points and align
phase 3 with cloud-qualified identities and provider-specific semantics.

**Advice:** Extend typed contracts explicitly, not through speculative GCP models,
registries, or AWS interpretations of Azure NSGs/RBAC.

**Acceptance check:** Focused/full offline tests, installed journeys, Ruff lint/format
excluding `.junie`, documentation examples/links, and whitespace pass using
`.venv/bin/python`. Live Azure collection and semantic analyzers remain deferred.
