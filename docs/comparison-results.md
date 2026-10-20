# Comparison result contract

A snapshot comparison reports what two explicitly selected snapshots record: matched
resources with their field differences, resources with no counterpart, and matches that are
ambiguous. It is a separate result type from [analysis results](analysis-results.md) and never
changes either snapshot. **It reports recorded differences and qualified absences, never proof
that a resource was added, removed, or has drifted.** This page describes the contract (`archdiscov.models.comparison`), the matching and
comparison rules, the persisted file, and the `compare` and `comparison` commands.

## Envelope

`ComparisonResult` (schema version `"1"`) has a labelled `left` and `right` side
(`ComparisonSnapshot`), a creation time, the `result_limit` it was made with (1 to 10000, default 1000), the entries, and snapshot-level limitations. Each side
records its snapshot UUID, platform, complete scope, source kind (`aws`, `azure_fixture`,
`terraform_state`, `terraform_plan`, `cloudformation`, or `mock`), an optional source label, and
the collection start and end, so left and right are never swapped silently. Two sides must be
labelled `left` then `right`, a side's scope must match its platform, and its collection must
not end before it starts.

`ComparisonLimitation` (`cross_cloud`, `scope_mismatch`, `source_kind_mismatch`,
`result_limit_reached`) is a structured, snapshot-level note about the comparison itself. Its
message is descriptive only, and each kind appears at most once.

## Entries

| Entry | Meaning |
|---|---|
| `MatchedPair` | two resources paired by `basis` (`physical_identity` or `logical_address`) in the same complete scope, with `differences` and `qualifications` |
| `UnmatchedResource` | a resource with no counterpart on its `side`, qualified by planned lifecycle or coverage only (a lifecycle concerns the resource's own side; coverage may concern either side) |
| `AmbiguousMatch` | several candidates share one match `key`: every candidate is listed on each side and none is chosen |

A resource appears in at most one entry on its side, resource references carry the scope of
their side, and every list is unique and in canonical order. Every entry has a deterministic
ID derived from all of its semantic fields; a supplied ID must match.

### Field differences

A `FieldDifference` has a `subject` (`field`, `dependency`, or `containment`), a JSON-pointer
`path`, a `kind` (`added`, `removed`, `changed`, or `indeterminate`), the two values, and the
evidence cited on each side. The value of the side where the path is absent is `null`; an added
field has no left value, a removed one no right value, and a changed one different values. An
`indeterminate` difference (a field that cannot be compared) carries no values and needs a
qualification saying why.

### Qualifications

A `Qualification` names the side it concerns and why a statement is qualified:

| Kind | Carries |
|---|---|
| `planned_lifecycle` | the Terraform plan's lifecycle (a planned deletion is not an observed absence) |
| `limited_coverage` | the recorded coverage operation that was failed, incomplete, skipped, or unsupported |
| `unrecorded_coverage` | the service only: no operation is invented |
| `unknown_value`, `unresolved_reference`, `redacted_value` | cited evidence only |

## Matching rules

Matching (`archdiscov.services.comparison_matching`) pairs the resources of two snapshots by
identity and never guesses:

- Resources match only inside one complete scope: the same cloud and the same AWS account and
  partition, or Azure subscription and tenant. Snapshots whose scopes differ in any way match
  nothing: every resource is unmatched on its side and the result carries a `cross_cloud` or
  `scope_mismatch` limitation.
- A resource with a physical identity matches on its native type and its scope-qualified
  physical identity (for AWS this includes the region and service). Azure ARM IDs and native
  types compare case-insensitively, and only Azure's do. A physical identity is never replaced
  by a logical address, and names and short IDs are never keys.
- A resource without a physical identity matches its logical address, including a Terraform
  module address and instance index, only when both snapshots come from the same IaC context
  (Terraform state or plan, or a CloudFormation template). Observed resources have no such
  context, and Terraform and CloudFormation addresses never match each other.
- When several resources of a side share one key and the other side has a candidate, every
  candidate is reported as an ambiguous match and none is chosen. A key found on one side only
  pairs nothing.
- Matching is symmetric apart from the side labels, deterministic, and linear in the number
  of resources. Differences between matched resources, lifecycle, and coverage qualification
  are described by the later comparison steps.

## Field comparison

Field comparison (`archdiscov.services.comparison_fields.FieldComparer`) compares each matched
pair and returns `FieldDifference`s in canonical order. A difference is a difference between two
recorded snapshots, not a drift verdict.

- **Fields** (`subject: field`): the resource document, meaning its name, resource type, typed
  fields, and attributes (including tags), compared recursively with escaped JSON-pointer paths.
  Lists are compared by position. The resource ID, identity, evidence IDs and fact citations,
  children (compared as their own resources), and the planned lifecycle (qualified separately)
  are not compared: they identify the snapshot, not the resource. The `terraform_type` attribute
  that only a Terraform import records is source bookkeeping and is not compared either.
- **Containment** (`subject: containment`, path `/parent`): the containment parent. Parents are
  compared by matched identity; the values are `{"left": ID, "right": ID}`, with `null` for a
  parent that has no counterpart.
- **Dependencies** (`subject: dependency`, path `/relationship/attachment kind/location/target`):
  outgoing edges grouped by relationship, attachment kind, and location, as sets of targets. A
  local target is compared by matched identity, so edges to two resources that match each other
  are equal; an external target is compared by its recorded target. Other edge attributes are
  not compared.
- **Citations**: each side cites the fact at the difference's path (or its nearest cited
  ancestor), an edge's own evidence, or else the resource's own evidence. No citation is invented,
  so a side may cite nothing.
