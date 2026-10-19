# Cross-account trust analysis

`cross_account_trust` reports who IAM role trust policies and SQS queue policies are
configured to trust, from recorded snapshot data. It reads the shared `GraphIndex`
through the [analyzer boundary](ownership-analysis.md) and never contacts a provider. It
is the third implemented analyzer (after ownership and
[security-group impact](security-group-impact.md)).

**Configured trust is not effective access.** A finding never says a principal can assume
a role or reach a queue: it ignores permission boundaries, SCPs, identity policies, and
conditions' runtime outcome.

## Findings

One finding per recorded principal entry, citing the principal entry and its whole
sanitized statement (so `Effect` and `Condition` come from that cited statement, and
policy documents are not re-parsed):

| Source | Recorded edge | Trusting resource |
|---|---|---|
| `role_trust_policy` | `trust_principal` | IAM role |
| `resource_policy` | `resource_policy_principal` | SQS queue (recorded at ingestion from the stored queue policy) |

Each finding carries a structured `trust` block (see the
[vocabulary](analysis-results.md#cross-account-trust-vocabulary)):

- **Classification:** `same_account` (a collected role, or an account equal to the
  snapshot's), `external_account` (a named account), `service`, `federated`, `wildcard`
  (the cited entry contains `*` or `?`), or `unresolved` (for example a principal ID
  that names no account, an unsupported form, or any `NotPrincipal`).
- **Effect:** `Allow` or `Deny`, kept from the statement; **conditional** when the
  statement carries a `Condition`; **NotPrincipal** statements are always `unresolved`.
- External, wildcard, and unresolved principals stay terminals with `external_target` or
  `unresolved_target` limitations; no resource is invented.
- A statement with no valid `Effect` is `insufficient_evidence` (`trust_statement_unavailable`),
  never trust.

## Qualification and limits

- Findings are qualified through the shared helper: failed, incomplete, skipped, or
  unsupported IAM or SQS collection downgrades them to `partial`.
- A role with no stored trust document yields `insufficient_evidence`
  (`trust_document_unavailable`); a queue yields `policy_unavailable` only when its policy
  is explicitly recorded as unreadable. A queue with no policy is normal and produces
  nothing. Missing trust data is never read as an absence of trust.
- A group-level `insufficient_evidence` finding appears when needed, reporting limited IAM
  or SQS operations by their recorded coverage, `coverage_unrecorded_iam` or
  `coverage_unrecorded_sqs` when a service has no recorded collection (no operation is
  invented), and `result_limit_reached`.
- Group-level coverage checks look at every recorded IAM and SQS operation, not per queue
  region; a queue finding is still qualified by the coverage recorded for its own region.
- `--result-limit` (default 100, maximum 10,000) bounds findings; results are
  deterministic.

Terraform and CloudFormation imports do not ingest queue policies, so they contribute no
resource-policy findings. Azure RBAC is not IAM trust: Azure snapshots return one
`insufficient_evidence` finding with an `unsupported_platform` limitation.

## CLI

```text
archdiscov analyze --analyzer cross_account_trust --snapshot SNAPSHOT --output RESULT \
    [--result-limit N] [--json]
archdiscov findings RESULT --snapshot SNAPSHOT [--json]
archdiscov evidence EVIDENCE_ID --snapshot SNAPSHOT [--json]
```

`findings` prints each trust finding's source, classification, effect, and any
`conditional` / `NotPrincipal` qualifier. Results are saved atomically, never overwrite an
existing path, and are verified against their snapshot (the trusting resource must be an
IAM role or SQS queue matching the source). `--resource-id`, `--max-depth`, and
`--ownership-key` are rejected for this analyzer. Schema 1 snapshots hold only local
dependencies, so they can report only principals that are collected roles.
