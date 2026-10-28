# Network-path evaluation primitives

Step 29 defines which AWS network cases are evaluated initially and how uncertainty is
handled, and provides reusable primitives over the shared `GraphIndex`. The decision
vocabulary, composition rules, and all primitives described here are implemented. The
[database access](database-access.md) and [internet exposure](internet-exposure.md)
analyzers compose them; no other analyzer or CLI command uses them directly.

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
  denies implicitly. A rule naming a managed prefix list is resolved through the
  `prefix_list_reference` edge citing it (`services.network_prefix_lists`) and matches when a
  recorded entry of the peer's family holds the peer; the allow cites the rule and each entry
  it used. A list that is not stored, is external, or has unrecorded entries (an AWS-managed
  list, a list over the entry limit, or an import with separate entry resources) is
  `unknown` with a blocker naming the list, and an empty recorded list matches nothing. The
  list reads (`DescribeManagedPrefixLists`, `GetManagedPrefixListEntries`) qualify the
  result, so a limited read is never taken for an empty list.
- `evaluate_network_acl` (`services.network_acls`): one direction against the subnet's
  single observed ACL association, entries in rule-number order, first match wins, with
  the implicit final deny. No default ACL is assumed, and an ACL with entries that could not
  be recorded is unknown. Callers compose both directions explicitly (NACLs are stateless).
