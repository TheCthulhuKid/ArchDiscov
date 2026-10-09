# Account-wide IAM collection mapping (step 14)

`archdiscov collect --service iam` collects IAM once per verified account, independently of
regional EC2 collection. Resources live in `global_resources`; IAM coverage and dependencies
use `global`. This is observed configuration, not effective-policy evaluation or proof of access.

## Read operations and permissions

The exact eight IAM operations are allowlisted in `archdiscov.ingestion.operations`:

| Operation / required read permission | Requests and retained response records |
| --- | --- |
| `iam:ListRoles` | Account-wide, paginated `Roles[i]` |
| `iam:GetRole` | One request per discovered `RoleName`; whole response containing `Role` |
| `iam:ListInstanceProfiles` | Account-wide, paginated `InstanceProfiles[i]` |
| `iam:ListAttachedRolePolicies` | Per discovered `RoleName`, paginated `AttachedPolicies[i]` |
| `iam:ListRolePolicies` | Per discovered `RoleName`, paginated `PolicyNames[i]` |
| `iam:GetRolePolicy` | Per discovered `(RoleName, PolicyName)`; whole inline-policy response |
| `iam:GetPolicy` | Per distinct referenced `PolicyArn`; whole response containing `Policy` |
| `iam:GetPolicyVersion` | Per `(PolicyArn, VersionId)` discovered from `DefaultVersionId`; whole response containing `PolicyVersion` |

Account identity is checked with STS `GetCallerIdentity`; configured role collection additionally
uses `sts:AssumeRole`. EC2 selection requires the separate reads in
[network-mapping.md](network-mapping.md). No writes, user/group inventory, arbitrary policy
enumeration, policy-version history, or traversal into trusted accounts are performed.

Managed policies are fetched only when referenced by a collected role attachment or permissions
boundary. Both customer-managed and AWS-managed policy ARNs are supported. Only the discovered
default version matching `v[0-9]+` is requested; no older version or replacement document is
substituted. An unattached policy is not inventoried. Requests are deduplicated and sorted;
concurrency, timeouts, and retries use `AwsAccountConfig` limits.

## Identities, fields, and evidence

Roles use AWS `RoleId`, profiles use `InstanceProfileId`, and managed policies use `PolicyId`
as `identity.physical_id`, not their ARN or name. The stable ID is
`<collection-account>/global/iam/physical/<percent-encoded-source-ID>`; `arn` is a separate typed
field. AWS-managed policies with account component `aws` still belong to this snapshot's
collection account for identity/coverage purposes. Their ARN is retained unchanged; this does
not assert account ownership or duplicate the policy across requested regions.

IAM evidence locators are `iam:<Operation>`. Source paths include the canonical request
parameters (sorted compact JSON) and zero-based page and item indices, so repeated per-role
or per-policy reads cannot collide. For example:

```text
Request[{}].Page[0].Roles[0].RoleId
Request[{"RoleName":"AppRole"}].Page[0].Role.AssumeRolePolicyDocument
Request[{"PolicyArn":"arn:aws:iam::aws:policy/ReadOnlyAccess","VersionId":"v1"}].Page[0].PolicyVersion.Document
```

Pagination markers do not change the canonical request context; subsequent pages use
`Page[1]`, etc. Whole-item evidence is retained alongside exact fact citations. In the table,
`R` is the full `ListRoles` item path or `GetRole` response path plus `.Role`, `P` is the full
`ListInstanceProfiles` item path, and `M` is the `GetPolicy` response path plus `.Policy`.
`A`, `N`, `I`, and `V` denote the full attached-policy item, inline-name item, `GetRolePolicy`
response, and `GetPolicyVersion` response paths respectively.

| Normalized fact pointer | Exact source suffix / operation |
| --- | --- |
| `/identity/physical_id` | `R.RoleId`, `P.InstanceProfileId`, `M.PolicyId` |
| `/arn` | `R.Arn`, `P.Arn`, `M.Arn` |
| `/name` | `R.RoleName`, `P.InstanceProfileName`, `M.PolicyName` |
| `/attributes/path`, `/attributes/create_date` | `R`, `P`, or `M` plus `.Path`, `.CreateDate` when present |
| `/attributes/max_session_duration` | `R.MaxSessionDuration` when present |
| `/attributes/tags` | `R`, `P`, or `M` plus `.Tags` when supplied; normalized key/value map |
| `/trust_document` | `R.AssumeRolePolicyDocument` |
| `/boundary_arn` | `R.PermissionsBoundary.PermissionsBoundaryArn` |
| `/attributes/attached_policy_arns/n` | `A.PolicyArn` from `ListAttachedRolePolicies`; distinct ARNs sorted |
| `/attributes/inline_policy_names/n` | `N` from `ListRolePolicies`; distinct names sorted |
| `/inline_policies/n/name` | Discovered `N` from `ListRolePolicies` |
| `/inline_policies/n/document` | `I.PolicyDocument` from `GetRolePolicy` |
| `/role_arns/n` | `P.Roles[i].Arn` from `ListInstanceProfiles` |
| `/default_version_id` | `M.DefaultVersionId` from `GetPolicy` |
| `/document` | `V.PolicyVersion.Document` from `GetPolicyVersion` |

