# Regional ELBv2 collection mapping (step 15)

`archdiscov collect --service elasticloadbalancing --region us-east-1` collects observed
Application and Network Load Balancer configuration in each selected region. The boto3 client
is `elbv2`; the selection, identity service, evidence locator prefix, and coverage service are
`elasticloadbalancing`. EC2 and IAM are independent selections, not implicit prerequisites.
This slice records configuration and target-health observations, **not end-to-end reachability**.

## Read operations and permissions

The exact six ELB read permissions are allowlisted in `archdiscov.ingestion.operations`:

| Operation / required read permission | Requests and retained response records |
| --- | --- |
| `elasticloadbalancing:DescribeLoadBalancers` | Regional inventory, paginated `LoadBalancers[i]` |
| `elasticloadbalancing:DescribeTargetGroups` | Independent regional inventory, paginated `TargetGroups[i]` |
| `elasticloadbalancing:DescribeListeners` | Per discovered supported `LoadBalancerArn`, paginated `Listeners[i]` |
| `elasticloadbalancing:DescribeRules` | Per discovered ALB `ListenerArn`, paginated `Rules[i]`; no NLB rule requests |
| `elasticloadbalancing:DescribeTargetHealth` | Per discovered supported `TargetGroupArn`, non-paginated `TargetHealthDescriptions[i]` |
| `elasticloadbalancing:DescribeTags` | Sorted distinct supported load-balancer, listener, rule, and target-group ARNs in batches of at most 20 `ResourceArns`; non-paginated `TagDescriptions[i]` |

STS `GetCallerIdentity` verifies the final session's account before resource reads;
configured role collection additionally uses `sts:AssumeRole`. Selecting EC2 or IAM adds the
separate reads in [network-mapping.md](network-mapping.md) and [iam-mapping.md](iam-mapping.md).
No writes or additional ELB APIs are called. Concurrency, timeouts, and retries use existing
`AwsAccountConfig` limits; this slice adds no package dependencies.

Only `application` (ALB) and `network` (NLB) load balancers are supported. A valid Gateway Load
Balancer identity is retained as `UnknownResource` with its original
`AWS::ElasticLoadBalancingV2::LoadBalancer` type, sanitized summary, and explicit unsupported
limitation. It makes inventory coverage `incomplete`; no gateway listeners or tags are requested.
Other unsupported load-balancer types use the same summary-only path. Classic ELB is not
inventoried: no classic `elb` client or classic collection support is implied. Target groups
support `instance`, `ip`, `alb`, and `lambda`; an unsupported target type retains a typed group
summary with an unavailable `target_type` and limitations, without health or tag requests.

## Identities, containment, and schema compatibility

`LoadBalancer`, `Listener`, `ListenerRule`, and `TargetGroup` have resource types
`AWS::ElasticLoadBalancingV2::LoadBalancer`, `AWS::ElasticLoadBalancingV2::Listener`,
`AWS::ElasticLoadBalancingV2::ListenerRule`, and `AWS::ElasticLoadBalancingV2::TargetGroup`.
Their `identity.physical_id` is the full ARN, also stored in typed `arn`, not an AWS name or
ARN suffix. The stable ID is
`<account>/<region>/elasticloadbalancing/physical/<percent-encoded-ARN>`.

Collection validates ARN shape, collection account and region, and listener/rule request-parent
consistency. Invalid identities and conflicting duplicate identities are omitted with exact
source-path limitations. Identical repeated records may contribute citations but produce one
resource; retained source counts are not deduplicated resource counts.

Each resource is stored once. With observed parents of the expected type, containment is
VPC → load balancer → listener → rule, and VPC → target group. Load balancers and target groups
are VPC children, not duplicated beneath every subnet. A missing parent leaves the resource at
the regional root with `attributes.unresolved_parent` containing `physical_id`, expected
`resource_type`, and a reason. No VPC, listener, or other parent is fabricated. ELB-only collection
therefore leaves VPC-dependent resources at the root; Lambda groups without a VPC also carry
an unresolved-parent explanation. Listeners/rules can still nest beneath collected ELB parents.

