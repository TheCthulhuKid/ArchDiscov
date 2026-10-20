# Internet exposure analysis

`internet_exposure` reports which recorded entry points could expose a service to the
internet, from recorded snapshot data. It reads the shared `GraphIndex` through the
[analyzer boundary](ownership-analysis.md), composes the
[network-path primitives](network-path-primitives.md), and never contacts a provider. It is
the fifth implemented analyzer (after ownership, [security-group impact](security-group-impact.md),
[cross-account trust](cross-account-trust.md), and [database access](database-access.md)).

**A finding is candidate exposure from recorded configuration, not proof of reachability.**
A public address, a route to an internet gateway, and an open security group are recorded
configuration. Host firewalls, authentication, the application, and runtime state are not
considered, and observed target health is never used to decide a path.

## Entry points and decisions

| Entry kind | Entry point | Elements (in order) |
|---|---|---|
| `public_address` | a network interface, or an instance without stored interfaces, with a recorded public IPv4 address | `public_address`, `route`, `nacl`, `security_group` |
| `load_balancer` | an ELBv2 load balancer (an internal one is a recorded denial) | `scheme`, `route`, `nacl`, `security_group`, `listener`, `target` |

Elements must all allow; a recorded denial dominates an unknown element
([vocabulary](analysis-results.md#internet-exposure-vocabulary)). One finding is reported per
entry point, protocol, and uniformly decided port range:

- **Public addresses.** The ports come from ingress rules for `0.0.0.0/0`, merged per
  protocol and split where other rules or ACL entries start and stop. The route is the
  subnet's route to an attached internet gateway for a documented probe address
  (203.0.113.10), and the NACLs are checked for the request and for the return path on an
  ephemeral port (49152). A resource with no open range is a cited denial on tcp 1–65535.
  Rules naming only referenced groups, prefix lists, or IPv6 never create an exposure and are
  reported (`exposure_rule_unsupported`). A resource that merely shares a subnet with a public
  one is not an entry point, and a resource whose address is not recorded is not listed
  (limited collection is reported at group level).
- **Load balancers.** Per listener and client protocol: the best subnet's route and NACLs (any
  one node may carry the traffic), the balancer's security groups (omitted for a network
  balancer with none), the listener, and forwarding to registered targets. Within
  `--max-depth` (at least 2) targets are followed to recorded backend instances, whose
  security groups must admit the balancer's groups on the target port. A private backend is
  reached through the balancer and is never publicly addressed; it follows the entry point in
  the finding's resources. Unresolved, IP, and non-instance targets and unsupported listener
  actions are `unknown`.
- **Health.** Observed target health states are recorded as `observed_target_health` and never
  required or interpreted.
- NAT, transit gateway, peering, endpoint, prefix-list, and IPv6 elements are unsupported:
  they make a result `unknown`, never an allow. A NACL entry or security-group rule that covers
  only part of the internet is `unknown`, not a denial.

| Decision | Finding status |
|---|---|
| `allow`, `deny` | `supported` (`partial` when relevant collection was limited) |
| `unknown` | `insufficient_evidence`, with blockers as limitations |

## Qualification and limits

- Findings are qualified by recorded EC2 and load-balancing coverage; limited route, NACL,
  security-group, listener, or target collection makes the affected decision `unknown`. A
  group-level `insufficient_evidence` finding reports limited or unrecorded collection
  (`coverage_unrecorded_<service>` without invented operations) and `result_limit_reached`.
- `--result-limit` (default 100, maximum 10,000) keeps decided candidates first (allow, unknown,
  then deny, then entry kind and identity); results are deterministic and saved in finding-ID
  order. `--max-depth` (default 3, maximum 10) bounds how far beyond the entry point targets
  are followed.
- Azure snapshots return one `insufficient_evidence` finding with an `unsupported_platform`
  limitation.

## CLI

```text
archdiscov analyze --analyzer internet_exposure --snapshot SNAPSHOT --output RESULT \
    [--max-depth N] [--result-limit N] [--json]
archdiscov findings RESULT --snapshot SNAPSHOT [--json]
archdiscov evidence EVIDENCE_ID --snapshot SNAPSHOT [--json]
```

`findings` prints each finding's entry kind, decision, protocol and ports, every element's
decision with its blocker codes, and any observed target health. Results are saved atomically,
never overwrite an existing path, and are verified against their snapshot (the entry point must
match its kind). `--resource-id`, `--port`, and `--ownership-key` are rejected for this analyzer.
