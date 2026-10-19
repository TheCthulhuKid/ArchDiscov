# Analyzer execution, persistence, and the ownership analyzer

Step 26 delivers a small analyzer boundary, atomic result persistence, and the
ownership analyzer; [security-group impact](security-group-impact.md) is the second.
No other analyzer is implemented; the remaining parameter models in
[analysis result contracts](analysis-results.md) are contracts only. Analyzers read
recorded snapshot data through the shared `GraphIndex`; they do not contact a cloud
provider, and their findings are recorded signals, not proof.

## Execution

`archdiscov.services.run_analyzer(index, parameters)` validates typed parameters
(a mapping or model), looks up the registered analyzer, runs it, and checks that the
returned result names the same snapshot, analyzer kind, version, and parameters. It
returns a revalidated, detached copy. `DEFAULT_REGISTRY` currently holds the ownership and
security-group impact analyzers; unavailable kinds raise `AnalyzerError(unsupported_analyzer)`.
`AnalyzerError` codes (`unsupported_analyzer`, `invalid_parameters`, `invalid_result`)
carry fixed messages that never echo parameter values. An analyzer implements the
`Analyzer` protocol (`kind`, `version`, `analyze(index, parameters)`) and builds its
result with `build_result`, which sorts findings by ID.

## Coverage qualification

`qualify_status(index, resources, status)` is the one place coverage changes a result:

- A failed, incomplete, skipped, or unsupported operation recorded for a resource's
  scope, location, and service adds one `coverage_<status>` limitation, deduplicated
  across resources, and turns a `supported` conclusion into `partial`.
- `partial` and `insufficient_evidence` are never upgraded.
- Successful operations, including successful-zero collection, are not limitations.
- Absent coverage records are not interpreted: no limitation is added for them.

## Persistence

`save_analysis(result, path, snapshot=...)` publishes only a result that passes the
integrity check against the given snapshot. Publication is atomic and never overwrites
an existing path. `load_analysis(path, snapshot=...)` reads UTF-8 JSON, checks the result
contract, and, when given the snapshot, the full integrity check. Finding IDs derive from
content, so edited files are rejected even without a snapshot. I/O, JSON, and conflict
failures reuse the snapshot error types; validation failures raise
`AnalysisValidationError` with the file path and sanitized details.

## Ownership semantics

The analyzer inspects each resource's recorded `attributes.tags`, without branching on
cloud platform:

- A key counts as an owner tag when it is recorded with a nonblank string value. Key
  matching is exact and case-sensitive; an empty or whitespace value does not count.
  Defaults are `Owner` and `Team`; any listed key is enough.
- A resource with an owner tag produces no finding.
- A resource without one is a **supported** signal only if its whole tag map is cited as
  present; the finding cites that evidence.
- If tags are not recorded, uncited, redacted, omitted, or unresolved, the finding is
  **insufficient evidence** with a `tags_unavailable` limitation. Missing tags are never
  reported as absence without provenance.
- Coverage qualification may downgrade a supported signal to **partial**.

Limits: a finding means the recorded data shows no usable ownership tag, not that nobody
owns the resource. Ownership may be recorded elsewhere (account structure, IaC, a CMDB),
and collectors may not record tags for every resource type or cloud.

## CLI

```text
archdiscov analyze --snapshot SNAPSHOT --output RESULT [--analyzer ownership] [--ownership-key KEY]... [--json]
archdiscov findings RESULT --snapshot SNAPSHOT [--json]
archdiscov evidence EVIDENCE_ID --snapshot SNAPSHOT [--json]
```

`analyze` loads the snapshot and index once, runs the analyzer, and saves the verified
result. `findings` reloads it with full verification and shows status, summary,
resources, cited evidence IDs, and limitations; inspect a cited ID with `evidence`.
Exit codes: `0` success, `1` unreadable file, `2` invalid request, result, or snapshot
(including an unsupported analyzer or a result for a different snapshot), `3` existing
output path.