- `evaluate_subnet_route` (`services.network_routes`): the longest-prefix active route of
  the query's address family in the subnet's explicit route table, or in the VPC's observed
  main table only when the subnet has no explicit association. A route to an internet
  gateway is a recorded route, not proof of internet reachability; it also needs the gateway
  recorded as available and attached to the VPC. A route to an egress-only internet gateway
  (`RouteReach.EGRESS_ONLY_INTERNET_GATEWAY`) needs the gateway recorded as attached to the
  VPC. It carries outbound traffic only: callers ask for it only for outbound traffic, and
  inbound exposure, which asks for an internet gateway, never accepts it. A route to a NAT
  gateway (`RouteReach.NAT_GATEWAY`, outbound internet through NAT, `services.network_nat`)
  reaches the internet only when the gateway is recorded as public, `available`, and
  addressed, and its own subnet routes the same address to an available, attached internet
  gateway; the allow cites the route, the gateway's state, connectivity, and address, its
  subnet, and that subnet's route and gateway attachment. A `failed`, `deleting`, or
  `deleted` gateway denies citing its state, a private one denies citing its connectivity,
  and a `pending` or unrecorded state is `unknown`. A NAT gateway never carries inbound
  traffic: an internet-gateway question whose best route leads to one denies, citing the
  route. `DescribeNatGateways` qualifies a NAT question. A route to a gateway endpoint
  (`RouteReach.VPC_ENDPOINT`, `services.network_endpoints`) reaches it when the endpoint is
  stored, of the `gateway` type, `available`, recorded as associated with the subnet's
  route table (`endpoint_route_table`), and the route's prefix list is named for the
  endpoint's service; the allow cites the route and the matched entry, the association, the
  endpoint's type, service, and state, its table edge, and the list's recorded entry (which
  holds its name). States are compared in lower case: `pendingAcceptance`, `deleting`,
  `deleted`, `rejected`, `failed`, and `expired` deny citing the state, and `pending`,
  `partial`, or an unrecorded state is `unknown`, as is an endpoint not associated with the
  table or a list not named for its service. A route to a Gateway Load Balancer endpoint,
  or to one whose type is not modelled (`Resource`, `ServiceNetwork`) or not recorded, is
  unsupported for every question, because where it leads depends on what lies behind it.
  `DescribeVpcEndpoints` qualifies an endpoint question. An interface endpoint is no route
  target: `endpoint_interfaces` returns its stored network interfaces and security groups
  with citations, for callers to decide with the local route, NACL, and security-group
  primitives, and a state that carries no traffic denies. A route whose
  destination is a managed prefix list contributes each recorded entry of the query's
  family as a prefix, resolved as for security-group rules, and cites the entry it matched;
  its target still decides reach. AWS prefers, at equal prefix length, a static route, then
  a prefix-list route, then a propagated one, and picks at random between overlapping prefix
  lists ([route priority](https://docs.aws.amazon.com/vpc/latest/userguide/route-tables-priority.html)).
  The route's origin is not recorded, so a tie between a CIDR route and a prefix-list entry,
  or between two prefix lists, is `unknown` and says why. A prefix-list route whose list is
  not stored or has unrecorded entries could cover any address, so the table's result is
  `unknown` naming the list, and the list reads qualify the result as for security groups.
  A question whose best route leads to a supported kind it did not ask for, such as an
  internet-gateway question on a NAT, peering, transit gateway, or gateway endpoint route,
  denies citing the route.
- `evaluate_vpc_route` (`services.network_routes`, with `services.network_peering` and
  `services.network_transit`): whether
  the subnet's best route for an address leads into a given VPC. It allows the local route
  when that VPC is the subnet's own, or a route to a peering connection that is `active`,
  joins the subnet's VPC to that VPC in the same account, and records CIDRs on the peer side
  holding the address; the allow cites the route, the association, the connection's state
  and both sides, and its two VPC edges. Any other recorded status (`pending-acceptance`,
  `deleted`, `rejected`, and so on) denies citing the state, as does a peer side whose
  CIDRs do not hold the address or a connection leading to another VPC. A peer VPC in
  another account, or not recorded, is `unknown` naming it and the account. Peering is not
  transitive, so the hop ends in the peer VPC, and return traffic is the reverse question
  asked from the other side.

  A route to a transit gateway leads into the VPC through one hop
  ([how transit gateways work](https://docs.aws.amazon.com/vpc/latest/tgw/how-transit-gateways-work.html)).
  The gateway must be stored, owned by the subnet's account, and `available`. The subnet's
  VPC attachment to it must be `available` and have a recorded subnet in the source
  subnet's Availability Zone, since only resources in such zones reach the gateway; the
  destination's zones are not checked, because the gateway delivers into any zone the
  destination attachment serves. The attachment must have one `associated` route table that
  records its whole route search (`routes_complete`, with no omitted routes). That table's
  longest-prefix route, a static route winning a tie with a propagated one, must be
  `active` and name one `available` VPC attachment of `vpc_id`. The allow cites the VPC
  route and association, the gateway's state and owner, the source attachment's gateway,
  VPC, and subnet edges with both subnets' zones, the table association, the route search
  and selected route, and the destination attachment's edges and state.

  A blackhole, deleting, or deleted route; a `deleting` or `deleted` gateway or table; an
  attachment that is `failed`, `rejected`, `deleted`, or otherwise not carrying; an
  attachment with no subnet in the source zone; no matching route; or a route to another
  VPC's attachment denies, citing the hop so far and the element. `pending` and
  `modifying` elements are `unknown`, as is an unrecorded state. Also `unknown`, with the
  reason: a gateway outside the snapshot (shared from another account) or an attachment of
  another account, named; attachment subnets unrecorded, or an attachment an import left a
  value out of (an imported `SubnetIds` with a `!Ref` keeps only its literal subnets); a
  missing association; an incomplete route search; a tie of the same route type; and a
  deeper chain: several attachments (ECMP), a peering, VPN, or Direct Connect attachment,
  or a prefix-list destination. `DescribeVpcPeeringConnections` and the transit gateway
  reads qualify every VPC question.
- `evaluate_public_address` (`services.network_addressing`): one address family per call
  (`family`, IPv4 by default). Over IPv4, a recorded public address on an ENI or instance
  allows and a recorded absence denies. Over IPv6, an ENI's recorded global unicast address
  allows (AWS IPv6 addresses need no separate public address), a recorded empty list or only
  non-global addresses (documentation, unique-local, link-local) deny, and an unrecorded
  field is `unknown`. Instances record no IPv6 addresses, so an IPv6 question about an
  instance is `unknown`; ask about its ENIs.
- `evaluate_load_balancer_scheme` and `evaluate_load_balancer_forwarding`
  (`services.network_load_balancers`): internet-facing versus internal, and a listener that
  accepts the port and protocol and forwards to recorded targets. Conditional rules are
  alternatives, so an allow means some request can be forwarded. Target health must be
  recorded but is never interpreted, and security groups are not considered.

## Supported and unsupported cases

Supported, each evaluated for one direction or one question per call:

| Element | Supported | Unsupported (always `unknown`, never `allow`) |
|---|---|---|
| Security-group rules | IPv4 and IPv6 CIDRs within the query's address family, same-account referenced groups, managed prefix lists with recorded entries, tcp/udp/icmp (names or numbers) and all-protocol, inclusive port ranges | Prefix lists without recorded entries (`unknown`, named), cross-account references, other protocols (including ICMPv6), statefulness |
| NACLs | IPv4 and IPv6 CIDRs within the query's address family, tcp/udp/icmp and all-protocol, rule-number order with implicit final deny, one direction per call | Other protocols (including ICMPv6), ICMP type/code, subnets without an observed association (no default ACL is assumed) |
| Routes | IPv4 and IPv6 longest prefix within the query's address family, including prefix-list destinations with recorded entries, to local, internet-gateway, egress-only internet gateway, NAT gateway (outbound only), or gateway endpoint targets, and peering connections or one transit gateway hop into another VPC of the account (`evaluate_vpc_route`), explicit table or observed main table | Gateway Load Balancer endpoints and endpoints of an unmodelled or unrecorded type, and instance/interface targets; another account's peer VPC, transit gateway, or attachment (`unknown`, named); transit gateway routes to several attachments, to non-VPC attachments, or to prefix lists; prefix-list destinations without recorded entries and ties involving a prefix list (`unknown`, with the reason) |
| Public addressing | Recorded public IPv4 on ENIs and instances; recorded global IPv6 on ENIs | IPv6 on instances (their ENIs answer) |
| Load balancers | Scheme, listener port and protocol, forward actions, target groups with registered targets | Redirect, fixed-response, and authentication actions; GENEVE (Gateway Load Balancer) listeners; IP and unresolved targets |

Security groups, NACLs, and routes answer for the address family of the query, an IPv4 or
IPv6 address or CIDR, and public addressing for the family it is asked about. A rule, entry,
or route of the other family never matches, so it is skipped rather than making the result
`unknown`: an IPv6 deny entry ahead of an IPv4 allow no longer hides the allow. Ask once per
family for a dual-stack resource. The analyzers still ask IPv4 questions only; they adopt the
IPv6 and prefix-list decisions in step 54.

Not modelled at all: statefulness, target health meaning, host firewalls, authentication,
and runtime state.

Snapshots can record NAT gateways, egress-only internet gateways, peering connections, transit
gateways, endpoints, prefix lists, and IPv6 fields (see the
[snapshot format](snapshot-format.md#network-path-elements)). Security groups and NACLs
decide IPv6 rules and entries, security groups and routes decide prefix lists with recorded
entries, routes decide IPv6 routes, egress-only internet gateways, NAT gateways,
peering connections, transit gateways, and gateway endpoints, interface endpoints resolve to
their interfaces and groups, and public addressing decides ENIs' IPv6 addresses; the cases
above stay unsupported until the primitives decide them.

## Bounded addresses

When an address is not recorded, a caller may pass the CIDR that bounds it (for example
the subnet): `Peer.within` for security groups, and a CIDR `address` for `evaluate_network_acl`
and `evaluate_subnet_route`. A rule, entry, or route covering the whole range matches, a
disjoint one does not, and one covering only part of it makes the decision `unknown`.

## Composing primitives

Primitives never call each other. A caller composes them explicitly with `all_of` (and
`any_of` for alternatives), for example an inbound request and its return path:

```python
all_of(
    [
        evaluate_public_address(index, eni_id),
        evaluate_subnet_route(index, subnet_id, client_ip, reach=RouteReach.INTERNET_GATEWAY),
        evaluate_network_acl(
            index, subnet_id, RuleDirection.INGRESS, client_ip, protocol="tcp", port=443
        ),
        evaluate_network_acl(
            index, subnet_id, RuleDirection.EGRESS, client_ip, protocol="tcp", port=49152
        ),
        evaluate_security_group(
            index, group_id, RuleDirection.INGRESS, peer, protocol="tcp", port=443
        ),
    ]
)
```

A recorded denial dominates an unknown element, so a composed `deny` means some element
denies even if others could not be evaluated, while failed route, NACL, or other relevant
collection makes an otherwise allowing composition `unknown` with a `coverage_limited`
blocker. Every decision, including an allow, describes recorded configuration, not live
reachability.

## Non-AWS snapshots

Every primitive returns one `unknown` result with an `unsupported_platform` blocker for
Azure and other non-AWS snapshots ("Analyze an AWS snapshot instead").
