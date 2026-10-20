# Network-path evaluation primitives

Step 29 defines which AWS network cases are evaluated initially and how uncertainty is
handled, then provides reusable primitives over the shared `GraphIndex`. The decision
vocabulary and composition rules described here are implemented; the individual
primitives are delivered in later substeps (security-group rules and NACLs so far). No analyzer or CLI uses them yet (database
access and internet exposure are later steps).

**A decision describes recorded configuration, not live reachability.** It ignores
anything that was not collected, host firewalls, authentication, and runtime state.

## Decisions

Every primitive returns a `NetworkResult` (`archdiscov.models.network_path`):

- `allow`: every needed element was observed and allows. Always cited, never has blockers.
- `deny`: a recorded element denies, explained only by `denied` blockers.
- `unknown`: the answer cannot be established. It always has at least one blocker saying
  why.

Blockers are structured (`NetworkBlocker`): a code, a message, optionally the element,
resource, citing evidence, and (for limited collection) the recorded coverage operation.

| Blocker code | Meaning |
|---|---|
| `denied` | A recorded rule, entry, or configuration denies |
| `missing_evidence` | Required data was not recorded |
| `unsupported_element` | The element is outside the supported catalogue |
| `coverage_limited` | A relevant collection operation failed, was incomplete, skipped, or unsupported |
| `unsupported_platform` | The snapshot is not AWS |

## Uncertainty rules

- Missing, unsupported, or limited-collection input is always `unknown`, never `allow`.
- An empty composition is `unknown`: there is no vacuous allow or deny.
- Failed, incomplete, skipped, or unsupported collection of a relevant operation turns an
  apparent `allow` or `deny` into `unknown` (`qualify_coverage`), keeping the evidence and
  adding one `coverage_limited` blocker per recorded operation. Successful-zero collection
  is not a limitation, and absent coverage records are not interpreted: a primitive
  reports missing data itself.
- Non-AWS snapshots return one `unsupported_platform` `unknown` result (`require_aws`);
  Azure networking is not evaluated.

## Composition

`archdiscov.services.network_decisions` composes results with three-valued logic:

| Composition | Meaning | Precedence |
|---|---|---|
| `all_of` | Every needed element must allow (one hop after another) | deny, then unknown, then allow |
| `any_of` | Alternatives (several rules or paths) | allow, then unknown, then deny |
| `first_match` | Ordered entries; the first matching entry decides, else the default | an entry that cannot be matched or ruled out makes the result unknown |

Evidence and blockers are deduplicated and deterministically ordered. Only contributing
results are kept: allows contribute evidence, and blockers come from the results that
decided the outcome.

## Implemented primitives

- `evaluate_security_group` (`services.network_security_groups`): one direction against a
  group's recorded rules. Any matching cited rule allows; with none, a recorded group
  denies implicitly.
- `evaluate_network_acl` (`services.network_acls`): one direction against the subnet's
  single observed ACL association, entries in rule-number order, first match wins, with
  the implicit final deny. No default ACL is assumed, and an ACL with entries that could not
  be recorded is unknown. Callers compose both directions explicitly (NACLs are stateless).

## Supported and unsupported cases

Initially supported (delivered by later substeps of step 29): IPv4 CIDRs and same-account
referenced groups in security-group rules, protocols and port ranges, ordered stateless
NACL evaluation, subnet route-table selection (the main-table fallback only when it is
observed), internet-gateway routes, public IPv4 addressing on ENIs and instances, and
load-balancer scheme, listener, and target forwarding.

Explicitly unsupported at first, always reported as `unsupported_element` and never as an
allow: IPv6, prefix lists, NAT gateways, transit gateways, VPC peering, VPC endpoints,
cross-account group references, and unrecognized protocols or listener actions.
