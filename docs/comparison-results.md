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