Matching `GetRole` fields override listed fields; absent detail fields retain the listed source.
Conflicting role identity details are omitted, not merged. Inline policies remain nested facts
on their role, not standalone resources. Normalized list indices may differ from source indices
after sorting, deduplication, or omission; citations retain the original source index.

## Attachments and trust references

All relationships use `attached_to` with the following `attributes.kind`. Dependencies are
grouped by the source resource's region: EC2 instance links are regional; IAM links are global.

| `kind` | Source → target | Citation |
| --- | --- | --- |
| `instance_profile` | EC2 instance → IAM instance profile | `DescribeInstances`: `Reservations[i].Instances[j].IamInstanceProfile.Arn` |
| `profile_role` | Instance profile → role | `P.Roles[i].Arn` |
| `role_policy` | Role → managed policy | `A.PolicyArn` |
| `permissions_boundary` | Role → boundary managed policy | `R.PermissionsBoundary.PermissionsBoundaryArn` |
| `trust_principal` | Role → principal referenced in its trust document | Exact principal entry and the whole statement, including effect, actions, and conditions |

An exact ARN resolving to one collected resource of the expected type is `local`. Missing or
ambiguous same-account targets (including unavailable AWS-managed policies) are `unresolved`
with the source path; a reference to another account is `external`. EC2-only collection can
retain an unresolved profile reference without collecting IAM or claiming IAM coverage.

Trust extraction handles a single statement object or a statement list, scalar/list principals,
and `AWS`, `Service`, and `Federated` principal kinds. An exact collected role ARN can be local;
recognized AWS account IDs, IAM ARNs, and principal IDs otherwise remain external, as do
service/federated identifiers. Wildcards, `NotPrincipal`, unknown forms, and ambiguous matches
remain unresolved. `principal_kind` and `not_principal` preserve that distinction. A trust
statement's `Deny` effect or conditions are not discarded or evaluated into a grant.

Attachments, boundaries, and trust references are **not proof of granted permissions, successful
role assumption, or effective access**. No evaluation of policy interaction, SCPs, session
policies, resource policies, condition satisfaction, or caller permissions is performed.

## Coverage and partial results

Selecting IAM produces eight aggregate `coverage.operations` records, one per account,
`global`, `iam`, and operation—not one per role, request, or page. `result_count` counts retained
source records: list items (including inline-policy names), or whole responses for getter calls.
It is not a normalized resource, statement, request-attempt, or page count.

- Completed zero-result discovery and empty downstream batches with complete prerequisites are
  `success` with count `0`.
- A batch failure before any page succeeds is `failed` with sanitized failures and no count.
- A failure after any successful page is `incomplete`, even if retained count is `0`.
  Earlier pages and successful sibling requests are kept and normalized. Missing or repeated
  pagination markers fail safely rather than looping.
- Incomplete/failed upstream discovery does not prevent reads for already discovered items.
  A downstream batch with calls is `incomplete` with a prerequisite reason (or `failed` if all
  calls fail before a page succeeds); with no calls it is `skipped`, not successful absence.
- `GetRole`, attached-policy and inline-name lists depend on `ListRoles`; `GetRolePolicy`
  depends on `ListRolePolicies`; `GetPolicy` depends on `ListRoles`, `GetRole`, and
  `ListAttachedRolePolicies`; `GetPolicyVersion` depends on `GetPolicy`. Profile inventory is
  independent. Prerequisites describe execution outcomes, not a later normalization re-run.
- Normalization adds `unresolved_values` with exact source paths and resource limitations,
  changing successful coverage to `incomplete` when supported facts are malformed or missing.
  Retained counts are unchanged by resource deduplication or omission. An empty policy list
  after incomplete discovery is not proof that a role has no policies.

## Document validation and redaction

Policy/trust documents accept objects, JSON strings, or percent-encoded JSON strings. They are
decoded before evidence construction, including documents embedded in whole role/profile
responses. A document must be a JSON object with `Statement` as an object or list of objects;
this is structural validation, not full IAM semantic validation.

Sensitive named fields and secret-named AWS tag-pair values become `[REDACTED]`. Within document
conditions, context keys whose terminal namespace/path component is `ExternalId`, `SourceIdentity`, `UserId`,
`UserName`, `Email`, `Sub`, or `NameQualifier` are also redacted (case/punctuation normalized),
along with shared secret-name matches. Principal ARNs, actions, and non-sensitive condition
values remain available. Evidence records `value_status: redacted` and RFC 6901
`redacted_paths` relative to its decoded value; whole-item, document-fact, and trust-statement
citations therefore have different pointer prefixes.

Malformed document values are omitted from whole-item evidence and typed document facts;
raw encoded or invalid text is never substituted or echoed in diagnostics. Missing documents
stay unavailable (`null` for trust/managed-policy documents, omitted inline-policy entries),
with no invented empty policy. Missing/invalid/ambiguous source identities omit the resource;
conflicting inline-policy/version details omit the document. Limitations and coverage record
these omissions or unavailable reads without claiming absence. Redaction is a field/context-name
policy, not a general detector for secrets hidden in arbitrary strings.