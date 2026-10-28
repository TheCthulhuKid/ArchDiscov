# Archdiscov implementation plan

This plan follows the four delivery increments in [README.md](README.md) and adds a fifth, a
sixth, and a seventh. It holds the active phases only. Completed phases are archived, one file
per phase, in [docs/plan-archive/](docs/plan-archive/); read them for how an earlier step was
planned and verified, not for current work.

| Phase | Steps | Status | Plan |
| --- | --- | --- | --- |
| 1. Models, snapshots, fixtures, and CLI foundation | 1–9 | Complete | [phase-1.md](docs/plan-archive/phase-1.md) |
| 2. AWS and infrastructure-as-code ingestion | 10–22 | Complete | [phase-2.md](docs/plan-archive/phase-2.md) |
| Cloud-platform foundation before phase 3 | 22.1–22.4 | Complete | [cloud-platform-foundation.md](docs/plan-archive/cloud-platform-foundation.md) |
| 3. Graph queries, findings, and comparisons | 23–33 | Complete | [phase-3.md](docs/plan-archive/phase-3.md) |
| 4. Providers, orchestration, and validated answers | 34–44 | Complete | [phase-4.md](docs/plan-archive/phase-4.md) |
| 5. MCP server over the assistant tools | 45–48 | Complete | [phase-5.md](docs/plan-archive/phase-5.md) |
| 6. Complete the AWS network paths | 49–56 | In progress | [Below](#6-complete-the-aws-network-paths) |
| 7. Browser UI for results, evidence, and questions | 57–60 | Planned | [Below](#7-browser-ui-for-results-evidence-and-questions) |

When a phase completes, move its section to a new file in `docs/plan-archive/` and update this
table.

## Current starting point

The existing code in [src/archdiscov/models/architecture.py](src/archdiscov/models/architecture.py) provides recursive resource models, a discriminated resource union, validated account/region containment, local/external/unresolved dependencies, fact-level evidence citations, and operation coverage. Dedicated resource models cover EC2/network, IAM, ELBv2, ECS, Lambda, RDS/Aurora, and SQS configuration; other types can use `UnknownResource`.

AWS discovery supports the documented regional service slices and account-wide IAM configuration through `collect`; Terraform state exports and plans and CloudFormation templates are imported through `ingest --format`. Phase 3 delivered the graph index, queries, five deterministic analyzers, immutable result persistence, and snapshot comparison. Phase 4 delivered the `ask` command with the fake, Anthropic, OpenAI-compatible, and Bedrock providers, plus evaluation suites. Phase 5 serves the same tools over MCP through `archdiscov mcp`.

## 6. Complete the AWS network paths

**Phase 6 milestone:** Collect or import a VPC whose traffic leaves through a NAT gateway, crosses a peering connection or transit gateway, uses a VPC endpoint or managed prefix list, or travels over IPv6 → the network primitives decide each element from cited configuration instead of stopping at it → `analyze` reports database access across VPCs, internet exposure over IPv6, and internet egress with the same statuses, blockers, and citations as today → `ask` and the MCP server answer questions about them unchanged.

Today a route whose target is a NAT gateway, transit gateway, peering connection, VPC endpoint, or prefix list, a security-group rule that names a prefix list, and every IPv6 element are `unknown`. The route records its target kind (`RouteTargetKind`) but the target itself is never collected, so a path stops there. `docs/network-path-primitives.md` and the internet-exposure and database-access pages list these as unsupported. This phase records those elements and decides them, so the analyzers answer more questions without a new kind of claim. It stays AWS-only, read-only, and offline in tests, and adds no cloud platform, provider, or remediation.

A decision still describes recorded configuration, never live connectivity. A path is decided only when every element on it, in both directions where the return path matters, is recorded and cited. Anything missing, unsupported, or outside the snapshot stays `unknown` with a blocker that names it.

Implement steps 49–56 in order, verifying each before the next. Break each step into substeps when it starts, as steps 34–48 were. Keep this phase planned until step 56 passes.

**Where things go:**
- New resource models in `models/resources/network.py`, joining the supported resource union; new collection in `ingestion/network/`, with its operations in `ingestion/operations.py` and the allowlist.
- Route, prefix-list, and address-family decisions in the existing `services/network_*` primitives; cross-VPC route composition in `services/network_composition.py`.
- The analyzers stay in their modules; a new egress analyzer follows the `Analyzer` protocol, its results in `models/analysis/`, registered in `DEFAULT_REGISTRY`.

**What is reused:**
- `DetailRunner`, `EvidenceBuilder`, coverage and unresolved values, the SDK call budgets, and the sanitizers, as every other collector uses them.
- `NetworkResult`, `all_of`/`any_of`, `cited_decision`, `sole_associate`, and the `missing`/`unsupported` blockers in `services/network_matching.py` and `network_decisions.py`.
- The Terraform and CloudFormation mapping catalogs, the analyzer registry, result persistence, and the phase 3–5 acceptance harnesses.

### Step 49: Settle the contracts for the new network elements
**Status:** Implemented. Substeps 49.1–49.7 are implemented: the new resource types, IPv6 fields, and typed relationships validate in schemas `2` and `3`, every existing fixture and saved result validates to the same data, and the contracts are documented. No collector, importer, or primitive decides the new elements yet; steps 50–53 do.
**Prerequisite:** Step 48.

- Write representative snapshot fixtures first: a NAT gateway with its subnet and Elastic IP, a same-account peering connection between two VPCs, a transit gateway with VPC attachments and a route table, gateway and interface VPC endpoints, a managed prefix list referenced by a route and a security-group rule, and a dual-stack VPC.
- Add resource models for NAT gateways, VPC peering connections, transit gateways with their attachments and route tables, VPC endpoints, and managed prefix lists, and add the IPv6 fields: VPC and subnet IPv6 CIDRs, ENI IPv6 addresses, IPv6 routes, egress-only internet gateways, and IPv6 security-group and NACL entries.
- Record each target as a typed relationship (for example, route to NAT gateway, attachment to transit gateway, endpoint to route table), so the graph can follow it with evidence.
- Keep schema `3` and add the new types and fields as optional, so every existing snapshot and saved result validates unchanged. A snapshot collected before this phase has no coverage for the new operations: its unknowns say coverage was unrecorded, never that the element is absent.

**Advice:** Shape the models from real `Describe*` response shapes, but keep only what a decision needs (state, CIDRs, attachments, owners, and entries); keep everything else out of typed fields.

**Acceptance check:** The new fixtures validate, round-trip through persistence, and keep exact citations; every existing fixture and saved result validates unchanged; model tests reject impossible states (for example, a peering connection whose two sides are the same VPC).

Implement and verify substeps 49.1–49.7 in order. Keep the parent planned until every substep's acceptance checks pass.

**Where things go:**
- Enum members in `models/enums.py`; resource models in `models/resources/network.py` (or a sibling module such as `models/resources/transit.py` if it grows past one readable file), added to `SupportedResource` in `models/architecture.py`.
- Fixtures beside the existing ones in `tests/fixtures/snapshots/`, one file per element family, so a failure names its family; model tests in `tests/models/`.
- `docs/` pages and the README are updated once, in 49.7, because the contracts are not user-visible until collection (step 50) lands.

**What is reused:**
- `Resource`, `Model`, `NonEmptyString`, and the sanitizing validators in `models/resource_base.py`; `Route`, `SecurityGroupRule`, `NaclEntry`, and `PrivateIpAddress` extended in place rather than copied.
- `DependencyRelationship` and `AttachmentKind` for typed relationships (`attributes["kind"]`), and the snapshot graph index's existing edge-and-evidence handling.
- The coverage key and outcome contracts in `models/coverage_key.py` and `models/coverage_outcomes.py`.

#### Step 49.1: Fix the vocabulary and guard compatibility
**Status:** Implemented. `ResourceType` gains the NAT gateway, egress-only internet gateway, peering connection, transit gateway, attachment, transit gateway route table, VPC endpoint, and managed prefix list types (CloudFormation type names); `RouteTargetKind` gains `egress_only_internet_gateway`; `AttachmentKind` gains the kinds step 49.6 records (NAT gateway subnet and interface, peering requester and accepter VPC, transit gateway attachment, VPC attachment, route table association and propagation, endpoint route table, subnet, interface and security group, egress-only gateway attachment, and prefix list reference). No existing value changed and no model uses them yet. `tests/models/test_fixture_compatibility.py` pins the canonical-JSON hash of every existing snapshot fixture and both saved-result fixtures as validated by `load_snapshot` and `load_analysis`, and fails when a snapshot fixture is neither pinned nor listed in `PHASE_SIX_SNAPSHOTS`. The coverage operation names were not declared here: an absent operation already reads as unrecorded coverage by service, and the names belong with the allowlist and permissions page in step 50.
**Prerequisite:** Step 48.

- Add the `ResourceType` members: NAT gateway, VPC peering connection, transit gateway, transit gateway attachment, transit gateway route table, VPC endpoint, managed prefix list, and egress-only internet gateway. Add the `RouteTargetKind` member for an egress-only internet gateway, and `AttachmentKind` members for each typed relationship step 49.6 records.
- Before changing any model, add a compatibility guard: every snapshot fixture and saved-result fixture validates to the same data, as the first test to run after each later substep.

**Advice:** Add enum members only, with no models yet. The guard is the safety net for the remaining substeps, so it must pass here, before anything else changes.

**Acceptance check:** The enums list the new members, the guard passes on the unchanged fixtures, and no existing enum value changes.

#### Step 49.2: NAT gateways, egress-only internet gateways, and peering connections
**Status:** Implemented. `NatGateway` (reported state, `NatConnectivity`, VPC, subnet, network interface, and IPv4 public addresses), `EgressOnlyInternetGateway` (VPC), and `VpcPeeringConnection` (reported state and a requester and accepter `PeeringSide`, each with VPC, owner account, and CIDRs) are in `models/resources/network.py` and the supported resource union, so schemas `2` and `3` accept them; every new field is optional except a peering connection's two sides and each side's VPC. The models reject a peering connection whose sides are the same VPC, a malformed owner account, an available public NAT gateway with no public address, and also a private NAT gateway with one. Fixtures `egress-gateways.json` (a public NAT gateway with its Elastic IP in a public subnet, and an egress-only internet gateway) and `vpc-peering.json` (a same-account peering between two VPCs) cite each fact to its source entry and are listed in `PHASE_SIX_SNAPSHOTS`; `tests/models/test_egress_and_peering.py` covers them. No collector, importer, relationship, or primitive uses the models yet.
**Prerequisite:** Step 49.1.

- Write the fixtures first: a NAT gateway in a public subnet with its Elastic IP, a same-account peering connection between two VPCs, and an egress-only internet gateway on a VPC.
- Add `NatGateway` (state, subnet, VPC, public or private connectivity, public IPs, network interface), `EgressOnlyInternetGateway` (VPC), and `VpcPeeringConnection` (state, requester and accepter VPC, CIDRs, and owner account ID for each side).
- Reject impossible states: a peering connection whose two sides are the same VPC, a public NAT gateway with no public address once `available`, and a malformed owner account.

**Advice:** Record both owner accounts on a peering side even when equal, so the same-account test is a comparison of recorded values, never an assumption.

**Acceptance check:** The fixtures validate and round-trip with exact citations, each impossible state is rejected, and the compatibility guard still passes.

#### Step 49.3: Transit gateways, attachments, and route tables
**Status:** Implemented. `TransitGateway` (reported state, owner account, and default association and propagation route table IDs), `TransitGatewayAttachment` (reported state, transit gateway, attached resource type, ID, and owner, association state, and associated route table), and `TransitGatewayRouteTable` (reported state, transit gateway, associations and propagations as `TransitGatewayLink`s, routes, and `routes_complete`) are in `models/resources/network.py` and the supported resource union. A `TransitGatewayRoute` has a reported destination, a list of attachment IDs (AWS can return several), a `TransitGatewayRouteType`, and a reported state. The attached resource type stays a reported string because AWS adds attachment kinds. `routes_complete` is false when the route search reports more routes than it returned and absent when unrecorded, so step 50 can mark incomplete coverage without a new contract. A route is rejected when it names attachments while `blackhole` or names none otherwise. Both snapshot schemas reject an attachment associated with, or propagating to, a route table of another transit gateway when both are recorded, with the coded error `snapshot_transit_gateway_link_mismatch` (`validate_transit_gateway_links` in `models/snapshot_integrity.py`). Fixture `transit-gateway.json` (a transit gateway with two VPC attachments and one route table with an association, a propagation, and a static, a propagated, and a blackhole route) cites each fact to its source entry and is listed in `PHASE_SIX_SNAPSHOTS`; `tests/models/test_transit_gateway.py` covers it. No collector, importer, relationship, or primitive uses the models yet.
**Prerequisite:** Step 49.1.

- Write the fixtures first: a transit gateway with two VPC attachments, one route table with an association, a propagation, and a static and a blackhole route.
- Add `TransitGateway` (state, owner, default association and propagation route table IDs), `TransitGatewayAttachment` (resource type, resource ID and owner, state, association state, associated route table), and `TransitGatewayRouteTable` (associations, propagations, and routes with destination, attachment, type, and state).
- Reject impossible states: a route naming an attachment while marked `blackhole`, a route with neither, and an association to a route table of another transit gateway.

**Advice:** Record the search bound the collector may hit as a field of the route table (such as whether its routes are known to be complete), so step 50 can mark incomplete coverage without a new contract.

**Acceptance check:** The fixtures validate and round-trip with exact citations, each impossible state is rejected, and the compatibility guard still passes.

#### Step 49.4: Endpoints, managed prefix lists, and their references
**Status:** Implemented. `VpcEndpoint` (a `VpcEndpointType` of gateway, interface, or gateway load balancer; service name, reported state, VPC, owner account, and route table, subnet, ENI, and security group IDs) and `ManagedPrefixList` (an `AddressFamily`, an owner that is an account ID or `AWS`, reported state, version, and CIDR entries) are in `models/resources/network.py` and the supported resource union. A prefix list's `entries` is `None` when the entries were not recorded, so an AWS-managed list is valid without them and stays distinguishable from a recorded empty list. Entry descriptions are not kept: they are free text the network collector already redacts, and no decision reads them. `Route` gains `prefix_list_id` beside the reported `destination`; `SecurityGroupRule.prefix_list_id` already existed. `NaclEntry` gains nothing, because network ACL entries cannot reference a prefix list in AWS. The models reject an entry whose CIDR family differs from its list's, a gateway endpoint with subnets, and an interface endpoint with route tables. Fixture `endpoints-and-prefix-lists.json` (a gateway endpoint on a route table, an interface endpoint with an ENI and a security group, an AWS-managed and a customer-owned prefix list, a route to each, and a security-group rule naming the customer list) cites each fact to its source entry and is listed in `PHASE_SIX_SNAPSHOTS`; `tests/models/test_endpoints_and_prefix_lists.py` covers it. A field added to an existing model is listed in `omitted_when_absent` on the new `OmitsAbsentFields` base in `models/resource_base.py`, which also replaces the hand-written omission of `Resource.lifecycle` and `ResourceLifecycle.value_scope`; an unset field is left out of output, so existing files dump and save exactly as before and the guard keeps its hashes. Step 49.5 adds its IPv6 fields the same way. `SupportedResource` is now a `type` alias: as an inline union, Pydantic rebuilt it inside each member's `children`, nesting one level per resource model, and import had reached about 940 frames, past the default limit under pytest collection; as an alias it is one shared definition and import stays under 100 frames however many models are added. `_attachment_evidence` in `services/network_routes.py` now finds a route's target edge by the evidence the route cites rather than by its destination string, so routes whose destinations share a reported form are not confused (review note 5). No collector, importer, relationship, or primitive uses the new models yet.
**Prerequisite:** Step 49.1.

- Write the fixtures first: a gateway endpoint on route tables, an interface endpoint with ENIs and security groups, and a managed prefix list referenced by a route and by a security-group rule, one customer-owned and one AWS-managed.
- Add `VpcEndpoint` (gateway or interface, service name, state, VPC, route tables, subnets, ENIs, security groups, owner) and `ManagedPrefixList` (address family, owner, state, version, entries with CIDR and description, and whether entries are recorded).
- Let `Route`, `SecurityGroupRule`, and `NaclEntry` name a prefix list through typed fields; `Route.destination` keeps its reported form.
- Reject impossible states: an entry whose CIDR family differs from its list, a gateway endpoint with subnets, and an interface endpoint with route tables.

**Advice:** An AWS-managed list is valid with no recorded entries; its explicit "entries not recorded" state is what lets step 52 answer `unknown` for the right reason.

**Acceptance check:** The fixtures validate and round-trip with exact citations, each impossible state is rejected, and the compatibility guard still passes.

#### Step 49.5: IPv6 fields and the dual-stack fixture
**Status:** Implemented. VPC and subnet IPv6 CIDRs stay in the existing `cidr_blocks`, told apart by family, rather than a new `ipv6_cidr_blocks`: the Terraform and CloudFormation importers already record them there with tested fact pointers, and a second field would give one fact two homes in snapshots already written. `Route.destination`, `SecurityGroupRule.cidr`, and `NaclEntry.cidr` already accepted IPv6 by family and are unchanged. `NetworkInterface` gains `ipv6_addresses` (typed `IPv6Address`, `None` when not recorded, omitted from output while unset); there is no separate public flag, because AWS assigns globally routable IPv6 addresses and whether one is public follows from the address. The models reject a subnet IPv6 CIDR outside its VPC's IPv6 CIDRs when the subnet is the VPC's child and the VPC records any, an IPv4 value as an IPv6 address, and a security-group rule naming both a CIDR and a prefix list (no collector or importer builds one). Fixture `dual-stack.json` (a VPC and two subnets with IPv4 and IPv6 CIDRs, an ENI with an IPv6 address, an IPv6 default route to an internet gateway in one table and to an egress-only internet gateway in another, and IPv6 security-group and NACL entries) cites each fact to its source entry and is listed in `PHASE_SIX_SNAPSHOTS`; `tests/models/test_dual_stack.py` covers it. Collection of the IPv6 fields stays in step 50.
**Prerequisite:** Step 49.1.

- Write the dual-stack fixture first: a VPC and subnet with IPv6 CIDRs, an ENI with an IPv6 address, an IPv6 default route to an internet gateway and one to an egress-only internet gateway, and IPv6 security-group and NACL entries.
- Add optional `ipv6_cidr_blocks` to `Vpc` and `Subnet`, a list of IPv6 addresses (with any public one) to `NetworkInterface`, and IPv6 destinations and CIDRs to `Route`, `SecurityGroupRule`, and `NaclEntry` through the existing `cidr` field's family, not a second field.
- Reject impossible states: a subnet IPv6 CIDR outside its VPC's, an IPv6 address that is not IPv6, and a rule that sets both a CIDR and a prefix list.

**Advice:** Keep IPv4 fields exactly as they are; add IPv6 beside them, so "no IPv6 recorded" and "no IPv6 collected" stay distinguishable by coverage, not by empty lists.

**Acceptance check:** The dual-stack fixture validates, round-trips, and keeps exact citations; each impossible state is rejected; the compatibility guard still passes.

#### Step 49.6: Typed relationships and graph traversal
**Status:** Implemented. The five phase-six fixtures record each relationship as an `attached_to` dependency citing the source entry it came from: NAT gateway to subnet and ENI, egress-only internet gateway to VPC, peering connection to requester and accepter VPC, transit gateway attachment to transit gateway and to its VPC, transit gateway route table to the attachments it associates and propagates, endpoint to route table, subnet, ENI, and security group, route table to NAT gateway, peering connection, transit gateway, endpoint, and egress-only internet gateway, and route and security-group rule to prefix list (`prefix_list_reference`). `egress-gateways.json`, `vpc-peering.json`, and `transit-gateway.json` each gained a route table for their route. A target outside the snapshot is external (the endpoints fixture's route to a transit gateway it does not record), and a reference the snapshot should hold but does not is a cited unresolved dependency (the NAT gateway's ENI). The graph index needed no change: it already follows any `AttachmentKind` in both directions with its evidence. One table-driven test in `tests/graph/test_graph_dependencies.py` follows each new relationship kind both ways with its exact evidence, plus the external and unresolved targets; the route tables added to the three fixtures are validated on load and serve step 53. The collector's network builder changed only where its sources already exist: one `_COLLECTED_BY` table names the types it stores and their operations, so a reference to a stored type resolves locally or is unresolved, and any other type is external; routes to an egress-only internet gateway are classified as such instead of `unknown`; a route whose destination is a prefix list adds a `prefix_list_reference` edge; and a security-group rule naming a prefix list is now `prefix_list_reference` rather than `security_group_reference` with `target_kind` `prefix_list`, which security-group impact still skips in older snapshots. `docs/network-mapping.md` lists the change. Handlers whose source is a new resource type (NAT gateway, egress-only internet gateway, peering connection, transit gateway attachment and route table, endpoint) move to step 50: their entries and `Describe*` operations do not exist until it adds the normalizers, and adding an operation enables its collection.
**Prerequisite:** Steps 49.2–49.5.

- Record each target as a typed dependency with evidence: route to NAT gateway, peering connection, transit gateway, endpoint, egress-only internet gateway, and prefix list; attachment to transit gateway and to its VPC; association and propagation to a transit gateway route table; endpoint to route table, subnet, ENI, and security group; NAT gateway to subnet and ENI; rule and route to prefix list.
- Give a target in another account, or absent from the snapshot, a representation that is an external reference, never a resource of this snapshot.
- Extend the graph index so each relationship can be followed in both directions with its evidence, using the existing identity, containment, and dependency handling.

**Advice:** Add the relationships to the fixtures that already exist rather than writing new graph fixtures, so one fixture proves both the model and the traversal. The relationship builders dispatch through per-resource handler tables (`_HANDLERS` in `ingestion/network/relationships.py` and `ingestion/elbv2/relationships.py`, `_GLOBAL_HANDLERS` in `ingestion/iam/relationships.py`); give each new resource type its own handler and table entry rather than a branch in a shared function.

**Acceptance check:** Graph tests follow every new relationship in both directions with exact evidence; an external target is reported as external; and a dangling reference to a missing resource is a cited unresolved dependency.

#### Step 49.7: Verify compatibility and close the step
**Status:** Implemented. Schema `3` is unchanged (no schema literal changed during step 49), and every new type and field is optional: the compatibility guard still pins every earlier snapshot fixture and both saved results to the same validated data, and each phase-six fixture's model test saves and reloads it with exact citations. A snapshot written before step 49 has no coverage record for the new read operations, and the coverage index already reports an absent record as unrecorded (`None`), never as successful collection (`test_missing_coverage_is_not_successful_zero_or_failure`); the route primitive's existing tests show an unsupported route target is `unknown`, never a denial. No test was added for either, since both are already covered. `docs/snapshot-format.md` gains a "Network path elements" section (the new types and what each records, the fields added to existing models, the rejected states, the relationship kinds, and how external, unresolved, and unrecorded coverage read) and rows for the five new fixtures, and states that nothing decides them yet. `docs/network-path-primitives.md` adds egress-only internet gateways to the unsupported route targets and links to that section. The analyzer pages already list these elements as `unknown`, and the README's capability table is unchanged because no behaviour changed.
**Prerequisite:** Step 49.6.

- Confirm schema `3` is unchanged and every new type and field is optional: a snapshot and a saved result written before this step validate unchanged, and a snapshot with the new types round-trips through persistence.
- Confirm a pre-step-49 snapshot reads as "coverage unrecorded" for the new operations, never as "element absent".
- Document the new resource types, fields, and relationships in the matching `docs/` page, keep the "unsupported" lists in `docs/network-path-primitives.md` accurate until steps 52–53 decide them, and update `README.md`'s capability table only where behaviour changed.
- Run the focused and full offline tests, the acceptance suite, Ruff, and the whitespace check, then review the full change as the Checks section requires.

**Advice:** Do not describe the elements as decided anywhere yet: this step settles contracts, and the primitives still answer `unknown` for them.

**Acceptance check:** Every check passes; all fixtures old and new validate; the docs describe the contracts without claiming decisions; the plan marks step 49 implemented.

### Step 50: Collect the new elements from AWS
**Status:** Implemented. Substeps 50.1–50.6 are implemented. The AWS collector reads every element step 49 modelled through 18 more EC2 operations (12 list reads and 6 per-item detail reads), each in the allowlist and the published policy (53 operation pairs). It records each relationship step 49.6 put in the fixtures, and qualifies every read's coverage, so a denied, failed, or bounded read leaves its elements unrecorded, never absent. Prefix-list entries are capped at 100 per list and transit gateway routes at 1000 per table. A target owned by another account, including a transit gateway shared through AWS RAM, is external. Analyzer results on IPv4 paths are unchanged.
**Prerequisite:** Step 49.

- Collect NAT gateways, VPC peering connections, transit gateways with their attachments, route tables, associations, propagations, and routes, VPC endpoints, and managed prefix lists with their entries, through read-only `Describe*`, `Get*`, and `Search*` calls added to the allowlist and the permissions documentation.
- Read the IPv6 fields already present in the VPC, subnet, ENI, route-table, security-group, and NACL responses, and egress-only internet gateways.
- Bound every new call with the shared call budgets and pagination limits, sanitize values as the existing collectors do, and qualify each operation's coverage, so a denied or failed read leaves its elements `unknown` rather than absent.
- Resolve the recorded route targets to the new resources; a target owned by another account is recorded as external, never as a resource of this snapshot.
- Record the relationships step 49.6 put in the fixtures from each new source: give each new resource type its own handler in `_HANDLERS` in `ingestion/network/relationships.py`, and add each collected type and its operation to `_COLLECTED_BY`, so its references resolve locally or are unresolved rather than external.

**Advice:** Transit gateway routes are the expensive read; search each route table once per collection with a bounded filter, and record when the bound is reached as incomplete coverage.

**Acceptance check:** Botocore Stubber tests cover each operation's success, pagination, denial, malformed responses, and bounds; the whole-architecture collection includes the new elements with coverage; the permissions page and allowlist list exactly the new read actions; no test needs AWS.

Implement and verify substeps 50.1–50.6 in order. Keep the parent planned until every substep's acceptance checks pass.

**Where things go:**
- Operation names in `Ec2Operation` (`ingestion/operations.py`). Adding a member allowlists it and puts it in the EC2 service catalog, so it is collected from then on. Add each name only in the substep that normalizes its results. Plain list reads join `EC2_OPERATIONS` in `ingestion/aws.py`; per-item reads (prefix-list entries, transit gateway route-table details) go through `DetailRunner`, as the ELBv2 collector does.
- Normalizers in `ingestion/network/resources.py`, or a sibling module such as `ingestion/network/transit.py` if it grows past one readable file; placement in `ingestion/network/containment.py`; relationships as one handler per source type in `_HANDLERS`, with each collected type and its operation added to `_COLLECTED_BY`.
- Stubber tests beside the existing ones in `tests/ingestion/aws/` (`test_network_collection.py`, `test_network_normalization.py`, `test_network_relationships.py`), with shared response data in `tests/support/network_data.py`.
- Each substep adds its actions to the IAM policy in `docs/aws-permissions.md` and its resources and relationships to `docs/network-mapping.md`, because the change is visible in collected snapshots as soon as it lands.

**What is reused:**
- `NetworkNormalizer`, `EvidenceBuilder`, `_validated`, the `Normalized` entries, and `normalize_region`; `DetailRunner` with its pagination policies, call budgets, and coverage records.
- The model contracts from steps 49.2–49.5 and the relationship kinds and fixture shapes from step 49.6: a collected snapshot records each element as the matching fixture does, with the same paths and attributes.
- The network Stubber helpers and the `unavailable` mapping, so a denied or failed read makes its references unresolved, never absent.

**Advice:** Keep tests to the usual path, as `AGENTS.md` asks. Denial, malformed-response, and budget handling are shared code that existing tests already cover. Each substep needs one normalization-and-relationship test from a representative response, and one Stubber test of the new reads' success and pagination. Add a denial or bound case only where the substep adds behaviour of its own, such as a search bound or an entries read.

#### Step 50.1: IPv6 fields in existing responses
**Status:** Implemented. `Ec2Normalizer` adds each VPC and subnet IPv6 CIDR whose association state is `associated` to `cidr_blocks`, after the IPv4 CIDRs, citing `/cidr_blocks/N` to its `Ipv6CidrBlockAssociationSet[n]` entry; other states are not recorded. `NetworkNormalizer` reads ENI `Ipv6Addresses` into `ipv6_addresses`, each cited to `NetworkInterfaces[i].Ipv6Addresses[n]`: an absent field leaves it unset, an empty list records none, and an invalid or non-string address is omitted with a limitation. Routes set `prefix_list_id` from `DestinationPrefixListId`. Route IPv6 destinations, security-group `CidrIpv6` and `Ipv6Ranges`, and NACL `Ipv6CidrBlock` were already read and cited per route, rule, or entry like their IPv4 forms, so they are unchanged. Only `subnet_network` reads `cidr_blocks` in the services, and it already keeps IPv4 only, so no decision changes. One new test, `test_dual_stack_fields_are_read_and_cited_to_their_entries`, normalizes a dual-stack response (including a disassociated VPC CIDR) to the shapes and citations of `dual-stack.json`; two existing tests gained an assertion each (an ENI without the field, and an invalid address). Existing network collection tests pass unchanged. `docs/network-mapping.md` describes the fields, and `docs/snapshot-format.md` now says the collector reads them.
**Prerequisite:** Step 49.

- Read the associated IPv6 CIDRs of VPCs and subnets (`Ipv6CidrBlockAssociationSet`, state `associated` only) into `cidr_blocks`, and ENI `Ipv6Addresses` into `ipv6_addresses`, each cited to its association or address entry, as in `dual-stack.json`.
- Set `Route.prefix_list_id` from `DestinationPrefixListId`. Route IPv6 destinations, security-group `CidrIpv6` and `Ipv6Ranges`, and NACL `Ipv6CidrBlock` are already read; confirm each is cited like its IPv4 form, and change nothing that already works.
- An ENI response without the field leaves `ipv6_addresses` unset (not recorded), and one with an empty list records none.

**Advice:** No new operation or permission is needed, so this substep changes only normalization. Recorded IPv4 output must not change: the existing collection tests are the proof.

**Acceptance check:** A dual-stack response normalizes to the shapes and citations of `dual-stack.json`; a disassociated IPv6 CIDR is not recorded; existing network collection tests pass unchanged.

#### Step 50.2: NAT gateways and egress-only internet gateways
**Status:** Implemented. `DescribeNatGateways` and `DescribeEgressOnlyInternetGateways` are allowlisted, collected with the other EC2 list reads, and documented in `docs/aws-permissions.md` (now 43 operation pairs) and `docs/network-mapping.md`. `NetworkNormalizer.nat_gateway` records state, connectivity, VPC, subnet, the primary address's network interface, and the public IPv4 addresses, citing each address field; a gateway is recorded in every reported state, including `deleted`. A NAT gateway goes under its subnet, or under its VPC when it has none (a regional NAT gateway). An invalid address or connectivity value is omitted with a limitation, and a gateway whose connectivity contradicts its addresses is recorded without `connectivity`, with a limitation saying why; any other validation error still fails, as for VPCs. `egress_only_internet_gateway` records the attached VPC, cited to its attachment, and goes under that VPC. The `_nat_gateway` handler links the subnet and network interface from the cited facts; `_egress_only_internet_gateway` shares the attachment logic of `_internet_gateway` and records the attachment state. Both types are in `_COLLECTED_BY`, so a route to either resolves locally, or is unresolved when missing or when its read did not complete. Tests: one new relationship test (placement, the three links, and both routes), the existing collection test extended with a two-page NAT read, the existing denied-gateway test extended with a denied NAT read, and one malformed NAT assertion in the existing malformed-values test. Three tests that hard-coded the EC2 operation count now derive it from `EC2_OPERATIONS`, so 50.3–50.5 need not change them.
**Prerequisite:** Step 50.1.

- Add `DescribeNatGateways` and `DescribeEgressOnlyInternetGateways`. Normalize to `NatGateway` (state, connectivity, VPC, subnet, and, from `NatGatewayAddresses`, the network interface and public IPv4 addresses) and `EgressOnlyInternetGateway` (the VPC from its attachment).
- Place a NAT gateway under its subnet, and an egress-only gateway under its VPC, never inventing a missing parent.
- Add `_nat_gateway` and `_egress_only_internet_gateway` handlers: NAT gateway to subnet and network interface; egress-only gateway to VPC, with the attachment state. Add both types to `_COLLECTED_BY`, so route targets naming them resolve locally or are unresolved.

**Advice:** Normalize a deleted or failed NAT gateway as reported rather than dropping it; deciding what its state means is step 53's job. A response that breaks the model's rules (an available public gateway without an address) is malformed: record it as a limitation, as `_validated` does for other entries, never as a valid gateway.

**Acceptance check:** Stubbed responses produce the resources and relationships of `egress-gateways.json`, with a route to the NAT gateway resolving locally; a denied `DescribeNatGateways` makes that route's target unresolved with the "did not complete" reason; the permissions page and allowlist list both actions.

#### Step 50.3: VPC peering connections
**Status:** Implemented. `DescribeVpcPeeringConnections` is allowlisted, collected with the other EC2 list reads, and documented (`docs/aws-permissions.md`, now 44 operation pairs, and `docs/network-mapping.md`). `NetworkNormalizer.vpc_peering_connection` stores the connection as a regional root with the reported status code as state and both sides' VPC, owner, and CIDRs (the IPv4 and IPv6 CIDR sets, else the primary CIDR), citing `/state`, `/requester`, and `/accepter` to `Status`, `RequesterVpcInfo`, and `AccepterVpcInfo`. The `_vpc_peering_connection` handler links each side's VPC from those citations: a side in this account and region resolves locally or is unresolved, and any other side is external with its account and region. The region is compared as reported, so a region `AwsRegion` does not know is still treated as elsewhere. `_external` takes an optional region for this. Peering connections are in `_COLLECTED_BY`, so a route to one resolves locally. Tests: the existing route-target test now also collects a same-account and a cross-account connection, and the collection test includes the read. A malformed side (no VPC ID, a bad owner, or the same VPC twice) still fails normalization, as a malformed VPC does; AWS does not report one.
**Prerequisite:** Step 50.2.

- Add `DescribeVpcPeeringConnections`. Normalize to `VpcPeeringConnection`, with each side's VPC, owner account, and IPv4 and IPv6 CIDRs, and the reported status code as state.
- Store a connection as a regional root, since it belongs to neither VPC alone.
- Add a `_vpc_peering_connection` handler for the requester and accepter VPC. A side in this account and region resolves locally or is unresolved; a side in another account or region is external, carrying that account and region.

**Advice:** A cross-account connection appears in both accounts' collections, as each side's own snapshot records it. Do not try to match them up: composing snapshots is out of scope for this phase (see the open questions).

**Acceptance check:** A same-account response produces `vpc-peering.json`'s resources and relationships; a cross-account response gives an external accepter VPC with its account ID; a route to the connection resolves locally.

#### Step 50.4: VPC endpoints and managed prefix lists
**Status:** Implemented. `DescribeVpcEndpoints` and `DescribeManagedPrefixLists` join the EC2 list reads, and `GetManagedPrefixListEntries` runs once per listed prefix list through `DetailRunner` (a new `ec2` pagination mode). The EC2 job's reads moved from `collection_jobs.py` to `read_region` in the new `ingestion/network/collector.py`. All three actions are in `docs/aws-permissions.md` (now 47 operation pairs). **Entry limit (decided here):** each entries read is a single call with `MaxResults` 100, the API's largest page. A list that reports more is not paged through: its `entries` stay unset and the read's coverage is incomplete with an unresolved value. A denied or failed read does the same, and with no list collected the entries read is `skipped`. An invalid entry also leaves `entries` unset, so a list's entries are recorded whole or not at all. AWS-managed lists are read too; `entries` alone says whether they were recorded. `vpc_endpoint` goes under its VPC with its type, service, state, owner, and each route table, subnet, ENI, and security group cited to its list position; a type the model does not know (`Resource`, `ServiceNetwork`, `Tunnel`) is recorded without `endpoint_type`, with a limitation. `managed_prefix_list` is a regional root citing each entry to the entries response. The `_vpc_endpoint` handler links all four kinds through a `_fact_link` helper now shared with `_nat_gateway`. Route tables, endpoints, and prefix lists are in `_COLLECTED_BY`, so endpoint routes and prefix-list references from routes and rules resolve locally. ELBv2's limited-coverage helper moved to `detail_execution.with_problems` for both collectors. `DetailResponse` is imported only for type checking in `ingestion/network`, so analysis modules still never load botocore. Tests: one new relationship test (placement, the four endpoint links, route and rule prefix-list references, recorded and unread entries, a redacted entry description); one new collection test (a denied and an over-limit entries read); the existing collection test checks one entries read per list. Tests that took `EC2_OPERATIONS` as the recorded set now use the catalog's `REGIONAL_OPERATIONS`, which includes the entries read, and the fake EC2 client answers direct detail calls.
**Prerequisite:** Step 50.3.

- Add `DescribeVpcEndpoints`. Normalize to `VpcEndpoint` and place it under its VPC. Add an `_vpc_endpoint` handler for its route tables, subnets, ENIs, and security groups.
- Add `DescribeManagedPrefixLists` and `GetManagedPrefixListEntries` (one bounded detail read per list). Normalize to `ManagedPrefixList` as regional roots, with `entries` left unset when the entries read was denied, failed, or hit its bound, and the coverage for that read marked accordingly.
- Add both types to `_COLLECTED_BY`, so route targets naming an endpoint, and route destinations and security-group rules naming a prefix list, resolve locally or are unresolved.

**Advice:** Read the entries of AWS-managed lists too, since the API allows it; whether they were recorded is still told by `entries`, not assumed from the owner. Decide here how many entries one list may return before it is incomplete, and state the limit in the limitations section of `docs/network-mapping.md`.

**Acceptance check:** Stubbed responses produce `endpoints-and-prefix-lists.json`'s resources and relationships; a denied or bounded entries read leaves `entries` unset with qualified coverage, while the list itself is recorded.

#### Step 50.5: Transit gateways, attachments, and route tables
**Status:** Implemented. **Route limit (settled here):** one `SearchTransitGatewayRoutes` per route table with `MaxResults` 1000, the API's largest, filtered to `static` and `propagated` routes and never repeated to read past the limit. When the search reports `AdditionalRoutesAvailable`, the routes returned are kept, `routes_complete` is false, and the search's coverage is incomplete with an unresolved value. The limit is `TRANSIT_GATEWAY_ROUTE_LIMIT` in `ingestion/network/collector.py` and is stated in `docs/network-mapping.md`. `DescribeTransitGateways`, `DescribeTransitGatewayAttachments`, and `DescribeTransitGatewayRouteTables` join the EC2 list reads. Per route table, `read_region` runs `GetTransitGatewayRouteTableAssociations`, `GetTransitGatewayRouteTablePropagations` (both paged in full), and the search through the same `DetailRunner` as the prefix-list entries. A table's associations or propagations count only when its last page ended the read. All six actions are in `docs/aws-permissions.md` (now 53 operation pairs). The allowlist safety test now also permits `Search`, a read-only prefix. The normalizers are in the new `ingestion/network/transit.py`, and every type is a regional root. A failed detail read leaves that list empty with a limitation on the table, and the table is kept. A transit gateway shared from another account through AWS RAM is not stored: `normalize_region` passes these gateways and their owners to the relationship builder, so attachments and VPC routes naming one are external with the owner account. The `_transit_gateway_attachment` handler links the transit gateway and, for a VPC attachment, the VPC, each external when another account owns it. `_transit_gateway_route_table` links each associated and propagated attachment with its state. Both go through `_fact_link`, which now takes an owner and attributes. All three types are in `_COLLECTED_BY`. `normalize_region` passed the complexity limit, so it now reads one-argument normalizers from a table, with helpers for EC2, rules, and shared gateways. Tests: one new relationship test (attachments, associations, propagations, routes, the shared gateway, and VPC routes to an owned and a shared gateway), one new collection test (an over-limit search and denied associations), and the existing collection test with the reads included. The ingestion-service test now expects every detail read to be `skipped` when its list read fails.
**Prerequisite:** Step 50.4.

- Settle the open question first: how many routes one transit gateway route table search may return before its coverage is incomplete. Record the bound in the code and in the limitations section of `docs/network-mapping.md`.
- Add `DescribeTransitGateways`, `DescribeTransitGatewayAttachments`, and `DescribeTransitGatewayRouteTables`, normalized to the step 49.3 models as regional roots.
- For each route table, through `DetailRunner`: `GetTransitGatewayRouteTableAssociations`, `GetTransitGatewayRouteTablePropagations`, and one `SearchTransitGatewayRoutes` with a bounded `MaxResults` and a `type` filter of `static` and `propagated`. `AdditionalRoutesAvailable` sets `routes_complete` to false and marks that read's coverage incomplete.
- Add handlers: attachment to transit gateway and attached VPC; route table to the attachments it associates and propagates. A transit gateway or attached resource owned by another account (shared through AWS RAM) is external.

**Advice:** This is the expensive read, so search each table once per collection and never repeat a search to page past the bound. A failed detail read leaves that table's associations, propagations, or routes unrecorded, without dropping the table.

**Acceptance check:** Stubbed responses produce `transit-gateway.json`'s resources and relationships; a search that reports more routes gives `routes_complete: false` and incomplete coverage; a VPC route to the gateway resolves locally.

#### Step 50.6: Verify the whole collection and close the step
**Status:** Implemented. A new `element_pages()` in `tests/support/network_data.py` gives fake-client pages for every element read beyond the base network data, including the detail reads. It replaces the setup `test_network_collection.py` had grown during 50.4–50.5. The whole-architecture acceptance collection (`test_whole_architecture_acceptance.py`, two accounts and two regions over every service) now includes those pages. Its inventory check requires all eight new resource types in every region, and an EC2 coverage record for every operation in `REGIONAL_OPERATIONS`, in both complete and partly denied collections. `test_new_elements_leave_ipv4_analysis_unchanged` collects once with every new element and once with the new list reads empty, and gets identical internet-exposure and security-group-impact results (apart from `created_at`); the first includes the public instance's finding. The analyzer comparison runs in the fast network collection test, not the acceptance journey, so the slow journey is not run twice. `test_permissions_documentation.py` already checks that the published policy lists exactly the allowlisted reads. Re-reading the docs found four stale statements, now fixed. `docs/aws-collection.md` still said nine EC2 reads (now 21) and listed only the original network resources. The prefix-list bullet in `docs/network-mapping.md` said a denied read marks coverage incomplete when it can also fail. That page's title now names step 50 too. `docs/roadmap.md`'s phase 6 row says the elements are modelled and collected. The README is unchanged: it says phase 6 is in progress, which is still true. Review of the whole step found one redundant check in `_resolve`, removed.
**Prerequisite:** Step 50.5.

- Run one whole-architecture Stubber collection over a VPC holding every new element, and confirm the snapshot validates, records coverage for every new operation, and that analyzer results on its IPv4 paths match the same collection without the new elements (the primitives still answer `unknown` for them).
- Confirm the permissions page's policy and the allowlist list exactly the new read actions (`test_permissions_documentation.py`), and that `docs/network-mapping.md` and `docs/aws-collection.md` describe what is collected.
- Run the focused and full offline tests, the acceptance suite, Ruff, and the whitespace check, then review the full change as the Checks section requires.

**Advice:** Extend the existing whole-collection test with the new responses rather than writing a second one.

**Acceptance check:** Every check passes; the whole collection includes the new elements with coverage; existing analyzer results are unchanged; the plan marks step 50 implemented.

### Step 51: Import the new elements from Terraform and CloudFormation
**Status:** Implemented. Substeps 51.1–51.6 are implemented. Terraform state and plans and CloudFormation templates import NAT and egress-only gateways, peering connections, endpoints, managed prefix lists, and transit gateways with their attachments and route tables onto the step 49 models. Separately declared parts stay separate resources that reference their parent, and the parent's list is never claimed complete. Unknown and `!Ref` values stay unset. Imported links stay configuration references, so the primitives answer `unknown` for imported paths. Observed and declared elements match by identity only. Existing import output is unchanged. `PeeringSide.vpc_id` became optional (51.2).
**Prerequisite:** Step 50.

- Map the Terraform resource types (`aws_nat_gateway`, `aws_vpc_peering_connection`, `aws_ec2_transit_gateway` and its attachment and route-table types, `aws_vpc_endpoint`, `aws_ec2_managed_prefix_list`, `aws_egress_only_internet_gateway`) and the matching CloudFormation types onto the new models, from state, plans, and templates, in place of today's unknown resources.
- Keep the importers' rules: an unknown or planned value is never a decided one, and a reference that cannot be resolved is a cited unresolved dependency.
- Extend comparison so an observed element and its declared counterpart match only by provider identity, as today.

**Advice:** Add one Terraform state, one plan, and one CloudFormation fixture per new type rather than one large template, so a mapping failure points at its type.

**Acceptance check:** Each type imports from each source with exact citations; unknown planned values stay indeterminate; comparison matches observed and declared elements without guessing; existing import fixtures are unchanged.

Implement and verify substeps 51.1–51.6 in order. Keep the parent planned until every substep's acceptance checks pass.

**Where things go:**
- Terraform: `RESOURCE_MAPPINGS` in `ingestion/terraform/catalog.py` and the field mapping in `mapping.py` and `mapping_values.py`; references in `ingestion/terraform/relationships.py` (`_PARENT_CANDIDATES` and the cited reference fields), with planned values through `plan_values.py` and `plan_relationships.py`.
- CloudFormation: `RESOURCE_MAPPINGS` in `ingestion/cloudformation/catalog.py`, the property mapping in `mapping.py`, and parents in `relationships.py` (`_PARENTS`). `Ref` and `Fn::GetAtt` references are already linked generically by intrinsic evaluation.
- Fixtures beside the existing ones in `tests/fixtures/terraform/` and `tests/fixtures/cloudformation/`, named by element family as in step 49 (for example `egress-gateways.state.json`, `egress-gateways.plan.json`, `egress-gateways.yaml`); tests in `tests/ingestion/terraform/` and `tests/ingestion/cloudformation/`.
- Each substep updates `docs/terraform-state-mapping.md`, `docs/terraform-plan-mapping.md`, and `docs/cloudformation-mapping.md` for its types, since import output changes as soon as it lands.

**What is reused:**
- The model contracts from step 49, the typed-model path the importers already use for VPCs, subnets, route tables, and security groups, and their sanitization, citation, unknown-value, and limitation handling.
- The step 49 fixtures as the target shape: a declared element records the fields its source declares, in the same models.

**Decisions made in this plan (as today's importers do):**
- Imported links stay configuration references: `terraform_configuration_reference` or `cloudformation_configuration_reference`, with the field named. A declared reference is not a deployed attachment, so the importers never record the collector's typed kinds (`nat_gateway_subnet` and so on). The primitives therefore keep answering `unknown` for imported network paths, as they do today for imported route tables and security groups.
- A typed field records only what the element's own declaration states. Several elements AWS reports as one resource are declared as separate child resources. Those children (`aws_ec2_transit_gateway_route_table_association`, `_propagation`, and `aws_ec2_transit_gateway_route`; `aws_vpc_endpoint_route_table_association` and its subnet and security-group siblings; `aws_ec2_managed_prefix_list_entry`; and their CloudFormation counterparts) stay unknown resources with cited references to their parent. They are not folded into it. A parent that such a child names records a limitation that its list may be incomplete, and a list the model can leave unset (prefix-list `entries`, `routes_complete`) stays unset.
- Out of scope, unchanged from today: Terraform inline `route` and `ingress`/`egress` blocks (imported only when empty), CloudFormation `AWS::EC2::Route` and inline security-group rules, and Terraform data sources (so an AWS-managed prefix list read through `data.aws_ec2_managed_prefix_list` is not imported). Route targets, prefix-list destinations, and IPv6 rules therefore come only from collection in this phase. Mapping them is a separate step if wanted.

**Advice:** Keep tests to the usual path, as `AGENTS.md` asks. Each substep needs one test per source (state, plan, template) built from its fixtures, checking the typed fields, citations, and references. Add a case only for behaviour of its own, such as an unknown planned value or a separately declared child. Test plan values through the substep's own plan fixture rather than a second plan test.

#### Step 51.1: NAT gateways and egress-only internet gateways
**Status:** Implemented. Terraform `aws_nat_gateway`, previously an unknown resource, now maps to `NatGateway`: `connectivity_type`, `network_interface_id`, a nonempty `public_ip` as a one-item `public_ips` (a new `address_lists` mapping field), and `subnet_id` or `vpc_id`. `aws_egress_only_internet_gateway` maps to `EgressOnlyInternetGateway` with `vpc_id`. CloudFormation `AWS::EC2::NatGateway` (`ConnectivityType`) and `AWS::EC2::EgressOnlyInternetGateway` map the same way; the NAT gateway's `AWS::EC2::EIP` stays an unsupported summary, so `public_ips` stays empty. Both importers now record `vpc_id` and `subnet_id` as typed fields when the model declares them, as collection does, and as attributes otherwise, so existing types are unchanged. Terraform placement and references read whichever the model uses (`_reference` in `terraform/relationships.py`). A NAT gateway's parent is its subnet, else its VPC; an egress-only gateway's is its VPC; neither gets a state. The CloudFormation typed branch's per-model `if` chain moved into the catalog's `string_fields`. A declared value the model rejects (such as `ConnectivityType: Public`) now falls back to an unsupported summary with its Name tag and a coverage limitation, as Terraform already did, instead of failing the import. Fixtures `egress-gateways.state.json`, `egress-gateways.plan.json` (unknown ID, interface, address, and subnet), and `egress-gateways.yaml` are in `tests/fixtures/terraform/` and `tests/fixtures/cloudformation/`. Tests: `test_terraform_network_elements.py` (state, and plan with indeterminate unknown values) and `test_cloudformation_network_elements.py` (template, and a rejected value). The NAT network interface is a typed field only, not a reference, because AWS creates it and no configuration declares it. Existing import fixtures and tests are unchanged.
**Prerequisite:** Step 50.

- Write the fixtures first: `egress-gateways.state.json`, `egress-gateways.plan.json` (a NAT gateway created with an unknown ID and network interface), and `egress-gateways.yaml`, each with a VPC, a subnet, a public NAT gateway with its Elastic IP, and an egress-only internet gateway.
- Terraform `aws_nat_gateway` (replacing today's unknown mapping) maps `subnet_id`, `connectivity_type`, `network_interface_id`, `public_ip`, and `vpc_id` for a regional NAT gateway; `aws_egress_only_internet_gateway` maps `vpc_id`. CloudFormation `AWS::EC2::NatGateway` maps `SubnetId` and `ConnectivityType` (its address comes from a referenced `AWS::EC2::EIP`, which stays unknown), and `AWS::EC2::EgressOnlyInternetGateway` maps `VpcId`.
- A NAT gateway's parent is its subnet, else its VPC; an egress-only gateway's is its VPC. Neither declares a state, so `state` stays unset.

**Advice:** A declared public NAT gateway has no recorded state, so the model's available-without-address rule does not apply. Do not default `state` to make it apply.

**Acceptance check:** Each source imports both types with exact citations and resolved references to the subnet and VPC; the plan's unknown ID and interface stay indeterminate; existing import fixtures and their tests are unchanged.

#### Step 51.2: VPC peering connections
**Status:** Implemented. **Contract change:** `PeeringSide.vpc_id` is now optional, and both sides default to an empty `PeeringSide`, relaxing step 49.2. A template's `VpcId: !Ref Vpc` and a plan's unknown VPC ID have no deployed ID, so with required fields nearly every declared connection would have fallen back to an unknown summary. An unset side means "not recorded". The same-VPC rule now applies only when the sides record a VPC. Collection always records both, and every existing snapshot still validates. Terraform `aws_vpc_peering_connection` and CloudFormation `AWS::EC2::VPCPeeringConnection` map to `VpcPeeringConnection`: the requester VPC from `vpc_id`/`VpcId`; the accepter VPC and owner from `peer_vpc_id`/`PeerVpcId` and `peer_owner_id`/`PeerOwnerId`; the region from `peer_region`/`PeerRegion` as an attribute; and Terraform's `accept_status` as state. The requester's owner is never assumed. A string field's target may now name a nested field (`requester/vpc_id`) in both importers, and a field the mapping claims skips the generic `vpc_id` handling. Terraform references read the nested sides (`_NESTED_REFERENCES`), and CloudFormation's `!Ref` sides are references through intrinsic evaluation. A connection is a regional root. `aws_vpc_peering_connection_accepter` stays an unknown summary. Its unknown service gives it a stable ID distinct from the connection, so the two never collide. Unknown summaries keep only their type, so in a state it carries no reference to the connection; in a plan, its configuration expressions do. Neither importer produces external targets, so a peer VPC in another account is an unresolved reference, as any missing declaration is. Fixtures `vpc-peering.state.json` (with an accepter resource), `vpc-peering.plan.json`, and `vpc-peering.yaml`; one test per source added to the two `*_network_elements.py` files.
**Prerequisite:** Step 51.1.

- Write the fixtures first: a same-account peering between two VPCs declared in each source.
- Terraform `aws_vpc_peering_connection` maps the requester side from `vpc_id` and the accepter side from `peer_vpc_id`, `peer_owner_id`, and `peer_region`, and `accept_status` as state when the state records it. CloudFormation `AWS::EC2::VPCPeeringConnection` maps `VpcId`, `PeerVpcId`, `PeerOwnerId`, and `PeerRegion`. Side CIDRs are not declared, so they stay empty.
- The requester's owner is not declared, so leave it unset rather than assuming the importing account. Record `peer_owner_id` only when the source states it.
- Settle `aws_vpc_peering_connection_accepter` first: it declares the same connection ID from the accepter's account, so mapping it as a second peering connection would store one ID twice. The plan assumes it stays an unknown resource with a cited reference to the connection.

**Advice:** A connection is a regional root, as in collection; its two VPCs are references, not parents.

**Acceptance check:** Each source imports the connection with both sides cited and its VPC references resolved; a requester owner is never invented; an accepter resource does not duplicate the connection.

#### Step 51.3: VPC endpoints and managed prefix lists
**Status:** Implemented. Terraform `aws_vpc_endpoint` and CloudFormation `AWS::EC2::VPCEndpoint` map to `VpcEndpoint`: the endpoint type (an unmodelled type such as `Resource` is omitted), service name, VPC, and route table, subnet, network interface (Terraform only), and security group IDs, each cited by position; Terraform also records `state` and `owner_id`. The endpoint `policy` is not mapped. Terraform `aws_ec2_managed_prefix_list` and CloudFormation `AWS::EC2::PrefixList` map to `ManagedPrefixList`: address family, owner and version (Terraform), name (`PrefixListName` in CloudFormation), and each entry's CIDR, recorded whole or not at all and cited to the CIDR leaf, so descriptions never reach the evidence. The AWS spellings (`Gateway`, `IPv4`) moved to a shared `ingestion/aws_values.py` used by the collector and both importers. The new field helpers (`choice`, `string_list`, `cidr_entries`) live once in `ingestion/import_fields.TypedFields`, which both importers' field mappers now extend; Terraform adds `integer` and `address_lists` of its own. An endpoint is placed under its VPC and references its route tables and subnets (Terraform `_TYPE_REFERENCES`, the renamed `_NESTED_REFERENCES`) and its security groups; a prefix list is a regional root. **Separately declared children:** the new `terraform/children.py` lists `aws_ec2_managed_prefix_list_entry` and the three `aws_vpc_endpoint_*_association` types. `SnapshotBuilder` records each child's parent ID as it maps the child. Once every resource is mapped, the child gets a cited `terraform_configuration_reference` to its parent (unresolved when the parent is absent), and the parent gets a limitation that its list may be incomplete. A prefix list's `entries` is also left unset. CloudFormation declares neither kind of child, so it needs none of this. In CloudFormation an ID list's `!Ref` items are references, and the typed list keeps only literal IDs, with a limitation for each item left out. In a Terraform plan, an unknown list keeps the model's default empty list uncited, with the unknown value recorded as unresolved. Fixtures `endpoints-and-prefix-lists.state.json` (with a separate entry resource), `.plan.json`, and `.yaml`; one test per source.
**Prerequisite:** Step 51.2.

- Write the fixtures first: a gateway endpoint on a route table, an interface endpoint with a subnet and a security group, a customer-managed prefix list with two entries, and one separately declared entry resource, in each source.
- Terraform `aws_vpc_endpoint` maps `vpc_endpoint_type`, `service_name`, `vpc_id`, `route_table_ids`, `subnet_ids`, `security_group_ids`, and `network_interface_ids` when the state records them. `aws_ec2_managed_prefix_list` maps `address_family`, `version`, and its inline `entry` blocks (CIDRs only; descriptions are not kept). CloudFormation `AWS::EC2::VPCEndpoint` and `AWS::EC2::PrefixList` (`Entries`) map the same fields.
- A list with a separately declared entry resource keeps `entries` unset, with a limitation naming the child, because its inline entries are not the whole list.

**Advice:** An endpoint type the model does not know is recorded without `endpoint_type`, as the collector does.

**Acceptance check:** Each source imports both types with exact citations and resolved references to the VPC, route table, subnet, and security group; a list with a separate entry resource has `entries` unset and says why.

#### Step 51.4: Transit gateways, attachments, and route tables
**Status:** Implemented. Terraform `aws_ec2_transit_gateway`, `aws_ec2_transit_gateway_vpc_attachment`, and `aws_ec2_transit_gateway_route_table`, and CloudFormation `AWS::EC2::TransitGateway`, `AWS::EC2::TransitGatewayVpcAttachment` and `AWS::EC2::TransitGatewayAttachment` (one shared mapping), and `AWS::EC2::TransitGatewayRouteTable`, map to the three step 49.3 models, all regional roots. A gateway records its owner and default route table IDs. An attachment records its gateway and its VPC as the attached resource with its owner, and references both. Its `attached_resource_type` is `vpc`, cited to the declared type, through a new `constants` mapping in both importers; CloudFormation uses a `TYPE_SOURCE` marker that `_record_facts` cites to `Type`. A route table records and references its gateway. A CloudFormation `TransitGatewayVpcAttachment` imports as the model's `AWS::EC2::TransitGatewayAttachment` type, and its declared type stays cited on `/resource_type`. **Children:** Terraform `aws_ec2_transit_gateway_route_table_association`, `_propagation`, and `aws_ec2_transit_gateway_route` joined `terraform/children.py`, which now lets a child declare further references. Each child references its route table and the attachment it names; a blackhole route names none. CloudFormation's three counterparts stay unsupported summaries whose `Ref`s already reference the table and attachment. A new `qualify_children` in `cloudformation/relationships.py` gives the table its limitation, worded by the shared `import_fields.incomplete_list`. The table's associations, propagations, and routes stay empty and uncited, and `routes_complete` stays unset, so nothing claims them complete. No fixture sets an attachment's associated route table, so the `snapshot_transit_gateway_link_mismatch` check does not apply to imports. Fixtures `transit-gateway.state.json`, `.plan.json`, and `.yaml`; one test per source.
**Prerequisite:** Step 51.3.

- Write the fixtures first: a transit gateway, a VPC attachment, a route table, and separately declared association, propagation, and static route resources, in each source.
- Terraform `aws_ec2_transit_gateway` maps `owner_id` and the default association and propagation route table IDs. `aws_ec2_transit_gateway_vpc_attachment` maps `transit_gateway_id`, `vpc_id` as the attached resource, and `vpc_owner_id`; its attached resource type is `vpc`, cited to the declared Terraform type. `aws_ec2_transit_gateway_route_table` maps `transit_gateway_id`. CloudFormation `AWS::EC2::TransitGateway`, `AWS::EC2::TransitGatewayVpcAttachment` and `AWS::EC2::TransitGatewayAttachment`, and `AWS::EC2::TransitGatewayRouteTable` map the same fields.
- Associations, propagations, and routes stay child resources, as decided above: the route table records a limitation for each child that names it, and `routes_complete` stays unset.
- CloudFormation's `AWS::EC2::TransitGatewayVpcAttachment` imports as the model's `AWS::EC2::TransitGatewayAttachment` type. Record the declared type in the evidence so the change of name is cited.

**Advice:** Check snapshot integrity on the fixtures early: an attachment whose associated route table belongs to another transit gateway is rejected (`snapshot_transit_gateway_link_mismatch`), and a declaration can say that.

**Acceptance check:** Each source imports the three types with exact citations; the association, propagation, and route resources reference the table and attachment; the table's associations, propagations, and routes are not claimed as complete.

#### Step 51.5: Compare observed and declared elements
**Status:** Implemented. No comparison code changed: matching and field comparison are generic over resource types, as expected. `test_collected_network_elements_match_their_declarations_by_identity_only` in `tests/comparison/test_comparison_service.py` collects the step 50 elements (`element_pages()`) and compares them with a Terraform state declaring the same IDs. Every declared element pairs on `physical_identity`, none is ambiguous, and a pair reports the fields one side lacks as differences. The transit gateway template (logical IDs only) and plan (unknown IDs) pair with nothing.
**Prerequisite:** Step 51.4.

- Compare a step 50 collection against a Terraform state declaring the same elements by their physical IDs. Each new type pairs by native type and physical ID only. Fields the state does not declare (such as a NAT gateway's state) are reported as the comparison reports other one-sided fields today, and typed links on the observed side differ from configuration references on the declared side, as they already do for route tables.
- Confirm a CloudFormation template, which has logical IDs only, never pairs with an observed element, and a plan's unknown-ID element stays unmatched.
- Change comparison only if a check fails. The plan expects no change, because matching and field comparison are generic over resource types.

**Advice:** Reuse the step 50 collection data (`element_pages()`) for the observed side, so the declared fixtures carry the same IDs.

**Acceptance check:** Observed and declared elements of each new type match by identity and nothing else; template and unknown-ID elements are unmatched; existing comparison tests pass unchanged.

#### Step 51.6: Verify the imports and close the step
**Status:** Implemented. The three original fixtures (`state.json`, `plan.json`, `template.yaml`) import to byte-identical JSON with the code from before step 51 (`0c1a40f~1`) and with the current code. All twelve new fixtures validate, save, and load back equal (checked once rather than by a new test, since persistence is generic and already covered). Re-reading the import docs against the code found three stale statements, now fixed. The state page's note on typed `vpc_id`/`subnet_id` named only the gateway models, and it now also covers the data-source gap for AWS-managed prefix lists. `docs/infrastructure-as-code.md` listed only the original typed resources. Neither the state page nor the CloudFormation page said that configuration references keep imported network paths `unknown`. The out-of-scope list still matches the code: Terraform inline routes and rules remain unavailable fields, `AWS::EC2::Route` has no mapping, and data sources are skipped. `docs/roadmap.md`'s phase 6 row says the elements are now imported. A scan of step 51's source diff found no unused code or leftovers.
**Prerequisite:** Step 51.5.

- Confirm the existing `state.json`, `plan.json`, and `template.yaml` imports are byte-for-byte unchanged, and that every new fixture validates and round-trips.
- Re-read `docs/terraform-state-mapping.md`, `docs/terraform-plan-mapping.md`, `docs/cloudformation-mapping.md`, and `docs/infrastructure-as-code.md` against the code, including the out-of-scope list above, and update `docs/roadmap.md`'s phase 6 row.
- Run the focused and full offline tests, the acceptance suite, Ruff, and the whitespace check, then review the full change as the Checks section requires.

**Advice:** Do not describe imported network paths as decided anywhere: the primitives read typed links, which the importers do not record.

**Acceptance check:** Every check passes; existing import output is unchanged; the docs match the code; the plan marks step 51 implemented.

### Step 52: Decide prefix lists and IPv6 in the primitives
**Status:** Implemented. Substeps 52.1–52.6 are implemented. Security groups, NACLs, and routes answer per address family, and so does public addressing. Security-group rules and routes decide managed prefix lists from recorded, cited entries. Egress-only internet gateways are a route reach. Every analyzer result on the existing fixtures is unchanged (52.6).
**Prerequisite:** Step 49.

- Expand a managed prefix list referenced by a security-group rule or route into its recorded entries, citing the list. NACL entries cannot reference a prefix list (step 49.4). An AWS-managed list whose entries are not recorded stays `unknown`.
- Evaluate each primitive per address family: IPv6 security-group and NACL entries, IPv6 longest-prefix routes, egress-only internet gateways (outbound only), and recorded public IPv6 addresses on ENIs.
- Keep the existing IPv4 behaviour and results unchanged, except where 52.1 decides otherwise.

**Advice:** Treat a dual-stack resource as two questions, one per address family, rather than one merged answer, so a denial over IPv4 never hides an allow over IPv6.

**Acceptance check:** Primitive tests decide each new case from cited configuration, a partially covering prefix-list or IPv6 entry is `unknown`, unrecorded list entries are `unknown`, and every existing primitive test passes unchanged apart from the cases 52.1 lists.

Implement and verify substeps 52.1–52.6 in order. Keep the parent planned until every substep's acceptance checks pass.

**Where things go:**
- The shared address and CIDR screening in `services/network_matching.py` (`screen_cidr`, `parse_network`, `Undecided`); the primitives in `services/network_security_groups.py` (`Peer`), `network_acls.py`, `network_routes.py`, and `network_addressing.py`.
- Prefix-list resolution, shared by security groups and routes, in a new `services/network_prefix_lists.py`: from a rule or route to its `ManagedPrefixList` through the `prefix_list_reference` edge that cites the same source entry, as `_attachment_evidence` finds a route's target.
- A `RouteReach` member for an egress-only internet gateway in `models/enums.py`. Tests beside the existing ones in `tests/network/`, built from `dual-stack.json` and `endpoints-and-prefix-lists.json` where they fit.
- Each substep updates the supported and unsupported table in `docs/network-path-primitives.md` for what it decides, because the analyzers that call these primitives can now return different results.

**What is reused:**
- `NetworkResult`, `cited_decision`, `missing` and `unsupported`, `qualify_coverage`, `sole_associate`, and the bounded-address handling (`Peer.within` and a CIDR `address`), which already makes partial coverage `unknown`.
- The relationships and typed fields from steps 49–50: `prefix_list_reference` edges, `Route.prefix_list_id`, `ManagedPrefixList.entries` (`None` when unrecorded), `NetworkInterface.ipv6_addresses`, and `egress_only_internet_gateway_attachment` edges with their state.

**Advice:** A primitive answers for the address family of its query and never merges the two: the caller asks once per family. Keep each primitive's signature: an IPv6 query is an IPv6 address or CIDR where an IPv4 one is accepted today. Add tests only for what each substep decides, as `AGENTS.md` asks: one allow, one deny, and one `unknown` case per new element, and an assertion in an existing test where one already sets up the case.

#### Step 52.1: Address families in the shared screening, security groups, and NACLs
**Status:** Implemented. `screen_cidr` compares within one address family: the same family decides as before, including bounded partial overlap, and the other family is `False`. The new `parse_network` accepts an IPv4 or IPv6 address or CIDR. `Peer` and `evaluate_network_acl` use it, so both decide IPv6 queries from cited IPv6 rules and entries. `parse_ipv4_network` stayed for routes until 52.2, which removed it. **Changed IPv4 results (the listed cases):** (1) an IPv6 security-group rule was `unsupported` for an IPv4 peer and is now skipped, so a group whose only other rules do not match denies implicitly instead of being `unknown`; (2) an IPv6 NACL entry ahead of the IPv4 match was `unsupported` and is now skipped, so the IPv4 entry decides. (3) A NACL entry with no recorded CIDR (possible from imports) was mislabelled as unsupported IPv6 and is now `missing_evidence`; it stays `unknown`. No analyzer or acceptance fixture contains these cases, so every analyzer result is unchanged. Internet exposure still decides IPv4 only, and `docs/internet-exposure.md`, `docs/database-access.md`, and `docs/analyzers.md` now say that IPv6 rules are skipped rather than making the result `unknown`. `dual-stack.json` gained the cited association of `acl-dual` to `subnet-dual-public`. Tests: the NACL test that asserted the old `unsupported` result now asserts the skip, and the security-group test dropped its IPv6 `unsupported` case. Over IPv6, `dual-stack.json`'s rule and entry allow 443 and deny 22 implicitly, the rule never matches an IPv4 peer, and a bounded IPv6 peer partly covered by a rule is `unknown`. ICMPv6 (protocol 58) stays an unrecognized protocol, so it is `unknown`.
**Prerequisite:** Step 49.

- Settle first: today an IPv6 rule or NACL entry is `unsupported` even for an IPv4 query. That makes some IPv4 results `unknown`; for example, an IPv6 deny entry ahead of an IPv4 allow in first-match order (`test_network_acls.py`). Evaluating per family means a rule or entry of the other family can never match, so it is skipped and those results become decided. The plan recommends this, because the `unknown` came from an unsupported element rather than missing configuration. That relaxes "IPv4 results unchanged" for exactly these cases: list them in the status, and update the existing tests that assert the old `unsupported` result.
- `screen_cidr` compares within one family: same family decides as today, including bounded partial overlap; the other family is `False`. `Peer` and the NACL query accept an IPv6 address or CIDR, and `parse_ipv4_network` gains an IPv6 counterpart (or one family-aware parser).
- Security groups and NACLs then decide IPv6 queries from cited IPv6 rules and entries, with the same blockers and citations as IPv4.

**Advice:** Prefix-list rules stay `unsupported` here; 52.4 decides them.

**Acceptance check:** `dual-stack.json`'s IPv6 rule and NACL entry allow and deny IPv6 queries as recorded; an IPv6 query partly covered by a bounded entry is `unknown`; IPv4 results are unchanged apart from the listed cases.

#### Step 52.2: IPv6 routes and egress-only internet gateways
**Status:** Implemented. `evaluate_subnet_route` parses the query with `parse_network` and selects the longest-prefix route of the query's address family. Routes of the other family are skipped, so IPv4 results are unchanged, and the now unused `parse_ipv4_network` is removed. `RouteReach.EGRESS_ONLY_INTERNET_GATEWAY` is new. One `_GATEWAYS` table names each gateway reach's resource type, attachment kind, required state (`available` for an internet gateway, `attached` for an egress-only one), and collection operation. `_attachment_evidence` reads that table for both kinds, and coverage qualification limits only the queried gateway's operation. A best route to an egress-only gateway denies an internet-gateway query, so inbound exposure, which asks for an internet gateway, never accepts it. IPv4 routes cannot target an egress-only gateway, so this changes no IPv4 result. `dual-stack.json` gained cited associations of both route tables to their subnets and the attachments of `igw-dual` (`available`) and `eigw-dual` (`attached`) to `vpc-dual`. Its IPv6 local routes stay uncited, so a local IPv6 query there is `unknown`. Tests: one dual-stack test (the public table reaches the internet gateway over IPv6 with the route, association, and attachment cited; the private table reaches the egress-only gateway; and the egress-only route denies an internet-gateway query) and one test for an unrecorded or detached egress-only gateway (`unknown`) against an attached one (allow). The existing route test also asserts that an IPv6 query ignores the IPv4 default route. `route_data.py` gained an `egress_only_gateway` helper. The composition test's per-fixture call counts rose by one per subnet, because it calls the route primitive once per `RouteReach` member.
**Prerequisite:** Step 52.1.

- `evaluate_subnet_route` selects the longest-prefix route of the query's family. An IPv4 query ignores IPv6 routes as today, and an IPv6 query ignores IPv4 routes.
- Add `RouteReach.EGRESS_ONLY_INTERNET_GATEWAY`. A route to one reaches it only when the gateway is recorded with an `attached` attachment to the subnet's VPC, as an internet gateway route requires an `available` attachment. It is outbound only: the primitive answers what a route reaches; the caller asks for it only for outbound traffic, and inbound exposure never accepts it.
- An IPv6 route to an internet gateway reaches it under the same attachment rule as IPv4.

**Advice:** Reuse `_attachment_evidence` for both gateway kinds rather than copying it: it already finds the route's target edge by the route's citation.

**Acceptance check:** `dual-stack.json`'s public table reaches the internet gateway over IPv6 and its private table reaches the egress-only gateway; a missing or detached gateway is `unknown`; IPv4 route results are unchanged.

#### Step 52.3: Public IPv6 addresses
**Status:** Implemented. `evaluate_public_address` takes a keyword `family` (`AddressFamily`, IPv4 by default), so its positional signature and every existing caller are unchanged. A small per-family function answers each question, and the two share one absence check (a cited implicit deny, or `unknown` when normalization dropped addresses). Over IPv6, an ENI's recorded addresses that are `is_global` allow, citing each one's `/ipv6_addresses/<n>` fact. A recorded empty list, or only non-global addresses, denies. An unset field is `unknown`. Instances record no IPv6 field, so an IPv6 question about an instance is `unknown` and names the ENIs as the place to ask. The IPv4 answer is unchanged. **Fixture choice:** the fixtures keep their documentation-range `2001:db8::/32` addresses, and the check treats them as stated: not global, so not public. `dual-stack.json`'s `eni-dual` therefore denies over IPv6, and the allow case uses a global address (`2600:1f18:…`) built through the normalizer. Tests: one new test covers allow (a global address beside a unique-local one, citing only the global one), deny for an empty list, `unknown` for an unrecorded field and for an instance, and the documentation-range deny on `dual-stack.json`. Every existing public-addressing test passes unchanged.
**Prerequisite:** Step 52.1.

- `evaluate_public_address` answers per family. For IPv6, an ENI's recorded global unicast `ipv6_addresses` allow, a recorded empty list denies, and an unset field (not recorded) is `unknown`. AWS IPv6 addresses are globally routable, so no separate public flag is needed (step 49.5).
- The IPv4 answer is unchanged, including that an instance whose only public address is IPv6 has a recorded absence of public IPv4.

**Advice:** Decide "public" from the address itself (`is_global`), not from the subnet; a documentation-range address such as the fixtures' `2001:db8::/32` is not global. Choose whether the fixtures keep it and test with a global address, or the check treats it explicitly: say which in the status.

**Acceptance check:** A recorded global IPv6 address allows, an empty list denies, an unrecorded field is `unknown`, and every existing public-addressing test passes unchanged.

#### Step 52.4: Prefix lists in security-group rules
**Status:** Implemented. The new `services/network_prefix_lists.py` has `resolve_prefix_list`, which 52.5 can reuse unchanged. It returns a stored list's recorded entries, each CIDR with its `/entries/<n>` citations, or an `Undecided` naming the list: "is not recorded" when the list is not stored, is external, or has an unresolved reference, and "entries are not recorded" when `entries` is unset. It finds the list through a new shared `cited_target` in `network_matching.py`: the one edge of a kind that cites the same source entry. `network_routes._attachment_evidence` now uses that helper instead of its own copy. In `network_security_groups.py`, a prefix-list rule matches when some recorded entry of the peer's family holds the peer, using the existing `screen_cidr`. A bounded peer partly covered by an entry is `unknown`, an empty recorded list matches nothing, and an allow cites the rule and each entry that matched. An implicit deny still cites the group's own evidence, not the entries. When a rule in the evaluated direction names a list, `DescribeManagedPrefixLists` and `GetManagedPrefixListEntries` join the qualifying operations, so a limited list read gives `unknown` (checked once by hand: a failed entries read turns the fixture's allow into `coverage_limited`). **Result change:** a rule naming an unrecorded list was `unsupported_element` and is now `missing_evidence` with the list named; it stays `unknown`. A rule naming a list with recorded entries is decided. The analyzers that call this primitive (database access, internet exposure) see that change; no analyzer or acceptance fixture names a recorded list. `test_database_access_paths.py`'s unsupported-element test now uses a cross-account group reference, which is still unsupported. The primitives page, `docs/database-access.md`, `docs/analyzers.md`, and `docs/internet-exposure.md` say how prefix-list rules are decided. Internet exposure still opens no range from a prefix-list rule; step 54 changes that. Tests (on `endpoints-and-prefix-lists.json`): the `pl-office` rule allows a peer inside an entry, citing that entry and the rule, and denies a peer outside; a partly covered bounded peer is `unknown`; and an empty recorded list denies. A second test shows an unrecorded-entries list and an unstored list are each `unknown` with the list named. The old prefix-list `unsupported` case was removed from the security-group test.
**Prerequisite:** Step 52.1.

- A rule naming a prefix list resolves the list through its `prefix_list_reference` edge. When the list is stored with recorded `entries`, the rule matches when an entry of the query's family contains the peer. A bounded peer partly covered by an entry is `unknown`, as for a CIDR. The decision cites the rule and each entry it used.
- A list that is not stored, is external, or has `entries` unset (an AWS-managed list, a list over the entry limit, or an import with separate entry resources) is `unknown`, with a blocker naming the list. Qualify the result by the `DescribeManagedPrefixLists` and `GetManagedPrefixListEntries` coverage, so a limited read is never read as an empty list.
- An empty recorded list matches nothing.

**Advice:** Put the resolution in `network_prefix_lists.py` so 52.5 reuses it unchanged; it returns the list's entries with their citations, or an `Undecided` saying why not.

**Acceptance check:** `endpoints-and-prefix-lists.json`'s rule naming `pl-office` allows a peer inside an entry and denies one outside; the same rule against an unrecorded list is `unknown` with the list named; partial coverage is `unknown`.

#### Step 52.5: Prefix-list route destinations
**Status:** Implemented. The AWS page was re-checked when implementing ([How route priority works](https://docs.aws.amazon.com/vpc/latest/userguide/route-tables-priority.html), read 2026-10-28) and still gives the ordering above. In `network_routes.py`, each route contributes `_Candidate` prefixes of the query's family. A CIDR route gives its destination, and a prefix-list route gives each recorded entry, resolved by 52.4's `resolve_prefix_list` with the entry's citations. A route that records only a `pl-` destination (older snapshots) is resolved the same way and, having no reference edge, is `unknown` naming the list. The longest prefix wins as before. A tie between distinct routes is `unknown` with one of three reasons: several CIDR routes (the existing message), a CIDR route and a prefix-list route ("whether the route is static or propagated is not recorded"), or several prefix-list routes ("AWS chooses between overlapping prefix lists at random"). A prefix-list route whose list is not stored or has unrecorded entries makes the table's result `unknown` naming the list, because it could cover any address. As before, when the list's address family differs from the query's, the result is still `unknown`. The selected route's target still decides reach, and the decision also cites the entry that matched; an endpoint target stays `unsupported`. A table with a prefix-list route is also qualified by the two list reads. **Result change:** a table with a prefix-list route was always `unknown` (`unsupported_element`). It is now decided when its lists' entries are recorded, and is otherwise `unknown` with `missing_evidence` naming the list. `endpoints-and-prefix-lists.json` gained the cited association of `rtb-svc` to `subnet-svc`. As recorded, its AWS-managed `pl-s3` has no entries, so the table is `unknown` naming `pl-s3`. Tests: with `pl-s3` given recorded entries, an address in `pl-office` selects `/routes/2` (a transit gateway target, still `unsupported` until step 53), an address in no list is an implicit deny, and a blackholed `pl-office` route denies citing the route and the matched entry. One more test checks that an unrecorded list, a CIDR-and-list tie, and a two-list tie are each `unknown` with their reason. The old route test that asserted `unsupported` for a prefix-list route now covers only the other-family case. The fixture-editing helper moved from the security-group tests to `tests/support/prefix_list_data.py`, now shared by both test modules.
**Prerequisite:** Step 52.4.

- A route whose destination is a prefix list contributes each recorded entry of the query's family as a candidate prefix. Longest prefix wins as today.
- Follow AWS route priority ([How route priority works](https://docs.aws.amazon.com/vpc/latest/userguide/route-tables-priority.html)): at equal length a static route beats a prefix-list route, which beats a propagated route, and overlapping prefix lists with different targets are chosen at random. The collector does not record a route's origin, so a tie between a CIDR route and a prefix-list entry, or between two prefix lists, is `unknown` and says why.
- A prefix-list route whose list is unresolved or has unrecorded entries is `unknown` when it could cover the query, as any undecidable route is today. The selected route's target still decides reach. An endpoint target stays `unsupported` until step 53.

**Advice:** Check the AWS documentation again when implementing: the ordering above is from the VPC user guide and could change.

**Acceptance check:** `endpoints-and-prefix-lists.json`'s route to `pl-office` is selected for an address in its entries; a tie and an unrecorded list are `unknown` with their reasons; routes without prefix lists are unchanged.

#### Step 52.6: Verify the primitives and close the step
**Status:** Implemented. **Analyzer results:** a scratch script ran every analyzer over every snapshot fixture in `tests/fixtures`: ownership, cross-account trust, and internet exposure on each; security-group impact on each group; and database access on each database at ports 5432, 3306, and 1433. It ran once with the source exported from `5d40d67` (before step 52) and once with the current code. All 132 runs give identical results apart from `created_at`, so no fixture exercises 52.1's listed cases or the new decisions, and no analyzer result changed. The acceptance suite (205) and the full offline suite pass. **Docs:** `docs/network-path-primitives.md`'s supported table, primitive descriptions, and per-family paragraph match the code. The page now says the analyzers still ask IPv4 questions and adopt the new decisions in step 54. `docs/internet-exposure.md` and `docs/security-group-impact.md` point to step 54 for prefix-list and IPv6 candidates and for naming prefix-list references. `docs/database-access.md` and `docs/analyzers.md` no longer list prefix-list routes as always `unknown`. `docs/roadmap.md`'s phase 6 row names step 52. **Review:** the full diff of steps 52.1–52.5 was re-read. The fixes are recorded in each substep's status. Deliberately left as is: an implicit deny from a security group does not cite the prefix-list entries it checked; a list's state (for example `modify-in-progress`) is not interpreted; ICMPv6 is an unrecognized protocol; and a prefix-list route whose list is unrecorded makes the table `unknown` even when the list's address family differs from the query's.
**Prerequisite:** Step 52.5.

- Re-run the phase 3 acceptance fixtures and the analyzer tests. Results that involve no IPv6 or prefix-list element must not change; any change from 52.1's listed cases is named in the status.
- Confirm `docs/network-path-primitives.md`'s supported table describes exactly what is now decided, and that the internet-exposure, database-access, and security-group-impact pages still state their limits correctly. The analyzers adopt these primitives in step 54, so their pages say so until then.
- Run the focused and full offline tests, the acceptance suite, Ruff, and the whitespace check, then review the full change as the Checks section requires.

**Advice:** Keep `unknown` the default: an element this step cannot settle from recorded, cited configuration stays `unknown` with a blocker that names it.

**Acceptance check:** Every check passes; analyzer results outside the listed cases are unchanged; the docs match the code; the plan marks step 52 implemented.

### Step 53: Route through NAT gateways, peering, transit gateways, and endpoints
**Status:** Planned.
**Prerequisite:** Steps 50 and 52.

- Extend `RouteReach` and route evaluation: a NAT gateway route reaches the internet outbound only, and only when the NAT gateway is available, has a public address, and its own subnet routes to an attached internet gateway; a NAT gateway never allows inbound traffic.
- A peering route reaches the peer VPC's CIDR only when the connection is active and both sides' route tables route to each other.
- A transit gateway route reaches a destination only when the source attachment's associated transit gateway route table routes to the destination's attachment, and the return path does too.
- A gateway endpoint route reaches its AWS service's prefix list; an interface endpoint is an ENI with security groups.
- Add a composed route between two subnets in `network_composition.py` that follows local, peering, and transit gateway routes in both directions, citing every hop.
- A hop owned by another account, or outside the snapshot, is `unknown` with a blocker naming it; it is never an allow.

**Advice:** Bound the hops: a path crosses at most one transit gateway, or one peering connection, plus local routes. A deeper chain is `unknown` and says so, rather than searching the graph.

**Acceptance check:** Each route kind decides `allow`, `deny`, and `unknown` cases from fixtures with every hop cited; blackhole, pending, and deleted states deny or are `unknown` as recorded; a cross-account hop is `unknown`; existing local and internet-gateway route results are unchanged apart from the cases 53.1 lists.

Implement and verify substeps 53.1–53.7 in order. Keep the parent planned until every substep's acceptance checks pass.

**Where things go:**
- Route selection stays in `services/network_routes.py`. The selected route's target is decided through one table of target handlers keyed by `RouteTargetKind` (local, internet gateway, and egress-only gateway today), so each new kind adds a handler instead of another branch in `_decide`.
- The handlers for each new kind live in their own modules, because each needs its own state rules: `network_nat.py`, `network_peering.py`, `network_transit.py`, and `network_endpoints.py`. The composed two-subnet route goes in `network_composition.py`.
- New `RouteReach` members in `models/enums.py` for the questions that name a kind (`NAT_GATEWAY` for outbound internet through NAT, `VPC_ENDPOINT`). A hop into another VPC answers a different question: does the best route for this address lead into VPC V? It is a new primitive, `evaluate_vpc_route(index, subnet_id, address, vpc_id)`, added in 53.2 and extended in 53.4. It allows the local route when V is the subnet's own VPC, or one peering or transit gateway hop into V.
- Tests beside the existing ones in `tests/network/`, built from `egress-gateways.json`, `vpc-peering.json`, `transit-gateway.json`, and `endpoints-and-prefix-lists.json`. Each fixture records only one direction today, so each substep adds the subnets, associations, return routes, and gateways its decisions need, each one cited. Generalize `tests/support/prefix_list_data.py` into one fixture-editing helper (`edited_index(fixture, changes)`) so state, account, and blackhole variants are edits of the recorded fixture, not new fixtures.
- Each substep updates `docs/network-path-primitives.md`'s tables and coverage for what it decides.

**What is reused:**
- `_best_route` and its tie and partial-coverage rules (52.5), `cited_target` to follow a route to its target by citation, `_attachment_evidence` and the `_GATEWAYS` table, `cited_decision`, `qualify_coverage`, `all_of` and `any_of`, and the bounded-address handling.
- The relationships and fields from steps 49–50: `nat_gateway_subnet`, the two peering VPC edges, the transit gateway attachment, association, and propagation edges with their states, `endpoint_route_table`, `endpoint_interface`, and `endpoint_security_group`, plus `routes_complete` and `TransitGatewayRoute.route_type`.

**Advice:**
- One rule for every state: a recorded state that carries traffic continues the hop. A recognized state that carries none (blackhole, pending, deleted, rejected, and so on) denies, citing the state. A missing or unrecognized state is `unknown`. Each substep lists its element's states in a lookup table, checked against the AWS API reference when it is implemented, since AWS adds states.
- Another account's element is `unknown` with a blocker naming it and the account, as is an element outside the snapshot (the open question above stays settled: snapshots are not composed).
- Qualify each hop by the collection operations it read: `DescribeNatGateways`, `DescribeVpcPeeringConnections`, the transit gateway reads, and `DescribeVpcEndpoints`.
- Re-run the scratch analyzer comparison from 52.6 after each substep that can change a result. A change is allowed only where 53.1 lists it.

#### Step 53.1: Route-target handlers and NAT gateways
**Status:** Implemented. **Settled (accepted 2026-10-28):** a supported target kind that a question does not ask for is a cited deny ("leads elsewhere"), not `unknown`. **Refactor:** `_decide` now finds the selected route's target check in `_TARGETS`, a table keyed by `RouteTargetKind`. Each check takes a `RouteHop` (the subnet, table, route citations, element, and address, plus `follow` to ask the same address from another subnet) and returns the evidence confirming the target, or the result that ends the decision. `_attachment_evidence` is the check for both gateway kinds, collection operations per reach come from one `_REACH_OPERATIONS` table, and `_Gateway` lost its `operation` field. With only the existing kinds, the full suite and the analyzer comparison were unchanged. **NAT gateways:** `RouteReach.NAT_GATEWAY` is new. `services/network_nat.py` has `nat_gateway_evidence`. Using the `DescribeNatGateways` state table, `available` carries traffic; `failed`, `deleting`, and `deleted` deny citing `/state`; and `pending` or an unrecorded state is `unknown`. A private gateway denies citing `/connectivity`, and missing connectivity or address is `unknown`. Otherwise the gateway's `nat_gateway_subnet` must route the same address to an available, attached internet gateway (`RouteHop.follow`, one level, which cannot recurse). The allow cites the route, the association, the gateway's state, connectivity, and addresses, the subnet edge, and the onward route, association, and gateway attachment. `DescribeNatGateways` qualifies a NAT question. `network_nat` imports `RouteHop` for type hints only, so the modules do not import each other at runtime. **Listed change:** an internet-gateway or local question whose best route leads to a NAT gateway was `unknown` (`unsupported_element`) and is now a cited deny. In the analyzers, internet exposure of a public address in a subnet routed through NAT is now a denial (`test_exposure_addresses.py`'s test renamed and updated). The scratch analyzer comparison against the code before 53.1 (132 runs) shows no other change: no fixture analyzer run reaches a NAT route. **Fixture and helper:** `egress-gateways.json` gained `subnet-private` on `rtb-private`, `rtb-public` on `subnet-public` with a cited route to the new `igw-egress`, and that gateway's `available` attachment. `tests/support/prefix_list_data.py` became `tests/support/fixture_edits.py` (`edited_index(fixture, changes, without=...)`), where `without` drops dependencies citing given evidence. The two prefix-list test modules bind it with `partial`. **Tests:** `tests/network/test_nat_gateways.py` covers the cited allow from `subnet-private` and the inbound deny; deleted, pending, and private gateways; and a NAT subnet without a route table (`unknown`). In `test_subnet_routes.py`, the NAT case left the unsupported-targets test, and the longest-prefix test now asserts the selected NAT route denies an internet-gateway question. The composition test's call counts rose by one per subnet for the new reach. **Docs:** the primitives page (description, routes row, summary), `docs/internet-exposure.md`, `docs/database-access.md`, and `docs/analyzers.md`.
**Prerequisite:** Step 52.

- **Settled (accepted 2026-10-28).** Today a best route to a NAT gateway, peering connection, transit gateway, or endpoint is `unsupported`, so an internet-gateway or local question whose best route leads there is `unknown`. Once a kind is supported, the route "leads elsewhere" and the question denies, citing the route. For example, internet exposure of a resource in a private subnet behind NAT changes from `unknown` to a cited deny. The plan recommends this: the `unknown` came from an unsupported element, not from missing configuration, as in 52.1. List each changed case in the substep that adds the kind, and confirm with the analyzer comparison that nothing else changed.
- Refactor `_decide` so the selected route's target is decided by a handler table keyed by `RouteTargetKind`, with no change in behaviour: the full suite and the analyzer comparison are unchanged before any new kind is added.
- Add `RouteReach.NAT_GATEWAY` for outbound internet traffic through NAT. A route to a NAT gateway reaches the internet only when the gateway is stored, `public` connectivity, in a carrying state (`available`), and records a public address, and when its own subnet (`nat_gateway_subnet`) routes the same address to an available, attached internet gateway (a nested `evaluate_subnet_route(..., reach=INTERNET_GATEWAY)`, which cannot recurse further). The decision cites the route, the gateway's state and address, and the NAT subnet's route and gateway attachment.
- A private NAT gateway never reaches the internet: deny, citing its connectivity. A NAT gateway never allows inbound traffic, because inbound questions ask for an internet gateway, and a NAT route then leads elsewhere.

**Advice:** Use the gateway state list from the `DescribeNatGateways` API reference (`pending`, `failed`, `available`, `deleting`, `deleted`): `pending` is `unknown`, and `failed`, `deleting`, and `deleted` deny.

**Acceptance check:** `egress-gateways.json`, extended with a private subnet on `rtb-private` and a public subnet whose table routes to an attached internet gateway, reaches the internet through `nat-public` with every hop cited. A failed or deleted gateway denies, a pending one and a NAT subnet without a recorded route are `unknown`, a private NAT gateway denies, and an internet-gateway question on the private subnet denies (a listed change). Every other existing result is unchanged.

#### Step 53.2: VPC peering hops
**Status:** Implemented. **Primitive:** `evaluate_vpc_route(index, subnet_id, address, vpc_id)` in `network_routes.py`. It shares one `_evaluate` skeleton with `evaluate_subnet_route` (table selection, decision, and coverage). `_decide` now takes the target kinds a question wants, each with its check: a reach question wants its one kind, and a VPC question wants `local` when `vpc_id` is the subnet's own VPC, else `vpc_peering`. `_VPC_KINDS` holds the kinds only a VPC question asks for, so any other question whose best route is a peering connection denies (the settled rule). **Peering:** `services/network_peering.py` has `peering_evidence`. Its state table follows the `VpcPeeringConnection` status codes: only `active` carries traffic, and `initiating-request`, `pending-acceptance`, `provisioning`, `rejected`, `failed`, `expired`, `deleting`, and `deleted` deny, citing `/state`. An unrecorded state is `unknown`. The subnet's VPC must be one side, found through the two VPC edges. The other side must be a stored VPC with the same owner. Otherwise the result is `unknown`, naming the connection, the VPC, and the account; a side recording no owner while the other does counts as another account. A peer VPC other than `vpc_id` denies ("leads to …, not the VPC asked"). The peer side's recorded CIDRs must hold the whole address (`screen_cidr`, bounded): a partly covered range is `unknown`, a disjoint one denies, and unrecorded CIDRs are `unknown`. The allow cites the route, the association, the connection's `/state`, `/requester`, and `/accepter`, and both VPC edges. `DescribeVpcPeeringConnections` qualifies a VPC question. **Shared:** NAT's private deny and state handling moved onto `RouteHop` as `denied(resource, message, element, *pointers)` and `state_result(resource, state, states, label, element)`, which `network_nat.py` now uses. Being methods, they need no runtime import of `network_routes`. **Listed change:** a question whose best route is a peering connection was `unknown` (`unsupported_element`) and now denies, citing the route. The analyzer comparison against the code before 53.2 (132 runs) shows no change. **Fixture:** `vpc-peering.json` gained `subnet-app` and `subnet-data`, cited associations of `rtb-app` and the new `rtb-data`, and `rtb-data`'s cited return route to `pcx-app-data`. **Tests:** `tests/network/test_vpc_peering.py` covers both directions allowed with the forward hop's citations, and the internet-gateway question on a peering route denying. A table-driven test covers `pending-acceptance` (deny citing the route), another-account accepter (`unknown`, naming the account), and a peer CIDR not holding the address (deny). The peering case left `test_subnet_routes.py`'s unsupported-targets test. **Docs:** the primitives page (new `evaluate_vpc_route` entry, a "leads elsewhere" note, the routes row, the summary), `docs/internet-exposure.md`, `docs/database-access.md`, and `docs/analyzers.md`. Left as is: a peering hop's deny does not cite the route table's association, as for NAT; and the same-VPC local route is uncited in the fixtures, so it is not tested for an allow here.
**Prerequisite:** Step 53.1.

- Add `evaluate_vpc_route(index, subnet_id, address, vpc_id)`. It allows when the subnet's best route for the address is the local route and `vpc_id` is the subnet's own VPC. Or the route leads to a peering connection whose one side is the subnet's VPC and whose other side is `vpc_id`, and the address lies in that side's recorded CIDRs. The connection must be in a carrying state (`active`). It cites the route, the connection, and its two VPC edges.
- A connection whose other side is in another account (owner IDs differ) or not stored is `unknown`, naming the connection and account. Peering is not transitive, so traffic is never forwarded out of the peer VPC: the hop ends there.
- Return traffic is not decided here: the peer side's route back is the reverse question, which 53.6 composes.

**Advice:** Use the `VpcPeeringConnection` status codes from the API reference (`initiating-request`, `pending-acceptance`, `active`, `deleted`, `rejected`, `failed`, `expired`, `provisioning`, `deleting`). Only `active` carries traffic.

**Acceptance check:** `vpc-peering.json`, extended with a subnet in each VPC and `vpc-data`'s return route, allows `vpc-app` to `vpc-data` with the route and connection cited. A `pending-acceptance` or `deleted` connection denies, a cross-account accepter is `unknown` naming the account, and an address outside the peer's CIDRs is not routed into it.

#### Step 53.3: Record transit gateway attachment subnets
**Status:** Implemented (option a). **Collection:** `DescribeTransitGatewayVpcAttachments` joined `Ec2Operation` and `EC2_OPERATIONS` (result key `TransitGatewayVpcAttachments`), so it is allowlisted, collected per region with pagination, and covered like the other list reads. `docs/aws-permissions.md` now lists 54 operation pairs (52 collector actions), and the policy test checks the action. It is a plain list read, not a detail read. `normalize_region` joins its entries to the attachments by ID (`_vpc_attachments`, as `_rules_by_group` joins rules). A read that did not complete gives an empty mapping, so no subnets are recorded from it. **Model:** `TransitGatewayAttachment.subnet_ids: list[str] | None`, omitted when absent, so existing snapshots and saved output are unchanged. `transit.py` records the list only when the VPC attachment entry's `SubnetIds` is a list of non-empty strings, never a partial list. It cites each subnet as `/subnet_ids/<n>` ← `TransitGatewayVpcAttachments[j].SubnetIds[n]`, or `/subnet_ids` for an empty list. A denied or failed read, an attachment missing from the response, or a malformed list leaves it unset. **Relationship:** the new `transit_gateway_attachment_subnet` edge links a VPC attachment to each subnet, citing that subnet's fact. It is external with the VPC's owner when another account owns the attached VPC. **Imports:** Terraform `aws_ec2_transit_gateway_vpc_attachment.subnet_ids` and CloudFormation `SubnetIds` (both attachment types) map through the shared `string_lists`. The Terraform state fixture records `subnet-app`, the plan (unknown value) leaves it unset, and the CloudFormation fixture's `!Ref` subnet is a reference and leaves it unset. **Found and carried to 53.4:** the shared importer list helper keeps a list's literal items when another item is a `!Ref`, with a limitation. A mixed `SubnetIds` therefore imports partial, as `SubnetIds` on VPC endpoints already does. Changing the helper would change step 51's endpoint output, so 53.4 now requires an attachment with unset subnets or an import limitation to be `unknown`. **Tests:** a new collection test checks that the fake collection records `tgw-attach-1`'s subnet cited to the VPC attachment read with a matching edge, and that a denied read leaves `subnet_ids` unset with `failed` coverage. There are assertions in the existing relationship test (the subnet edge) and in the Terraform state, Terraform plan, and CloudFormation import tests. The fake EC2 client gained the new operation's empty page, and `network_data.py` gained `TRANSIT_GATEWAY_VPC_ATTACHMENT`. **Docs:** `docs/network-mapping.md` (the operation list, the attachment row, the new edge row, and how unrecorded subnets arise), `docs/snapshot-format.md` (the field and edge), `docs/terraform-state-mapping.md`, and `docs/cloudformation-mapping.md`. No primitive reads the field yet, so analyzer results cannot change.
**Prerequisite:** Step 50.

- **Settled (accepted 2026-10-28): option (a).** A transit gateway VPC attachment only sends and receives traffic in the Availability Zones where it has a subnet, and the snapshot does not record those subnets (`DescribeTransitGatewayAttachments` does not return them). Without them, a transit gateway allow is not fully cited. The options are: (a) collect `DescribeTransitGatewayVpcAttachments` and record `subnet_ids` on VPC attachments (one more read-only operation, in the allowlist, the published policy, and the importers' `SubnetIds`), or (b) make every transit gateway hop `unknown` for the missing subnet. The plan recommends (a): a hop is then decided from cited configuration, and (b) would leave every transit gateway path `unknown`.
- With (a): normalize the subnets and cite them, add a `transit_gateway_attachment_subnet` edge from attachment to subnet, and record the operation's coverage. A failed read leaves the subnets unrecorded, never empty.

**Advice:** Follow the 50.5 pattern for a new operation: the allowlist, `docs/aws-permissions.md`, the policy count, and the Stubber-backed collection test. The Terraform resource (`aws_ec2_transit_gateway_vpc_attachment.subnet_ids`) and the CloudFormation property (`SubnetIds`) already carry the subnets for 51.4's importers.

**Acceptance check:** A collected VPC attachment records its subnets with citations, a denied read leaves them unrecorded with limited coverage, the imports record them, and existing collection and import output is otherwise unchanged.

#### Step 53.4: Transit gateway hops
**Status:** Planned.
**Prerequisite:** Steps 53.2 and 53.3.

- Extend `evaluate_vpc_route` so it can follow a route to a stored, same-account transit gateway in a carrying state through these steps:
  - Take the source VPC's attachment to that gateway. It must be in a carrying state and have a subnet in the source subnet's Availability Zone.
  - Take the attachment's associated transit gateway route table, whose association is recorded as `associated`.
  - Select the longest-prefix route in that table.
  - That route must name one VPC attachment to `vpc_id` in a carrying state.
- The decision cites every element: the VPC route, the attachment, the association, the transit gateway route, and the destination attachment.
- Transit gateway route priority is recorded (`route_type`), so an equal-length tie follows AWS's order: a static route beats a propagated one. Two routes of the same type at the same length are `unknown`, though rare, since the table shows only each CIDR's preferred route. A blackhole route denies. A table whose `routes_complete` is false is `unknown` unless the selected route is longer than any route the search could have missed, which cannot be known, so it is `unknown`. A route to several attachments (ECMP), to a peering, VPN, or Direct Connect attachment, or a prefix-list destination is `unknown` with the reason: a deeper chain is out of scope.
- A gateway shared from another account (an external route target) or an attachment owned by another account is `unknown`, naming it.
- An attachment whose `subnet_ids` is unset, or which carries an import limitation (an imported list with a `!Ref` item keeps only its literal subnets, see 53.3), is `unknown`: its subnet list is never read as complete.

**Advice:** Check the transit gateway route evaluation order in the AWS documentation again when implementing ([How transit gateways work](https://docs.aws.amazon.com/vpc/latest/tgw/how-transit-gateways-work.html), "Route evaluation order", read 2026-10-28: most specific first, then static, system, prefix-list, and VPC-propagated routes, then the other propagated kinds). The same page states the Availability Zone rule behind 53.3. Reuse `_best_route`'s longest-prefix and partial-coverage logic for transit gateway routes rather than copying it, by extracting the selection over candidate prefixes.

**Acceptance check:** `transit-gateway.json`, extended with subnets, attachment subnets, `vpc-data`'s return route, and its association, allows `vpc-app` to `vpc-data` with every element cited. The fixture's blackhole route denies, an attachment without a subnet in the source zone denies, a missing association or incomplete route search is `unknown`, and `endpoints-and-prefix-lists.json`'s external `tgw-main` is `unknown` naming it.

#### Step 53.5: VPC endpoints
**Status:** Planned.
**Prerequisite:** Step 53.1.

- Add `RouteReach.VPC_ENDPOINT`. A route whose destination is a service prefix list and whose target is a stored gateway endpoint reaches it when the following are recorded: the endpoint is in a carrying state (`available`), its type is `gateway`, its `endpoint_route_table` edge names the subnet's route table, and the route's prefix list is that service's list (matching `service_name`). The route is selected by 52.5's prefix-list rules, so an address outside the list's recorded entries does not reach it.
- An interface endpoint is reached as its ENIs: the endpoint's recorded `network_interface_ids` and `security_group_ids`. Add a small helper that returns them with citations, so callers compose the existing local route, NACL, and security-group primitives; no new route kind is needed.
- A Gateway Load Balancer, `Resource`, or `ServiceNetwork` endpoint, or one of unknown type, stays `unsupported` (the open question above stays settled for Gateway Load Balancer endpoints; the other two are newer types with no route semantics here).

**Advice:** Use the endpoint state list from the [`VpcEndpoint` API reference](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_VpcEndpoint.html) (`PendingAcceptance`, `Pending`, `Available`, `Deleting`, `Deleted`, `Rejected`, `Failed`, `Expired`, `Partial`, read 2026-10-28). Compare case-insensitively, because responses are lower-case where the reference capitalizes. `Partial` is `unknown`.

**Acceptance check:** On `endpoints-and-prefix-lists.json` with `pl-s3` entries recorded, an S3 address reaches `vpce-s3` with the route, the entry, the endpoint, and its route-table edge cited. A deleted endpoint denies, an endpoint not associated with the table is `unknown`, and `vpce-sqs` resolves to `eni-vpce` and `sg-endpoint` with citations.

#### Step 53.6: Composed route between two subnets
**Status:** Planned.
**Prerequisite:** Step 53.4.

- Add a composition to `network_composition.py`: the source subnet's route to the destination address must lead into the destination VPC, and the destination subnet's route back to the source address must lead into the source VPC. This is `all_of` two `evaluate_vpc_route` calls. Addresses may be exact or bounded by the subnets' CIDRs, as the existing compositions do.
- Every hop of both directions is cited in the result. A path that would need more than one peering connection or transit gateway is `unknown` and says so.
- Security groups and NACLs are not part of it: callers compose them, as database access does.

**Advice:** Keep the composition thin: it is two primitive calls and a bound, so the analyzers in step 54 can use it beside `groups_allow` and `nacl_round_trip`.

**Acceptance check:** On the extended peering and transit gateway fixtures, two subnets in different VPCs are allowed in both directions with every hop cited. A missing return route denies, and a cross-account hop in either direction is `unknown`. Two subnets in one VPC give the same result as the local route.

#### Step 53.7: Verify the routes and close the step
**Status:** Planned.
**Prerequisite:** Step 53.6.

- Re-run the analyzer comparison against the code before step 53. Every changed result is one 53.1 listed (a question whose best route now leads to a supported kind), and each is named in the status.
- Confirm that `docs/network-path-primitives.md` describes exactly what is decided, and that the analyzer pages still state their limits (the analyzers adopt these paths in step 54).
- Run the focused and full offline tests, the acceptance suite, Ruff, and the whitespace check, then review the full change as the Checks section requires.

**Advice:** Keep `unknown` the default: an element these substeps cannot settle from recorded, cited configuration stays `unknown` with a blocker that names it.

**Acceptance check:** Every check passes; analyzer results outside the listed cases are unchanged; the docs match the code; the plan marks step 53 implemented.

### Step 54: Use the new paths in the existing analyzers
**Status:** Planned.
**Prerequisite:** Step 53.

- Database access: sources in another VPC of the same snapshot are evaluated through peering or a transit gateway, instead of being unsupported; a source behind another account's hop stays an `unknown` candidate.
- Internet exposure: recorded public IPv6 addresses are candidates over IPv6, and security-group rules that name prefix lists are decided from their entries.
- Security-group impact: rules that reference a prefix list name it with its entries.
- Update the analyzers' vocabulary, limitations, and the analyzer pages; keep each result contract additive, so saved results from earlier versions load unchanged.

**Advice:** Re-run the phase 3 acceptance fixtures before changing any analyzer: results that do not involve a new element must not change, and a test should prove it.

**Acceptance check:** New fixtures produce cited cross-VPC database-access findings and IPv6 exposure findings; every existing analyzer fixture gives the same result as before; the assistant tools return the new findings unchanged, since they read the analyzer results.

### Step 55: Add the internet egress analyzer
**Status:** Planned.
**Prerequisite:** Step 54.

- Add an `internet_egress` analyzer: which compute resources (EC2, ECS tasks, Lambda functions in a VPC) can open a connection to the internet, through an internet gateway with a public address, a NAT gateway, or an egress-only gateway over IPv6, composed from security groups, NACLs, and routes, with the same statuses, blockers, and citations as internet exposure.
- Register it in `DEFAULT_REGISTRY`, add its result models, document it in `docs/analyzers.md` and its own page, and offer it to the assistant tools.

**Advice:** Report destinations as the whole internet or as the recorded prefix-list or CIDR a rule allows; do not attempt per-destination reachability.

**Acceptance check:** Fixtures with a public subnet, a private subnet behind a NAT gateway, a subnet with no route out, and an IPv6-only egress path each give the expected cited result; the analyzer reference, drift tests, and tool catalog include it.

### Step 56: Close phase 6 with acceptance and documentation
**Status:** Planned.
**Prerequisite:** Step 55.

- Add offline acceptance journeys through the installed CLI: collect (Stubber-backed) or import the new fixtures, analyze them, and ask about them through `ask` with fake transcripts and through the MCP server, with cited answers that state their limitations.
- Update the README (capabilities, delivery table with a phase 6 row, permissions, acceptance scenarios), `docs/network-path-primitives.md`'s supported table, and every page that lists these elements as unsupported.
- Run focused and full offline tests, the acceptance suite, Ruff, whitespace checks, and the suite in Docker with networking disabled.

**Advice:** Keep the scenario table honest: list what is still `unknown` after this phase (another account's hops, deeper transit chains, Network Firewall, Gateway Load Balancer endpoints) next to what is now decided.

**Completion criterion:** Every route target kind that `RouteTargetKind` records is either decided from cited configuration or `unknown` with a named blocker; database access across VPCs, internet exposure over IPv6, and internet egress are answered offline with citations; existing results are unchanged; no test needs AWS or the network.

### Phase 6 decisions and open questions

**Advice:** Decide an element only from recorded, cited configuration. Each new kind of element widens what the analyzers can say, so keep the `unknown` path the default.

**Open questions (settle before the named step):**
- Should snapshots of several accounts be composed, so a peering connection or transit gateway that crosses accounts can be decided (step 53)? Collection writes one snapshot per account. The plan assumes not in this phase: a cross-account hop stays `unknown`. Composing snapshots would need a multi-snapshot graph and its own phase.
- Is the internet egress question added to the README example questions, the assistant prompts, and the evaluation suites (step 55)? Adding it changes the phase 4 and 5 fixtures; the plan assumes the analyzer is available to the tools without becoming a sixth README question.
- Are AWS Network Firewall and Gateway Load Balancer endpoints in scope (step 53)? The plan assumes not: a route to either stays `unknown`.
- ~~How many transit gateway routes may one collection read before coverage is marked incomplete (step 50.5)?~~ Settled in step 50.5: one search of up to 1000 routes per route table.

## 7. Browser UI for results, evidence, and questions

**Phase 7 milestone:** A user starts `archdiscov serve` over a directory of snapshots and saved results → opens a local browser page → browses snapshots, findings, comparisons, and the evidence behind every fact → asks one question with a configured provider and sees the same validated, rendered answer `ask` prints → nothing on any page states a fact the renderers did not.

The web UI is a second presentation over the existing services, as the MCP server is a second front end over the tool session. Its distinct value over an MCP client is browsing saved findings, comparisons, and evidence without a model in the loop, so the pages are results first and the question form second. The server is local, single-user, and read-only: it binds to loopback only, serves files chosen from its configured directory and never from a browser-supplied path, keeps provider configuration and keys on the server, runs one question at a time within the `ask` limits, and writes nothing except a transcript when asked to. Text from the cloud is untrusted: every template autoescapes, and quoted text is shown as `ask` shows it. The pages are server-rendered HTML with no JavaScript build chain, so the repository stays Python-only and the tests run offline through an in-process HTTP client and the scripted fake provider. This phase adds no analyzer, collector, cloud platform, provider, or remediation; the README's deferral of a web UI is lifted by this phase and nowhere else.

Implement steps 57–60 in order, verifying each before the next. Break each step into substeps when it starts, as steps 34–48 were. Keep this phase planned until step 60 passes. Phase 7 touches only new modules and may start before step 56 if the schedule needs it, but its evidence and route pages are easier to finish once phase 6 has settled the network contracts.

**Where things go:**
- The server lives in a new `src/archdiscov/web/` package: an application factory built from a workspace directory and options, route modules by page family (snapshots and results, evidence and queries, ask), Jinja2 templates under `web/templates/`, and one stylesheet under `web/static/`. Only `web/` imports the web framework, the template engine, and the ASGI server, and only when the server is built, as `mcp_server.py` does with `mcp`.
- The command lives in `cli_serve.py`, which stays routing and presentation: it parses options, builds the application, and runs the ASGI server.
- Logic a page needs that today lives only in a `cli_*_output` module (naming, quoting, ordering, summaries) moves into `services/` or `assistant/` so both presentations share it; `web/` never imports a `cli_*` module.

**What is reused:**
- `load_snapshot`, `load_analysis`, `load_comparison`, `GraphIndex`, and the detached `SnapshotAccess` for browsing; the query services (`services/queries.py` and the search, inspection, neighbour, and path services) with their exact filters and bounds; the evidence services behind the `evidence` command.
- `ToolSession.from_paths`, `answer_selected`, `AskCancelled`, `RunLimits`, `load_provider_config`, and `resolve_api_key` for questions; `render_answer`, `render_failure`, and `RenderedAnswer` for what the page shows; the `ask` pre-run diagnostics and outcome codes, mapped to HTTP statuses.
- The sanitizers and the quoting of untrusted text; the no-overwrite persistence for transcripts.
- The import-isolation test, the installed-CLI harness, the offline guard, and the Docker image and compose file for acceptance.

### Step 57: Serve the workspace and browse snapshots and results
**Status:** Planned.
**Prerequisite:** Step 56 (may start after step 49 if phase 6 stalls; see above).

- Add `archdiscov serve WORKSPACE` with `--host` (default `127.0.0.1`), `--port` (default stated in the docs, `0` for an ephemeral port), and `--config PATH` for the provider configuration step 59 uses. Refuse a non-loopback host with a fixed error; a later step may add authentication, this one does not.
- Build the application from the workspace: list the snapshot files, analysis results, and comparison results in the directory once at start (and again on a `refresh` action), validate each with the existing loaders, and give each a stable opaque key. Every page addresses a file by its key; a path, a `..`, or an unknown key is a `404`, never a filesystem read.
- Pages: an index of snapshots (platform, account, region, collection time, resource count, coverage summary) with the saved results that name each one; a snapshot page with resource counts by type, coverage by operation, and limitations; an analysis page listing findings with status, blockers, and citations; a comparison page listing matched, differing, unmatched, and ambiguous entries with both sides' citations. A file that fails validation appears with its diagnostic and no pages.
- Every page has a JSON twin at the same path with an `Accept: application/json` header or a `.json` suffix, returning the loaded model's JSON, so the HTML never carries a fact the JSON lacks.
- Import the web framework, template engine, and ASGI server only when the server is built; extend the import-isolation test so `ask`, `mcp`, and every other command never import them.

**Advice:** Choose the opaque key from the file's validated content (for example the snapshot ID or result ID), not from its name, so two directories holding the same files address them alike and a renamed file keeps its links.

**Acceptance check:** In-process HTTP client tests walk every page over the committed fixtures and compare each JSON twin to the loader's output; a traversal path, an unknown key, and an invalid file give the documented statuses; a non-loopback host is refused before binding; no command other than `serve` imports the web dependencies.

### Step 58: Drill down to resources, evidence, and queries
**Status:** Planned.
**Prerequisite:** Step 57.

- Add a resource page: the inspection the `query` services give (type, identity, containment, fields with their fact citations, dependencies to local, external, and unresolved targets, coverage for its operations), with each citation linking to an evidence page that shows the recorded evidence entry (source operation, path, time, and the sanitized recorded value) exactly as the `evidence` command does.
- Add neighbours and paths pages over the directional neighbour and bounded path services with the same bounds and limitation reporting; the limitations are shown, never silently dropped.
- Add a search form over the exact-filter search service, with the service's own limits on results and no free-text matching the service does not offer.
- Link every citation on the analysis, comparison, and (in step 59) answer pages to its evidence page, so a reader reaches the receipt behind any claim in one click.
- Render imported text (names, tags, descriptions, policy text) through autoescaping and the same quoting the renderers use, so a resource named like an instruction reads as a quoted value on every page.

**Advice:** Keep the pages thin over the query services: if a page needs a filter, bound, or ordering the service lacks, add it to the service so the `query` CLI and the assistant tools gain it too.

**Acceptance check:** Tests open a finding, follow a citation to its evidence page, and compare the shown value to the evidence record; neighbours, paths, and search give the services' results and limitations; an unresolved or external target is shown as such; a resource name holding HTML and an instruction-like string renders escaped and quoted.

### Step 59: Ask a question from the browser
**Status:** Planned.
**Prerequisite:** Step 58.

- Add a question form on a snapshot page (and on a comparison pair) that posts one question. The server builds a `ToolSession` from the selected files, the provider from the configuration given at start, and runs `answer_selected` with `RunLimits.from_config`; the browser never names a configuration, a key, or a file path.
- Run one question at a time: a second question while one runs gets `409` with a fixed message. Cancel the run when the client disconnects, through the existing cancellation, and explain the cancelled run as `ask` does.
- Show the `RenderedAnswer`: the validated conclusions rendered from records, the acknowledged limitations, the retrieved summary, and the model's explanation in a clearly labelled unverified box; a rejected, insufficient, provider-failed, or cancelled run shows `render_failure`. Each citation links to its evidence page from step 58. The JSON twin returns the `ask --json` rendering unchanged.
- Save a transcript only when `serve` was started with `--transcripts DIR`, through the no-overwrite persistence, and show the saved path on the answer page.
- Reuse the `ask` pre-run diagnostics (missing configuration, provider profile unavailable, result not belonging to the selected snapshots) as `4xx` responses with the same codes and messages.

**Advice:** Do not add chat, memory, or follow-up questions: each submission is one `ask` run, as the CLI and the MCP server are, and the answer page says so.

**Acceptance check:** With the scripted fake provider, each of the five README questions asked from the browser renders the same conclusions, limitations, and citations as `ask` prints for the same transcript; a rejected and a repaired transcript show their outcomes; a concurrent question gets `409`; a disconnect cancels the run and leaves no transcript; the JSON twin equals the `ask --json` output; no key or configuration content appears in any response.

### Step 60: Close phase 7 with acceptance and documentation
**Status:** Planned.
**Prerequisite:** Step 59.

- Add offline acceptance journeys through the installed CLI: start `archdiscov serve` on an ephemeral port over a workspace of committed fixtures, walk the index, a snapshot, a finding, a citation, and an evidence page with an HTTP client, ask a README question with the fake provider, and check the rendered answer and its JSON twin; stop the server cleanly.
- Run the same journey in the Docker image with networking disabled, binding inside the container and connecting from inside it.
- Write `docs/web-ui.md`: what the pages show, the workspace layout, the loopback-only and read-only boundaries, what a transcript is written for, how untrusted text is rendered, and what the UI does not do (no chat, no analysis on demand, no authentication, no remote binding). Add the capability row and a phase 7 delivery row to the README, remove the web UI from the README's deferred list, and link the page from `docs/project-layout.md`.
- Run focused and full offline tests, the acceptance suite, Ruff, whitespace checks, and the import-isolation test, then review the full change as the Checks section requires.

**Advice:** Keep the documentation's limits honest, as the MCP page does: the UI shows what the renderers produce and links every claim to evidence, but it is a local tool for one user and makes no claim about hosting, access control, or multi-user use.

**Completion criterion:** Every page is rendered from loaded models or the existing renderers and has a JSON twin equal to the service output; every citation reaches its evidence record; a question from the browser gives the `ask` result unchanged; the server binds only to loopback, reads only its workspace, and writes only requested transcripts; the suite stays offline.

### Phase 7 decisions and open questions

**Advice:** Treat the UI as presentation only. If a page needs a fact, a filter, or a summary the services do not give, add it to the service and its tests first, so the CLI, the assistant tools, and the page all agree.

**Decisions made in this plan:**
- Server-rendered HTML with Starlette, Jinja2, and uvicorn as regular dependencies imported only by `web/`; FastAPI is not needed because the JSON twins return existing models and no OpenAPI schema is served. Revisit only if another client needs a documented HTTP API.
- No JavaScript in this phase. Progressive enhancement (for example a vendored htmx file for partial updates, or streamed progress for a running question) is a later step and must not change what a page shows.
- No authentication, and non-loopback binding is refused. Opening the server beyond loopback needs a token or a reverse proxy and its own step with a security review.
- Saved results only: the server does not run analyzers, comparisons, or collection. Running an analyzer from the browser would make the server write to the workspace and is left to a later phase if wanted.

**Open questions (settle before the named step):**
- Is the workspace one flat directory, or the `snapshots/`, `analyses/`, and `comparisons/` layout the CLI examples use (step 57)? The plan assumes one directory scanned by file type, with subdirectories allowed one level deep.
- Does the index page need a coverage summary per snapshot, or is the snapshot page enough (step 57)? The plan assumes a one-line summary (operations complete, limited, and failed) on the index.
- Should the question form offer the five README questions as presets, as the MCP server offers them as prompts (step 59)? The plan assumes yes, since they are the questions the evaluation suites verify.
- Should the comparison pages be part of step 57 or deferred to a later step to keep 57 small (step 57)? The plan assumes they are in 57, because the loader and model already exist and the page is a list.
