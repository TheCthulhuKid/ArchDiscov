# Result file contracts

Analyzers and comparisons each write a separate, versioned JSON result file that names the
snapshot UUID or UUIDs it was made from. Result files are not snapshots, never change a
snapshot, and are immutable once saved. This page compares the two contracts; the linked pages
define them.

| | Analysis result | Comparison result |
|---|---|---|
| Written by | `archdiscov analyze` / `save_analysis` | `archdiscov compare` / `save_comparison` |
| Contract | [analysis results](analysis-results.md) | [comparison results](comparison-results.md) |
| Model | `AnalysisResult`, schema version `"1"` | `ComparisonResult`, schema version `"1"` |
| Names | one snapshot UUID, the analyzer kind and version, and typed parameters | both snapshot UUIDs with a labelled left and right side (platform, scope, source kind, label, collection window) and the result limit |
| Holds | findings with status, cited evidence, paths, and limitations | matched pairs with field differences, unmatched resources per side, ambiguous matches, qualifications, and limitations |
| Identity | deterministic finding IDs over the content (creation time excluded) | deterministic entry IDs over every semantic field |
| Loaded with | `load_analysis(path, snapshot=...)`, `archdiscov findings` | `load_comparison(path, left=..., right=...)`, `archdiscov comparison` |
| Error | `AnalysisValidationError` (`invalid_analysis_result`) | `ComparisonValidationError` (`invalid_comparison_result`) |

## Immutability

Both kinds are published atomically through a temporary file and a hard link, so an existing
path is never overwritten (exit code 3, `SnapshotConflictError`), including by an identical
result. A result that fails verification is never written. Loading never modifies the file or
a snapshot, and loaded models are mutable copies: revalidate after changing one.

## Integrity checks

- **Analysis results** are checked against the selected snapshot: the UUID matches, every
  resource and evidence ID exists, dependency and containment path steps are stored edges with
  source-owned citations, coverage references name recorded operations, and the
  analyzer-specific blocks match the resources they describe. Finding IDs derive from
  content, so an edited file fails even without a snapshot.
- **Comparison results** are verified by recomputing the comparison from both snapshots with
  the recorded result limit. The left and right UUIDs must be the ones named
  (`snapshot_mismatch`), and every label, entry, citation, qualification, and limitation must
  equal the recomputation (`comparison_mismatch`). Only the creation time is taken from the
  file.

Integrity proves that references correspond to stored data. It does not prove that a summary
is true, that coverage is sufficient, or that a resource was added, removed, or has drifted.

## Safe diagnostics

Both error types share their shape: a fixed `code`, a fixed message, the file path, and
`details` of location, type, and fixed message. Locations keep only known field names and
list positions, and raw input, dynamic keys, and validator contexts are never exposed. Read,
JSON, and conflict failures reuse the snapshot error types with their exit codes: `1`
unreadable, `2` invalid or mismatched data, and `3` an existing output.

## Limits

Both kinds are bounded and report truncation explicitly (`result_limit_reached`):

| Limit | Applies to | Default | Range |
|---|---|---|---|
| `result_limit` | findings of an analyzer | 100 | 1–10,000 |
| `result_limit` | each kind of comparison entry, and each side's unmatched resources | 1,000 | 1–10,000 |

See the [analyzer reference](analyzers.md) and the [query limits](graph-queries.md#limits-at-a-glance).