Live snapshots use schema `"2"`, with discriminated `target` objects for `local`, `external`,
and `unresolved` dependencies. Schema `"1"` snapshots still load and save using their existing
local `target_id` dependency representation; no automatic migration occurs. Schema `"1"` rejects
`target`, and schema `"2"` rejects `target_id`. Typed ELB resources participate in the same
recursive resource union and evidence/integrity validation as other resource types.

## Source paths and typed facts

Evidence locators are `elasticloadbalancing:<Operation>`. Paths retain sorted compact JSON
request parameters and zero-based page/item positions; pagination `Marker` is excluded from
the canonical request context. For example:

```text
Request[{}].Page[0].LoadBalancers[0].VpcId
Request[{"LoadBalancerArn":"<load-balancer-ARN>"}].Page[1].Listeners[0].DefaultActions[0].Type
Request[{"ListenerArn":"<listener-ARN>"}].Page[0].Rules[0].Conditions[0]
Request[{"TargetGroupArn":"<target-group-ARN>"}].Page[0].TargetHealthDescriptions[0].Target.Id
Request[{"ResourceArns":["<resource-ARN>"]}].Page[0].TagDescriptions[0].Tags
```

Resources retain sanitized whole-item evidence and `attributes.summary`, alongside
`fact_evidence` mapping normalized RFC 6901 pointers to exact source fields. In the table,
`B`, `L`, `R`, `G`, `H`, and `T` denote full load-balancer, listener, rule, target-group,
target-health, and tag item paths from their corresponding operations above. `n`/`m` are normalized
list positions; `i`/`j` are original source positions, which survive omission of malformed items.

| Normalized fact pointer | Exact source suffix / context |
| --- | --- |
| `/arn` | `B.LoadBalancerArn`, `L.ListenerArn`, `R.RuleArn`, `G.TargetGroupArn` |
| `/name` | `B.LoadBalancerName`, `G.TargetGroupName`, when supplied |
| `/lb_type`, `/scheme`, `/dns_name`, `/ip_address_type` | `B.Type`, `B.Scheme`, `B.DNSName`, `B.IpAddressType` |
| `/vpc_id` | `B.VpcId`, `G.VpcId` |
| `/subnet_ids/n` | `B.AvailabilityZones[i].SubnetId` |
| `/security_group_ids/n` | `B.SecurityGroups[i]`; required collection for ALB, optional for NLB |
| `/attributes/availability_zones`, `/attributes/state` | `B.AvailabilityZones`, `B.State` |
| `/load_balancer_arn` | `L.LoadBalancerArn`, or `Request[{...}].LoadBalancerArn` if absent from the response |
| `/listener_arn` | `R.ListenerArn`, or `Request[{...}].ListenerArn` if absent from the response |
| `/protocol`, `/port` | `L.Protocol`, `L.Port`, `G.Protocol`, `G.Port`; optional for Lambda groups |
| `/priority`, `/is_default` | `R.Priority` (string), `R.IsDefault` (boolean) |
| `/actions/n`, `/actions/n/type`, `/actions/n/order` | `L.DefaultActions[i]` or `R.Actions[i]`, plus `.Type`, optional `.Order` |
| `/actions/n/target_group_arn` | Action `.TargetGroupArn`, when supplied |
| `/actions/n/forward_groups/m/arn`, `/actions/n/forward_groups/m/weight` | Action `.ForwardConfig.TargetGroups[j].TargetGroupArn`, optional `.Weight` |
| `/conditions/n`, `/conditions/n/field` | `R.Conditions[i]`, `R.Conditions[i].Field` |
| `/target_type` | `G.TargetType` |
| `/health_check/<AWS-key>` | `G.<AWS-key>` for each accepted health-check setting |
| `/targets/n`, `/targets/n/id`, `/targets/n/port`, `/targets/n/availability_zone` | `H`, `H.Target.Id`, optional `H.Target.Port`, `H.Target.AvailabilityZone` |
| `/targets/n/health_state`, `/targets/n/health_reason`, `/targets/n/health_description` | `H.TargetHealth.State`, optional `.Reason`, `.Description` |
| `/attributes/tags` | `T.Tags`, normalized into a sanitized key/value map |

