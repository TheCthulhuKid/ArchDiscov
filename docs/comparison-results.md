# Comparison result contract

A snapshot comparison reports what two explicitly selected snapshots record: matched
resources with their field differences, resources with no counterpart, and matches that are
ambiguous. It is a separate result type from [analysis results](analysis-results.md) and never
changes either snapshot. **It reports recorded differences and qualified absences, never proof
that a resource was added, removed, or has drifted.** This page describes the contract
(`archdiscov.models.comparison`); the matching, comparison, persistence, and CLI steps build on
it and are described as they are implemented.

## Envelope

`ComparisonResult` (schema version `"1"`) has a labelled `left` and `right` side
(`ComparisonSnapshot`), a creation time, the entries, and snapshot-level limitations. Each side
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
| `UnmatchedResource` | a resource with no counterpart on its `side`, qualified by planned lifecycle or coverage only |
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
  are not compared: they identify the snapshot, not the resource.
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
  side is never compared by content (`redacted_value`); a field absent from a side whose
  resource recorded unknown or omitted values may be one of them (`unknown_value`); and a
  dependency group holding an unresolved reference cannot be judged against the other side's
  targets (`unresolved_reference`).
