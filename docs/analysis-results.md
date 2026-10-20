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
cross-account trust, database access (resource/port/result limit), and internet exposure
(depth/result limit). Defining a parameter model does not implement that analyzer; the
ownership, security-group impact, cross-account trust, and database-access analyzers are
implemented.

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
security-group impact analyzer ([details](security-group-impact.md)). It names the selected group, an `ImpactKind` and an `ImpactLabel`:

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

## Cross-account trust vocabulary

`AnalysisFinding.trust` (`CrossAccountTrust`) is optional and reserved for the
cross-account trust analyzer ([details](cross-account-trust.md)). It records one configured trust principal of the finding's first resource
(the trusting role or queue):

| Field | Values |
|---|---|
| `source` | `role_trust_policy` (an IAM role) or `resource_policy` (an SQS queue) |
| `classification` | `same_account`, `external_account`, `service`, `federated`, `wildcard`, `unresolved` |
| `effect` | `Allow` or `Deny`, from the cited statement |
| `conditional` | whether the statement carries a `Condition` |
| `not_principal` | whether the statement used `NotPrincipal` |

`NotPrincipal` names a complement that cannot be listed, so it must be classified
`unresolved` (matching how ingestion stores it). All fields are part of finding
identity; findings without a `trust` block keep exactly their earlier IDs. The
principal itself is the path step's target, and unresolved or external forms remain
terminals with limitations. This is configured policy only: it never asserts that the
principal can assume the role or reach the queue.

A finding carries at most one analyzer-specific block (`impact`, `trust`, `database_access`, or `internet_exposure`), trust
findings must name the trusting resource, and a result carrying them must use
`cross_account_trust` parameters. Integrity requires the trusting resource to be an IAM
role for `role_trust_policy` and an SQS queue for `resource_policy`
(`trust_source_mismatch`). The shared `unsupported_platform` limitation code
(`archdiscov.models.analysis.references.UNSUPPORTED_PLATFORM_CODE`) covers clouds
without IAM trust semantics, such as Azure.

## Database-access vocabulary

`AnalysisFinding.database_access` (`DatabaseAccess`) is optional and reserved for the
database-access analyzer (step 30). It records one compute source's potential network path
to the selected database port. The source is the finding's first resource reference and
the selected RDS instance or cluster its second:

| Field | Values |
|---|---|
| `source_kind` | `ec2_instance`, `ecs_task`, or `lambda_function` |
| `port` | the requested database port (1–65535) |
| `decision` | `allow`, `deny`, or `unknown`, composing the elements (a deny dominates an unknown, which dominates an allow) |
| `elements` | each of `source_egress`, `database_ingress`, `source_nacl`, `database_nacl`, `route` at most once, in that order, with its `decision` and sorted `blocker_codes` |

Element decisions use the [network-path vocabulary](network-path-primitives.md): an
allow has no blocker codes, a deny only `denied`, and an unknown at least one code that
explains it. Cited detail lives in the finding's evidence, path, and limitations. Every
field is part of finding identity; findings without the block keep their earlier IDs.

An `allow` or `deny` finding is `supported` (or `partial` when qualified); an `unknown`
decision can never be `supported`. A result carrying these findings must use
`database_access` parameters (`resource_id`, `port`, and a `result_limit` between 1 and
10000, default 100), the second resource and the port must match them, and integrity
requires the source to match its kind (`database_source_mismatch`) and the database to be
an RDS instance or cluster (`database_target_mismatch`). A finding is network permission
from recorded configuration: never authentication, credentials, IAM database access, or
live reachability.

## Internet-exposure vocabulary

`AnalysisFinding.internet_exposure` (`InternetExposure`) is optional and reserved for the
internet-exposure analyzer (step 31). It records the candidate exposure of one entry point on
a protocol and port range. The entry point is the finding's first resource reference, followed
by any targets reached through a load balancer:

| Field | Values |
|---|---|
| `entry_kind` | `public_address` (an EC2 instance or network interface with a recorded public IPv4 address) or `load_balancer` (an ELBv2 load balancer) |
| `protocol`, `port_start`, `port_end` | `tcp` or `udp`, and an inclusive port range (1–65535) |
| `decision` | `allow`, `deny`, or `unknown`, composing the elements (a deny dominates an unknown, which dominates an allow) |
| `elements` | each at most once, in this order, with its `decision` and sorted `blocker_codes`: `public_address`, `route`, `nacl`, `security_group` for a public address; `scheme`, `route`, `nacl`, `security_group`, `listener`, `target` for a load balancer |
| `observed_target_health` | the sorted, unique health states observed for the reached targets (load balancers only) |

Element decisions use the [network-path vocabulary](network-path-primitives.md) and the same
rules as the database-access vocabulary. Target health is an observation, not an element: it
is never part of the composed decision. Every field is part of finding identity, and findings
without the block keep their earlier IDs. `allow` and `deny` findings are `supported`
(`partial` when qualified) and an `unknown` decision can never be `supported`. A result
carrying these findings must use `internet_exposure` parameters, and integrity requires the
entry point to be an EC2 instance or network interface for `public_address` and a load
balancer for `load_balancer` (`exposure_entry_mismatch`). A finding is candidate exposure from
recorded configuration, never proof of reachability.

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