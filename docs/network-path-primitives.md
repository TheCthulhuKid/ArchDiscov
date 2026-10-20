# Network-path evaluation primitives

Step 29 defines which AWS network cases are evaluated initially and how uncertainty is
handled, and provides reusable primitives over the shared `GraphIndex`. The decision
vocabulary, composition rules, and all primitives described here are implemented. No
analyzer or CLI uses them yet: database access and internet exposure analysis are later
steps and will compose these primitives.

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
- `evaluate_subnet_route` (`services.network_routes`): the longest-prefix active route of
  the subnet's explicit route table, or of the VPC's observed main table only when the
  subnet has no explicit association. A route to an internet gateway is a recorded route,
  not proof of internet reachability; it also needs the gateway recorded as available and
  attached to the VPC.
- `evaluate_public_address` (`services.network_addressing`): a recorded public IPv4
  address on an ENI or instance allows, a recorded absence denies, IPv6 is ignored.
- `evaluate_load_balancer_scheme` and `evaluate_load_balancer_forwarding`
  (`services.network_load_balancers`): internet-facing versus internal, and a listener that
  accepts the port and protocol and forwards to recorded targets. Conditional rules are
  alternatives, so an allow means some request can be forwarded. Target health must be
  recorded but is never interpreted, and security groups are not considered.

## Supported and unsupported cases

Supported, each evaluated for one direction or one question per call:

| Element | Supported | Unsupported (always `unknown`, never `allow`) |
|---|---|---|
| Security-group rules | IPv4 CIDRs, same-account referenced groups, tcp/udp/icmp (names or numbers) and all-protocol, inclusive port ranges | IPv6, prefix lists, cross-account references, other protocols, statefulness |
| NACLs | IPv4 CIDRs, tcp/udp/icmp and all-protocol, rule-number order with implicit final deny, one direction per call | IPv6 entries, other protocols, ICMP type/code, subnets without an observed association (no default ACL is assumed) |
| Routes | IPv4 longest prefix to local or internet-gateway targets, explicit table or observed main table | NAT, transit gateway, peering, VPC endpoint, prefix-list, and instance/interface targets; IPv6 routes are ignored |
| Public addressing | Recorded public IPv4 on ENIs and instances | IPv6 addresses are ignored |
| Load balancers | Scheme, listener port and protocol, forward actions, target groups with registered targets | Redirect, fixed-response, and authentication actions; GENEVE (Gateway Load Balancer) listeners; IP and unresolved targets |

Not modelled at all: statefulness, target health meaning, host firewalls, authentication,
and runtime state.

## Composing primitives

Primitives never call each other. A caller composes them explicitly with `all_of` (and
`any_of` for alternatives), for example an inbound request and its return path:

```python
all_of([
    evaluate_public_address(index, eni_id),
    evaluate_subnet_route(index, subnet_id, client_ip, reach=RouteReach.INTERNET_GATEWAY),
    evaluate_network_acl(index, subnet_id, RuleDirection.INGRESS, client_ip, protocol="tcp", port=443),
    evaluate_network_acl(index, subnet_id, RuleDirection.EGRESS, client_ip, protocol="tcp", port=49152),
    evaluate_security_group(index, group_id, RuleDirection.INGRESS, peer, protocol="tcp", port=443),
])
```

A recorded denial dominates an unknown element, so a composed `deny` means some element
denies even if others could not be evaluated, while failed route, NACL, or other relevant
collection makes an otherwise allowing composition `unknown` with a `coverage_limited`
blocker. Every decision, including an allow, describes recorded configuration, not live
reachability.

## Non-AWS snapshots

Every primitive returns one `unknown` result with an `unsupported_platform` blocker for
Azure and other non-AWS snapshots ("Analyze an AWS snapshot instead").
