# Analysis result contracts

Step 23 implements contracts and snapshot-backed integrity. Analyzer execution,
persistence, and the ownership analyzer are described in
[ownership analysis](ownership-analysis.md). Snapshot schemas 1–3 and
legacy `Finding`/`Answer` models remain unchanged.

## Models

`archdiscov.models.analysis.results.AnalysisResult` has result `schema_version="1"`,
an explicit snapshot UUID, `AnalyzerKind`, nonblank analyzer version, typed
parameters, timezone-aware creation time, and findings. Parameters must match the
selected analyzer; finding UUIDs must match the envelope and IDs must be unique.
An empty findings list is allowed, but does not by itself prove complete analysis.

The discriminated parameter models in `models.analysis.parameters` cover ownership
(default keys `Owner`, `Team`), security-group impact (resource/depth/result limit),
cross-account trust, database access (resource/port), and internet exposure
(depth/result limit). Defining a parameter model does not implement that analyzer.

`AnalysisFinding` contains a summary, status, evidence IDs, cloud-aware resource
references, ordered path steps, and structured limitations. Supported and partial
findings require evidence; partial and insufficient-evidence findings require
limitations, preserving the existing status rules. Resource scopes explicitly
distinguish AWS accounts/partitions and Azure subscriptions/tenants.

Paths distinguish containment from dependencies. Containment may have no evidence
because it describes stored tree structure, not a discovered attachment. Any
supplied containment evidence must belong to the parent. Dependencies preserve
relationship, attributes, local/external/unresolved target, and nonempty citations.
External and unresolved targets terminate a path; they never imply invented nodes.

Limitations have a nonblank code/message and optional evidence, complete operation
coverage context, or a source-owned unresolved dependency reference. Noncoverage
scope caveats are allowed even for supported findings. These contracts do not
automatically downgrade findings based on coverage; the shared qualification helper
in [ownership analysis](ownership-analysis.md) does.

## Security-group impact vocabulary

`AnalysisFinding.impact` (`SecurityGroupImpact`) is optional and used only by the
security-group impact analyzer, which is not implemented yet; this step defines the
vocabulary. It names the selected group, an `ImpactKind` and an `ImpactLabel`:

| Kind | Label | Meaning |
|---|---|---|
| `direct_attachment` | `observed` | A recorded membership edge attaches the finding's resource to the group |
| `rule_reference` | `observed` | A recorded rule references the group, or the group's rule references the resource; also carries `rule_direction` (ingress/egress) and `reference_side` |
| `downstream_dependency` | `potential` | A traced dependency path from an impacted resource; a possibility, not a conclusion |

The label must match the kind, rule fields appear only on rule references, and the
label, kind, and rule fields are part of finding identity. Findings without an `impact`
keep exactly their earlier IDs. The impacted resource is the finding's resource
reference; paths and citations use the existing path and evidence fields. Observed
means recorded structure, not reachability or effective access.

A result carrying impact findings must use `security_group_impact` parameters whose
`resource_id` is the impact's selected group, and integrity requires that resource to
exist in the snapshot and be a security group (`resource_not_security_group`).
Parameter-only results keep the earlier looser selection check. A cloud the analyzer
does not support is reported as an `insufficient_evidence` finding with an
`unsupported_platform` limitation (`UNSUPPORTED_PLATFORM_CODE`) rather than an impact.
Analyzer errors `resource_not_found` and `unsupported_resource` have fixed safe messages.

## Deterministic IDs

Finding IDs are derived from normalized finding content with SHA-256, scoped by the
snapshot UUID. The envelope creation time is not part of finding identity. Supplied
IDs must match derivation, so changed findings require newly derived IDs rather
than retaining stale ones. Summary changes produce a new ID; limitation wording
and envelope metadata do not. Resources, limitations and citation lists are
canonically sorted; path and attribute-list order remain significant. See the model's
`deterministic_finding_id` helper for canonicalization details.

## Validation API

```python
import json
from pathlib import Path

from archdiscov.services import load_snapshot, validate_analysis_result

snapshot = load_snapshot("tests/fixtures/analysis/ownership-snapshot.json")
data = json.loads(Path("tests/fixtures/analysis/ownership.json").read_text())
result = validate_analysis_result(data, snapshot)
assert result.findings[0].evidence_ids == ["tags"]
```

The API returns a newly validated detached model, including when given mutable
models nested within mappings. It uses `SnapshotAccess` for schemas 1–3; no implicit
file migration occurs. It checks:

- Matching snapshot UUIDs and resource scope; all resource and evidence IDs exist.
- Exact stored dependency targets, relationships, attributes, and edge citations.
- Actual containment edges, path continuity, and source ownership of path evidence.
- Complete cloud/scope/location/service/operation coverage references; Azure service
  matching follows the snapshot's case-insensitive namespace rule.
- Unresolved-target limitations match a stored dependency and its citations.

`AnalysisValidationError` exposes `code="invalid_analysis_result"` and safe
`details` (location/type/fixed message), retaining the cause for debugging but not
raw input, dynamic keys, or validator contexts in public diagnostics. Invalid
snapshots retain the existing snapshot validation errors.

Integrity proves that references correspond to stored data, **not** that a summary
is true, an analyzer ran, coverage is sufficient, or citations were returned by a
tool. Free-text summaries/limitations must not contain secrets. Models remain
mutable; revalidate after changes. Immutable result saving is step 26.

## Fixtures and compatibility

`tests/fixtures/analysis/ownership.json` cites the observed tags in
`ownership-snapshot.json`. `partial-path.json` cites the unresolved dependency in
`tests/fixtures/snapshots/dependency-targets.json`; it makes no reachability claim.
These are hand-authored contract examples, not analyzer output. Tests validate
legacy AWS, explicitly upgraded AWS, and schema-3 Azure references and coverage,
with isolated rejection cases. Existing `Finding` and `Answer` tests are retained.