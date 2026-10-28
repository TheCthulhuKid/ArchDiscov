# Database access analysis

`database_access` reports which compute sources have a recorded potential network path to a
selected RDS instance or cluster on a port, from recorded snapshot data. It reads the shared
`GraphIndex` through the [analyzer boundary](ownership-analysis.md), composes the
[network-path primitives](network-path-primitives.md), and never contacts a provider. It is
the fourth implemented analyzer (after ownership, [security-group impact](security-group-impact.md),
and [cross-account trust](cross-account-trust.md)).

**A finding is network permission from recorded configuration.** It never says that a client
authenticated, holds credentials, or has IAM database access, and it does not prove live
reachability. Candidates are placements, not usage: nothing says the compute resource
actually talks to the database.

## Candidates and decisions

For a selected database and port, every EC2 instance, awsvpc ECS task, and Lambda function in
the database's VPC is a candidate. Each has one or more interfaces (a subnet, security groups,
and the recorded private IPv4 addresses). The analyzer composes, for tcp on the port:

| Element | Evaluated |
|---|---|
| `source_egress` | the source's security-group egress to the database |
| `database_ingress` | the database's security-group ingress for the source, by CIDR or by a group reference through the source's recorded groups |
| `source_nacl`, `database_nacl` | each subnet's NACL for the request and for the return path (omitted when both ends share a subnet) |
| `route` | the local route in both directions |

Elements must all allow. A source's interfaces and addresses are alternatives (the best is
reported). The database sits in one of its recorded subnets, which is not recorded, so every
possible subnet must work (the worst case decides). The database address is bounded by its
subnet CIDR and a source without a recorded address by its subnet, so a rule covering only
part of that range is `unknown`. The return path is checked on one representative ephemeral
port (49152) and security-group statefulness is not modelled.

| Decision | Finding status |
|---|---|
| `allow`, `deny` | `supported` (`partial` when relevant collection was limited) |
| `unknown` | `insufficient_evidence`, with blockers as limitations |

Each finding names the source and then the database, cites the rules, entries, routes, and
placement used, and carries a structured `database_access` block (see the
[vocabulary](analysis-results.md#database-access-vocabulary)). Missing or unsupported
elements (prefix lists without recorded entries, cross-account references, and sources
in other VPCs, which step 54 evaluates through peering and transit gateways) are `unknown`,
never an allow; security-group rules and routes naming
a prefix list are decided from the list's recorded entries, and IPv6 rules and
entries never match the IPv4 addresses evaluated. A recorded denial still dominates an
unknown element.

## Placement, qualification, and limits

- The database's subnets, groups, VPC, and endpoint port come from recorded attachments and
  facts. A cluster takes its stored member instances. Missing or conflicting placement and a
  requested port that differs from, or is missing in, the recorded port are limitations in a
  group-level `insufficient_evidence` finding, never absence of access.
- Sources that cannot be placed (no subnet or security group, non-awsvpc tasks, Lambda
  without VPC configuration) are `unknown` candidates. Sources in other VPCs are unsupported
  and only counted (`sources_other_vpc`).
- Findings are qualified by recorded EC2, RDS, ECS, and Lambda coverage; unrecorded services
  are reported as `coverage_unrecorded_<service>` without inventing operation names.
- `--result-limit` (default 100, maximum 10,000) keeps decided sources first (allow, unknown,
  then deny, then source kind and identity) and reports `result_limit_reached`. Results are
  deterministic and ordered by finding identity.
- An unknown selection is `resource_not_found` and a resource that is not an RDS instance or
  cluster `unsupported_resource`. Azure snapshots return one `insufficient_evidence` finding
  with an `unsupported_platform` limitation.

## CLI

```text
archdiscov analyze --analyzer database_access --snapshot SNAPSHOT --output RESULT \
    --resource-id RDS_ID --port N [--result-limit N] [--json]
archdiscov findings RESULT --snapshot SNAPSHOT [--json]
archdiscov evidence EVIDENCE_ID --snapshot SNAPSHOT [--json]
```

`findings` prints each finding's source kind, decision, port, and every element's decision
with its blocker codes. Results are saved atomically, never overwrite an existing path, and
are verified against their snapshot. `--max-depth` and `--ownership-key` are rejected for
this analyzer, and `--port` is rejected for the others.
