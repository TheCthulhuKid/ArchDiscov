# Security-group impact analysis

`security_group_impact` reports what recorded configuration says about one AWS
security group: which resources are attached to it, which groups reference it or are
referenced by it, and what those resources depend on downstream. It reads the shared
`GraphIndex` through the [analyzer boundary](ownership-analysis.md); it never contacts
a provider. It is the second implemented analyzer (after ownership).

**Observed structure is not reachability.** Nothing here evaluates whether traffic is
allowed, whether a route exists, or what access is effective.

## Findings

Each finding has an `impact` (see the [vocabulary](analysis-results.md#security-group-impact-vocabulary)),
cites the stored edge's source-owned evidence, and carries the edge as a path step with
its attributes unchanged.

| Kind | Label | Source |
|---|---|---|
| `direct_attachment` | `observed` | A recorded `security_group_membership` edge from an EC2 instance, ENI, load balancer, ECS, Lambda, or RDS resource to the group |
| `rule_reference` | `observed` | A recorded `security_group_reference` edge, with `rule_direction` (ingress/egress) and `reference_side` (selected group references another, or another references it); protocol/port context stays as recorded |
| `downstream_dependency` | `potential` | A dependency chain from an impacted resource, traced with the bounded path traversal |

Rules:

- Attachment is only an explicit membership edge. VPC or subnet containment never
  implies attachment.
- A group referencing itself is reported once. Prefix-list references are not groups
  and are skipped (step 54 names them with their entries). A reference with no recorded
  rule direction cannot be labelled and becomes a `rule_direction_unrecorded` limitation.
- Cross-account or otherwise external references and unresolved references stay
  terminals with `external_target` / `unresolved_target` limitations; no resource is
  invented, and a chain stops at them.
- Downstream tracing starts from each reported attached resource and referencing group
  (retained findings only), follows outgoing dependencies but not containment, never
  re-enters the selected group, and stops at cycles. `--max-depth` counts steps from
  the starting resource.

## Qualification and limits

Each finding is qualified through the shared coverage helper: failed, incomplete,
skipped, or unsupported collection for the traced resources downgrades it to
`partial`. A group-level `insufficient_evidence` finding appears when needed and
reports:

- limited coverage for any attaching service (ec2, elasticloadbalancing, ecs, lambda,
  rds) in the group's region, per recorded operation;
- `coverage_unrecorded_<service>` for a service with no recorded collection (no
  operation name is invented), so an absent attachment is not read as proof;
- `result_limit_reached`, `depth_limit_reached`, and `work_limit_reached` truncation.

`--result-limit` bounds all impact findings (direct, rule, downstream, in that order);
downstream tracing shares a fixed work budget. Results are deterministic. A membership
reference that cannot be tied to the selected group (for example an unresolved
identifier) is not reported.

Azure snapshots return one `insufficient_evidence` finding with an `unsupported_platform`
limitation; AWS semantics are not reused. A missing group raises `resource_not_found`
and a non-group `unsupported_resource` (exit code 2).

## CLI

```text
archdiscov analyze --analyzer security_group_impact --resource-id GROUP_ID \
    --snapshot SNAPSHOT --output RESULT [--max-depth 1-10] [--result-limit N] [--json]
archdiscov findings RESULT --snapshot SNAPSHOT [--json]
archdiscov evidence EVIDENCE_ID --snapshot SNAPSHOT [--json]
```

`findings` prints each finding's impact label, kind, and (for rule references) rule
direction and side. Results are saved atomically, never overwrite an existing path, and
are verified against their snapshot (integrity requires the selected resource to be a
security group). `--ownership-key` is rejected for this analyzer, and
`--resource-id`/`--max-depth`/`--result-limit` for ownership.