Actions support `forward`, `authenticate-oidc`, `authenticate-cognito`, `redirect`, and
`fixed-response`. Typed values retain optional order, direct group ARN, and weighted groups
(weights 0–999; absent weight remains `null`, not an invented default). Remaining action
configuration is preserved as sanitized JSON, not evaluated; non-forward actions produce no
forwarding edge. Required configuration wrappers are checked for object shape, not complete
AWS semantic validity.

Conditions support `host-header`, `path-pattern`, `http-header`, `http-request-method`,
`query-string`, and `source-ip`. Their matching wrapper, legacy `Values`, or `RegexValues` is
retained in sanitized configuration, with whole-condition and field citations. Conditions,
priority, action order, redirects, authentication, stickiness, and weights are observations,
not simulated request-routing decisions. Unknown condition/action kinds are omitted with
limitations rather than converted into forwarding facts.

Health-check settings use the original AWS keys: `HealthCheckEnabled`, `HealthCheckProtocol`,
`HealthCheckPort`, `HealthCheckPath`, `HealthCheckIntervalSeconds`, `HealthCheckTimeoutSeconds`,
`HealthyThresholdCount`, `UnhealthyThresholdCount`, and `Matcher`. Present values receive basic
shape/range checks; no full matcher semantics are evaluated. The health port accepts
`traffic-port` or a decimal port string. Registrations retain target ID, optional port/AZ,
health state/reason/description, and sanitized `attributes.target_health`. Valid state values
are `initial`, `healthy`, `unhealthy`, `unhealthy.draining`, `unused`, `draining`, and
`unavailable`. A health observation is not proof of client access, network reachability,
successful authentication, or application availability.

## Attachments and forwarding references

Dependencies are grouped by the source region, cite the exact source fact, and retain
`attributes.observed_identifier`. Containment alone is not an attachment or forwarding edge.

| Relationship / attributes | Source → expected target | Citation |
| --- | --- | --- |
| `attached_to`, `kind: subnet_membership` | Load balancer → subnet | `/subnet_ids/n` |
| `attached_to`, `kind: security_group_membership` | Load balancer → security group | `/security_group_ids/n` |
| `attached_to`, `kind: listener_load_balancer` | Listener → load balancer | `/load_balancer_arn` |
| `attached_to`, `kind: rule_listener` | Rule → listener | `/listener_arn` |
| `forwards_to`, `action_index`, `action_order`, `weight` | Listener/rule → target group | Weighted group ARN, or direct action group ARN |
| `forwards_to`, `port`, `availability_zone`, `health_state`, `health_reason` | Target group → registration | `/targets/n/id` |

When usable weighted groups exist, they take precedence over the direct action ARN so it is
not counted twice. With no normalized weighted groups, the current resolver falls back to a
retained direct ARN, including when `ForwardConfig` is present but empty or malformed;
normalization limitations remain visible. Zero-weight groups still have observed edges.

Only one uniquely observed resource of the expected type in the same collected region can
be `local`. Subnet, security-group, target-group, listener, and load-balancer attachments never
resolve to an unrelated type sharing an identifier. Instance registrations require an observed
EC2 instance; `alb` registrations require an observed **application** load balancer, not an NLB
or unknown summary. Missing or ambiguous same-scope references stay `unresolved` with the
source path; no placeholder local resources are created. IP registrations always remain
`unresolved`: ownership is not inferred from addresses, even if an EC2 address matches.
Lambda registrations always use `external` with the observed identifier, `lambda` service,
`AWS::Lambda::Function` type, and account component when recognizable, including same-account
Lambda ARNs. The collector does not follow these registrations or query Lambda.

## Shared execution, coverage, and partial results