- **Indeterminate differences** carry no values and a qualification: a value redacted on either
  side is never compared by content (`redacted_value`); a field absent from, or null and
  uncited on, a side whose resource recorded unknown or omitted values may be one of them, since
  a plan stores an unknown typed value as null (`unknown_value`); and a
  dependency group holding an unresolved reference cannot be judged against the other side's
  targets (`unresolved_reference`).

## Qualification

Qualification (`archdiscov.services.comparison_qualification`) labels each side and qualifies
entries; a qualified absence is never reported as a removal.

- **Sides** are labelled from the snapshot: platform, scope, source kind, a source label (the
  mock or fixture label, else the file path, else the AWS profile; never a role or credential),
  and the collection window.
- **Planned lifecycle**: a resource's Terraform plan lifecycle (including module-addressed
  resources) qualifies its matched pair or unmatched entry on its own side, citing the plan's
  action evidence. A planned deletion or replacement is a plan, not an observed absence.
- **Coverage** uses the shared contextual coverage helper. Recorded failed, incomplete,
  skipped, or unsupported operations for a resource's scope, location, and service give
  `limited_coverage`; a context with no recorded coverage gives `unrecorded_coverage` with only
  the service. An unmatched resource is qualified by the coverage of the side where it is
  absent, and only when both snapshots share a scope; each difference of a matched pair carries
  the coverage qualifications of both sides.
- **Snapshot limitations** are added for `cross_cloud` and `scope_mismatch` (nothing can
  match) and for `source_kind_mismatch` whenever the two source kinds differ, for example a
  declaration compared with an observation.

## Comparing, limits, and saved files

`compare_snapshots(left, right, result_limit=...)` (`archdiscov.services`) reads one read-only
graph index per snapshot and composes matching, field comparison, and qualification into one
deterministic `ComparisonResult`; neither snapshot is changed. Every matched pair is listed,
including pairs with no differences. Each kind of entry, and each side's unmatched resources,
keeps its first `result_limit` resources in ID order; when more exist the result carries a
`result_limit_reached` limitation, so truncation is always reported.

`save_comparison` and `load_comparison` use the same atomic, no-overwrite publication as
analysis results. Both take the two snapshots and verify the result by recomputing it from them
with its recorded `result_limit`: the left and right snapshot UUIDs must be the ones named
(`snapshot_mismatch` for swapped or wrong snapshots), and every label, entry, citation,
qualification, and limitation must equal the recomputation (`comparison_mismatch`, naming the
differing field). Only the creation time comes from the file. A result that fails is never
written, and a saved file is never modified. Unreadable or conflicting paths fail with the
same safe snapshot errors as other inputs, and invalid results with the safe
`invalid_comparison_result` diagnostics (no untrusted keys or values).

## CLI

```console
archdiscov compare --left LEFT.json --right RIGHT.json --output COMPARISON.json [--result-limit N] [--json]
archdiscov comparison COMPARISON.json --left LEFT.json --right RIGHT.json [--json]
```

`compare` saves a new result (never overwriting an existing path) and prints it; `comparison`
shows a saved result after verifying it against both snapshots. Readable output labels both
sides (snapshot, source kind and label, scope, collection window), then lists limitations and
the matched, unmatched, and ambiguous entries with their qualifications and cited evidence IDs;
inspect a citation with `archdiscov evidence ID --snapshot PATH`. Exit codes follow the other
commands: 1 for unreadable files, 2 for invalid or mismatched data, 3 for an existing output.
A comparison is not drift detection: it reports what two snapshots record, and an absence
qualified by coverage or a plan is not proof that anything was removed.
