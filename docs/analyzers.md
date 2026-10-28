# Analyzer reference

Five deterministic analyzers run over a saved snapshot through the shared
[graph index](graph-index.md) and the [analyzer boundary](ownership-analysis.md). They read
recorded data only: none contacts a cloud provider, and none proves reachability, effective
access, ownership, or drift. Each guide linked below holds the full rules; this page is the
one place to compare them.

```text
archdiscov analyze --snapshot SNAPSHOT --output RESULT [--analyzer KIND] [options] [--json]
archdiscov findings RESULT --snapshot SNAPSHOT [--json]
archdiscov evidence EVIDENCE_ID --snapshot SNAPSHOT [--json]
```

`analyze` saves a verified, immutable [analysis result](result-contracts.md); `findings`
shows it again after verifying it against the snapshot, and `evidence` inspects a cited ID.

## Analyzers

| Analyzer (`kind`) | Reports | Supported cases | Guide |
|---|---|---|---|
| `ownership` | resources with no usable owner tag | recorded `attributes.tags` on any resource of any supported cloud | [ownership](ownership-analysis.md) |
| `security_group_impact` | what is attached to, references, or depends on one security group | AWS security groups: observed attachments and rule references, potential downstream dependencies | [security-group impact](security-group-impact.md) |
| `cross_account_trust` | who IAM role trust policies and SQS queue policies are configured to trust | AWS IAM roles and SQS queues | [cross-account trust](cross-account-trust.md) |
| `database_access` | compute sources with a recorded potential network path to one database port | AWS EC2 instances, awsvpc ECS tasks, and Lambda functions to an RDS instance or cluster | [database access](database-access.md) |
| `internet_exposure` | entry points that could expose a service to the internet | AWS public addresses (instances and interfaces) and ELBv2 load balancers | [internet exposure](internet-exposure.md) |

Network decisions come from the [network-path primitives](network-path-primitives.md), which
cover IPv4 security-group rules, stateless NACLs, subnet and internet-gateway routes, public
addresses, and load-balancer forwarding.

## Parameters and options

| Analyzer | Parameter | CLI option | Default | Range |
|---|---|---|---|---|
| `ownership` | `ownership_keys` | `--ownership-key` (repeatable) | `Owner`, `Team` | one or more unique keys |
| `security_group_impact` | `resource_id` | `--resource-id` | required | stable ID of a security group |
| `security_group_impact` | `max_depth` | `--max-depth` | 3 | 1–10 |
| `security_group_impact` | `result_limit` | `--result-limit` | 100 | 1–10,000 |
| `cross_account_trust` | `result_limit` | `--result-limit` | 100 | 1–10,000 |
| `database_access` | `resource_id` | `--resource-id` | required | stable ID of an RDS instance or cluster |
| `database_access` | `port` | `--port` | required | 1–65,535 |
| `database_access` | `result_limit` | `--result-limit` | 100 | 1–10,000 |
| `internet_exposure` | `max_depth` | `--max-depth` | 3 | 1–10 |
| `internet_exposure` | `result_limit` | `--result-limit` | 100 | 1–10,000 |

An option an analyzer does not take is rejected (`invalid_parameters`, exit code 2). The
result records its parameters, so a saved result states what was asked. `--analyzer` defaults
to `ownership`. Result limits keep the most decisive findings first and report
`result_limit_reached`; depth and work limits report `depth_limit_reached` and
`work_limit_reached`. Results are deterministic and ordered by finding ID.

## Uncertainty rules

All analyzers share these rules; the guides add analyzer-specific ones.

- **Status.** `supported` needs cited evidence. `partial` and `insufficient_evidence` need
  limitations. `partial` and `insufficient_evidence` are never upgraded.
- **Coverage.** A failed, incomplete, skipped, or unsupported operation recorded for a
  resource's scope, location, and service turns a `supported` conclusion into `partial`.
  Successful collection, including successful-zero, is no limitation. A service with no
  recorded coverage is reported as `coverage_unrecorded_<service>` without inventing an
  operation, and an absent record is never read as proof of absence.
- **Network decisions.** `allow` and `deny` are supported; `unknown` is
  `insufficient_evidence` with blockers as limitations. A recorded denial dominates an unknown
  element, and an unknown dominates an allow. Missing or unsupported elements (prefix lists
  without recorded entries, endpoints, and paths across VPCs until step 54) are `unknown`,
  never `allow`, and a route to a NAT gateway, peering connection, or transit gateway
  carries no internet traffic inbound; security-group rules and
  routes naming a prefix list are decided from its recorded entries, and an IPv6 rule or
  entry never matches an IPv4 question.
- **Unknown, unresolved, redacted, and external elements** stay terminals with structured
  limitations; no resource is invented and a path stops there.
- **Findings are signals.** Candidate exposure, configured trust, observed attachment, and
  recorded ownership tags are recorded configuration, not proof.

## Limitations

| Analyzer | Does not |
|---|---|
| `ownership` | say nobody owns a resource; ownership may be recorded elsewhere, and missing or uncited tags are insufficient evidence, not absence |
| `security_group_impact` | evaluate whether traffic is allowed; attachment is only an explicit membership edge, and downstream impact is `potential` |
| `cross_account_trust` | say a principal can assume a role or reach a queue; permission boundaries, SCPs, identity policies, and conditions' outcomes are not evaluated, and Terraform and CloudFormation imports contribute no queue policies |
| `database_access` | say a client authenticated or uses the database; candidates are placements, and sources in other VPCs are only counted |
| `internet_exposure` | consider host firewalls, authentication, the application, or runtime state; observed target health is recorded but never decides a path |

## Azure and other clouds

Azure snapshots work with the generic mechanics (queries, evidence, comparison) but not with
AWS semantics. Every analyzer except `ownership` returns one `insufficient_evidence` finding
with an `unsupported_platform` limitation instead of applying AWS rules. `ownership` reads
recorded tags without branching on cloud. The Azure foundation is offline fixtures only, not
Azure discovery or analysis.

## Errors

`analyze` fails with a fixed, safe message that never echoes parameter values:
`unsupported_analyzer`, `invalid_parameters`, `invalid_result`, `resource_not_found` (an
unknown selected resource), and `unsupported_resource` (a resource of the wrong kind). All
exit with code 2; an unreadable file exits 1 and an existing output path exits 3.

## Not analyzers

[Graph queries](graph-queries.md) return recorded structure and
[comparisons](comparison-results.md) report recorded differences between two snapshots. Both
have their own result contracts and are not analysis results. The question-answering
[assistant](ask-command.md) and the [MCP server](mcp-server.md) read analysis results through
the assistant tools; they are not analyzers either.