The shared `DetailRunner` was extracted from IAM execution. `IamCall`, `IamResponse`,
`IamResult`, and `IamRunner` preserve IAM's existing interfaces and account-wide `global`
scope; no regional IAM duplication is introduced. IAM pagination still follows
`IsTruncated` plus `Marker`; ELBv2 follows `NextMarker` and sends it as the next request's
`Marker`. Missing required IAM markers or repeated pagination markers fail safely. Requests
are validated against the allowlist before client access, sorted by canonical parameters,
and bounded by account concurrency.

Selecting ELB produces six aggregate coverage records per account/region/service, not one
per ARN, request, or page. `result_count` counts retained source collection items after
collector identity/tag filtering, not normalized resources, calls, attempts, or pages.
Normalization can add limitations without changing these counts. A missing or non-list result
collection is unavailable and `incomplete`, not an empty successful inventory.

- Complete zero-result inventory and empty downstream batches with complete prerequisites are
  `success` with count `0`; legitimate NLB-only collection needs no rule requests.
- Failure before any page succeeds is `failed` with sanitized failures and no count. Failure
  after any successful page is `incomplete`, even with count `0`; earlier pages and successful
  sibling calls are retained.
- Failed/incomplete prerequisites do not suppress reads for already discovered safe items.
  A downstream batch with calls is `incomplete` with a prerequisite reason, unless all reads
  fail before a page succeeds (`failed`). Without calls it is `skipped`, not successful absence.
- Listeners depend on load-balancer discovery; rules depend on load-balancer discovery and
  relevant ALB listener discovery (an NLB-only listener failure does not itself qualify rule
  coverage). Health depends on target-group discovery. Tags depend on load-balancer,
  target-group, listener, and rule discovery. These are staged execution prerequisites, not
  a later re-run of normalization-derived limitations.
- Missing/malformed supported facts add exact-path `unresolved_values` and resource
  `attributes.limitations`, changing successful coverage to `incomplete`. Unavailable scalar
  or collection facts remain `null` or absent rather than fabricated empty values.
- Target health has an operation-wide aggregate, not a per-group success ledger. A group
  with no returned health items gets cited `targets: []` only when the entire health operation
  succeeded and the target type is known. In a partial batch, an empty successful group cannot
  be distinguished from an unreturned/failed call, so its targets remain unavailable with an
  explicit limitation. Returned registrations are retained but qualified by partial coverage.
- Tags are matched only to requested ARNs. Malformed, unexpected, or conflicting replies are
  omitted/limited; every missing requested reply has a source-local limitation. No missing
  reply is replaced with `{}`. An actual valid `Tags: []` yields an empty map. An entirely failed
  tag batch remains `failed`, while missing-reply limitations are still recorded. Summary-only
  unsupported resources also retain explicit unavailable-tags limitations.

## Redaction and collection boundaries

Redaction happens before constructing summaries, typed configuration, or evidence. Shared
secret-name matching protects named fields and secret-named AWS tag-pair values. Additional
ELB rules replace these values with `[REDACTED]`:

- OIDC `ClientSecret` and every `AuthenticationRequestExtraParams` value (parameter names
  remain visible).
- Fixed-response `MessageBody`.
- HTTP-header/query-string condition `Values`, `Value`, and `RegexValues`, including nested
  `HttpHeaderConfig`/`QueryStringConfig` and legacy forms. Header names and non-sensitive query
  keys remain available; host/path/method/source-IP match values are otherwise retained.
- `Certificates` wherever present in collected source items. Certificate configuration is
  explicitly outside this slice, even when the listener API includes it.

Typed action/condition configuration also applies model-layer redaction, including unwrapped
header/query conditions. Evidence records `value_status: redacted` and RFC 6901
`redacted_paths` relative to each evidence value; whole-item and nested-fact prefixes differ.
Diagnostics do not echo raw exceptions or protected values. This is a field/context-name
policy, not a detector for secrets hidden in arbitrary free text.

No listener certificate enumeration, certificate/TLS configuration analysis, load-balancer or
target-group attribute reads, listener attribute reads, WAF, access logs, metrics, DNS queries,
probes, or reachability/policy evaluation are performed. An internet-facing scheme, listener,
forwarding edge, or healthy target alone does not prove an externally accessible path.